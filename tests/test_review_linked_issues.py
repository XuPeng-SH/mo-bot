import copy
import json
import unittest
from unittest.mock import Mock

from mo_bot.github import GitHub
from mo_bot.review_context import issue_references, linked_issue_digest


def graphql(nodes=(), more=False, cursor=None):
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "closingIssuesReferences": {
                        "nodes": list(nodes),
                        "pageInfo": {"hasNextPage": more, "endCursor": cursor},
                    }
                }
            }
        }
    }


def issue(number, body="actual reproduction", repo="o/r"):
    return {
        "id": number * 10,
        "number": number,
        "title": f"Issue {number}",
        "body": body,
        "state": "open",
        "html_url": f"https://github.com/{repo}/issues/{number}",
        "updated_at": "2026-10-27T00:00:00Z",
    }


def comment(number, body="new context", actor="human"):
    return {
        "id": number,
        "body": body,
        "user": {"login": actor, "type": "User"},
        "created_at": "2026-10-27T00:00:00Z",
        "updated_at": "2026-10-27T00:00:00Z",
    }


def issue_node(number, body="actual reproduction", comments=(), more=False, repo="o/r"):
    return {
        "__typename": "Issue",
        "databaseId": number * 10,
        "number": number,
        "title": f"Issue {number}",
        "body": body,
        "state": "OPEN",
        "url": f"https://github.com/{repo}/issues/{number}",
        "updatedAt": "2026-10-27T00:00:00Z",
        "comments": {
            "nodes": [
                {
                    "databaseId": c["id"],
                    "body": c["body"],
                    "url": c.get("html_url"),
                    "createdAt": c["created_at"],
                    "updatedAt": c["updated_at"],
                    "author": {
                        "login": c["user"]["login"],
                        "__typename": c["user"]["type"],
                    },
                }
                for c in comments
            ],
            "pageInfo": {"hasNextPage": more, "endCursor": "cursor" if more else None},
        },
    }


def batch(*values):
    return {"data": {f"r{i}": {"target": value} for i, value in enumerate(values)}}


class TestIssueReferences(unittest.TestCase):
    def test_body_references_and_nonreferences(self):
        body = """## Which issue(s) this PR fixes:
- #31056
- o/r#31056
- https://github.com/o/r/issues/99
- github.com/o/r/issues/98
- notgithub.com/o/r/issues/97
- Org/Other#42
- https://github.com/o/r/pull/8
- `#555`
<!-- do not use #666 -->
```sql
-- #777
select 1;
```
See #100 and \\#999.
"""
        refs = issue_references(body, "o/r")
        self.assertEqual(
            refs,
            {("o/r", 31056), ("o/r", 99), ("o/r", 98), ("org/other", 42), ("o/r", 100)},
        )

    def test_oversized_issue_numbers_do_not_abort_review_or_create_partial_refs(self):
        body = "#" + "9" * 5000 + " #2147483648 #2147483647 #0"
        self.assertEqual(issue_references(body, "o/r"), {("o/r", 2147483647)})


