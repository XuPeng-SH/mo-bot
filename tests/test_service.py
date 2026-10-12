import json
import time
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from mo_bot.config import Config
from mo_bot.service import Service
from mo_bot.store import Store
from mo_bot.github import GitHub, marker
from mo_bot.rpc import Events


def candidate(pr=1, head="a" * 40):
    return {
        "repo": "matrixorigin/matrixone",
        "number": pr,
        "headRefOid": head,
        "forced": True,
    }


def assessment(job):
    return dict(
        job_id=job["id"],
        head_sha=job["head"],
        decision="approve",
        summary="Reviewed current source.",
        findings=[],
        validation=["Real entrance checked."],
        limitations=[],
    )


class TestWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(
            "test-bot",
            self.root,
            "matrixorigin/matrixone",
            "me",
            self.root,
            self.root / "queue.py",
            minimum_free_gib=0,
        )
        self.gh = Mock()
        self.gh.author = "me"
        self.gh.find_comment.return_value = None
        self.gh.find_review.return_value = None

        def submit_review(job, event, text):
            review = {"id": 100}
            self.gh.find_review.return_value = review
            return review

        self.gh.submit_review.side_effect = submit_review
        self.gh.pr.return_value = {
            "state": "open",
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
            "user": {"login": "other"},
        }
        self.workspace = Mock()
        self.workspace.clean.return_value = True
        self.workspace.ensure.return_value = self.root
        self.workspace.prepare.return_value = (self.root, "b" * 40)
        self.service = Service(self.config, github=self.gh, workspace=self.workspace)
        self.store = self.service.store
        for i in range(2):
            self.store.slot_update(i, ready=1, session_id=f"thread-{i}")

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def job(self, pr=1, head="a" * 40):
        c = candidate(pr, head)
        self.store.enqueue(c)
        return self.store.job(self.store.key(c["repo"], pr, head))

    def test_repeated_scan_unique_and_newer_commit_supersedes(self):
        snap = {
            "heads": {"1": "a" * 40},
            "candidates": [candidate()],
            "indeterminate": [],
        }
        self.service.scan(snap)
        self.service.scan(snap)
        self.assertEqual(len(self.store.jobs()), 1)
        snap["heads"]["1"] = "b" * 40
        snap["candidates"] = [candidate(head="b" * 40)]
        self.service.scan(snap)
        self.assertEqual(
            [j["state"] for j in self.store.jobs()], ["queued", "superseded"]
        )

    def test_sqlite_cross_connection_dedup_and_active_fences(self):
        self.job()
        other = Store(self.root / "state.sqlite3", "test-bot", 2)
        try:
            self.assertFalse(other.enqueue(candidate()))
            j = self.store.claim(0)
            self.assertIsNotNone(j)
            self.assertIsNone(other.claim(1))
            self.job(head="b" * 40)
            self.assertIsNone(other.claim(1))
            self.store.update(j["id"], state="done")
            self.assertIsNotNone(other.claim(1))
        finally:
            other.close()

    def test_force_is_not_a_duplicate_review_override(self):
        self.gh.api.return_value = self.gh.pr.return_value
        first = self.service.command("enqueue", {"pr": 1, "force": True})
        second = self.service.command("enqueue", {"pr": 1, "force": True})
        self.assertTrue(first["enqueued"])
        self.assertFalse(second["enqueued"])
        self.assertEqual(first["job"]["id"], second["job"]["id"])
        self.assertEqual(len(self.store.jobs()), 1)

    def test_remote_done_prevents_review_after_database_loss(self):
        self.gh.find_comment.return_value = {"id": 99}
        self.gh.owned_marker.return_value = {"state": "done"}
        self.service.scan(
            {"heads": {"1": "a" * 40}, "candidates": [candidate()], "indeterminate": []}
        )
        job = self.store.claim(0, self.service.eligible)
        self.service.launch(self.store.slots()[0], job)
        self.assertEqual(self.store.jobs()[0]["state"], "done")
        self.assertIsNone(self.store.claim(0))

    def test_ambiguous_comment_post_does_not_post_again(self):
        j = self.job()
        self.store.update(j["id"], claim_attempted=1)
        j = self.store.claim(0)
        self.service.launch(self.store.slots()[0], j)
        self.gh.ensure_comment.assert_not_called()
        self.assertEqual(self.store.job(j["id"])["state"], "failed")

    def test_publication_checkpoint_survives_retry_without_model(self):
        j = self.job()
        j = self.store.claim(0)
        self.service.save_result(j, self.store.slots()[0], assessment(j))
        self.service.eligible.add(j["id"])
        self.gh.patch_comment.side_effect = RuntimeError("timeout")
        with self.assertRaises(RuntimeError):
            self.service.publish(self.store.job(j["id"]))
        self.assertEqual(self.store.job(j["id"])["state"], "publishing")
        self.assertIsNone(self.store.claim(0))
        self.gh.patch_comment.side_effect = None
        # A formal approval removes this PR from the manual queue predicate.
        self.service.eligible.clear()
        self.service.publish(self.store.job(j["id"]))
        self.assertEqual(self.store.job(j["id"])["state"], "done")
        self.gh.submit_review.assert_called_once()
        self.assertEqual(self.store.job(j["id"])["review_id"], 100)

    def test_old_head_not_published_as_current_review(self):
        j = self.job()
        j = self.store.claim(0)
        self.service.save_result(j, self.store.slots()[0], assessment(j))
        self.gh.pr.return_value = {"state": "open", "head": {"sha": "c" * 40}}
        self.service.publish(self.store.job(j["id"]))
        self.assertEqual(self.store.job(j["id"])["state"], "superseded")
        self.assertEqual(self.gh.patch_comment.call_args.args[1], "superseded")

    def test_result_identity_and_shape_reject_invalid_verdict(self):
        j = self.job()
        for result in [
            dict(assessment(j), head_sha="bad"),
            dict(assessment(j), findings=["not-a-finding"]),
        ]:
            with self.assertRaises(ValueError):
                self.service.save_result(j, self.store.slots()[0], result)
        self.assertEqual(self.store.job(j["id"])["state"], "queued")

    def test_chat_steers_existing_session_instead_of_new_turn(self):
        turn = Mock()
        turn.steer.return_value = {"turnId": "turn-1"}
        self.service.backend = Mock(dead=False)
        self.service.turns[0] = (turn, None)
        response = self.service.command(
            "message", {"slot": 0, "text": "check unhappy paths"}
        )
        self.assertTrue(response["steered"])
        turn.steer.assert_called_once()
        self.service.backend.start.assert_not_called()

    def test_stale_progress_request_does_not_start_idle_conversation(self):
        self.service.backend = Mock(dead=False)
        with self.assertRaises(ValueError):
            self.service.command(
                "message",
                {"slot": 0, "expected_turn_id": "old-turn", "text": "progress"},
            )
        self.service.backend.start.assert_not_called()

    def test_model_switch_interrupts_and_continues_same_job_in_same_thread(self):
        self.service.models = [
            {
                "model": "gpt-6-astra",
                "supportedReasoningEfforts": [{"reasoningEffort": "xhigh"}],
            }
        ]
        self.service.backend = Mock(dead=False)
        job = self.job()
        job = self.store.claim(0)
        self.store.update(job["id"], comment_id=99)
        old_dir = self.root / "old-turn"
        old_dir.mkdir()
        (old_dir / "prompt.txt").write_text("Frozen review scope " + job["head"])
        old = Mock(
            model="gpt-6.1-sol",
            effort="xhigh",
            turn_id="old-turn",
            completed=False,
            directory=old_dir,
        )
        old.completion.wait.return_value = True
        self.service.turns[0] = (old, job["id"])
        new = Mock(model="gpt-6-astra", effort="xhigh", turn_id="new-turn", started=0)
        self.service.backend.start.return_value = new
        result = self.service.command(
            "model", {"slot": 0, "model": "gpt-6-astra", "reasoning_effort": "xhigh"}
        )
        self.assertEqual(result["effective"], "native_turn_started")
        self.assertEqual(result["continued_job"], job["id"])
        old.stop.assert_called_once()
        old.completion.wait.assert_called_once_with(10)
        self.assertEqual(
            self.service.backend.start.call_args.args[0]["session_id"], "thread-0"
        )
        self.assertEqual(
            self.service.backend.start.call_args.args[0]["model"], "gpt-6-astra"
        )
        self.assertIn(job["head"], self.service.backend.start.call_args.args[3])
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(self.store.job(job["id"])["comment_id"], 99)
        self.gh.ensure_comment.assert_not_called()
        self.assertEqual(self.store.slots()[0]["session_id"], "thread-0")
        with self.assertRaises(ValueError):
            self.service.command("model", {"slot": 0, "model": "invalid"})

    def test_failed_native_interruption_fences_model_switch(self):
        self.service.models = [
            {
                "model": "gpt-6-astra",
                "supportedReasoningEfforts": [{"reasoningEffort": "xhigh"}],
            }
        ]
        self.service.backend = Mock(dead=False)
        old = Mock(model="gpt-6.1-sol", effort="xhigh", completed=False)
        old.completion.wait.return_value = False
        self.service.turns[0] = (old, None)
        with self.assertRaises(RuntimeError):
            self.service.command(
                "model",
                {"slot": 0, "model": "gpt-6-astra", "reasoning_effort": "xhigh"},
            )
        self.service.backend.start.assert_not_called()
        self.service.backend.close.assert_called_once()

    def test_actual_review_entry_includes_core_contract(self):
        from mo_bot.service import review_instructions

        self.service.backend = Mock(dead=False)
        self.gh.evidence.return_value = {"comments": []}
        job = self.job()
        self.store.update(job["id"], comment_id=99)
        self.service.launch(self.store.slots()[0], self.store.job(job["id"]))
        prompt = self.service.backend.start.call_args.args[3]
        self.assertIn(review_instructions(), prompt)
        self.assertIn(job["head"], prompt)
        self.assertIn(job["id"], prompt)

    def test_new_review_loads_edited_skill_without_restart(self):
        skill = self.root / "SKILL.md"
        skill.write_text("---\nname: test\n---\nPolicy one")
        self.service.backend = Mock(dead=False)
        self.gh.evidence.return_value = {}
        with patch("mo_bot.service.REVIEW_SKILL", skill):
            for number, policy in ((1, "Policy one"), (2, "Policy two")):
                skill.write_text("---\nname: test\n---\n" + policy)
                job = self.job(pr=number)
                self.store.update(job["id"], comment_id=99)
                self.service.launch(self.store.slots()[0], self.store.job(job["id"]))
                prompt = self.service.backend.start.call_args.args[3]
                self.assertIn(policy, prompt)
                if number == 2:
                    self.assertNotIn("Policy one", prompt)

    def test_publication_event_mapping_and_self_author_guard(self):
        finding = dict(
            priority="P2", file="a.py", line=1, trigger="x", impact="y", evidence="z"
        )
        for number, (decision, author, event) in enumerate(
            (
                ("approve", "other", "APPROVE"),
                ("request_changes", "other", "REQUEST_CHANGES"),
                ("needs_validation", "other", "COMMENT"),
                ("approve", "me", "COMMENT"),
                ("request_changes", "me", "COMMENT"),
                ("needs_validation", "me", "COMMENT"),
                ("approve", "bot-account", "COMMENT"),
            ),
            1,
        ):
            with self.subTest(decision=decision, author=author):
                self.gh.author = "bot-account"
                self.gh.pr.return_value["user"]["login"] = author
                self.gh.find_review.return_value = None
                job = self.job(pr=number)
                result = dict(
                    assessment(job),
                    decision=decision,
                    findings=[finding] if decision == "request_changes" else [],
                )
                self.service.save_result(job, self.store.slots()[0], result)
                self.service.eligible.add(job["id"])
                self.service.publish(self.store.job(job["id"]))
                self.assertEqual(self.gh.submit_review.call_args.args[1], event)
                saved = self.store.job(job["id"])
                self.assertEqual(saved["state"], "done")
                self.assertEqual(saved["review_event"], event)

    def test_uncertain_review_post_is_not_repeated_when_marker_absent(self):
        job = self.job()
        self.service.save_result(job, self.store.slots()[0], assessment(job))
        self.service.eligible.add(job["id"])
        self.gh.submit_review.side_effect = RuntimeError("uncertain POST")
        with self.assertRaises(RuntimeError):
            self.service.publish(self.store.job(job["id"]))
        saved = self.store.job(job["id"])
        self.assertEqual(saved["state"], "publishing")
        self.assertTrue(saved["review_attempted"])
        self.service.publish(saved)
        self.gh.submit_review.assert_called_once()
        self.assertEqual(self.store.job(job["id"])["state"], "failed")
        self.assertIn("manual reconciliation", self.store.job(job["id"])["error"])

    def test_uncertain_post_reconciles_remote_review_after_eligibility_changes(self):
        job = self.job()
        self.service.save_result(job, self.store.slots()[0], assessment(job))
        self.service.eligible.add(job["id"])
        self.gh.submit_review.side_effect = RuntimeError("response lost")
        with self.assertRaises(RuntimeError):
            self.service.publish(self.store.job(job["id"]))
        # Recreate the coordinator: reconciliation relies on SQLite, not memory.
        self.service.close()
        self.service = Service(self.config, github=self.gh, workspace=self.workspace)
        self.store = self.service.store
        self.gh.find_review.return_value = {"id": 101}
        self.service.eligible.clear()
        self.service.publish(self.store.job(job["id"]))
        self.assertEqual(self.store.job(job["id"])["state"], "done")
        self.assertEqual(self.store.job(job["id"])["review_id"], 101)
        self.gh.submit_review.assert_called_once()
        self.gh.validate_review.assert_called_once()

    def test_changed_head_before_publication_does_not_submit_review(self):
        job = self.job()
        self.service.save_result(job, self.store.slots()[0], assessment(job))
        self.gh.pr.return_value["head"]["sha"] = "b" * 40
        self.service.publish(self.store.job(job["id"]))
        self.gh.submit_review.assert_not_called()
        self.assertEqual(self.store.job(job["id"])["state"], "superseded")

    def test_findings_and_verdict_must_agree(self):
        job = self.job()
        finding = dict(
            priority="P2", file="a.py", line=1, trigger="x", impact="y", evidence="z"
        )
        for decision, findings in (
            ("approve", [finding]),
            ("needs_validation", [finding]),
            ("request_changes", []),
        ):
            with self.subTest(decision=decision), self.assertRaises(ValueError):
                self.service.save_result(
                    job,
                    self.store.slots()[0],
                    dict(assessment(job), decision=decision, findings=findings),
                )

    def test_existing_database_migrates_without_replaying_completed_jobs(self):
        job = self.job()
        self.store.update(job["id"], state="done")
        for column in ("review_id", "review_event", "review_attempted"):
            self.store.db.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
        self.store.db.commit()
        other = Store(self.root / "state.sqlite3", "test-bot", 2)
        try:
            saved = other.job(job["id"])
            self.assertEqual(saved["state"], "done")
            self.assertFalse(saved["review_attempted"])
            self.assertIsNone(saved["review_id"])
            self.assertIsNone(other.claim(0))
        finally:
            other.close()

    def test_recovered_same_head_review_is_superseded_but_transient_ci_waits(self):
        job = self.job()
        self.store.update(
            job["id"], metadata=json.dumps({**candidate(), "forced": False})
        )
        snapshot = {
            "heads": {"1": job["head"]},
            "candidates": [],
            "indeterminate": [],
            "admission": {
                "1": {"head": job["head"], "state": "excluded", "reason": "not-green"}
            },
        }
        self.service.scan(snapshot)
        self.assertEqual(self.store.job(job["id"])["state"], "queued")
        self.service.refresh_status()
        status = self.service.status["jobs"][0]
        self.assertFalse(status["dispatchable"])
        self.assertEqual(status["wait_reason"], "not-green")
        snapshot["admission"]["1"]["reason"] = "already-reviewed"
        self.service.scan(snapshot)
        self.assertEqual(self.store.job(job["id"])["state"], "superseded")
        self.assertEqual(self.store.job(job["id"])["error"], "already-reviewed")
        self.assertIsNone(self.store.claim(0, self.service.eligible))
        self.service.scan(snapshot)
        self.assertEqual(len(self.store.jobs()), 1)

    def test_new_decisive_review_cancels_running_automatic_job(self):
        job = self.job()
        self.store.update(
            job["id"], metadata=json.dumps({**candidate(), "forced": False})
        )
        self.store.claim(0)
        self.service.scan(
            {
                "heads": {"1": job["head"]},
                "candidates": [],
                "indeterminate": [],
                "admission": {
                    "1": {
                        "head": job["head"],
                        "reason": "already-reviewed",
                        "state": "excluded",
                    }
                },
            }
        )
        self.assertEqual(self.store.job(job["id"])["state"], "canceling")
        self.assertIsNone(self.store.claim(1))

    def test_explicit_force_and_uncertain_post_are_not_canceled_by_review_gate(self):
        for number, forced, posted, state in [
            (1, True, False, "queued"),
            (2, False, True, "publishing"),
        ]:
            job = self.job(pr=number)
            self.store.update(
                job["id"],
                metadata=json.dumps({**candidate(number), "forced": forced}),
                state=state,
                review_attempted=int(posted),
            )
        self.service.scan(
            {
                "heads": {"1": "a" * 40, "2": "a" * 40},
                "candidates": [],
                "indeterminate": [],
                "admission": {
                    str(n): {
                        "head": "a" * 40,
                        "reason": "already-reviewed",
                        "state": "excluded",
                    }
                    for n in [1, 2]
                },
            }
        )
        self.assertEqual(
            [j["state"] for j in self.store.jobs()], ["publishing", "queued"]
        )

    def test_invalid_snapshot_does_not_close_or_admit_any_job(self):
        job = self.job()
        self.service.eligible.add(job["id"])
        with self.assertRaises(ValueError):
            self.service.scan(
                {"heads": {}, "candidates": [candidate()], "admission": {}}
            )
        self.assertEqual(self.store.job(job["id"])["state"], "queued")
        self.assertIn(job["id"], self.service.eligible)

    def test_lock_precedes_migration_and_duplicate_cannot_resize_pool(self):
        before = [dict(s) for s in self.store.slots()]
        with self.assertRaises(BlockingIOError):
            Service(
                replace(self.config, sessions=1, worker_sessions=2),
                github=self.gh,
                workspace=self.workspace,
            )
        self.assertEqual([dict(s) for s in self.store.slots()], before)

    def test_online_resize_reuses_sessions_and_persists_over_startup_defaults(self):
        first = self.service.command("resize_pool", {"role": "review", "sessions": 3})
        self.assertEqual(first["sessions"], 3)
        self.assertEqual(first["slots"][2]["model"], self.config.model)
        self.assertEqual(first["slots"][0]["session_id"], "thread-0")
        self.store.slot_update(2, session_id="added", ready=1, model="selected")
        self.service.command("resize_pool", {"role": "review", "sessions": 0})
        self.assertEqual(self.service.status["pools"]["review"]["sessions"], 0)
        self.assertTrue(
            all(s["state"] == "disabled" for s in self.service.status["sessions"])
        )
        self.service.close()
        self.service = Service(self.config, github=self.gh, workspace=self.workspace)
        self.store = self.service.store
        self.assertEqual(self.store.capacity("review"), 0)
        result = self.service.command("resize_pool", {"role": "review", "sessions": 3})
        self.assertEqual([s["id"] for s in result["slots"]], [0, 1, 2])
        self.assertEqual(result["slots"][2]["session_id"], "added")
        self.assertEqual(result["slots"][2]["model"], "selected")
        self.assertEqual(result["slots"][2]["ready"], 0)
        self.service.command("resize_pool", {"role": "review", "sessions": 3})
        self.assertEqual(len(self.store.slots(include_disabled=True)), 3)

    def test_busy_shrink_keeps_turn_steer_and_completes_idle_warmup(self):
        self.service.backend = Mock(dead=False)
        turn = Mock(
            session_id="thread-1",
            turn_id="warmup",
            model="fake",
            effort="low",
            schema=None,
        )
        turn.last_activity_at = time.time()
        turn.result.return_value = {"ready": True}
        turn.steer.return_value = {}
        self.service.turns[1] = (turn, None)
        result = self.service.command("resize_pool", {"role": "review", "sessions": 1})
        self.assertEqual(result["draining"], 1)
        turn.stop.assert_not_called()
        self.service.command(
            "message",
            {"slot": 1, "text": "report progress", "expected_turn_id": "warmup"},
        )
        turn.steer.assert_called_once()
        self.service.finish(1, turn, None)
        self.assertNotIn(1, self.service.turns)
        self.service.refresh_status()
        self.assertEqual(self.service.status["sessions"][1]["state"], "disabled")
        with self.assertRaisesRegex(ValueError, "slot disabled"):
            self.service.command(
                "message", {"slot": 1, "text": "new idle conversation"}
            )
        self.job()
        self.assertIsNone(self.store.claim(1))
        self.assertIsNotNone(self.store.claim(0))

    def test_resize_invalid_request_does_not_change_slots_or_capacity(self):
        before = [dict(s) for s in self.store.slots(include_disabled=True)]
        for args in (
            {"role": "review", "sessions": True},
            {"role": "review", "sessions": "2"},
            {"role": "review", "sessions": -1},
            {"role": "worker", "sessions": 9},
            {"role": "other", "sessions": 1},
        ):
            with self.assertRaises(ValueError):
                self.service.command("resize_pool", args)
        self.assertEqual(
            [dict(s) for s in self.store.slots(include_disabled=True)], before
        )
        self.assertIsNone(
            self.store.db.execute(
                "SELECT 1 FROM meta WHERE key LIKE 'pool_capacity:%'"
            ).fetchone()
        )

    def test_new_session_failure_isolated_and_backoff_does_not_touch_healthy(self):
        self.service.command("resize_pool", {"role": "review", "sessions": 3})
        self.service.backend = Mock(dead=False)
        self.service.backend.session.side_effect = RuntimeError(
            "provider temporarily unavailable"
        )
        new_slot = self.store.slots()[-1]
        self.service.open_session(new_slot)
        self.service.open_session(new_slot)
        self.service.backend.session.assert_called_once()
        self.service.backend.close.assert_not_called()
        self.assertEqual(self.store.slots()[0]["session_id"], "thread-0")
        self.assertEqual(self.store.slots()[0]["ready"], 1)
        self.assertEqual(self.store.slots()[-1]["ready"], 0)

    def test_drain_prevents_admission_survives_restart_and_keeps_jobs(self):
        job = self.job()
        self.store.update(job["id"], state="queued", slot_id=None)
        self.service.backend = Mock(dead=False)
        self.service.next_scan = float("inf")
        self.service.eligible.add(job["id"])
        self.service.command("drain", {"reason": "deploy without interrupting turns"})
        self.service.tick()
        self.assertEqual(self.store.job(job["id"])["state"], "queued")
        self.service.backend.start.assert_not_called()
        self.service.refresh_status()
        self.assertTrue(self.service.status["dispatch_control"]["drained"])
        self.assertEqual(
            self.service.status["jobs"][0]["wait_reason"], "service draining"
        )
        self.service.close()
        self.service = Service(self.config, github=self.gh, workspace=self.workspace)
        self.store = self.service.store
        self.assertTrue(self.service.dispatch_control["paused"])
        self.service.command("resume_dispatch", {"reason": "validated new runtime"})
        self.assertFalse(self.service.dispatch_control["paused"])

    def test_inactivity_and_publication_errors_are_visible_without_model_poll(self):
        job = self.job()
        turn = Mock(
            last_activity_at=time.time() - 400, turn_id="t", model="m", effort="xhigh"
        )
        self.service.turns[0] = (turn, job["id"])
        self.service.refresh_status()
        self.assertTrue(
            any(a["kind"] == "session_inactive" for a in self.service.status["alerts"])
        )
        self.service.turns.clear()
        self.store.update(
            job["id"],
            state="publishing",
            slot_id=None,
            error="provider timeout",
            retry_at=time.time() + 60,
        )
        self.service.refresh_status()
        alert = next(
            a for a in self.service.status["alerts"] if a["kind"] == "job_blocked"
        )
        self.assertEqual(alert["reason"], "provider timeout")

    def test_shutdown_rejects_new_and_resolves_queued_commands(self):
        pending = self.service.request("history", {"slot": 0})
        self.service.close()
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            pending.result(timeout=0.1)
        with self.assertRaisesRegex(RuntimeError, "stopping"):
            self.service.request("history", {"slot": 0})

    def test_snapshot_is_compact_and_detail_retains_full_evidence(self):
        job = self.job()
        evidence = "x" * 500000
        self.store.update(
            job["id"],
            metadata=json.dumps({**candidate(), "evidence": evidence}),
            data=json.dumps({"validation": evidence}),
        )
        self.service.refresh_status()
        self.assertLess(len(json.dumps(self.service.status)), 10000)
        self.assertEqual(
            self.service.command("job", {"job_id": job["id"]})["metadata"]["evidence"],
            evidence,
        )

    def test_status_keeps_active_job_outside_recent_history_limit(self):
        job = self.job()
        self.store.claim(0)
        for number in range(2, 205):
            newer = self.job(pr=number)
            self.store.update(newer["id"], state="done")
        self.service.refresh_status()
        self.assertIn(job["id"], {j["id"] for j in self.service.status["jobs"]})
        self.assertEqual(len(self.service.status["jobs"]), 201)

    def test_publication_failure_does_not_block_other_pr_or_reuse_free_slot(self):
        first = self.job()
        first = self.store.claim(0)
        self.service.save_result(first, self.store.slots()[0], assessment(first))
        second = self.job(pr=2)
        self.store.update(second["id"], comment_id=99)
        self.service.eligible.update([first["id"], second["id"]])
        self.service.backend = Mock(dead=False)
        self.service.next_scan = float("inf")
        self.gh.evidence.return_value = {}

        def pr(job):
            if job["pr"] == 1:
                raise RuntimeError("GitHub GET timed out")
            return {
                "state": "open",
                "head": {"sha": job["head"]},
                "base": {"sha": "b" * 40},
            }

        self.gh.pr.side_effect = pr
        self.service.tick()
        self.assertEqual(self.store.job(first["id"])["state"], "publishing")
        self.assertGreater(self.store.job(first["id"])["retry_at"], time.time())
        self.assertEqual(self.store.job(second["id"])["state"], "reviewing")
        self.assertEqual(self.store.job(second["id"])["slot_id"], 0)
        self.assertEqual(self.service.cooldown, 0)

    def test_recovery_rejects_stale_completed_turn_but_accepts_exact_turn(self):
        job = self.job()
        job = self.store.claim(0)
        directory = self.root / "artifacts"
        directory.mkdir()
        self.store.update(job["id"], turn_dir=str(directory))
        (directory / "turn.json").write_text(
            json.dumps(
                {
                    "thread": "thread-0",
                    "turn": "new-turn",
                    "model": "gpt-6-astra",
                    "effort": "xhigh",
                }
            )
        )
        (directory / "result.json").write_text(json.dumps(assessment(job)))

        def completion(turn):
            return {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-0",
                    "turn": {"id": turn, "status": "completed"},
                },
            }

        (directory / "events.jsonl").write_text(
            json.dumps(completion("old-turn")) + "\n"
        )
        self.service.recover()
        self.assertEqual(self.store.job(job["id"])["state"], "queued")
        self.store.claim(0)
        (directory / "events.jsonl").write_text(
            json.dumps(completion("new-turn")) + "\n"
        )
        self.service.recover()
        self.assertEqual(self.store.job(job["id"])["state"], "publishing")
        self.assertEqual(self.store.job(job["id"])["attempts"], 0)

    def test_async_discovery_timeout_keeps_jobs_and_serves_session_commands(self):
        job = self.job()
        self.store.update(
            job["id"], metadata=json.dumps({**candidate(), "forced": False})
        )
        self.service.eligible.add(job["id"])
        self.service.config_path = self.root / "config.toml"
        self.service.backend = Mock(dead=False)
        self.service.backend.call.side_effect = [
            {"thread": {"id": "thread-0"}},
            {"data": []},
        ]
        process = Mock(pid=999999, returncode=-9)
        process.poll.return_value = None
        with (
            patch("mo_bot.discovery.subprocess.Popen", return_value=process),
            patch("mo_bot.discovery.stop_owned"),
        ):
            self.service.scan()
            self.assertNotIn(job["id"], self.service.eligible)
            pending = self.service.request("history", {"slot": 0})
            self.service.next_scan = float("inf")
            self.service.tick()
            self.assertTrue(pending.done())
            self.service.discovery.started = time.monotonic() - 100
            self.service.tick()
        self.assertEqual(self.store.job(job["id"])["state"], "queued")
        self.assertIn("timed out", self.service.scan_error)
        self.assertEqual(self.service.cooldown, 0)
        self.assertIsNone(self.service.discovery.process)

    def test_async_discovery_invalid_output_and_success_recovery(self):
        self.service.config_path = self.root / "config.toml"
        process = Mock(pid=999999, returncode=0)
        process.poll.return_value = 0

        def launch(args, stdout, stderr, **kwargs):
            stdout.write("[1,2]")
            stdout.flush()
            return process

        with patch("mo_bot.discovery.subprocess.Popen", side_effect=launch):
            self.service.scan()
            self.service.poll_discovery()
        self.assertFalse(self.store.jobs())
        self.assertIn("JSON object", self.service.scan_error)
        self.service.scan(
            {"heads": {"1": "a" * 40}, "candidates": [candidate()], "indeterminate": []}
        )
        self.assertIsNone(self.service.scan_error)
        self.assertIsNotNone(self.service.last_scan_at)
        self.assertEqual(len(self.store.jobs()), 1)

    def test_retry_deadline_is_durable_and_claim_obeys_it(self):
        job = self.job()
        self.store.update(job["id"], retry_at=time.time() + 60)
        self.assertIsNone(self.store.claim(0))
        with patch("mo_bot.store.time.time", return_value=time.time() + 61):
            self.assertIsNotNone(self.store.claim(0))

    def test_complete_marker_required_before_background_snapshot_can_cancel(self):
        job = self.job()
        self.service.config_path = self.root / "config.toml"
        process = Mock(pid=999999, returncode=0)
        process.poll.return_value = 0

        def launch(args, stdout, stderr, **kwargs):
            stdout.write(json.dumps({"heads": {}, "candidates": []}))
            stdout.flush()
            return process

        with patch("mo_bot.discovery.subprocess.Popen", side_effect=launch):
            self.service.scan()
            self.service.poll_discovery()
        self.assertEqual(self.store.job(job["id"])["state"], "queued")
        self.assertIn("not complete", self.service.scan_error)

    def test_real_queue_predicate_closes_recovered_exact_head_request_changes(self):
        from datetime import datetime, timezone
        from mo_bot.github import discover, load_queue

        script = (
            Path.home()
            / ".codex/skills/mo-pr-review-queue/scripts/list_review_queue.py"
        )
        if not script.exists():
            self.skipTest("external queue predicate not installed")
        queue = load_queue(script)
        now = datetime.now(timezone.utc).isoformat()
        pr = {
            **candidate(),
            "isDraft": False,
            "author": {"login": "other"},
            "updatedAt": now,
            "mergeable": "MERGEABLE",
            "reviews": {"nodes": []},
            "statusCheckRollup": {
                "contexts": {
                    "nodes": [
                        {
                            "__typename": "CheckRun",
                            "name": "UT",
                            "status": "COMPLETED",
                            "conclusion": "SUCCESS",
                            "completedAt": now,
                        }
                    ]
                }
            },
        }
        job = self.job()
        self.store.update(
            job["id"], metadata=json.dumps({**candidate(), "forced": False})
        )
        with (
            patch("mo_bot.github.load_queue", return_value=queue),
            patch.object(queue, "all_prs", return_value=[pr]),
        ):
            self.service.scan(discover(self.config))
            self.assertIn(job["id"], self.service.eligible)
            pr["reviews"]["nodes"] = [
                {
                    "author": {"login": "me"},
                    "state": "CHANGES_REQUESTED",
                    "commit": {"oid": job["head"]},
                    "submittedAt": now,
                }
            ]
            self.service.scan(discover(self.config))
        self.assertEqual(self.store.job(job["id"])["state"], "superseded")
        self.assertIsNone(self.store.claim(0, self.service.eligible))
        self.gh.submit_review.assert_not_called()

    def test_history_paginates_native_items_without_hydrating_all_turns(self):
        self.service.backend = Mock(dead=False)
        self.service.backend.call.side_effect = [
            {"thread": {"id": "thread-0"}},
            {
                "data": [
                    {
                        "turnId": "turn-1",
                        "item": {"type": "agentMessage", "text": "latest"},
                    }
                ],
                "nextCursor": "more",
            },
        ]
        result = self.service.command("history", {"slot": 0, "cursor": "before"})
        self.assertEqual(result["thread"]["turns"][0]["items"][0]["text"], "latest")
        self.assertEqual(result["nextCursor"], "more")
        first, second = self.service.backend.call.call_args_list
        self.assertFalse(first.args[1]["includeTurns"])
        self.assertEqual(second.args[0], "thread/items/list")
        self.assertEqual(second.args[1]["limit"], 20)
        self.assertEqual(second.args[1]["cursor"], "before")

    def test_service_id_cannot_silently_change(self):
        with self.assertRaises(ValueError):
            Store(self.root / "state.sqlite3", "another-bot", 2)


class TestMarker(unittest.TestCase):
    def test_untrusted_author_and_embedded_marker_are_not_claims(self):
        gh = object.__new__(GitHub)
        gh.config = Mock(service_id="stable")
        gh.author = "me"
        job = {"id": "job", "head": "a" * 40}
        body = marker("stable", job, "done")
        self.assertIsNone(
            gh.owned_marker({"user": {"login": "someone"}, "body": body}, job)
        )
        self.assertIsNone(
            gh.owned_marker(
                {"user": {"login": "me"}, "body": body + "\nuntrusted trailing text"},
                job,
            )
        )
        self.assertEqual(
            gh.owned_marker({"user": {"login": "me"}, "body": body}, job)["state"],
            "done",
        )

    def test_sse_buffer_reports_gap_and_is_bounded(self):
        events = Events()
        cursor = events.sequence
        for i in range(1200):
            events.emit({"method": "delta", "params": {"text": str(i)}})
        items, gap = events.since(cursor, 0)
        self.assertTrue(gap)
        self.assertEqual(len(items), 1024)


if __name__ == "__main__":
    unittest.main()
