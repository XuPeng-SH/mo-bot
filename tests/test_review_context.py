import json
import unittest
from unittest.mock import Mock, patch

from mo_bot.github import GitHub, discover, marker
from mo_bot.review_context import inventory, digest, new_since
from tests import test_service as fixtures
from tests.test_service import assessment


def discussion(
    body="This is documented design; see the contract.", updated="2026-10-26T13:21:50Z"
):
    return {
        "issue_comments": [
            {
                "id": 7,
                "user": {"login": "author"},
                "body": body,
                "created_at": "2026-10-24T08:34:23Z",
                "updated_at": updated,
            }
        ],
        "review_comments": [
            {
                "id": 8,
                "user": {"login": "human"},
                "body": "Earlier concern",
                "created_at": "2026-10-24T08:00:00Z",
                "updated_at": "2026-10-24T08:00:00Z",
                "in_reply_to_id": 4,
                "original_line": 75,
                "commit_id": "old",
            }
        ],
        "reviews": [],
    }


class TestDiscussionInventory(unittest.TestCase):
    def test_all_sources_and_outdated_replies_remain_traceable(self):
        d = discussion()
        rows = inventory(d, "me")
        self.assertEqual(
            {r["key"] for r in rows}, {"issue_comments:7", "review_comments:8"}
        )
        self.assertEqual(rows[0]["in_reply_to_id"], 4)
        self.assertEqual(rows[0]["original_line"], 75)
        self.assertTrue(new_since(rows, "2026-10-26T13:09:15Z"))
        self.assertFalse(new_since(rows, "2026-10-26T14:00:00Z"))
        self.assertNotEqual(
            digest(rows), digest(inventory(discussion(body="Updated rebuttal"), "me"))
        )

    def test_bot_outputs_visible_but_cannot_trigger_feedback_loop(self):
        d = discussion()
        before = digest(inventory(d, "me"))
        d["reviews"].append(
            {
                "id": 99,
                "user": {"login": "me"},
                "submitted_at": "2026-10-27T00:00:00Z",
                "body": "Verdict\n"
                + marker("bot", {"id": "j", "head": "a" * 40}, "review"),
            }
        )
        d["issue_comments"].append(
            {
                "id": 100,
                "user": {"login": "other[bot]", "type": "Bot"},
                "created_at": "2026-10-27T00:00:00Z",
                "body": "Automated notice",
            }
        )
        rows = inventory(d, "me")
        self.assertEqual(len(rows), 4)
        self.assertEqual(digest(rows), before)
        self.assertFalse(new_since(rows, "2026-10-26T14:00:00Z"))

    def test_incomplete_discussion_fails_closed(self):
        with self.assertRaises(ValueError):
            inventory({"issue_comments": []}, "me")

    def test_github_evidence_paginates_every_comment_source(self):
        gh = object.__new__(GitHub)
        gh.author = "me"
        d = discussion()
        linked = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "closingIssuesReferences": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }
        gh.api = Mock(
            side_effect=[d[k] for k in ("issue_comments", "review_comments", "reviews")]
            + [linked]
        )
        e = gh.evidence(
            {"repo": "o/r", "pr": 1, "metadata": "{}"}, {"head": {"sha": "a"}}
        )
        self.assertEqual(len(e["discussion_index"]), 2)
        self.assertTrue(e["linked_issues"]["complete"])
        calls = gh.api.call_args_list
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(c.kwargs["pages"] for c in calls[:3]))
        self.assertEqual(calls[3].args[0], "graphql")