class TestLinkedIssueEvidence(unittest.TestCase):
    def setUp(self):
        self.gh = object.__new__(GitHub)
        self.gh.author = "me"
        self.job = {"repo": "o/r", "pr": 7, "metadata": "{}"}
        self.pr = {"body": "## Which issue(s) this PR fixes:\n\n- #31056\n"}

    def test_template_reference_and_github_link_are_collected_once(self):
        self.gh.api = Mock(
            side_effect=[
                graphql(
                    [
                        {"number": 31056, "repository": {"nameWithOwner": "O/R"}},
                        {"number": 55, "repository": {"nameWithOwner": "x/y"}},
                    ]
                ),
                batch(
                    issue_node(31056, comments=[comment(1)]),
                    issue_node(55, comments=[comment(1)], repo="x/y"),
                ),
            ]
        )
        ctx = self.gh.linked_issues(self.job, self.pr)
        self.assertTrue(ctx["complete"])
        self.assertEqual(
            [(x["repo"], x["number"]) for x in ctx["issues"]],
            [("o/r", 31056), ("x/y", 55)],
        )
        self.assertEqual(ctx["issues"][0]["issue"]["body"], "actual reproduction")
        self.assertEqual(ctx["issues"][0]["comments"][0]["body"], "new context")
        self.assertEqual(self.gh.api.call_count, 2)
        self.assertTrue(all(c.args[0] == "graphql" for c in self.gh.api.call_args_list))

    def test_human_review_discussion_references_are_collected(self):
        self.gh.api = Mock(
            side_effect=[graphql(), batch(issue_node(12), issue_node(31056))]
        )
        discussion = {
            "issue_comments": [comment(1, "Related issue #12")],
            "review_comments": [],
            "reviews": [],
        }
        ctx = self.gh.linked_issues(self.job, self.pr, discussion)
        self.assertTrue(ctx["complete"])
        self.assertEqual([x["number"] for x in ctx["issues"]], [12, 31056])

    def test_bot_discussion_does_not_add_references(self):
        marked = comment(
            1, 'See #12\n<!-- mo-bot-review:v1 {"job":"123"} -->', actor="me"
        )
        self.gh.api = Mock(side_effect=[graphql(), batch(issue_node(31056))])
        discussion = {
            "issue_comments": [marked, comment(2, "See #13", "other[bot]")],
            "review_comments": [],
            "reviews": [],
        }
        ctx = self.gh.linked_issues(self.job, self.pr, discussion)
        self.assertTrue(ctx["complete"])
        self.assertEqual([x["number"] for x in ctx["issues"]], [31056])

    def test_pagination_is_exhausted_and_pr_references_are_not_issues(self):
        self.gh.api = Mock(
            side_effect=[
                graphql(
                    [{"number": 2, "repository": {"nameWithOwner": "o/r"}}], True, "c1"
                ),
                graphql([{"number": 3, "repository": {"nameWithOwner": "o/r"}}]),
                batch(
                    issue_node(2, comments=[comment(1)]),
                    issue_node(3),
                    {"__typename": "PullRequest", "number": 4},
                ),
            ]
        )
        ctx = self.gh.linked_issues(self.job, {"body": "Issue #4"})
        self.assertTrue(ctx["complete"])
        self.assertEqual([x["number"] for x in ctx["issues"]], [2, 3])
        self.assertEqual(ctx["non_issues"], [{"repo": "o/r", "number": 4}])
        self.assertEqual(
            self.gh.api.call_args_list[1].kwargs["body"]["variables"]["cursor"], "c1"
        )

    def test_graphql_or_issue_read_failure_is_disclosed(self):
        self.gh.api = Mock(
            side_effect=[
                RuntimeError("discovery unavailable"),
                RuntimeError("batch unavailable"),
            ]
        )
        ctx = self.gh.linked_issues(self.job, self.pr)
        self.assertFalse(ctx["complete"])
        self.assertEqual(ctx["issues"][0]["status"], "unavailable")
        self.assertEqual(ctx["issues"][0]["number"], 31056)
        self.assertEqual(ctx["link_discovery"], "unavailable")

    def test_long_issue_thread_uses_existing_paginated_reader(self):
        self.gh.api = Mock(
            side_effect=[
                graphql(),
                batch(issue_node(31056, comments=[comment(1)], more=True)),
                [comment(1), comment(2)],
            ]
        )
        ctx = self.gh.linked_issues(self.job, self.pr)
        self.assertTrue(ctx["complete"])
        self.assertEqual(len(ctx["issues"][0]["comments"]), 2)
        self.assertEqual(
            self.gh.api.call_args_list[-1].args[0], "repos/o/r/issues/31056/comments"
        )
        self.assertTrue(self.gh.api.call_args_list[-1].kwargs["pages"])

    def test_official_issue_is_prioritized_when_many_refs_are_omitted(self):
        self.gh.api = Mock(
            side_effect=[
                graphql([{"number": 1000, "repository": {"nameWithOwner": "o/r"}}]),
                batch(*[issue_node(i) for i in [1000, *range(1, 32)]]),
            ]
        )
        pr = {"body": " ".join(f"#{i}" for i in range(1, 35))}
        ctx = self.gh.linked_issues(self.job, pr)
        self.assertFalse(ctx["complete"])
        self.assertIn(1000, [x["number"] for x in ctx["issues"]])
        self.assertEqual([x["number"] for x in ctx["omitted"]], [32, 33, 34])
        self.assertEqual(self.gh.api.call_count, 2)

    def test_partial_official_link_pagination_retains_earlier_pages(self):
        self.gh.api = Mock(
            side_effect=[
                graphql(
                    [{"number": 2, "repository": {"nameWithOwner": "o/r"}}],
                    True,
                    "next",
                ),
                RuntimeError("second page unavailable"),
                batch(issue_node(2)),
            ]
        )
        ctx = self.gh.linked_issues(self.job, {"body": ""})
        self.assertFalse(ctx["complete"])
        self.assertEqual(ctx["link_discovery"], "partial")
        self.assertEqual([x["number"] for x in ctx["issues"]], [2])

    def test_partial_batch_keeps_available_issues_and_reports_missing(self):
        payload = batch(issue_node(2), None)
        payload["errors"] = [{"message": "some issues unavailable"}]
        self.gh.api = Mock(side_effect=[graphql(), payload])
        ctx = self.gh.linked_issues(self.job, {"body": "#2 #3"})
        self.assertFalse(ctx["complete"])
        self.assertEqual([x["status"] for x in ctx["issues"]], ["ok", "unavailable"])
        self.assertEqual(ctx["issues"][0]["issue"]["number"], 2)

    def test_invalid_batch_is_disclosed_instead_of_aborting_review(self):
        self.gh.api = Mock(side_effect=[graphql(), None])
        ctx = self.gh.linked_issues(self.job, self.pr)
        self.assertFalse(ctx["complete"])
        self.assertEqual(ctx["issues"][0]["status"], "unavailable")
        self.assertEqual(ctx["errors"][0]["source"], "issue_batch")

    def test_comment_pagination_failure_retains_issue_body_with_partial_status(self):
        self.gh.api = Mock(
            side_effect=[
                graphql(),
                batch(issue_node(31056, more=True)),
                RuntimeError("pagination failed"),
            ]
        )
        ctx = self.gh.linked_issues(self.job, self.pr)
        self.assertFalse(ctx["complete"])
        self.assertEqual(ctx["issues"][0]["status"], "partial")
        self.assertEqual(ctx["issues"][0]["issue"]["body"], "actual reproduction")

    def test_fetch_deadline_stops_more_calls_and_marks_context_incomplete(self):
        from unittest.mock import patch

        self.gh.api = Mock(return_value=graphql())
        with patch("mo_bot.github.time.monotonic", side_effect=[0, 1, 31]):
            ctx = self.gh.linked_issues(self.job, self.pr)
        self.assertFalse(ctx["complete"])
        self.assertEqual(ctx["issues"][0]["status"], "unavailable")
        self.assertEqual(self.gh.api.call_count, 1)
        self.assertLessEqual(self.gh.api.call_args.kwargs["timeout"], 10)

    def test_comment_or_body_change_changes_snapshot_bot_chatter_does_not(self):
        self.gh.api = Mock(
            side_effect=[graphql(), batch(issue_node(31056, comments=[comment(1)]))]
        )
        old = self.gh.linked_issues(self.job, self.pr)
        baseline = linked_issue_digest(self.pr, old, "me")
        changed = copy.deepcopy(old)
        changed["issues"][0]["issue"]["body"] = "new reproduction"
        self.assertNotEqual(baseline, linked_issue_digest(self.pr, changed, "me"))
        changed = copy.deepcopy(old)
        changed["issues"][0]["comments"][0]["body"] = "new human comment"
        self.assertNotEqual(baseline, linked_issue_digest(self.pr, changed, "me"))
        changed = copy.deepcopy(old)
        changed["issues"][0]["comments"].append(comment(2, actor="other[bot]"))
        self.assertEqual(baseline, linked_issue_digest(self.pr, changed, "me"))
        self.assertNotEqual(baseline, linked_issue_digest({"body": "other"}, old, "me"))

    def test_evidence_carries_the_frozen_issue_snapshot(self):
        self.gh.api = Mock(
            side_effect=[
                [],
                [],
                [],
                graphql(),
                batch(issue_node(31056, comments=[comment(1)])),
            ]
        )
        e = self.gh.evidence(self.job, self.pr)
        self.assertEqual(e["linked_issues"]["issues"][0]["issue"]["number"], 31056)
        self.assertEqual(
            e["linked_issue_digest"],
            linked_issue_digest(self.pr, e["linked_issues"], "me"),
        )


