import unittest
from unittest.mock import Mock, patch

from mo_bot.github import GitHub, discover, marker
from mo_bot.review_queue import owner_wip


class TestFormalReviews(unittest.TestCase):
    def setUp(self):
        self.gh = object.__new__(GitHub)
        self.gh.config = Mock(service_id="stable")
        self.gh.author = "me"
        self.gh.api = Mock()
        self.job = {"id": "job", "head": "a" * 40, "repo": "owner/repo", "pr": 1}

    def review(self, state="APPROVED", author="me", head=None):
        return {
            "id": 12,
            "user": {"login": author},
            "state": state,
            "commit_id": head or self.job["head"],
            "body": "Assessment\n" + marker("stable", self.job, "review"),
        }

    def test_submit_pins_commit_and_uses_formal_review_endpoint(self):
        for event, state in (
            ("APPROVE", "APPROVED"),
            ("REQUEST_CHANGES", "CHANGES_REQUESTED"),
            ("COMMENT", "COMMENTED"),
        ):
            with self.subTest(event=event):
                self.gh.api.side_effect = [
                    {"state": "open", "head": {"sha": self.job["head"]}},
                    self.review(state),
                ]
                result = self.gh.submit_review(self.job, event, "evidence")
                self.assertEqual(result["id"], 12)
                args, kwargs = self.gh.api.call_args
                self.assertEqual(args[0], "repos/owner/repo/pulls/1/reviews")
                self.assertEqual(kwargs["method"], "POST")
                self.assertEqual(kwargs["body"]["commit_id"], self.job["head"])
                self.assertEqual(kwargs["body"]["event"], event)
                self.assertEqual(
                    self.gh.owned_marker(result, self.job)["state"], "review"
                )

    def test_changed_head_fences_post_at_wire_entrypoint(self):
        self.gh.api.return_value = {"state": "open", "head": {"sha": "b" * 40}}
        with self.assertRaises(RuntimeError):
            self.gh.submit_review(self.job, "APPROVE", "evidence")
        self.gh.api.assert_called_once()

    def test_only_authenticated_markers_count_and_duplicates_fail(self):
        owned = self.review()
        self.gh.api.return_value = [self.review(author="someone"), owned]
        self.assertEqual(self.gh.find_review(self.job), owned)
        self.gh.api.return_value = [owned, owned]
        with self.assertRaises(RuntimeError):
            self.gh.find_review(self.job)

    def test_dismissed_wrong_head_and_wrong_event_need_reconciliation(self):
        for review in (
            self.review(state="DISMISSED"),
            self.review(head="b" * 40),
            self.review(state="COMMENTED"),
        ):
            with self.assertRaises(RuntimeError):
                self.gh.validate_review(review, self.job, "APPROVE")


class TestOwnPRDiscovery(unittest.TestCase):
    def own_snapshot(self, reviews, ci="not-green", mergeable="MERGEABLE", labels=()):
        queue = Mock()
        queue.all_prs.return_value = [
            {
                "number": 1,
                "author": {"login": "me"},
                "labels": {"nodes": [{"name": label} for label in labels]},
                "headRefOid": "a" * 40,
                "updatedAt": "now",
                "mergeable": mergeable,
                "reviews": {"nodes": reviews},
            }
        ]
        queue.classify.return_value = ("excluded", "self-authored", None)
        queue.ci_state.return_value = (ci.startswith("fresh"), ci)
        config = Mock(
            repository="owner/repo", reviewer="me", exclude=(), stale_ci_days=14
        )
        with patch("mo_bot.github.load_queue", return_value=queue):
            return discover(config)

    def test_own_unreviewed_head_admitted_independently_of_ci(self):
        for ci in ("missing", "not-green", "stale (20d)", "fresh (0d)"):
            with self.subTest(ci=ci):
                snapshot = self.own_snapshot([], ci=ci)
                self.assertEqual(len(snapshot["candidates"]), 1)
                self.assertEqual(snapshot["admission"]["1"]["state"], "candidate")
                self.assertEqual(len(snapshot["own_prs"]), 1)
        for mergeable, unknown in (("CONFLICTING", []), ("UNKNOWN", [1])):
            snapshot = self.own_snapshot([], mergeable=mergeable)
            self.assertFalse(snapshot["candidates"])
            self.assertEqual(snapshot["indeterminate"], unknown)

    def test_owner_wip_excludes_both_pools_and_rest_schema_matches_graphql(self):
        snapshot = self.own_snapshot([], labels=["do-not-merge/wip"])
        self.assertFalse(snapshot["candidates"])
        self.assertFalse(snapshot["own_prs"])
        self.assertEqual(snapshot["admission"]["1"]["reason"], "owner-wip")
        self.assertTrue(
            owner_wip(
                {"user": {"login": "ME"}, "labels": [{"name": "do-not-merge/wip"}]},
                "me",
            )
        )
        self.assertFalse(
            owner_wip(
                {"user": {"login": "other"}, "labels": [{"name": "do-not-merge/wip"}]},
                "me",
            )
        )

    def test_formal_review_is_scoped_to_exact_head_and_ignores_drafts(self):
        for state in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"):
            with self.subTest(state=state):
                current = {"state": state, "commit": {"oid": "a" * 40}}
                snapshot = self.own_snapshot([current])
                self.assertFalse(snapshot["candidates"])
                self.assertEqual(
                    snapshot["admission"]["1"]["reason"], "already-reviewed"
                )
                # Old-head formal reviews must not suppress a newly pushed head.
                current["commit"]["oid"] = "b" * 40
                self.assertEqual(len(self.own_snapshot([current])["candidates"]), 1)
        for state in ("PENDING", "DISMISSED"):
            self.assertEqual(
                len(
                    self.own_snapshot([{"state": state, "commit": {"oid": "a" * 40}}])[
                        "candidates"
                    ]
                ),
                1,
            )
        # Ordinary comments have no formal review state/commit.
        self.assertEqual(
            len(self.own_snapshot([{"body": "looks fine"}])["candidates"]), 1
        )

    def test_truncated_review_window_cannot_prove_absence(self):
        old = {"state": "COMMENTED", "commit": {"oid": "b" * 40}}
        snapshot = self.own_snapshot([old] * 100)
        self.assertFalse(snapshot["candidates"])
        self.assertEqual(snapshot["indeterminate"], [1])
        snapshot = self.own_snapshot(
            [old] * 99 + [{"state": "APPROVED", "commit": {"oid": "a" * 40}}]
        )
        self.assertEqual(snapshot["admission"]["1"]["reason"], "already-reviewed")

    def test_draft_and_explicit_exclusion_are_not_overridden(self):
        for reason in ("draft", "explicitly-excluded", "already-reviewed"):
            queue = Mock()
            queue.all_prs.return_value = [{"number": 1, "headRefOid": "a" * 40}]
            queue.classify.return_value = ("excluded", reason, None)
            config = Mock(
                repository="owner/repo", reviewer="me", exclude=(), stale_ci_days=14
            )
            with patch("mo_bot.github.load_queue", return_value=queue):
                self.assertFalse(discover(config)["candidates"])
            queue.ci_state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