class TestContextReviewRounds(unittest.TestCase):
    setUp = fixtures.TestWorkflow.setUp
    tearDown = fixtures.TestWorkflow.tearDown
    job = fixtures.TestWorkflow.job

    def done(self):
        j = self.job()
        self.store.update(j["id"], state="done", review_id=99, review_attempted=1)
        self.gh.api.return_value = self.gh.pr.return_value
        d = discussion()
        d["reviews"] = [{"id": 99, "submitted_at": "2026-10-26T13:09:15Z"}]
        self.gh.discussion.return_value = d
        return self.store.job(j["id"])

    def test_new_author_comment_reopens_same_head_without_mutating_old_review(self):
        old = self.done()
        first = self.service.command("enqueue", {"pr": 1, "force": True})
        again = self.service.command("enqueue", {"pr": 1, "force": True})
        self.assertTrue(first["enqueued"])
        self.assertFalse(again["enqueued"])
        new = first["job"]
        self.assertNotEqual(new["id"], old["id"])
        self.assertEqual(new["id"], again["job"]["id"])
        self.assertEqual(new["head"], old["head"])
        self.assertEqual(self.store.job(old["id"])["review_id"], 99)
        self.assertEqual(self.store.job(old["id"])["state"], "done")
        self.assertEqual(new["review_attempted"], 0)
        self.assertNotEqual(
            marker("test-bot", old, "review"), marker("test-bot", new, "review")
        )

    def test_unchanged_completed_discussion_does_not_replay(self):
        old = self.done()
        rows = inventory(self.gh.discussion.return_value, "me")
        self.store.update(
            old["id"], metadata=json.dumps({"discussion_digest": digest(rows)})
        )
        r = self.service.command("enqueue", {"pr": 1, "force": True})
        self.assertFalse(r["enqueued"])
        self.assertEqual(r["job"]["id"], old["id"])

    def test_prior_uncertain_review_cannot_be_bypassed_by_new_discussion(self):
        self.done()
        other = self.job(pr=1, head="b" * 40)
        self.store.update(other["id"], state="failed", review_attempted=1)
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            self.service.command("enqueue", {"pr": 1, "force": True})
        self.assertEqual(len(self.store.jobs()), 2)

    def test_changed_discussion_before_publication_retains_result_and_queues_fresh_input(
        self,
    ):
        j = self.job()
        self.service.save_result(
            j, self.store.slots()[0], assessment(j), "fake", "xhigh"
        )
        self.store.update(
            j["id"],
            metadata=json.dumps(
                {**json.loads(j["metadata"]), "discussion_digest": "old"}
            ),
        )
        self.gh.discussion.return_value = discussion()
        self.service.publish(self.store.job(j["id"]))
        self.gh.submit_review.assert_not_called()
        old = self.store.job(j["id"])
        self.assertEqual(old["state"], "superseded")
        self.assertIsNotNone(old["result"])
        new = next(x for x in self.store.jobs() if x["id"] != j["id"])
        self.assertEqual(new["state"], "queued")
        self.assertIn(new["id"], self.service.eligible)

    def test_migration_preserves_old_jobs_and_context_round_identity(self):
        old = self.done()
        new = self.service.command("enqueue", {"pr": 1, "force": True})["job"]
        self.store.migrate()
        self.assertEqual(self.store.job(old["id"])["review_id"], 99)
        self.assertEqual(self.store.job(new["id"])["state"], "queued")
        self.assertIsNotNone(self.store.claim(0, {new["id"]}))
        self.assertIsNone(self.store.claim(1, {new["id"]}))


