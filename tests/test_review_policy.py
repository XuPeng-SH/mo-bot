import datetime as dt
import unittest

from mo_bot.review_queue import automatic_review_not_before


class TestAuthorDelay(unittest.TestCase):
    def test_exact_commit_clock_and_author_case_across_api_shapes(self):
        commit = {"oid": "a" * 40, "committed_at": "2026-11-03T03:00:00Z"}
        expected = dt.datetime(2026, 11, 3, 3, 20, tzinfo=dt.timezone.utc)
        for pr in (
            {"headRefOid": commit["oid"], "author": {"login": "MatrixAdventurer"}},
            {"head": {"sha": commit["oid"]}, "user": {"login": "MATRIXADVENTURER"}},
        ):
            with self.subTest(pr=pr):
                self.assertEqual(
                    automatic_review_not_before(pr, {"matrixadventurer": 20}, commit),
                    expected,
                )
                self.assertIsNone(automatic_review_not_before(pr, {}, {}))
        with self.assertRaisesRegex(ValueError, "does not match"):
            automatic_review_not_before(
                pr, {"matrixadventurer": 20}, dict(commit, oid="b" * 40)
            )
        with self.assertRaisesRegex(ValueError, "timezone"):
            automatic_review_not_before(
                pr,
                {"matrixadventurer": 20},
                dict(commit, committed_at="2026-11-03T03:00:00"),
            )
