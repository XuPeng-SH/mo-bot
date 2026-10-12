from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

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
        self.gh.find_comment.return_value = None
        self.gh.pr.return_value = {
            "state": "open",
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
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
        self.store.close()
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
        self.service.publish(self.store.job(j["id"]))
        self.assertEqual(self.store.job(j["id"])["state"], "done")

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

    def test_model_switch_persists_without_replacing_or_interrupting_session(self):
        self.service.models = [
            {
                "model": "gpt-6.1-sol",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "medium"},
                    {"reasoningEffort": "xhigh"},
                ],
            }
        ]
        active = Mock(model="gpt-6.1-sol", effort="xhigh", turn_id="turn-1")
        self.service.turns[0] = (active, None)
        result = self.service.command(
            "model", {"slot": 0, "model": "gpt-6.1-sol", "reasoning_effort": "medium"}
        )
        self.assertEqual(result["effective"], "next_turn")
        self.assertEqual(self.store.slots()[0]["session_id"], "thread-0")
        self.assertEqual(self.store.slots()[0]["effort"], "medium")
        active.stop.assert_not_called()
        with self.assertRaises(ValueError):
            self.service.command("model", {"slot": 0, "model": "invalid"})
        self.assertEqual(self.store.slots()[0]["effort"], "medium")

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