class TestContextDiscovery(unittest.TestCase):
    def test_discussion_after_marked_review_is_automatically_admitted(self):
        head = "a" * 40
        pr = {
            "number": 1,
            "headRefOid": head,
            "author": {"login": "author"},
            "updatedAt": "now",
            "mergeable": "MERGEABLE",
            "reviews": {
                "nodes": [
                    {
                        "author": {"login": "me"},
                        "state": "CHANGES_REQUESTED",
                        "commit": {"oid": head},
                        "submittedAt": "2026-10-26T13:09:15Z",
                        "body": marker("bot", {"id": "old", "head": head}, "review"),
                    }
                ]
            },
        }
        q = Mock()
        q.all_prs.return_value = [pr]
        q.classify.return_value = ("excluded", "already-reviewed", None)
        config = Mock(repository="o/r", reviewer="me", exclude=(), stale_ci_days=14)
        gh = Mock()
        gh.discussion.return_value = discussion()
        gh.author = "me"
        gh.api.return_value = []
        with (
            patch("mo_bot.github.load_queue", return_value=q),
            patch("mo_bot.github.GitHub", return_value=gh),
        ):
            s = discover(config)
        self.assertEqual(len(s["candidates"]), 1)
        self.assertTrue(s["candidates"][0]["context_review"])
        self.assertEqual(s["admission"]["1"]["reason"], "discussion-updated")
        q.classify.return_value = ("excluded", "draft", None)
        with (
            patch("mo_bot.github.load_queue", return_value=q),
            patch("mo_bot.github.GitHub", return_value=gh),
        ):
            self.assertEqual(discover(config)["candidates"], [])


class TestDiscussionLedger(unittest.TestCase):
    def test_empty_review_may_be_listed_but_is_not_required(self):
        from mo_bot.review_context import validate_ledger

        d = discussion()
        d["reviews"].append({"id": 9, "body": "", "user": {"login": "approver"}})
        rows = inventory(d, "me")
        ledger = [
            {
                "keys": [r["key"] for r in rows if r["body"].strip()],
                "disposition": "informational",
                "rationale": "Prior discussion was reviewed",
                "evidence": [],
            }
        ]
        validate_ledger(rows, ledger)
        ledger[0]["keys"].append("reviews:9")
        validate_ledger(rows, ledger)
        ledger[0]["keys"].append("reviews:10")
        with self.assertRaisesRegex(ValueError, "duplicate or unknown"):
            validate_ledger(rows, ledger)
        ledger[0]["keys"][-1] = "reviews:9"
        with self.assertRaisesRegex(ValueError, "duplicate or unknown"):
            validate_ledger(rows, ledger)

    def test_missing_rebuttal_and_unsupported_conclusions_are_rejected(self):
        from mo_bot.review_context import validate_ledger

        rows = inventory(discussion(), "me")
        ledger = [
            {
                "keys": [r["key"] for r in rows],
                "disposition": "accepted-design",
                "rationale": "Documented contract addresses the prior finding",
                "evidence": ["docs/contract.md:5"],
            }
        ]
        validate_ledger(rows, ledger)
        ledger[0]["keys"] = [rows[0]["key"]]
        with self.assertRaisesRegex(ValueError, "omits"):
            validate_ledger(rows, ledger)
        ledger[0]["keys"] = [r["key"] for r in rows]
        ledger[0]["evidence"] = []
        with self.assertRaisesRegex(ValueError, "requires evidence"):
            validate_ledger(rows, ledger)
        ledger[0]["disposition"] = "informational"
        validate_ledger(rows, ledger)

    def test_publication_gate_requires_complete_discussion_ledger(self):
        case = TestContextReviewRounds()
        case.setUp()
        try:
            j = case.job()
            directory = case.service.job_dir(j) / "attempt-1"
            directory.mkdir()
            rows = inventory(discussion(), "me")
            (directory / "evidence.json").write_text(
                json.dumps({"discussion_index": rows})
            )
            case.store.update(j["id"], turn_dir=str(directory))
            j = case.store.job(j["id"])
            with self.assertRaisesRegex(ValueError, "required"):
                case.service.save_result(j, case.store.slots()[0], assessment(j))
            ledger = [
                {
                    "keys": [r["key"] for r in rows],
                    "disposition": "resolved",
                    "rationale": "Source and contract reconcile the discussion",
                    "evidence": ["source.go:42"],
                }
            ]
            (directory / "discussion-assessment.json").write_text(json.dumps(ledger))
            case.service.save_result(j, case.store.slots()[0], assessment(j))
            self.assertEqual(case.store.job(j["id"])["state"], "publishing")
        finally:
            case.tearDown()
