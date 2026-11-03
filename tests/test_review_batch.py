import json
import subprocess
import time
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from mo_bot.codex import SourceChanged
from mo_bot.review_batch import BATCH_SCHEMA
from mo_bot.service import review_instructions
from tests import test_service as fixtures
from tests.test_service import assessment


class TestReviewBatch(unittest.TestCase):
    tearDown = fixtures.TestWorkflow.tearDown

    def setUp(self):
        fixtures.TestWorkflow.setUp(self)
        self.service.config = replace(self.config, batch_size=2)
        self.gh.evidence.return_value = {"discussion_index": [], "reviews": []}
        self.gh.pr.side_effect = lambda job: {
            "state": "open",
            "head": {"sha": job["head"]},
            "base": {"sha": "f" * 40},
            "user": {"login": "other"},
        }
        self.workspace.prepare.side_effect = self.prepare
        self.service.backend = Mock(dead=False)
        self.service.backend.start.side_effect = self.native
        self.sizes = {}

    def prepare(self, source_slot, job, pr, directory):
        source = self.root / "slots" / str(source_slot) / "source"
        source.mkdir(parents=True, exist_ok=True)
        (source / "identity").write_text(job["head"])
        (directory / "change.patch").write_text(
            "+change\n" * self.sizes.get(job["pr"], 1)
        )
        return source, pr["base"]["sha"]

    def native(self, slot, directory, workspace, prompt, schema):
        directory.mkdir(parents=True, exist_ok=True)
        turn = Mock(
            completed=False,
            error=None,
            model="fake",
            effort="xhigh",
            directory=directory,
            session_id=slot["session_id"],
            turn_id="batch-turn",
        )
        turn.result.return_value = None
        (directory / "prompt.txt").write_text(prompt)
        (directory / "events.jsonl").write_text("")
        (directory / "turn.json").write_text(
            json.dumps(
                {
                    "thread": slot["session_id"],
                    "turn": turn.turn_id,
                    "model": turn.model,
                    "effort": turn.effort,
                }
            )
        )
        return turn

    def job(self, number, **metadata):
        c = {
            **fixtures.candidate(number, f"{number:040x}"),
            "forced": False,
            **metadata,
        }
        self.store.enqueue(c)
        key = self.store.key(c["repo"], number, c["headRefOid"])
        self.store.update(key, comment_id=number + 100)
        self.service.eligible.add(key)
        return self.store.job(key)

    def launch_pair(self):
        jobs = [self.job(1), self.job(2)]
        first = self.store.claim(0, self.service.eligible)
        self.service.batches.launch(self.store.slots()[0], first)
        return [self.store.job(j["id"]) for j in jobs], self.service.turns[0][0]

    def checkpoint(self, job, value=None):
        (Path(job["turn_dir"]) / "review-result.json").write_text(
            json.dumps(value or assessment(job))
        )

    def complete(self, turn, results=None, error=None):
        turn.completed = True
        for result in results or []:
            job = self.store.job(result["job_id"])
            self.checkpoint(job, result)
        turn.result.return_value = {
            "completed_job_ids": [r["job_id"] for r in results or []]
        }
        turn.result.side_effect = error
        self.service.finish(0, turn, self.service.turns[0][1])

    def test_two_distinct_heads_share_one_prompt_and_keep_independent_evidence(self):
        jobs, turn = self.launch_pair()
        self.service.backend.start.assert_called_once()
        slot, directory, source, prompt, schema = (
            self.service.backend.start.call_args.args
        )
        self.assertEqual(schema, BATCH_SCHEMA)
        self.assertEqual(prompt.count(review_instructions()), 1)
        self.assertEqual({j["state"] for j in jobs}, {"reviewing"})
        self.assertEqual({j["slot_id"] for j in jobs}, {0})
        self.assertNotEqual(jobs[0]["turn_dir"], jobs[1]["turn_dir"])
        self.assertEqual((source / "identity").read_text(), jobs[0]["head"])
        self.assertEqual(
            (self.root / "slots/0/batch/source/identity").read_text(), jobs[1]["head"]
        )
        self.assertIn(jobs[1]["turn_dir"], slot["review_directories"])
        self.assertNotIn(str(source), slot["review_directories"])
        self.assertTrue((Path(jobs[1]["turn_dir"]) / "check-run").is_file())
        third = self.job(3)
        self.assertIsNone(
            self.store.claim(0, self.service.eligible, companion_of=jobs[0]["id"])
        )
        self.assertIsNone(self.store.claim(0, self.service.eligible))
        self.assertEqual(self.store.claim(1, self.service.eligible)["id"], third["id"])
        self.service.refresh_status()
        self.assertEqual(
            self.service.status["sessions"][0]["job_ids"], [j["id"] for j in jobs]
        )
        self.complete(turn, [assessment(j) for j in jobs])
        self.assertEqual(
            [self.store.job(j["id"])["state"] for j in jobs],
            ["publishing", "publishing"],
        )

    def test_completed_member_can_publish_before_sibling_failure_and_is_never_replayed(
        self,
    ):
        jobs, turn = self.launch_pair()
        self.checkpoint(jobs[0])
        self.service.finish(0, turn, jobs[0]["id"])
        self.assertEqual(self.store.job(jobs[0]["id"])["state"], "publishing")
        self.assertEqual(self.store.job(jobs[1]["id"])["state"], "reviewing")
        self.service.publish(self.store.job(jobs[0]["id"]))
        self.assertEqual(self.store.job(jobs[0]["id"])["state"], "done")
        self.complete(turn, error=RuntimeError("connection lost"))
        self.assertEqual(self.store.job(jobs[0]["id"])["state"], "done")
        self.assertEqual(self.store.job(jobs[1]["id"])["state"], "queued")
        self.assertEqual(self.gh.submit_review.call_count, 1)
        self.assertNotIn(0, self.service.turns)

    def test_wrong_head_fails_only_its_member(self):
        jobs, turn = self.launch_pair()
        bad = {**assessment(jobs[1]), "head_sha": jobs[0]["head"]}
        self.complete(turn, [assessment(jobs[0]), bad])
        self.assertEqual(self.store.job(jobs[0]["id"])["state"], "publishing")
        self.assertEqual(self.store.job(jobs[1]["id"])["state"], "queued")
        self.assertIn("identity mismatch", self.store.job(jobs[1]["id"])["error"])

    def test_missing_discussion_ledger_fails_only_its_member(self):
        jobs, turn = self.launch_pair()
        evidence = Path(jobs[1]["turn_dir"]) / "evidence.json"
        evidence.write_text(
            json.dumps(
                {
                    "discussion_index": [
                        {"key": "issue_comments:7", "body": "Please check cancellation"}
                    ]
                }
            )
        )
        self.complete(turn, [assessment(j) for j in jobs])
        self.assertEqual(self.store.job(jobs[0]["id"])["state"], "publishing")
        self.assertEqual(self.store.job(jobs[1]["id"])["state"], "queued")
        self.assertIn(
            "discussion-assessment.json", self.store.job(jobs[1]["id"])["error"]
        )

    def test_migration_preserves_two_active_members_and_slot_and_pr_fences(self):
        from mo_bot.store import Store

        jobs, turn = self.launch_pair()
        other = Store(self.root / "state.sqlite3", "test-bot", 2)
        try:
            self.assertIsNone(other.claim(0))
            self.assertIsNone(other.claim(1))
            other.update(jobs[0]["id"], state="publishing", slot_id=None)
            self.assertIsNone(other.claim(0, companion_of=jobs[1]["id"]))
        finally:
            other.close()

    def test_member_checkpoint_is_not_collected_while_shared_turn_still_uses_it(self):
        jobs, turn = self.launch_pair()
        self.checkpoint(jobs[0])
        self.service.finish(0, turn, jobs[0]["id"])
        self.store.update(jobs[0]["id"], state="done")
        turn.started = time.monotonic()
        self.service.dispatch_control["paused"] = True
        with (
            patch.object(self.service.artifacts, "due", return_value=True),
            patch.object(self.service.artifacts, "poll") as collect,
        ):
            self.service.tick()
        self.assertEqual(collect.call_args.args[1], {j["id"] for j in jobs})

    def test_setup_failure_after_pair_claim_releases_both_without_starting_model(self):
        self.job(1)
        self.job(2)
        first = self.store.claim(0, self.service.eligible)
        with patch(
            "mo_bot.review_batch.prepare_validation", side_effect=OSError("disk full")
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.service.batches.launch(self.store.slots()[0], first)
        self.assertEqual([j["state"] for j in self.store.jobs()], ["queued", "queued"])
        self.service.backend.start.assert_not_called()

    def test_partial_file_and_omitted_member_never_become_an_approval(self):
        jobs, turn = self.launch_pair()
        path = Path(jobs[0]["turn_dir"]) / "review-result.json"
        path.write_text('{"job_id":')
        self.service.finish(0, turn, jobs[0]["id"])
        self.assertEqual(self.store.job(jobs[0]["id"])["state"], "reviewing")
        path.unlink()
        self.complete(turn)
        self.assertEqual(
            [self.store.job(j["id"])["state"] for j in jobs], ["queued", "queued"]
        )
        self.gh.submit_review.assert_not_called()

    def test_sibling_pause_preserves_completed_checkpoint(self):
        jobs, turn = self.launch_pair()
        self.checkpoint(jobs[0])
        self.service.command("pause_job", {"job_id": jobs[1]["id"]})
        turn.stop.assert_called_once()
        self.complete(turn, error=RuntimeError("interrupted"))
        self.assertEqual(self.store.job(jobs[0]["id"])["state"], "publishing")
        paused = self.store.job(jobs[1]["id"])
        self.assertEqual(paused["state"], "queued")
        self.assertTrue(json.loads(paused["data"])["paused"])

    def test_slot_interrupt_cancels_both_unfinished_members(self):
        jobs, turn = self.launch_pair()
        self.service.command("interrupt", {"slot": 0})
        self.assertEqual(
            [self.store.job(j["id"])["state"] for j in jobs], ["canceling", "canceling"]
        )
        self.complete(turn, error=RuntimeError("interrupted"))
        self.assertEqual(
            [self.store.job(j["id"])["state"] for j in jobs],
            ["superseded", "superseded"],
        )

    def test_force_promotion_of_running_member_bypasses_wip(self):
        jobs, turn = self.launch_pair()
        meta = {**json.loads(jobs[1]["metadata"]), "forced": True}
        self.store.update(jobs[1]["id"], metadata=json.dumps(meta))
        self.service.admission["2"] = {"head": jobs[1]["head"], "reason": "owner-wip"}
        self.complete(turn, [assessment(j) for j in jobs])
        self.assertEqual(
            [self.store.job(j["id"])["state"] for j in jobs],
            ["publishing", "publishing"],
        )

    def test_model_switch_keeps_completed_member_and_requeues_only_unfinished_one(self):
        jobs, turn = self.launch_pair()
        self.checkpoint(jobs[0])
        self.service.models = [
            {
                "model": "different",
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
            }
        ]

        def stop():
            turn.completed = True
            turn.result.side_effect = RuntimeError("interrupted")

        turn.stop.side_effect = stop
        turn.completion.wait.return_value = True
        self.service.command(
            "model", {"slot": 0, "model": "different", "reasoning_effort": "high"}
        )
        self.assertEqual(self.store.job(jobs[0]["id"])["state"], "publishing")
        self.assertEqual(self.store.job(jobs[1]["id"])["state"], "queued")
        self.assertEqual(self.store.slots()[0]["model"], "different")
        turn.release.assert_called_once()

    def test_two_real_git_worktrees_preserve_heads_and_detect_dirty_or_wrong_sources(
        self,
    ):
        from mo_bot.codex import Workspace

        repo = self.root / "repo"
        repo.mkdir()

        def git(*args):
            return subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    *args,
                ],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()

        git("init")
        git("commit", "--allow-empty", "-m", "first")
        first = git("rev-parse", "HEAD")
        (repo / "code.txt").write_text("second version")
        git("add", "code.txt")
        git("commit", "-m", "second")
        second = git("rev-parse", "HEAD")
        workspace = Workspace(replace(self.config, source_dir=repo))
        one = workspace.ensure(0, first)
        two = workspace.ensure("0/batch", second)
        self.assertTrue(workspace.clean(0, head=first))
        self.assertTrue(workspace.clean("0/batch", head=second))
        self.assertFalse(workspace.clean("0/batch", head=first))
        self.assertFalse((one / "code.txt").exists())
        (two / "code.txt").write_text("unauthorized modification")
        self.assertFalse(workspace.clean("0/batch", head=second))
        self.assertTrue(workspace.clean(0, head=first))
        self.assertFalse(workspace.clean("missing", head=first))

    def test_head_change_cannot_publish_old_member_but_other_result_is_saved(self):
        jobs, turn = self.launch_pair()
        self.checkpoint(jobs[0])
        self.store.update(jobs[1]["id"], state="canceling", error="PR head changed")
        self.complete(turn, [assessment(j) for j in jobs])
        self.assertEqual(self.store.job(jobs[0]["id"])["state"], "publishing")
        self.assertEqual(self.store.job(jobs[1]["id"])["state"], "superseded")

    def test_restart_recovers_checkpoint_without_completed_turn_and_requeues_only_missing_member(
        self,
    ):
        jobs, turn = self.launch_pair()
        self.checkpoint(jobs[0])
        with patch("mo_bot.service.recover_host_tools"):
            self.service.recover()
        self.assertEqual(self.store.job(jobs[0]["id"])["state"], "publishing")
        self.assertEqual(self.store.job(jobs[1]["id"])["state"], "queued")
        self.assertEqual(self.service.backend.start.call_count, 1)

    def test_final_acknowledgement_without_checkpoints_cannot_recover_a_verdict(self):
        jobs, turn = self.launch_pair()
        (turn.directory / "result.json").write_text(
            json.dumps({"completed_job_ids": [j["id"] for j in jobs]})
        )
        with patch("mo_bot.service.recover_host_tools"):
            self.service.recover()
        self.assertEqual(
            [self.store.job(j["id"])["state"] for j in jobs], ["queued", "queued"]
        )

    def test_source_tampering_is_quarantined_without_discarding_other_verdict(self):
        jobs, turn = self.launch_pair()
        self.workspace.clean.side_effect = lambda slot, **kw: slot != "0/batch"
        self.complete(turn, [assessment(j) for j in jobs])
        self.assertEqual(self.store.job(jobs[0]["id"])["state"], "publishing")
        self.assertEqual(self.store.job(jobs[1]["id"])["state"], "failed")
        self.workspace.quarantine.assert_called_once_with("0/batch")

    def test_no_partner_force_context_and_large_patch_start_without_waiting(self):
        for case in ("alone", "force", "context", "large", "disabled", "retry"):
            with self.subTest(case=case):
                first = self.job(
                    10 + len(self.store.jobs()),
                    **{
                        "forced": case == "force",
                        "context_review": case == "context",
                    },
                )
                if case != "alone":
                    self.job(first["pr"] + 1)
                if case == "large":
                    self.sizes[first["pr"]] = 20000
                if case == "retry":
                    self.store.update(first["id"], attempts=1)
                self.service.config = replace(
                    self.service.config, batch_size=1 if case == "disabled" else 2
                )
                claimed = self.store.claim(0, {first["id"]})
                self.service.batches.launch(self.store.slots()[0], claimed)
                self.assertNotEqual(
                    self.service.backend.start.call_args.args[4], BATCH_SCHEMA
                )
                for j in self.store.jobs():
                    self.store.update(j["id"], state="superseded", slot_id=None)
                self.service.turns.clear()

    def test_companion_setup_failure_does_not_block_first_review(self):
        self.job(1)
        peer = self.job(2)

        def prepare(slot, job, pr, directory):
            if job["id"] == peer["id"]:
                raise SourceChanged("head moved")
            return self.prepare(slot, job, pr, directory)

        self.workspace.prepare.side_effect = prepare
        first = self.store.claim(0, self.service.eligible)
        self.service.batches.launch(self.store.slots()[0], first)
        self.assertEqual(self.store.job(peer["id"])["state"], "superseded")
        self.assertNotEqual(self.service.backend.start.call_args.args[4], BATCH_SCHEMA)

    def test_ambiguous_start_releases_both_jobs_for_existing_backend_recovery(self):
        self.job(1)
        self.job(2)
        first = self.store.claim(0, self.service.eligible)
        self.service.backend.start.side_effect = RuntimeError("turn/start timeout")
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            self.service.batches.launch(self.store.slots()[0], first)
        self.assertEqual([j["state"] for j in self.store.jobs()], ["queued", "queued"])
        self.assertTrue(all(j["slot_id"] is None for j in self.store.jobs()))
