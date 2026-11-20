import datetime as dt
import unittest
from unittest.mock import Mock

from mo_bot.review_queue import Queue, ci_state


def check(name, required, conclusion="SUCCESS", status="COMPLETED"):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    return {
        "__typename": "CheckRun",
        "name": name,
        "isRequired": required,
        "conclusion": conclusion,
        "status": status,
        "startedAt": now,
        "completedAt": now,
    }


def pr_with(*checks):
    return {"statusCheckRollup": {"contexts": {"nodes": list(checks)}}}


class RequiredCheckTests(unittest.TestCase):
    def test_required_pass_ignores_cancelled_nonrequired(self):
        self.assertTrue(
            ci_state(
                pr_with(check("CI Required", True), check("macos", False, "CANCELLED")),
                30,
            )[0]
        )

    def test_required_pending_or_failed_blocks(self):
        for conclusion, status in [("FAILURE", "COMPLETED"), (None, "IN_PROGRESS")]:
            self.assertEqual(
                ci_state(
                    pr_with(
                        check("CI Required", True, conclusion, status),
                        check("other", False),
                    ),
                    30,
                ),
                (False, "not-green"),
            )

    def test_without_required_checks_preserves_existing_policy(self):
        self.assertEqual(
            ci_state(pr_with(check("optional", False, "CANCELLED")), 30),
            (False, "not-green"),
        )
        self.assertEqual(ci_state(pr_with(), 30), (False, "missing"))

    def test_incomplete_or_changed_head_is_not_admitted(self):
        gh = Mock()
        gh.api.return_value = {
            "data": {
                "repository": {"p1": {"headRefOid": "old", "statusCheckRollup": None}}
            }
        }
        pr = {"number": 1, "headRefOid": "new"}
        Queue(gh)._load_required_checks("owner", "repo", [pr])
        self.assertEqual(ci_state(pr, 30), (False, "missing"))
        gh.api.return_value = {
            "data": {
                "repository": {
                    "p1": {
                        "headRefOid": "new",
                        "statusCheckRollup": {
                            "contexts": {
                                "pageInfo": {"hasNextPage": True},
                                "nodes": [check("CI Required", True)],
                            }
                        },
                    }
                }
            }
        }
        Queue(gh)._load_required_checks("owner", "repo", [pr])
        self.assertEqual(ci_state(pr, 30), (False, "missing"))