class TestLinkedIssuePublication(unittest.TestCase):
    from tests.test_service import TestWorkflow as fixture

    setUp = fixture.setUp
    tearDown = fixture.tearDown
    job = fixture.job

    def issue_context(self, body="original"):
        return {
            "complete": True,
            "link_discovery": "complete",
            "issues": [
                {
                    "repo": "matrixorigin/matrixone",
                    "number": 123,
                    "status": "ok",
                    "issue": issue(123, body),
                    "comments": [comment(1)],
                }
            ],
            "non_issues": [],
            "omitted": [],
            "errors": [],
        }

    def assessed_job(self, context):
        from tests.test_service import assessment

        j = self.job()
        self.gh.pr.return_value["body"] = "Fixes #123"
        self.service.save_result(
            j, self.store.slots()[0], assessment(j), "fake", "xhigh"
        )
        metadata = json.loads(j["metadata"])
        metadata["linked_issue_digest"] = linked_issue_digest(
            self.gh.pr.return_value, context, self.gh.author
        )
        metadata["linked_issue_complete"] = context["complete"]
        self.store.update(j["id"], metadata=json.dumps(metadata))
        return self.store.job(j["id"])

    def test_changed_issue_supersedes_result_and_queues_new_review(self):
        job = self.assessed_job(self.issue_context())
        self.gh.linked_issues.return_value = self.issue_context("updated contract")
        self.service.publish(job)
        self.gh.submit_review.assert_not_called()
        old = self.store.job(job["id"])
        self.assertEqual(old["state"], "superseded")
        self.assertIsNotNone(old["result"])
        new = next(x for x in self.store.jobs() if x["id"] != job["id"])
        self.assertEqual(new["state"], "queued")
        self.assertIn(new["id"], self.service.eligible)

    def test_unchanged_issue_allows_publication(self):
        context = self.issue_context()
        job = self.assessed_job(context)
        self.gh.linked_issues.return_value = context
        self.service.publish(job)
        self.gh.submit_review.assert_called_once()
        self.assertEqual(self.store.job(job["id"])["state"], "done")

    def test_pr_body_change_supersedes_even_if_code_and_issue_are_unchanged(self):
        context = self.issue_context()
        job = self.assessed_job(context)
        self.gh.pr.return_value["body"] = "Fixes #123; revised expected behavior"
        self.gh.linked_issues.return_value = context
        self.service.publish(job)
        self.gh.submit_review.assert_not_called()
        self.assertEqual(self.store.job(job["id"])["state"], "superseded")

    def test_new_discussion_requeues_without_waiting_for_issue_lookup(self):
        from mo_bot.review_context import digest, inventory
        from tests.test_review_context import discussion

        context = self.issue_context()
        job = self.assessed_job(context)
        metadata = json.loads(job["metadata"])
        metadata["discussion_digest"] = digest(inventory(discussion(body="old"), "me"))
        self.store.update(job["id"], metadata=json.dumps(metadata))
        self.gh.discussion.return_value = discussion(body="new")
        self.gh.linked_issues.side_effect = RuntimeError("temporary outage")
        self.service.publish(self.store.job(job["id"]))
        self.gh.linked_issues.assert_not_called()
        self.assertEqual(self.store.job(job["id"])["state"], "superseded")

    def test_legacy_job_publishes_without_issue_snapshot_requery(self):
        from tests.test_service import assessment

        job = self.job()
        self.service.save_result(
            job, self.store.slots()[0], assessment(job), "fake", "xhigh"
        )
        self.service.publish(self.store.job(job["id"]))
        self.gh.linked_issues.assert_not_called()
        self.gh.submit_review.assert_called_once()

    def test_previously_complete_issue_snapshot_unavailable_defers_publication(self):
        job = self.assessed_job(self.issue_context())
        broken = self.issue_context()
        broken["complete"] = False
        broken["issues"][0]["status"] = "unavailable"
        self.gh.linked_issues.return_value = broken
        self.service.publish(job)
        self.gh.submit_review.assert_not_called()
        self.assertEqual(self.store.job(job["id"])["state"], "publishing")
        self.assertGreater(self.store.job(job["id"])["retry_at"], 0)
        self.assertEqual(len(self.store.jobs()), 1)
        self.gh.linked_issues.return_value = self.issue_context()
        self.service.publish(self.store.job(job["id"]))
        self.gh.submit_review.assert_called_once()
        self.assertEqual(self.store.job(job["id"])["state"], "done")

    def test_persistent_unavailability_queues_review_with_incomplete_evidence(self):
        job = self.assessed_job(self.issue_context())
        broken = self.issue_context()
        broken["complete"] = False
        broken["issues"][0]["status"] = "unavailable"
        self.gh.linked_issues.return_value = broken
        self.service.publish(job)
        self.assertEqual(self.store.job(job["id"])["state"], "publishing")
        self.service.publish(self.store.job(job["id"]))
        self.gh.submit_review.assert_not_called()
        self.assertEqual(self.store.job(job["id"])["state"], "superseded")
        new = next(x for x in self.store.jobs() if x["id"] != job["id"])
        self.assertEqual(new["state"], "queued")
        self.assertIn(new["id"], self.service.eligible)


if __name__ == "__main__":
    unittest.main()
