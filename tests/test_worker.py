import copy
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import time
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

from mo_bot.codex import SourceChanged, Workspace
from mo_bot.config import Config
from mo_bot.service import Service
from mo_bot.store import Store


HEAD = "a" * 40
BASE = "c" * 40
NEW = "b" * 40
ACTOR = {"id": 7, "login": "me"}
UPSTREAM = {
    "id": 1,
    "name": "repo",
    "full_name": "matrixorigin/repo",
    "default_branch": "main",
    "owner": {"id": 99},
}
FORK = {
    "id": 2,
    "full_name": "me/repo",
    "default_branch": "main",
    "owner": ACTOR,
    "parent": UPSTREAM,
}


def pr(number=1):
    return {
        "number": number,
        "state": "open",
        "draft": False,
        "user": ACTOR,
        "mergeable": True,
        "head": {"sha": HEAD, "ref": "fix/foo", "repo": copy.deepcopy(FORK)},
        "base": {"sha": BASE, "ref": "main", "repo": UPSTREAM},
    }


class FakeGitHub:
    def __init__(self):
        self.author = "me"
        self.actor = ACTOR
        self.prs = {1: pr(), 2: pr(2)}
        self.issue = {
            "id": 100,
            "number": 5,
            "state": "open",
            "title": "bug",
            "body": "reproduce bug",
            "user": {"id": 9},
        }
        self.comments_data, self.reviews, self.threads_data = [], [], []
        self.runs, self.checks, self.statuses = [], [], []
        self.issues, self.created_prs, self.writes = [], [], []
        self.claim = None
        self.ambiguous = False

    def api(self, endpoint, method="GET", body=None, pages=False):
        if method == "POST":
            if endpoint == "graphql":
                return {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": self.threads_data,
                                    "pageInfo": {"hasNextPage": False},
                                }
                            }
                        }
                    }
                }
            self.writes.append((endpoint, body))
            if endpoint.endswith("/issues"):
                issue = {
                    "number": 1000,
                    "html_url": "issue/1000",
                    "user": ACTOR,
                    **body,
                }
                self.issues.append(issue)
                if self.ambiguous:
                    raise RuntimeError("network response lost")
                return issue
            if endpoint.endswith("/pulls"):
                self.created_prs.append(
                    {
                        "number": 20,
                        "html_url": "pr/20",
                        "state": "open",
                        "user": ACTOR,
                        "body": body["body"],
                        "head": {
                            "repo": FORK,
                            "ref": body["head"].split(":", 1)[1],
                            "sha": NEW,
                        },
                        "base": {"repo": UPSTREAM, "ref": "main"},
                    }
                )
                if self.ambiguous:
                    raise RuntimeError("network response lost")
                return self.created_prs[-1]
            if endpoint.endswith("/rerun-failed-jobs"):
                run_id = int(endpoint.split("/runs/", 1)[1].split("/", 1)[0])
                next(r for r in self.runs if r["id"] == run_id)["run_attempt"] += 1
                return None
            raise AssertionError(endpoint)
        if endpoint in ("user", "users/me"):
            return self.actor
        if endpoint.startswith("search/issues?"):
            query = parse_qs(urlsplit(endpoint).query)["q"][0]
            token = query.rsplit('"', 2)[1]
            found = [i for i in self.issues if token in i.get("body", "")]
            return {
                "items": found,
                "total_count": len(found),
                "incomplete_results": False,
            }
        if (
            endpoint.startswith("repos/matrixorigin/repo/issues/")
            and not endpoint.endswith("/comments")
            and endpoint.rsplit("/", 1)[-1].isdigit()
            and int(endpoint.rsplit("/", 1)[-1]) >= 1000
        ):
            return next(
                i
                for i in self.issues
                if i["number"] == int(endpoint.rsplit("/", 1)[-1])
            )
        if endpoint == "repos/matrixorigin/repo":
            return UPSTREAM
        if endpoint == "repos/me/repo":
            return FORK
        if "/branches/" in endpoint:
            return {"commit": {"sha": getattr(self, "branch_sha", BASE)}}
        if "/pulls/" in endpoint:
            if endpoint.endswith("/reviews"):
                return self.reviews
            number = int(endpoint.rsplit("/", 1)[1])
            return (
                self.prs[number]
                if number in self.prs
                else next(p for p in self.created_prs if p["number"] == number)
            )
        if "/issues/5" in endpoint and not endpoint.endswith("/comments"):
            return self.issue
        if "/check-runs?" in endpoint:
            return {"check_runs": self.checks}
        if endpoint.endswith("/statuses"):
            return self.statuses
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": self.runs}
        if "/actions/runs/" in endpoint:
            return next(
                r for r in self.runs if r["id"] == int(endpoint.rsplit("/", 1)[1])
            )
        if "/issues?" in endpoint:
            return self.issues
        if "/pulls?" in endpoint:
            return self.created_prs
        raise AssertionError(endpoint)

    def pr(self, job):
        return self.api(f"repos/{job['repo']}/pulls/{job['pr']}")

    def comments(self, job):
        return self.comments_data

    def find_comment(self, job):
        return self.claim

    def ensure_comment(self, job):
        self.claim = {"id": 44}
        return self.claim

    def owned_marker(self, comment, job):
        return {"state": "reviewing"}

    def patch_comment(self, *args):
        pass


def result(job, state, **changes):
    metadata = json.loads(job["metadata"])
    covered_issues = [m["number"] for m in metadata.get("members", [])] or (
        [job["target_number"]] if job["target_type"] == "issue" else []
    )
    value = dict(
        job_id=job["id"],
        head_sha=job["head"],
        phase=job["phase"],
        covered_issues=covered_issues,
        classification="pr_defect",
        accepted=True,
        summary="Causal fix verified",
        evidence=["logs/control"],
        limitations=[],
        findings=[],
        rebase=False,
        candidate_head=state["head"],
        validation_complete=True,
        commit_message="fix: resolve causal defect",
        pr_body="Behavior and validation with UT and SCA",
        cause_signature="",
        issue_title="",
        issue_body="",
        rerun_ids=[],
        causes=[],
        failure_dispositions=[],
    )
    value.update(changes)
    return value


class TestWorker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        skill = self.root / "issue-skill"
        skill.write_text("Explicit issue-to-pr gate")
        config = Config(
            "test",
            self.root,
            "matrixorigin/repo",
            "me",
            self.root,
            self.root / "queue",
            worker_sessions=2,
            issue_skill=skill,
            minimum_free_gib=0,
        )
        self.gh = FakeGitHub()
        self.ws = Mock()
        self.ws.worker_prepare.return_value = self.root
        self.ws.worker_state.return_value = {
            "head": HEAD,
            "branch": "owned",
            "status": "",
            "index": "index",
            "config": "config",
            "source": "source",
        }
        self.ws.worker_git.return_value = BASE
        self.ws.worker_finalize.return_value = NEW
        self.ws.freeze_publication.return_value = "refs/mo-bot/test/delivery/frozen"
        self.ws.archive_worker.return_value = str(self.root / "saved-source")
        self.s = Service(config, github=self.gh, workspace=self.ws)
        self.w = self.s.worker
        self.s.models = [
            {
                "model": "gpt-6.1-sol",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low"},
                    {"reasoningEffort": "xhigh"},
                ],
            }
        ]
        self.s.backend = Mock(dead=False)
        for slot in self.s.store.slots():
            self.s.store.slot_update(
                slot["id"], ready=1, session_id="thread-" + str(slot["id"])
            )
        self.slot = dict(self.s.store.slots()[2])

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def job(self, number=1, issue=False):
        candidate = (
            self.w.issue_candidate(5)
            if issue
            else self.w.pr_candidate(number, explicit=True)
        )
        self.s.store.enqueue(candidate)
        return self.s.store.claim(self.slot["id"])

    def phase(self, job, **changes):
        self.w.launch(self.slot, job)
        job = self.s.store.job(job["id"])
        before = self.ws.worker_state.return_value
        value = result(job, before, **changes)
        model = "gpt-6.1-sol"
        effort = "xhigh" if job["phase"] in ("design", "final_review") else "low"
        self.w.consume(job, self.slot, value, model, effort)
        return self.s.store.job(job["id"])

    def test_four_phases_reuse_session_real_gate_model_params_and_fence(self):
        job = self.job()
        seen = []
        for phase in ("triage", "design", "implement", "final_review"):
            self.assertEqual(job["phase"], phase)
            job = self.phase(job)
            selected = self.s.backend.start.call_args.args[0]
            seen.append(
                (
                    selected["model"],
                    selected["effort"],
                    selected["source_write"],
                    selected["session_id"],
                )
            )
            if phase == "implement":
                self.ws.worker_state.return_value = {
                    **self.ws.worker_state.return_value,
                    "head": NEW,
                }
            if phase != "final_review":
                # No other work/review may take this target or the affine worker slot.
                self.assertIsNone(self.s.store.claim(3))
                job = self.s.store.claim(2)
        self.assertEqual(job["state"], "publishing")
        self.assertIsNone(self.s.store.claim(2))
        self.assertEqual([x[1] for x in seen], ["low", "xhigh", "low", "xhigh"])
        self.assertEqual([x[2] for x in seen], [False, False, True, False])
        self.assertEqual({x[3] for x in seen}, {"thread-2"})
        self.assertEqual(self.s.store.slots()[2]["effort"], "low")

    def test_gate_rejects_model_claim_without_actual_xhigh(self):
        job = self.phase(self.job())
        job = self.s.store.claim(2)
        self.w.launch(self.slot, job)
        job = self.s.store.job(job["id"])
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.w.consume(
                job,
                self.slot,
                result(job, self.ws.worker_state.return_value),
                "gpt-6.1-sol",
                "low",
            )

    def test_missing_gate_capability_never_falls_back(self):
        self.s.models = []
        job = self.job()
        with self.assertRaisesRegex(ValueError, "capability"):
            self.w.launch(self.slot, job)
        self.s.backend.start.assert_not_called()

    def test_bundled_workflow_launches_without_external_issue_skill(self):
        self.w.config = replace(self.w.config, issue_skill=None)
        job = self.job(issue=True)
        self.w.launch(self.slot, job)
        selected = self.s.backend.start.call_args.args[0]
        self.assertEqual(selected["effort"], "low")
        self.assertFalse(selected["source_write"])
        self.assertEqual(self.s.store.job(job["id"])["phase"], "triage")
        from mo_bot.worker import ISSUE_SKILL

        workflow = ISSUE_SKILL.read_text().split("---", 2)[2].strip()
        prompt = self.s.backend.start.call_args.args[3]
        self.assertEqual(prompt.count(workflow), 1)
        self.assertNotIn("Explicit issue-to-pr gate", prompt)

    def test_custom_workflow_supplements_bundled_workflow(self):
        from mo_bot.worker import ISSUE_SKILL

        self.w.launch(self.slot, self.job(issue=True))
        prompt = self.s.backend.start.call_args.args[3]
        self.assertIn(ISSUE_SKILL.read_text().split("---", 2)[2].strip(), prompt)
        self.assertIn("Explicit issue-to-pr gate", prompt)

    def test_configured_bundled_workflow_is_not_loaded_twice(self):
        from mo_bot.worker import ISSUE_SKILL

        self.w.config = replace(self.w.config, issue_skill=ISSUE_SKILL)
        self.w.launch(self.slot, self.job(issue=True))
        prompt = self.s.backend.start.call_args.args[3]
        self.assertEqual(
            prompt.count(ISSUE_SKILL.read_text().split("---", 2)[2].strip()), 1
        )

    def test_rebase_uses_actual_branch_target_and_keeps_it_pinned(self):
        target = "d" * 40
        self.gh.branch_sha = target
        job = self.phase(self.job(), rebase=True)
        job = self.s.store.claim(2)
        job = self.phase(job, rebase=True)
        data = json.loads(job["data"])
        self.assertEqual(data["rebase_target"]["sha"], target)
        self.assertEqual(json.loads(job["metadata"])["identity"]["base_sha"], BASE)
        self.gh.branch_sha = "e" * 40
        self.ws.worker_rebase_status.side_effect = ["pending", "conflicts"]
        self.w.launch(self.slot, self.s.store.claim(2))
        self.ws.worker_rebase.assert_called_once_with(2, target)
        data = json.loads(self.s.store.job(job["id"])["data"])
        self.assertFalse(data["rebased"])
        self.assertEqual(data["rebase_status"], "conflicts")

    def test_reentry_verifies_stale_success_flag_and_reuses_conflict_stop(self):
        job = self.job()
        data = {
            "needs_rebase": True,
            "rebased": True,
            "rebase_target": {"repo": job["repo"], "ref": "main", "sha": NEW},
        }
        self.s.store.update(job["id"], phase="implement", data=json.dumps(data))
        self.ws.worker_rebase_status.side_effect = ["pending", "conflicts"]
        self.w.launch(self.slot, self.s.store.job(job["id"]))
        self.ws.worker_rebase.assert_called_once_with(2, NEW)
        self.ws.worker_rebase.reset_mock()
        self.ws.worker_rebase_status.side_effect = None
        self.ws.worker_rebase_status.return_value = "conflicts"
        self.w.launch(self.slot, self.s.store.job(job["id"]))
        self.ws.worker_rebase.assert_not_called()

    def test_false_rebase_success_does_not_launch_worker(self):
        job = self.job()
        data = {
            "needs_rebase": True,
            "rebase_target": {"repo": job["repo"], "ref": "main", "sha": NEW},
        }
        self.s.store.update(job["id"], phase="implement", data=json.dumps(data))
        self.ws.worker_rebase_status.return_value = "pending"
        with self.assertRaisesRegex(RuntimeError, "neither approved target"):
            self.w.launch(self.slot, self.s.store.job(job["id"]))
        self.s.backend.start.assert_not_called()

    def test_coordinator_failure_retains_completed_implementation_output(self):
        job = self.phase(self.job())
        job = self.phase(self.s.store.claim(2))
        self.ws.worker_finalize.side_effect = RuntimeError("coordinator commit failed")
        with self.assertRaisesRegex(RuntimeError, "coordinator commit failed"):
            self.phase(
                self.s.store.claim(2),
                commit_message="perf(planner): verified improvement",
            )
        saved = json.loads(self.s.store.job(job["id"])["data"])
        self.assertTrue(saved["implement"]["result"]["accepted"])
        self.assertEqual(
            saved["implement"]["result"]["commit_message"],
            "perf(planner): verified improvement",
        )

    def test_legacy_rebase_checkpoint_returns_to_design(self):
        job = self.job()
        self.s.store.update(
            job["id"],
            phase="implement",
            data=json.dumps({"needs_rebase": True, "rebased": True}),
        )
        self.w.launch(self.slot, self.s.store.job(job["id"]))
        updated = self.s.store.job(job["id"])
        self.assertEqual(updated["phase"], "design")
        self.assertNotIn("rebased", json.loads(updated["data"]))
        self.ws.worker_rebase.assert_not_called()
        self.assertEqual(self.s.backend.start.call_args.args[0]["effort"], "xhigh")

    def test_blocked_implementation_preserves_source_and_can_resume(self):
        job = self.phase(self.job())
        job = self.phase(self.s.store.claim(2))
        self.s.turns.clear()
        self.s.backend.start.return_value.completed = True
        job = self.phase(
            self.s.store.claim(2),
            classification="blocked",
            accepted=False,
            summary="temporarily unavailable native dependency",
        )
        self.assertEqual(job["state"], "failed")
        self.ws.archive_worker.assert_called_once()
        self.assertIn("workspace_archive", json.loads(job["data"]))
        self.s.turns.clear()
        self.s.command("retry_job", {"job_id": job["id"]})
        updated = self.s.store.job(job["id"])
        self.assertEqual(updated["state"], "queued")
        self.assertEqual(updated["phase"], "implement")
        self.ws.restore_worker.assert_called_once()
        self.assertEqual(updated["slot_id"], self.slot["id"])
        self.assertEqual(updated["comment_id"], job["comment_id"])

    def test_from_input_reenters_same_failed_job_without_resetting_lifetime_attempts(
        self,
    ):
        job = self.phase(self.job())
        self.s.turns.clear()
        self.ws.worker_path.return_value = self.root / "missing-old-source"
        self.s.store.update(job["id"], state="failed", slot_id=None, attempts=99)
        self.s.command("retry_job", {"job_id": job["id"], "from_input": True})
        updated = self.s.store.job(job["id"])
        self.assertEqual(updated["attempts"], 99)
        self.assertEqual(updated["phase"], "design")
        self.assertEqual(updated["comment_id"], job["comment_id"])
        data = json.loads(updated["data"])
        self.assertNotIn("reentry_attempts_base", data)
        self.assertTrue(Path(data["reentry_record"]).is_file())
        self.assertNotIn("before", data)
        self.w.launch(self.slot, self.s.store.claim(2))
        self.assertEqual(self.s.store.job(job["id"])["attempts"], 100)

    def test_from_input_refuses_attempted_remote_effects_and_completed_jobs(self):
        job = self.job()
        self.s.store.update(job["id"], state="failed", slot_id=None)
        effect = self.s.store.prepare_effect(
            "effect", job["id"], "push", {"candidate": NEW}
        )
        self.s.store.effect_update(effect["key"], attempted=1)
        with self.assertRaisesRegex(ValueError, "remote effects exist"):
            self.s.command("retry_job", {"job_id": job["id"], "from_input": True})
        self.s.store.update(job["id"], state="done")
        with self.assertRaisesRegex(ValueError, "failed worker task"):
            self.s.command("retry_job", {"job_id": job["id"], "from_input": True})

    def test_retry_hint_and_rejection_reach_next_turn_without_cross_job_leak(self):
        job = self.phase(self.job())
        self.s.turns.clear()
        self.ws.worker_path.return_value = self.root / "missing-old-source"
        self.s.store.update(
            job["id"],
            state="failed",
            slot_id=None,
            error="missing grouped cause fields",
        )
        hint = "Reuse the completed checks; inspect the coordinator handoff first."
        self.s.command(
            "retry_job", {"job_id": job["id"], "from_input": True, "hint": hint}
        )
        restored = self.s.store.job(job["id"])
        self.assertEqual(restored["comment_id"], job["comment_id"])
        self.w.launch(self.slot, self.s.store.claim(2))
        prompt = self.s.backend.start.call_args.args[3]
        self.assertIn(hint, prompt)
        self.assertIn("missing grouped cause fields", prompt)
        data = json.loads(self.s.store.job(job["id"])["data"])
        self.assertEqual(data["operator_hint"], hint)

        self.s.store.enqueue(self.w.pr_candidate(2, explicit=True))
        other = self.s.store.claim(3)
        slot = next(s for s in self.s.store.slots() if s["id"] == 3)
        self.w.launch(slot, other)
        prompt = self.s.backend.start.call_args.args[3]
        self.assertNotIn(hint, prompt)
        self.assertNotIn("missing grouped cause fields", prompt)

    def test_invalid_hint_does_not_mutate_or_start_a_turn(self):
        job = self.job()
        before = dict(job)
        for hint in (42, "x" * 8001):
            with self.assertRaisesRegex(ValueError, "hint must be text"):
                self.s.command("retry_job", {"job_id": job["id"], "hint": hint})
        self.assertEqual(dict(self.s.store.job(job["id"])), before)
        self.s.backend.start.assert_not_called()

    def classifier_candidate(self, number=6):
        candidate = copy.deepcopy(self.w.issue_candidate(5))
        candidate["number"] = number
        candidate["trigger_key"] = "trigger-" + str(number)
        candidate["identity"]["issue_id"] = 100 + number
        candidate["evidence"]["issue"]["number"] = number
        candidate["evidence"]["issue"]["id"] = 100 + number
        return candidate

    def classifier_slot(self):
        self.s.command("resize_pool", {"role": "classifier", "sessions": 1})
        return next(s for s in self.s.store.slots() if s["role"] == "classifier")

    def test_semantic_grouping_uses_luna_xhigh(self):
        target = self.w.issue_candidate(5)
        self.s.store.enqueue(target)
        candidate = self.classifier_candidate()
        self.w.classification_queue.append(candidate)
        self.s.models = [
            {
                "model": "gpt-6-luna",
                "supportedReasoningEfforts": [{"reasoningEffort": "xhigh"}],
            }
        ]

        self.w.dispatch_classifier(self.classifier_slot())

        selected = self.s.backend.start.call_args.args[0]
        self.assertEqual(
            (selected["model"], selected["effort"]), ("gpt-6-luna", "xhigh")
        )

    def test_semantic_grouping_without_xhigh_keeps_issues_separate(self):
        target = self.w.issue_candidate(5)
        self.s.store.enqueue(target)
        candidate = self.classifier_candidate()
        self.w.classification_queue.append(candidate)
        self.s.models = [
            {
                "model": "gpt-6-luna",
                "supportedReasoningEfforts": [{"reasoningEffort": "medium"}],
            }
        ]

        self.w.dispatch_classifier(self.classifier_slot())

        self.s.backend.start.assert_not_called()
        admitted = self.s.store.issue_task_for_member(
            candidate["repo"], candidate["number"], candidate["trigger_key"]
        )
        self.assertIsNotNone(admitted)
        self.assertIn(
            "gpt-6-luna/xhigh unavailable",
            self.w.discovery_errors["semantic-classifier"],
        )

    def test_classifier_pool_default_and_model_change_are_pinned_to_xhigh(self):
        slot = self.classifier_slot()
        self.assertEqual((slot["model"], slot["effort"]), ("gpt-6-luna", "xhigh"))
        with self.assertRaisesRegex(ValueError, "gpt-6-luna/xhigh"):
            self.s.command(
                "model",
                {
                    "slot": slot["id"],
                    "model": "gpt-6-luna",
                    "reasoning_effort": "medium",
                },
            )

    def test_classifier_startup_repairs_persisted_medium_effort(self):
        slot = self.classifier_slot()
        self.s.store.slot_update(slot["id"], model="gpt-6-luna", effort="medium")
        self.s.close()

        self.s = Service(
            replace(self.s.config, classifier_sessions=1),
            github=self.gh,
            workspace=self.ws,
        )

        slot = next(s for s in self.s.store.slots() if s["role"] == "classifier")
        self.assertEqual((slot["model"], slot["effort"]), ("gpt-6-luna", "xhigh"))

    def test_unknown_ci_blocks_without_issue_or_rerun(self):
        job = self.phase(
            self.job(),
            classification="blocked",
            accepted=False,
            summary="CI cause unknown",
        )
        self.assertEqual(job["state"], "failed")
        self.assertFalse(self.gh.writes)
        self.ws.worker_finalize.assert_not_called()

    def test_environment_needs_concrete_evidence_and_cause_signature(self):
        job = self.phase(self.job())
        job = self.s.store.claim(2)
        with self.assertRaisesRegex(ValueError, "cause evidence"):
            self.phase(
                job,
                classification="flaky",
                cause_signature="",
                issue_title="flaky",
                issue_body="body",
            )
        self.assertFalse(self.gh.writes)

    def test_environment_grouped_cause_advances_without_legacy_fields(self):
        job = self.phase(self.job())
        job = self.phase(
            self.s.store.claim(2),
            classification="environment",
            causes=[
                {
                    "classification": "environment",
                    "cause_signature": "checkout TLS failure",
                    "issue_title": "CI checkout fails before tests",
                    "issue_body": "Failed checkout log with same-head passing control",
                    "evidence": ["checkout log and same-head build control"],
                    "evidence_keys": [],
                    "rerun_ids": [],
                }
            ],
        )
        self.assertEqual(job["phase"], "final_review")
        self.assertEqual(job["state"], "queued")
        self.assertFalse(self.gh.writes)

    def test_repeated_final_gaps_back_off_and_restore_without_round_limit(self):
        job = self.phase(self.job())
        job = self.phase(self.s.store.claim(2))
        job = self.phase(self.s.store.claim(2))
        self.ws.worker_state.return_value = {
            **self.ws.worker_state.return_value,
            "head": NEW,
        }
        job = self.phase(
            self.s.store.claim(2),
            classification="blocked",
            accepted=False,
            validation_complete=False,
            findings=["SCA unavailable"],
        )
        self.assertEqual(job["phase"], "implement")
        self.assertEqual(job["state"], "queued")
        self.assertEqual(json.loads(job["data"])["corrections"], 1)
        self.assertFalse(self.gh.writes)

        # Lifetime correction/turn counts do not terminate useful work.
        data = json.loads(job["data"])
        data["corrections"] = 50
        self.s.store.update(job["id"], data=json.dumps(data), attempts=100)
        self.phase(self.s.store.claim(2))
        job = self.phase(
            self.s.store.claim(2),
            classification="blocked",
            accepted=False,
            validation_complete=False,
            findings=["SCA unavailable"],
        )
        self.assertEqual(
            (job["state"], job["phase"], job["slot_id"]), ("queued", "implement", None)
        )
        self.assertGreater(job["retry_at"], time.time())
        self.assertIsNone(self.s.store.claim(2))
        self.ws.archive_worker.assert_called_once()
        self.s.refresh_status()
        self.assertTrue(
            next(j for j in self.s.status["jobs"] if j["id"] == job["id"])[
                "attention_required"
            ]
        )

        # The scheduled retry resumes the same source and task after backoff.
        self.s.store.update(job["id"], retry_at=0)
        self.phase(self.s.store.claim(2))
        self.ws.restore_worker.assert_called_once()
        self.ws.worker_state.return_value = {
            **self.ws.worker_state.return_value,
            "head": "d" * 40,
        }
        job = self.phase(
            self.s.store.claim(2),
            classification="blocked",
            accepted=False,
            validation_complete=False,
            findings=["SCA unavailable"],
        )
        self.assertEqual((job["state"], job["phase"]), ("queued", "implement"))
        self.assertEqual(json.loads(job["data"])["correction_stalls"], 0)
        self.assertIsNone(job["error"])
        self.assertFalse(self.gh.writes)

    def test_metadata_mutation_rejected_even_in_implementation(self):
        job = self.job()
        self.w.launch(self.slot, job)
        job = self.s.store.job(job["id"])
        state = dict(self.ws.worker_state.return_value)
        self.ws.worker_state.return_value = {**state, "config": "changed"}
        with self.assertRaises(SourceChanged):
            self.w.consume(job, self.slot, result(job, state), "gpt-6.1-sol", "low")

    def test_archived_retry_waits_for_capacity_then_restores_on_launch(self):
        job = self.job()
        archive = str(self.root / "saved-source")
        self.s.store.update(
            job["id"],
            state="failed",
            slot_id=None,
            data=json.dumps({"workspace_archive": archive}),
        )
        for slot in self.s.store.slots():
            if slot["role"] == "worker":
                self.s.store.slot_update(slot["id"], ready=0)
        self.s.command("retry_job", {"job_id": job["id"]})
        waiting = self.s.store.job(job["id"])
        self.assertEqual((waiting["state"], waiting["slot_id"]), ("queued", None))
        self.assertEqual(json.loads(waiting["data"])["workspace_archive"], archive)
        self.ws.restore_worker.assert_not_called()
        self.s.store.slot_update(2, ready=1)
        self.phase(self.s.store.claim(2))
        self.ws.restore_worker.assert_called_once_with(2, unittest.mock.ANY, archive)
        self.assertNotIn(
            "workspace_archive", json.loads(self.s.store.job(job["id"])["data"])
        )

    def test_environment_final_gaps_return_to_diagnosis(self):
        cause = {
            "classification": "environment",
            "cause_signature": "TLS checkout",
            "issue_title": "Checkout infrastructure",
            "issue_body": "failed checkout and passing control",
            "evidence": ["logs/control"],
            "evidence_keys": [],
            "rerun_ids": [],
        }
        self.phase(self.job())
        self.phase(self.s.store.claim(2), classification="environment", causes=[cause])
        job = self.phase(
            self.s.store.claim(2),
            classification="blocked",
            accepted=False,
            validation_complete=False,
            findings=["control evidence mismatch"],
        )
        self.assertEqual((job["state"], job["phase"]), ("queued", "design"))
        self.assertFalse(self.gh.writes)

    def test_noop_still_runs_design_and_final_gate(self):
        job = self.phase(self.job(), classification="noop")
        job = self.phase(self.s.store.claim(2), classification="noop")
        self.assertEqual(job["phase"], "final_review")
        job = self.phase(self.s.store.claim(2), classification="noop")
        self.w.publish(job)
        self.assertEqual(self.s.store.job(job["id"])["state"], "done")
        self.assertFalse(self.gh.writes)

    def test_dirty_interrupted_implementation_retains_slot_and_source(self):
        job = self.job()
        self.s.store.update(
            job["id"], phase="implement", turn_dir=str(self.root / "missing")
        )
        self.w.recover(self.s.store.job(job["id"]))
        recovered = self.s.store.job(job["id"])
        self.assertEqual((recovered["state"], recovered["slot_id"]), ("queued", 2))
        self.assertIsNone(self.s.store.claim(3))
        self.ws.worker_prepare.assert_not_called()

    def test_completed_recovery_checks_native_thread_turn_and_advances_once(self):
        job = self.job()
        self.w.launch(self.slot, job)
        job = self.s.store.job(job["id"])
        path = Path(job["turn_dir"])
        (path / "result.json").write_text(
            json.dumps(result(job, self.ws.worker_state.return_value))
        )
        (path / "turn.json").write_text(
            json.dumps(
                {
                    "thread": "thread-2",
                    "turn": "turn-1",
                    "model": "gpt-6.1-sol",
                    "effort": "low",
                }
            )
        )
        (path / "events.jsonl").write_text(
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-2",
                        "turn": {"id": "turn-1", "status": "completed"},
                    },
                }
            )
            + "\n"
        )
        self.w.recover(job)
        recovered = self.s.store.job(job["id"])
        self.assertEqual(
            (recovered["state"], recovered["phase"], recovered["slot_id"]),
            ("queued", "design", 2),
        )

    def test_authorship_draft_default_and_head_repository_scope(self):
        for change in ({"user": {"id": 9}}, {"draft": True}, {"state": "closed"}):
            self.gh.prs[1] = {**pr(), **change}
            with self.assertRaises(ValueError):
                self.w.pr_candidate(1, explicit=True)
        self.gh.prs[1] = pr()
        self.gh.prs[1]["head"]["ref"] = "main"
        with self.assertRaisesRegex(ValueError, "default branch"):
            self.w.pr_candidate(1, explicit=True)
        self.gh.prs[1] = pr()
        self.gh.prs[1]["head"]["repo"]["owner"] = {"id": 9}
        with self.assertRaisesRegex(ValueError, "owned fork"):
            self.w.pr_candidate(1, explicit=True)

    def test_conflict_and_ci_failures_admitted_without_green_review_predicate(self):
        self.gh.prs[1]["mergeable"] = False
        self.gh.checks = [
            {"id": 1, "name": "UT", "status": "completed", "conclusion": "failure"}
        ]
        candidate = self.w.pr_candidate(1)
        self.assertEqual(
            {a["kind"] for a in candidate["atoms"]}, {"conflict", "check_failure"}
        )

    def test_failed_and_superseded_history_does_not_hide_unresolved_conflict(self):
        self.gh.prs[1]["mergeable"] = False
        self.gh.checks = [
            {"id": 1, "name": "UT", "status": "completed", "conclusion": "failure"}
        ]
        raw = self.w.gather_candidate(1)
        self.s.store.fresh_evidence(raw["repo"], 1, HEAD, raw["atoms"])
        conflict = next(a for a in raw["atoms"] if a["kind"] == "conflict")
        check = next(a for a in raw["atoms"] if a["kind"] == "check_failure")
        for index, (state, atoms, disposition) in enumerate(
            [
                ("failed", [conflict], "blocked"),
                ("failed", [check], "failed"),
                ("superseded", [], "superseded"),
                ("done", [check], "done"),
            ]
        ):
            candidate = {
                **raw,
                "atoms": atoms,
                "input_evidence_keys": [a["key"] for a in atoms],
                "trigger_key": str(index),
            }
            self.s.store.enqueue(candidate)
            job = self.s.store.job(self.s.store.work_key(candidate))
            self.s.store.update(job["id"], state=state, attempts=1 if atoms else 0)
            self.s.store.dispose_evidence(job, disposition)
        candidate = self.w.pr_candidate(1)
        self.assertEqual([a["kind"] for a in candidate["atoms"]], ["conflict"])
        self.assertTrue(self.s.store.enqueue(candidate))
        self.assertIsNone(self.w.pr_candidate(1))

    def failed_conflict_attempts(self):
        self.gh.prs[1]["mergeable"] = False
        ids = []
        for _ in range(self.s.config.worker_tasks_per_head):
            candidate = self.w.pr_candidate(1)
            self.assertTrue(self.s.store.enqueue(candidate))
            job = self.s.store.job(self.s.store.work_key(candidate))
            self.s.store.update(job["id"], state="failed", attempts=1)
            self.s.store.dispose_evidence(job, "blocked")
            ids.append(job["id"])
        self.assertEqual(len(ids), len(set(ids)))

    def test_identical_unresolved_input_retries_are_bounded_and_visible(self):
        self.failed_conflict_attempts()
        self.w.scan([{"number": 1}])
        self.assertIn("retry budget exhausted", self.w.discovery_errors["1"])
        self.assertEqual(len(self.s.store.jobs()), self.s.config.worker_tasks_per_head)
        self.s.refresh_status()
        self.assertIn("1", self.s.status["worker_discovery_errors"])
        self.assertTrue(
            any(
                a["kind"] == "worker_discovery_blocked" and a["pr"] == 1
                for a in self.s.status["alerts"]
            )
        )

    def test_explicit_admission_can_reenter_after_automatic_budget_exhaustion(self):
        self.failed_conflict_attempts()
        candidate = self.w.pr_candidate(1, explicit=True)
        self.assertIsNotNone(candidate)
        self.assertTrue(self.s.store.enqueue(candidate))
        # A repeated explicit request reuses the same queued identity.
        repeated = self.w.pr_candidate(1, explicit=True)
        self.assertEqual(
            self.s.store.work_key(candidate), self.s.store.work_key(repeated)
        )
        self.assertFalse(self.s.store.enqueue(repeated))

    def test_new_input_can_reconsider_previously_blocked_conflict(self):
        self.failed_conflict_attempts()
        self.w.scan([{"number": 1}])
        self.gh.comments_data = [
            {
                "id": 99,
                "user": {"id": 9},
                "body": "A passing control now establishes the CI failure is independent",
            }
        ]
        candidate = self.w.pr_candidate(1)
        self.assertEqual(
            {a["kind"] for a in candidate["atoms"]}, {"conflict", "comment"}
        )
        self.assertTrue(self.s.store.enqueue(candidate))
        self.w.scan([{"number": 1}])
        self.assertNotIn("1", self.w.discovery_errors)
        self.assertIsNone(self.w.pr_candidate(1))

    def test_unresolved_conflict_cannot_be_approved_as_noop(self):
        self.gh.prs[1]["mergeable"] = False
        job = self.phase(self.job())
        job = self.s.store.claim(2)
        with self.assertRaisesRegex(
            ValueError, "merge conflict requires a rebase plan"
        ):
            self.phase(job, classification="noop")
        # A conflict that GitHub confirms disappeared needs no forced rebase.
        job = self.s.store.job(job["id"])
        self.gh.prs[1]["mergeable"] = True
        self.w.consume(
            job,
            self.slot,
            result(job, self.ws.worker_state.return_value, classification="noop"),
            "gpt-6.1-sol",
            "xhigh",
        )
        self.assertEqual(self.s.store.job(job["id"])["phase"], "final_review")

    def test_unchanged_final_candidate_cannot_hide_legacy_conflict(self):
        self.gh.prs[1]["mergeable"] = False
        job = self.job()
        self.s.store.update(
            job["id"],
            phase="final_review",
            data=json.dumps(
                {"candidate": HEAD, "before": self.ws.worker_state.return_value}
            ),
        )
        job = self.s.store.job(job["id"])
        with self.assertRaisesRegex(
            ValueError, "merge conflict requires a rebase plan"
        ):
            self.w.consume(
                job,
                self.slot,
                result(job, self.ws.worker_state.return_value),
                "gpt-6.1-sol",
                "xhigh",
            )

    def test_failed_check_queues_repair_while_workflow_running_without_repeating(self):
        self.gh.checks = [
            {"id": 11, "name": "UT", "status": "completed", "conclusion": "failure"},
            {"id": 12, "name": "BVT", "status": "in_progress", "conclusion": None},
        ]
        self.gh.runs = [
            {"id": 101, "head_sha": HEAD, "status": "in_progress", "conclusion": None}
        ]
        review_candidate = {
            "repo": self.s.config.repository,
            "number": 1,
            "headRefOid": HEAD,
        }
        self.s.store.enqueue(review_candidate)
        active_review = self.s.store.claim(0)
        self.w.scan([{"number": 1}])
        queued = [j for j in self.s.store.jobs() if j["kind"] == "work"]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["state"], "queued")
        meta = json.loads(queued[0]["metadata"])
        self.assertEqual([a["kind"] for a in meta["atoms"]], ["check_failure"])
        self.assertEqual(meta["evidence"]["runs"][0]["status"], "in_progress")
        self.assertIsNone(self.s.store.claim(2, self.s.eligible))
        self.assertEqual(self.s.store.job(active_review["id"])["state"], "reviewing")
        self.w.scan([{"number": 1}])
        self.assertEqual(
            len([j for j in self.s.store.jobs() if j["kind"] == "work"]), 1
        )
        self.s.store.update(active_review["id"], state="done", slot_id=None)
        self.assertEqual(self.s.store.claim(2, self.s.eligible)["id"], queued[0]["id"])

    def test_failed_status_admitted_while_checks_running_but_pending_only_is_not(self):
        self.gh.checks = [
            {"id": 1, "name": "UT", "status": "in_progress", "conclusion": None}
        ]
        self.gh.runs = [
            {"id": 101, "head_sha": HEAD, "status": "in_progress", "conclusion": None}
        ]
        self.assertIsNone(self.w.pr_candidate(1))
        self.gh.statuses = [
            {
                "id": 2,
                "context": "CI",
                "state": "failure",
                "target_url": "https://example.test/ci",
            }
        ]
        candidate = self.w.pr_candidate(1)
        self.assertEqual([a["kind"] for a in candidate["atoms"]], ["status_failure"])

    def test_progress_and_done_markers_do_not_retrigger_own_pr(self):
        self.gh.comments_data = [
            {
                "id": 1,
                "user": ACTOR,
                "body": 'fixed\n<!-- mo-bot-review:v1 {"service":"test","job":"j","head":"h","state":"done"} -->',
            }
        ]
        self.assertIsNone(self.w.pr_candidate(1))
        self.gh.comments_data.append(
            {"id": 2, "user": ACTOR, "body": "Please fix this defect"}
        )
        candidate = self.w.pr_candidate(1)
        self.assertIsNotNone(candidate)
        self.s.store.enqueue(candidate)
        self.assertIsNone(self.w.pr_candidate(1))
        job = self.s.store.job(self.s.store.work_key(candidate))
        self.s.store.dispose_evidence(job, "done")
        self.assertIsNone(self.w.pr_candidate(1))
        self.gh.comments_data[-1]["body"] = "Different defect with details"
        self.assertIsNotNone(self.w.pr_candidate(1))

    def test_unresolved_threads_are_paginated_and_truncation_fails_closed(self):
        self.gh.threads_data = [
            {
                "id": "thread",
                "isResolved": False,
                "isOutdated": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 1,
                            "author": {"login": "other"},
                            "body": "causal defect",
                        }
                    ],
                    "pageInfo": {"hasNextPage": False},
                },
            }
        ]
        self.assertIsNotNone(self.w.pr_candidate(1))
        self.gh.threads_data[0]["comments"]["pageInfo"]["hasNextPage"] = True
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            self.w.pr_candidate(1)

    def test_issue_explicit_only_version_ignores_worker_output_and_base_churn(self):
        candidate = self.w.issue_candidate(5)
        self.s.store.enqueue(candidate)
        key = self.s.store.work_key(candidate)
        self.gh.comments_data.append(
            {
                "id": 1,
                "user": ACTOR,
                "body": 'working <!-- mo-bot-review:v1 {"service":"test","job":"j","head":"h","state":"reviewing"} -->',
            }
        )
        changed = self.w.issue_candidate(5)
        changed["headRefOid"] = NEW
        self.assertEqual(self.s.store.work_key(changed), key)
        self.assertFalse(self.s.store.enqueue(changed))
        self.w.scan([])
        self.assertEqual(len(self.s.store.jobs()), 1)
        self.gh.issue["body"] = "new actual requirement"
        self.assertNotEqual(self.s.store.work_key(self.w.issue_candidate(5)), key)

    def test_stale_input_is_never_dispatched(self):
        job = self.job()
        self.gh.prs[1]["head"]["sha"] = NEW
        with self.assertRaises(SourceChanged):
            self.w.launch(self.slot, job)
        self.s.backend.start.assert_not_called()

    def test_write_timeout_reconciles_once_without_repeating_post(self):
        job = self.job()
        diagnosis = {
            "classification": "flaky",
            "cause_signature": "race in existing UT",
            "issue_title": "fix flaky race",
            "issue_body": "baseline and failed control logs",
        }
        self.gh.ambiguous = True
        with self.assertRaises(RuntimeError):
            self.w.cause_issue(job, diagnosis)
        self.gh.ambiguous = False
        issue = self.w.cause_issue(job, diagnosis)
        self.assertEqual(issue["number"], 1000)
        self.assertEqual(len(self.gh.writes), 1)
        self.s.store.update(job["id"], state="done", slot_id=None)
        other = self.job(2)
        self.assertEqual(self.w.cause_issue(other, diagnosis), issue)
        self.assertEqual(len(self.gh.writes), 1)

    def test_successful_issue_receipt_bypasses_search_index_lag(self):
        job = self.job()
        diagnosis = {
            "classification": "flaky",
            "cause_signature": "existing fixture",
            "issue_title": "fixture",
            "issue_body": "control evidence",
        }
        original = self.gh.api
        calls = []

        def api(endpoint, **kwargs):
            calls.append(endpoint)
            if endpoint.startswith("search/issues?"):
                return {"items": [], "total_count": 0, "incomplete_results": False}
            return original(endpoint, **kwargs)

        self.gh.api = api
        found = self.w.cause_issue(job, diagnosis)
        self.assertEqual(found["number"], 1000)
        self.assertEqual(sum(e.startswith("search/issues?") for e in calls), 1)
        self.assertIn("repos/matrixorigin/repo/issues/1000", calls)
        self.assertFalse(any("issues?state=all" in e for e in calls))

    def test_incomplete_cause_search_cannot_authorize_an_issue_write(self):
        job = self.job()
        original = self.gh.api
        self.gh.api = lambda endpoint, **kwargs: (
            {"items": [], "total_count": 0, "incomplete_results": True}
            if endpoint.startswith("search/issues?")
            else original(endpoint, **kwargs)
        )
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.w.cause_issue(
                job,
                {
                    "classification": "flaky",
                    "cause_signature": "fixture",
                    "issue_title": "f",
                    "issue_body": "f",
                },
            )
        self.assertFalse(self.gh.writes)

    def test_publication_timeout_releases_model_slot_but_retains_pr_fence(self):
        job = self.phase(
            self.job(),
            classification="flaky",
            cause_signature="fixture",
            issue_title="fixture",
            issue_body="control evidence",
        )
        job = self.phase(
            self.s.store.claim(2),
            classification="flaky",
            cause_signature="fixture",
            issue_title="fixture",
            issue_body="control evidence",
        )
        job = self.phase(
            self.s.store.claim(2),
            classification="flaky",
            cause_signature="fixture",
            issue_title="fixture",
            issue_body="control evidence",
        )
        original = self.gh.api

        def api(endpoint, **kwargs):
            if endpoint.startswith("search/issues?"):
                raise RuntimeError("GitHub GET timed out")
            return original(endpoint, **kwargs)

        self.gh.api = api
        self.w.publish(job)
        held = self.s.store.job(job["id"])
        self.assertEqual((held["state"], held["slot_id"]), ("publishing", None))
        # Transient reads keep backing off even after the old attempt limit.
        for _ in range(self.s.config.max_recoveries + 1):
            data = json.loads(held["data"])
            data["publication_next_at"] = 0
            self.s.store.update(job["id"], data=json.dumps(data))
            self.w.publish(self.s.store.job(job["id"]))
            held = self.s.store.job(job["id"])
        self.assertFalse(json.loads(held["data"])["manual_reconciliation"])
        self.s.store.enqueue(
            {"repo": "matrixorigin/repo", "number": 1, "headRefOid": HEAD}
        )
        self.assertIsNone(self.s.store.claim(0))
        self.s.store.enqueue(self.w.pr_candidate(2, explicit=True))
        self.assertEqual(self.s.store.claim(2)["target_number"], 2)
        self.s.refresh_status()
        alert = next(a for a in self.s.status["alerts"] if a["job_id"] == job["id"])
        self.assertIn("timed out", alert["reason"])
        self.assertIsNotNone(alert["next_retry_at"])

    def test_pause_preserves_source_and_resume_uses_free_slot_same_task(self):
        job = self.job()
        self.w.launch(self.slot, job)
        turn = self.s.turns[2][0]
        turn.completed = False
        self.s.command("pause_job", {"job_id": job["id"]})
        self.assertEqual(self.s.store.job(job["id"])["state"], "canceling")
        self.assertIsNotNone(self.s.store.job(job["id"])["slot_id"])
        turn.completed = True
        self.s.finish(2, turn, job["id"])
        held = self.s.store.job(job["id"])
        self.assertEqual((held["state"], held["slot_id"]), ("queued", None))
        self.assertTrue(json.loads(held["data"])["paused"])
        self.assertIsNone(self.s.store.claim(2))
        self.s.store.enqueue(self.w.pr_candidate(2, explicit=True))
        self.assertEqual(self.s.store.claim(2)["target_number"], 2)
        self.s.command("resume_job", {"job_id": job["id"]})
        resumed = self.s.store.job(job["id"])
        self.assertEqual(resumed["phase"], held["phase"])
        self.assertEqual(resumed["comment_id"], held["comment_id"])
        self.assertEqual(resumed["slot_id"], 3)
        self.ws.restore_worker.assert_called_once()
        self.assertEqual(self.s.store.claim(3)["id"], job["id"])

    def test_cleanup_refuses_uncertain_remote_effect_and_live_turn(self):
        job = self.job()
        self.s.store.prepare_effect(
            "ambiguous", job["id"], "create-pr", {"frozen": True}
        )
        self.s.store.effect_update("ambiguous", attempted=1)
        with self.assertRaisesRegex(ValueError, "uncertain remote"):
            self.s.command("cleanup_job", {"job_id": job["id"]})
        self.assertEqual(self.s.store.job(job["id"])["slot_id"], 2)

    def test_rerun_control_cannot_bypass_frozen_xhigh_gate(self):
        job = self.job()
        with self.assertRaisesRegex(ValueError, "xhigh"):
            self.s.command("rerun_job", {"job_id": job["id"], "run_id": 123})
        self.assertFalse(self.gh.writes)

    def test_ambiguous_absent_write_never_reposts_and_retains_fence(self):
        job = self.job()
        intent = {"immutable": "intent"}
        self.s.store.prepare_effect("write", job["id"], "create-pr", intent)
        self.s.store.effect_update("write", attempted=1)
        execute = Mock()
        with self.assertRaisesRegex(RuntimeError, "manual reconciliation"):
            self.w.effect(job, "write", "create-pr", intent, lambda: None, execute)
        execute.assert_not_called()

    def test_issue_pr_timeout_adopted_exact_marker_and_associated_pr_fenced(self):
        job = self.job(issue=True)
        data = {
            "candidate": NEW,
            "final_review": {"result": result(job, self.ws.worker_state.return_value)},
        }
        self.gh.ambiguous = True
        with self.assertRaises(RuntimeError):
            self.w.create_pr(job, data)
        self.gh.ambiguous = False
        created = self.w.create_pr(job, data)
        self.assertEqual(created["number"], 20)
        self.assertEqual(self.s.store.job(job["id"])["pr"], 20)
        self.assertEqual(len(self.gh.writes), 1)
        candidate = {"repo": "matrixorigin/repo", "number": 20, "headRefOid": NEW}
        self.s.store.enqueue(candidate)
        self.assertIsNone(self.s.store.claim(0))

    def test_rerun_only_current_latest_failed_associated_run_once(self):
        self.gh.runs = [
            {
                "id": 123,
                "head_sha": HEAD,
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "failure",
                "pull_requests": [{"number": 1}],
                "workflow_id": 2,
                "run_number": 1,
            }
        ]
        job = self.job()
        first = self.w.rerun(job, 123)
        self.assertEqual(first["attempt"], 2)
        self.assertEqual(self.w.rerun(job, 123), first)
        self.assertEqual(len(self.gh.writes), 1)
        self.gh.prs[1]["head"]["sha"] = NEW
        with self.assertRaises(SourceChanged):
            self.w.rerun(job, 123)
        self.assertEqual(len(self.gh.writes), 1)

    def test_wrong_pr_and_superseded_run_never_rerun(self):
        for associations, latest in (([{"number": 2}], False), ([{"number": 1}], True)):
            self.gh.runs = [
                {
                    "id": 123,
                    "head_sha": HEAD,
                    "run_attempt": 1,
                    "status": "completed",
                    "conclusion": "failure",
                    "pull_requests": associations,
                    "workflow_id": 2,
                    "run_number": 1,
                }
            ]
            if latest:
                self.gh.runs.append({**self.gh.runs[0], "id": 124, "run_number": 2})
            job = self.job()
            with self.assertRaises((SourceChanged, ValueError)):
                self.w.rerun(job, 123)
            self.assertFalse(self.gh.writes)
            self.s.store.update(job["id"], state="done", slot_id=None)

    def test_comment_review_does_not_clear_prior_decisive_request(self):
        self.gh.reviews = [
            {
                "id": 1,
                "user": {"id": 9},
                "state": "CHANGES_REQUESTED",
                "body": "causal defect",
            },
            {
                "id": 2,
                "user": {"id": 9},
                "state": "COMMENTED",
                "body": "status question",
            },
        ]
        candidate = self.w.pr_candidate(1)
        self.assertEqual(
            [
                a["identity"]
                for a in candidate["atoms"]
                if a["kind"] == "request_changes"
            ],
            [1],
        )
        self.gh.reviews.append(
            {"id": 3, "user": {"id": 9}, "state": "APPROVED", "body": "fixed"}
        )
        self.assertIsNone(self.w.pr_candidate(1))

    def test_resolved_outdated_thread_requires_source_triage_not_assumed_closure(self):
        self.gh.threads_data = [
            {
                "id": "thread",
                "isResolved": True,
                "isOutdated": True,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 1,
                            "author": {"login": "other"},
                            "body": "real bug still present",
                        }
                    ],
                    "pageInfo": {"hasNextPage": False},
                },
            }
        ]
        candidate = self.w.pr_candidate(1)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["atoms"][0]["kind"], "review_thread")

    def test_own_push_carries_semantic_dispositions_but_new_feedback_is_fresh(self):
        self.gh.comments_data = [{"id": 5, "user": {"id": 9}, "body": "causal defect"}]
        candidate = self.w.pr_candidate(1)
        self.s.store.enqueue(candidate)
        job = self.s.store.claim(2)
        self.s.store.prepare_effect(
            "push:" + job["id"], job["id"], "push", {"candidate": NEW}
        )
        self.s.store.effect_update("push:" + job["id"], attempted=1)
        # Remote push succeeded but response/result checkpoint has not materialized.
        self.gh.prs[1]["head"]["sha"] = NEW
        self.assertIsNone(self.w.pr_candidate(1))
        self.gh.comments_data[0]["body"] = "new concrete defect"
        self.assertIsNotNone(self.w.pr_candidate(1))
        # A human-produced different head can legitimately invalidate earlier facts.
        self.gh.comments_data[0]["body"] = "causal defect"
        self.gh.prs[1]["head"]["sha"] = "d" * 40
        self.assertIsNotNone(self.w.pr_candidate(1))

    def transport_failure(self, job):
        self.w.launch(self.slot, job)
        job = self.s.store.job(job["id"])
        turn = Mock(completed=True, error=None)
        turn.result.side_effect = RuntimeError("provider connection interrupted")
        self.s.finish(2, turn, job["id"])
        recovered = self.s.store.job(job["id"])
        self.assertEqual(
            (recovered["state"], recovered["slot_id"], recovered["phase"]),
            ("queued", 2, job["phase"]),
        )
        self.assertEqual(json.loads(recovered["data"])["recoveries"], 1)
        with patch("mo_bot.store.time.time", return_value=time.time() + 61):
            return self.s.store.claim(2)

    def test_service_finish_first_transport_failure_in_implementation_resumes_same_slot(
        self,
    ):
        self.phase(self.job())
        self.phase(self.s.store.claim(2))
        resumed = self.transport_failure(self.s.store.claim(2))
        self.assertEqual(resumed["attempts"], 3)
        complete = self.phase(resumed)
        self.assertEqual(complete["phase"], "final_review")
        self.assertEqual(json.loads(complete["data"])["recoveries"], 0)

    def test_service_finish_first_transport_failure_in_final_gate_resumes_same_slot(
        self,
    ):
        self.phase(self.job())
        self.phase(self.s.store.claim(2))
        self.phase(self.s.store.claim(2))
        self.ws.worker_state.return_value = {
            **self.ws.worker_state.return_value,
            "head": NEW,
        }
        resumed = self.transport_failure(self.s.store.claim(2))
        self.assertEqual(resumed["attempts"], 4)
        complete = self.phase(resumed)
        self.assertEqual(complete["state"], "publishing")

    def test_service_tick_dispatch_failure_uses_phase_recovery_and_does_not_throw(self):
        job = self.job()
        self.s.store.update(job["id"], state="queued", phase="implement", attempts=3)
        self.s.eligible.add(job["id"])
        self.s.next_scan = float("inf")
        with patch.object(
            self.w, "launch", side_effect=RuntimeError("read provider unavailable")
        ):
            self.s.tick()
        recovered = self.s.store.job(job["id"])
        self.assertEqual(
            (recovered["state"], recovered["slot_id"], recovered["attempts"]),
            ("queued", 2, 3),
        )
        self.assertEqual(json.loads(recovered["data"])["recoveries"], 1)

    def test_new_same_head_feedback_before_delivery_invalidates_frozen_gate(self):
        job = self.job()
        self.gh.comments_data.append(
            {"id": 7, "user": {"id": 9}, "body": "another material causal bug"}
        )
        with self.assertRaisesRegex(SourceChanged, "same-head"):
            self.w.check_input(job, refresh_evidence=True)
        self.assertFalse(self.gh.writes)

    def publication_with_new_feedback(self, head=HEAD):
        self.phase(self.job())
        self.phase(self.s.store.claim(2))
        self.phase(self.s.store.claim(2))
        self.ws.worker_state.return_value = {
            **self.ws.worker_state.return_value,
            "head": NEW,
        }
        job = self.phase(self.s.store.claim(2))
        self.s.turns.clear()
        self.gh.prs[1]["head"]["sha"] = head
        self.gh.comments_data.append(
            {"id": 7, "user": {"id": 9}, "body": "another material causal bug"}
        )
        candidate = self.w.pr_candidate(1, explicit=True)
        self.s.store.enqueue(candidate)
        return job, self.s.store.work_key(candidate)

    def test_unpublished_stale_assessment_releases_pr_fence(self):
        job, next_id = self.publication_with_new_feedback()
        self.assertIsNone(self.s.store.claim(3))
        self.w.publish(job)
        old = self.s.store.job(job["id"])
        self.assertEqual((old["state"], old["slot_id"]), ("superseded", None))
        data = json.loads(old["data"])
        self.assertNotIn("manual_reconciliation", data)
        self.assertEqual(data["candidate"], NEW)
        self.assertIn("publication_ref", data)
        self.assertIsNotNone(old["result"])
        self.assertFalse(self.gh.writes)
        self.assertEqual(self.s.store.claim(3)["id"], next_id)

    def test_stale_assessment_with_attempted_write_keeps_pr_fence(self):
        job, _ = self.publication_with_new_feedback()
        self.s.store.prepare_effect(
            "push:" + job["id"], job["id"], "push", {"candidate": NEW}
        )
        self.s.store.effect_update("push:" + job["id"], attempted=1)
        self.w.publish(job)
        old = self.s.store.job(job["id"])
        self.assertEqual(old["state"], "publishing")
        self.assertTrue(json.loads(old["data"])["manual_reconciliation"])
        self.assertIsNone(self.s.store.claim(3))
        self.assertFalse(self.gh.writes)

    def test_unpublished_assessment_on_old_head_releases_pr_fence(self):
        job, next_id = self.publication_with_new_feedback(head="d" * 40)
        self.w.publish(job)
        old = self.s.store.job(job["id"])
        self.assertEqual(old["state"], "superseded")
        self.assertNotIn("manual_reconciliation", json.loads(old["data"]))
        self.assertEqual(self.s.store.claim(3)["id"], next_id)
        self.assertFalse(self.gh.writes)

    def test_issue_base_default_change_rejected_but_sha_drift_is_recorded(self):
        job = self.job(issue=True)
        original = self.gh.api

        def api(endpoint, **kwargs):
            data = original(endpoint, **kwargs)
            return {"commit": {"sha": NEW}} if "/branches/" in endpoint else data

        self.gh.api = api
        self.w.check_input(job)
        data = json.loads(self.s.store.job(job["id"])["data"])
        self.assertEqual(data["base_drift"]["frozen"], BASE)

        def changed(endpoint, **kwargs):
            data = api(endpoint, **kwargs)
            return (
                {**data, "default_branch": "trunk"}
                if endpoint == "repos/matrixorigin/repo"
                else data
            )

        self.gh.api = changed
        with self.assertRaisesRegex(SourceChanged, "base/ref"):
            self.w.check_input(job)

    def test_uncertain_issue_pr_with_removed_marker_fences_possible_created_pr(self):
        job = self.job(issue=True)
        self.s.store.update(job["id"], state="publishing")
        data = {
            "candidate": NEW,
            "final_review": {"result": result(job, self.ws.worker_state.return_value)},
        }
        self.gh.ambiguous = True
        with self.assertRaises(RuntimeError):
            self.w.create_pr(job, data)
        remote = self.gh.created_prs[0]
        remote["body"] = "marker removed"
        self.s.store.enqueue(
            {"repo": "matrixorigin/repo", "number": 20, "headRefOid": NEW}
        )
        self.assertIsNone(self.s.store.claim(0))
        # A later human head still matches the pinned possibly-created head ref.
        remote["head"]["sha"] = "d" * 40
        self.gh.prs[20] = remote
        self.s.store.enqueue(
            {"repo": "matrixorigin/repo", "number": 20, "headRefOid": "d" * 40}
        )
        possible = self.s.store.claim(0)
        self.assertIsNotNone(possible)
        self.s.launch(self.s.store.slots()[0], possible)
        self.assertEqual(self.s.store.job(possible["id"])["state"], "queued")
        self.s.backend.start.assert_not_called()

    def test_manual_publication_error_stops_background_reads_and_explicit_reconcile_is_fenced(
        self,
    ):
        job = self.job()
        data = self.push_data(job)
        data["design"] = {"result": result(job, self.ws.worker_state.return_value)}
        self.s.store.update(
            job["id"],
            state="publishing",
            data=json.dumps(data),
            result=json.dumps(data["final_review"]["result"]),
        )
        remote = self.remote(job)
        remote["head"] = "d" * 40
        self.w.publish(self.s.store.job(job["id"]))
        held = self.s.store.job(job["id"])
        self.assertTrue(json.loads(held["data"])["manual_reconciliation"])
        self.ws.worker_git.reset_mock()
        self.w.publish(held)
        self.ws.worker_git.assert_not_called()
        self.w.reconcile(held)
        self.assertFalse(remote["pushes"])
        self.assertEqual(self.s.store.job(job["id"])["state"], "publishing")

    def test_recovered_affine_worker_does_not_depend_on_review_discovery(self):
        job = self.job()
        self.s.store.update(job["id"], state="queued", phase="implement")
        self.s.eligible.clear()
        self.s.scan_error = "review discovery timed out"
        self.w.poll()
        self.assertIn(job["id"], self.s.eligible)
        self.assertIsNotNone(self.s.store.claim(2, self.s.eligible))

    def test_finished_worker_ids_do_not_accumulate_in_eligibility_cache(self):
        job = self.job()
        self.s.eligible.add(job["id"])
        self.s.store.update(job["id"], state="done", slot_id=None)
        self.w.poll()
        self.assertNotIn(job["id"], self.s.eligible)

    def test_disabled_pool_drains_retained_affine_work_without_new_discovery(self):
        job = self.job()
        self.s.store.update(job["id"], state="queued", phase="implement")
        self.s.eligible.clear()
        self.s.store.resize_pool("worker", 0)
        self.s.refresh_status()
        self.assertEqual(self.s.status["pools"]["worker"]["sessions"], 0)
        self.assertEqual(self.s.status["pools"]["worker"]["draining"], 1)
        self.assertFalse(
            next(s for s in self.s.status["sessions"] if s["id"] == 2)["enabled"]
        )
        self.w.scan([])
        self.assertIn(job["id"], self.s.eligible)
        self.assertIsNotNone(self.s.store.claim(2, self.s.eligible))

    def test_dynamic_worker_enable_uses_sqlite_capacity_and_saved_remote_binding(self):
        self.s.store.resize_pool("worker", 0)
        self.w.pr_candidate = Mock(return_value=None)
        self.w.scan([{"number": 1}])
        self.w.pr_candidate.assert_not_called()
        self.s.command("resize_pool", {"role": "worker", "sessions": 2})
        self.w.scan([{"number": 1}])
        self.w.pr_candidate.assert_called_once()
        self.assertEqual(
            [s["id"] for s in self.s.store.slots() if s["role"] == "worker"], [2, 3]
        )
        self.s.config = replace(
            self.s.config,
            nodes=({"name": "remote", "ssh": "host", "codex": "/opt/codex"},),
            session_overrides=({"role": "worker", "slot": 2, "node": "remote"},),
        )
        result = self.s.command("resize_pool", {"role": "worker", "sessions": 3})
        self.assertEqual(result["slots"][-1]["node"], "remote")
        self.assertEqual(result["slots"][-1]["model"], self.s.config.worker_model)
        sid = result["slots"][-1]["id"]
        self.s.store.slot_update(sid, session_id="remote-thread")
        self.s.command("resize_pool", {"role": "worker", "sessions": 2})
        self.s.command("resize_pool", {"role": "worker", "sessions": 3})
        self.assertEqual(
            next(s for s in self.s.store.slots() if s["id"] == sid)["session_id"],
            "remote-thread",
        )

    def test_own_inline_feedback_and_external_copied_marker_are_evidence(self):
        self.gh.threads_data = [
            {
                "id": "thread",
                "isResolved": False,
                "isOutdated": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 1,
                            "author": {"login": "me"},
                            "body": "own defect found here",
                        }
                    ],
                    "pageInfo": {"hasNextPage": False},
                },
            }
        ]
        self.gh.comments_data = [
            {
                "id": 10,
                "user": {"id": 9, "login": "other"},
                "body": "real defect <!-- mo-bot-review:v1 {} -->",
            }
        ]
        candidate = self.w.pr_candidate(1)
        self.assertEqual(
            {a["kind"] for a in candidate["atoms"]}, {"comment", "review_thread"}
        )

    def test_mixed_failures_each_have_disposition_and_issue_precedes_source_push(self):
        self.gh.checks = [
            {
                "id": 1,
                "name": "regression",
                "status": "completed",
                "conclusion": "failure",
            },
            {"id": 2, "name": "flaky", "status": "completed", "conclusion": "failure"},
        ]
        job = self.job()
        atoms = json.loads(job["metadata"])["atoms"]
        issue = {
            "classification": "flaky",
            "cause_signature": "preexisting test timing race",
            "issue_title": "fix test race",
            "issue_body": "failed baseline control, with logs",
            "evidence": ["baseline fails without PR"],
            "evidence_keys": [atoms[1]["key"]],
            "rerun_ids": [],
        }
        dispositions = [
            {
                "evidence_key": atoms[0]["key"],
                "classification": "pr_defect",
                "evidence": ["PR causal code reproduction"],
            },
            {
                "evidence_key": atoms[1]["key"],
                "classification": "flaky",
                "evidence": ["preexisting baseline fails"],
            },
        ]
        job = self.phase(job)
        job = self.phase(
            self.s.store.claim(2), causes=[issue], failure_dispositions=dispositions
        )
        job = self.phase(self.s.store.claim(2))
        self.ws.worker_state.return_value = {
            **self.ws.worker_state.return_value,
            "head": NEW,
            "branch": "mo-bot/work/" + job["id"],
        }
        job = self.phase(
            self.s.store.claim(2), causes=[issue], failure_dispositions=dispositions
        )
        remote = self.remote(job)
        original = self.ws.worker_git.side_effect

        def git(slot, *args, **kwargs):
            if args[0] == "push":
                self.assertEqual(len(self.gh.issues), 1)
            return original(slot, *args, **kwargs)

        self.ws.worker_git.side_effect = git
        self.w.publish(job)
        self.assertEqual(self.s.store.job(job["id"])["state"], "done")
        self.assertEqual(len(remote["pushes"]), 1)
        self.assertEqual(len(self.gh.issues), 1)

    def test_mixed_failure_cannot_silently_omit_one_disposition(self):
        self.gh.checks = [
            {"id": 1, "name": "UT", "status": "completed", "conclusion": "failure"}
        ]
        self.phase(self.job())
        with self.assertRaisesRegex(ValueError, "every failed-CI"):
            self.phase(self.s.store.claim(2))
        self.assertFalse(self.gh.writes)

    def test_async_discovery_admits_only_after_read_only_child_completes(self):
        self.gh.comments_data = [{"id": 7, "user": {"id": 9}, "body": "causal defect"}]
        frozen = self.w.gather_candidate(1)
        self.s.config_path = self.root / "config.toml"
        process = Mock(pid=999999, returncode=0)
        process.poll.return_value = None

        def launch(argv, stdout, stderr, **kwargs):
            self.assertEqual(argv[-2:], [str(self.s.config_path), "1"])
            stdout.write(json.dumps(frozen))
            stdout.flush()
            return process

        with patch("mo_bot.discovery.subprocess.Popen", side_effect=launch):
            self.w.scan([{"number": 1}])
            self.w.poll()
            self.assertFalse(self.s.store.jobs())
            process.poll.return_value = 0
            self.w.poll()
        self.assertEqual(len(self.s.store.jobs()), 1)
        self.assertIsNone(self.w.discovery)

    def test_async_discovery_timeout_preserves_indeterminate_without_admission(self):
        self.s.config_path = self.root / "config.toml"
        process = Mock(pid=999999, returncode=-9)
        process.poll.return_value = None
        with (
            patch("mo_bot.discovery.subprocess.Popen", return_value=process),
            patch("mo_bot.discovery.stop_owned") as stop,
        ):
            self.w.scan([{"number": 1}])
            self.w.poll()
            self.w.discovery_child.started = time.monotonic() - 100
            self.w.poll()
            stop.assert_called_once()
        self.assertFalse(self.s.store.jobs())
        self.assertIn("timed out", self.w.discovery_errors["1"])

    def test_spent_rerun_identity_is_not_reissued_when_failure_attempt_changes(self):
        self.gh.runs = [
            {
                "id": 123,
                "head_sha": HEAD,
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "failure",
                "pull_requests": [{"number": 1}],
                "workflow_id": 2,
                "run_number": 1,
            }
        ]
        job = self.job()
        first = self.w.rerun(job, 123)
        meta = json.loads(job["metadata"])
        meta["evidence"]["runs"][0]["run_attempt"] = 2
        self.s.store.update(job["id"], metadata=json.dumps(meta))
        self.assertEqual(self.w.rerun(self.s.store.job(job["id"]), 123), first)
        self.assertEqual(len(self.gh.writes), 1)

    def test_rerun_aggregate_budget_blocks_second_distinct_failed_workflow(self):
        self.gh.runs = [
            {
                "id": 123,
                "head_sha": HEAD,
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "failure",
                "pull_requests": [{"number": 1}],
                "workflow_id": 2,
                "run_number": 1,
            },
            {
                "id": 124,
                "head_sha": HEAD,
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "failure",
                "pull_requests": [{"number": 1}],
                "workflow_id": 3,
                "run_number": 1,
            },
        ]
        job = self.job()
        self.w.config = replace(self.w.config, worker_reruns_per_head=1)
        self.w.rerun(job, 123)
        with self.assertRaisesRegex(ValueError, "budget"):
            self.w.rerun(job, 124)
        self.assertEqual(len(self.gh.writes), 1)

    def push_data(self, job):
        self.ws.worker_state.return_value = {
            **self.ws.worker_state.return_value,
            "head": NEW,
            "branch": "mo-bot/work/" + job["id"],
        }
        approval = result(job, self.ws.worker_state.return_value)
        return {
            "worker_slot": 2,
            "publication_ref": "refs/mo-bot/test/delivery/frozen",
            "candidate": NEW,
            "final_review": {
                "model": "gpt-6.1-sol",
                "effort": "xhigh",
                "result": approval,
            },
        }

    def remote(self, job, ambiguous=False):
        remote = {"head": HEAD if job["target_type"] == "pr" else "", "pushes": []}

        def git(*args, **kwargs):
            if args[0] == "ls-remote":
                return remote["head"] + "\trefs/heads/fix/foo" if remote["head"] else ""
            if args[0] == "push":
                remote["pushes"].append(args)
                remote["head"] = NEW
                if ambiguous:
                    raise RuntimeError("push response lost")
            return ""

        self.ws.publication_git.side_effect = git
        return remote

    def test_guarded_push_exact_candidate_explicit_lease_and_timeout_reconciliation(
        self,
    ):
        job = self.job()
        data = self.push_data(job)
        remote = self.remote(job, ambiguous=True)
        with self.assertRaises(RuntimeError):
            self.w.push(job, data)
        pushed = self.w.push(job, data)
        self.assertEqual(pushed["head"], NEW)
        self.assertEqual(len(remote["pushes"]), 1)
        args = remote["pushes"][0]
        self.assertIn("--force-with-lease=refs/heads/fix/foo:" + HEAD, args)
        self.assertEqual(args[-1], NEW + ":refs/heads/fix/foo")
        self.assertTrue((self.s.job_dir(job) / "push-intent.json").is_file())

    def test_push_rejects_moved_ref_and_changed_final_candidate_without_write(self):
        job = self.job()
        data = self.push_data(job)
        remote = self.remote(job)
        remote["head"] = "d" * 40
        with self.assertRaises(SourceChanged):
            self.w.push(job, data)
        self.assertFalse(remote["pushes"])
        remote["head"] = HEAD
        data["final_review"]["result"]["candidate_head"] = "d" * 40
        with self.assertRaisesRegex(ValueError, "xhigh approval"):
            self.w.push(job, data)
        self.assertFalse(remote["pushes"])

    def test_push_default_guard_applies_even_if_target_default_changes(self):
        job = self.job()
        data = self.push_data(job)
        remote = self.remote(job)
        original = self.gh.api

        def api(endpoint, **kwargs):
            value = original(endpoint, **kwargs)
            return (
                {**value, "default_branch": "fix/foo"}
                if endpoint == "repos/me/repo"
                else value
            )

        self.gh.api = api
        with self.assertRaisesRegex(ValueError, "default branch"):
            self.w.push(job, data)
        self.assertFalse(remote["pushes"])

    def test_issue_worker_complete_queue_to_fork_branch_and_verified_pr(self):
        job = self.job(issue=True)
        job = self.phase(job, classification="issue")
        job = self.phase(self.s.store.claim(2), classification="issue")
        job = self.phase(self.s.store.claim(2), classification="issue")
        self.ws.worker_state.return_value = {
            **self.ws.worker_state.return_value,
            "head": NEW,
            "branch": "mo-bot/work/" + job["id"],
        }
        job = self.phase(self.s.store.claim(2), classification="issue")
        remote = self.remote(job)
        self.w.publish(job)
        complete = self.s.store.job(job["id"])
        self.assertEqual(
            (complete["state"], complete["pr"], complete["slot_id"]), ("done", 20, None)
        )
        self.assertIn(
            "--force-with-lease=refs/heads/"
            + json.loads(job["metadata"])["identity"]["head_ref"]
            + ":",
            remote["pushes"][0],
        )
        self.assertEqual(len(self.gh.created_prs), 1)
        self.assertIn("Closes #5", self.gh.created_prs[0]["body"])
        self.w.publish(job)
        self.assertEqual(len(remote["pushes"]), 1)
        self.assertEqual(len(self.gh.created_prs), 1)

    def test_ambiguous_delivery_keeps_publishing_target_and_slot_lock(self):
        job = self.job()
        data = self.push_data(job)
        data["design"] = {"result": result(job, self.ws.worker_state.return_value)}
        self.s.store.update(
            job["id"],
            state="publishing",
            data=json.dumps(data),
            result=json.dumps(data["final_review"]["result"]),
        )
        remote = self.remote(job)
        remote["head"] = "d" * 40
        self.w.publish(self.s.store.job(job["id"]))
        held = self.s.store.job(job["id"])
        self.assertEqual((held["state"], held["slot_id"]), ("publishing", None))
        self.assertIn("manual reconciliation", held["error"])
        self.assertIsNone(self.s.store.claim(2))
        self.s.store.enqueue(
            {"repo": "matrixorigin/repo", "number": 1, "headRefOid": NEW}
        )
        self.assertIsNone(self.s.store.claim(0))


class TestStoreWorker(unittest.TestCase):
    def test_pool_resize_keeps_ids_and_cross_pool_pr_affinity(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            store = Store(path, "test", 2, 2)
            for slot in store.slots():
                store.slot_update(slot["id"], ready=1, session_id="s" + str(slot["id"]))
            worker_ids = [s["id"] for s in store.slots() if s["role"] == "worker"]
            c = {
                "repo": "r/x",
                "number": 1,
                "headRefOid": HEAD,
                "kind": "work",
                "trigger_key": "evidence",
            }
            store.enqueue(c)
            job = store.claim(2)
            store.update(job["id"], state="queued", phase="implement")
            store.enqueue({"repo": "r/x", "number": 1, "headRefOid": HEAD})
            self.assertIsNone(store.claim(0))
            self.assertIsNone(store.claim(3))
            store.close()
            store = Store(path, "test", 3, 1)
            self.assertEqual(
                [s["id"] for s in store.slots() if s["role"] == "worker"], [2]
            )
            self.assertEqual(
                [s["id"] for s in store.slots() if s["role"] == "review"], [0, 1, 4]
            )
            self.assertIsNotNone(store.claim(worker_ids[0]))
            store.close()

    def test_legacy_atomic_migration_preserves_every_job_and_review_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            db = sqlite3.connect(path)
            db.executescript(
                "CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT); INSERT INTO meta VALUES('service_id','test'); CREATE TABLE slots(id INTEGER PRIMARY KEY,session_id TEXT,generation INTEGER NOT NULL DEFAULT 0,model TEXT,effort TEXT,ready INTEGER NOT NULL DEFAULT 0); INSERT INTO slots VALUES(0,'existing',0,'gpt-6-astra','xhigh',1); CREATE TABLE jobs(id TEXT PRIMARY KEY,repo TEXT NOT NULL,pr INTEGER NOT NULL,head TEXT NOT NULL,state TEXT NOT NULL,metadata TEXT NOT NULL,created REAL,updated REAL,slot_id INTEGER REFERENCES slots(id),comment_id INTEGER,review_id INTEGER,review_event TEXT,review_attempted INTEGER,UNIQUE(repo,pr,head)); INSERT INTO jobs VALUES('legacy','r/x',1,'head','done','{}',1,1,0,77,88,'COMMENT',1);"
            )
            db.close()
            store = Store(path, "test", 1, 1)
            job = store.job("legacy")
            self.assertEqual(
                (
                    job["state"],
                    job["review_id"],
                    job["review_event"],
                    job["kind"],
                    job["target_number"],
                ),
                ("done", 88, "COMMENT", "review", 1),
            )
            self.assertEqual(store.slots()[0]["session_id"], "existing")
            store.close()
            store = Store(path, "test", 1, 1)
            self.assertEqual(store.job("legacy")["id"], "legacy")
            self.assertFalse(store.db.execute("PRAGMA foreign_key_check").fetchall())
            store.close()

    def test_concurrent_claim_across_connections_has_one_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            store = Store(path, "test", 1, 2)
            for slot in store.slots():
                store.slot_update(slot["id"], ready=1)
            store.enqueue(
                {
                    "repo": "r/x",
                    "number": 1,
                    "headRefOid": HEAD,
                    "kind": "work",
                    "trigger_key": "one",
                }
            )
            store.close()
            barrier = threading.Barrier(2)
            output = []

            def claim(slot):
                instance = Store(path, "test", 1, 2)
                barrier.wait(timeout=5)
                row = instance.claim(slot)
                output.append(row["id"] if row else None)
                instance.close()

            threads = [threading.Thread(target=claim, args=(i,)) for i in (1, 2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(any(t.is_alive() for t in threads))
            self.assertEqual(sum(v is not None for v in output), 1)


class TestIndependentGit(unittest.TestCase):
    def rebase_fixture(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        source = root / "source"
        source.mkdir()

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(source), *args],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()

        git("init", "-b", "main")
        git("config", "user.name", "test")
        git("config", "user.email", "test@example.invalid")
        (source / "file").write_text("base\n")
        git("add", ".")
        git("commit", "-m", "base")
        base = git("rev-parse", "HEAD")
        git("checkout", "-b", "feature")
        (source / "file").write_text("feature\n")
        git("commit", "-am", "feature")
        original = git("rev-parse", "HEAD")
        git("checkout", "main")
        (source / "file").write_text("target\n")
        git("commit", "-am", "target")
        target = git("rev-parse", "HEAD")
        ws = Workspace(Config("test", root / "state", "o/r", "me", source))
        ws.worker_ensure(2)
        ws.worker_git(2, "config", "user.name", "test")
        ws.worker_git(2, "config", "user.email", "test@example.invalid")
        ws.worker_git(2, "checkout", "-b", "mo-bot/work/job", original)
        return ws, base, original, target

    def test_real_git_old_ancestor_is_noop_but_target_rebase_stops_and_resumes(self):
        ws, base, original, target = self.rebase_fixture()
        self.assertTrue(ws.worker_rebase(2, base))
        self.assertEqual(ws.worker_git(2, "rev-parse", "HEAD"), original)
        self.assertEqual(ws.worker_rebase_status(2, target), "pending")
        self.assertFalse(ws.worker_rebase(2, target))
        self.assertEqual(ws.worker_rebase_status(2, target), "conflicts")
        with self.assertRaisesRegex(SourceChanged, "target differs"):
            ws.worker_rebase_status(2, base)
        (ws.worker_path(2) / "file").write_text("target and feature\n")
        candidate = ws.worker_finalize(2, "fix: resolve target conflict")
        self.assertNotEqual(candidate, original)
        self.assertEqual(ws.worker_rebase_status(2, target), "complete")
        self.assertEqual(
            (ws.worker_path(2) / "file").read_text(), "target and feature\n"
        )
        self.assertEqual(ws.worker_rebase_status(2, target), "complete")

    def test_performance_conventional_commit_can_finalize_real_source(self):
        ws, _, original, _ = self.rebase_fixture()
        (ws.worker_path(2) / "file").write_text("optimized source\n")
        candidate = ws.worker_finalize(2, "perf(planner): normalize disjunctions")
        self.assertNotEqual(candidate, original)
        self.assertEqual(
            ws.worker_git(2, "log", "-1", "--format=%s"),
            "perf(planner): normalize disjunctions",
        )
        with self.assertRaisesRegex(ValueError, "Conventional Commit"):
            ws.worker_finalize(2, "perf(planner): title\nother line")

    def test_reentry_keeps_old_archive_and_can_preserve_new_dirty_source(self):
        ws, _, original, _ = self.rebase_fixture()

        def own():
            owner = ws.worker_path(2).parent / "worker-owner.json"
            data = json.loads(owner.read_text())
            data["job"] = "job"
            owner.write_text(json.dumps(data))

        own()
        (ws.worker_path(2) / "file").write_text("old unfinished edit")
        archive = ws.archive_worker(2, {"id": "job"})
        retired = ws.retire_worker_archive({"id": "job"}, archive)
        self.assertEqual(Path(retired, "file").read_text(), "old unfinished edit")
        self.assertFalse(Path(archive).exists())
        ws.worker_ensure(2)
        own()
        ws.worker_git(2, "checkout", "-b", "mo-bot/work/job", original)
        (ws.worker_path(2) / "file").write_text("new unfinished edit")
        current = ws.archive_worker(2, {"id": "job"})
        self.assertEqual(Path(current, "file").read_text(), "new unfinished edit")
        self.assertEqual(Path(retired, "file").read_text(), "old unfinished edit")

    def test_real_git_missing_target_and_dirty_source_are_not_success(self):
        ws, _, original, target = self.rebase_fixture()
        with self.assertRaises(RuntimeError):
            ws.worker_rebase_status(2, "f" * 40)
        with self.assertRaisesRegex(ValueError, "invalid rebase target"):
            ws.worker_fetch_target(2, "o/r", "--invalid")
        (ws.worker_path(2) / "file").write_text("unfinished\n")
        with self.assertRaises(RuntimeError):
            ws.worker_rebase(2, target)
        self.assertEqual(ws.worker_git(2, "rev-parse", "HEAD"), original)
        self.assertEqual((ws.worker_path(2) / "file").read_text(), "unfinished\n")

    def test_archive_restore_dirty_source_and_idempotent_crash_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()

            def git(*args):
                return subprocess.run(
                    ["git", "-C", str(source), *args],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout.strip()

            git("init", "-b", "main")
            git("config", "user.name", "test")
            git("config", "user.email", "test@example.invalid")
            (source / "file").write_text("base")
            git("add", ".")
            git("commit", "-m", "base")
            ws = Workspace(
                Config("test", root / "state", "o/r", "me", source, root / "q")
            )
            path = ws.worker_ensure(2)
            ws.worker_git(2, "checkout", "-b", "mo-bot/work/job")
            owner = path.parent / "worker-owner.json"
            data = json.loads(owner.read_text())
            data["job"] = "job"
            owner.write_text(json.dumps(data))
            (path / "file").write_text("unfinished actual edit")
            (path / "untracked").write_text("preserve this too")
            inode = (path / "file").stat().st_ino
            archive = ws.archive_worker(2, {"id": "job"})
            self.assertFalse(path.exists())
            self.assertEqual(
                Path(archive, "file").read_text(), "unfinished actual edit"
            )
            new = ws.restore_worker(3, {"id": "job"}, archive)
            self.assertEqual((new / "file").stat().st_ino, inode)
            self.assertEqual((new / "untracked").read_text(), "preserve this too")
            self.assertEqual(
                ws.worker_git(3, "branch", "--show-current"), "mo-bot/work/job"
            )
            self.assertEqual(
                json.loads((new.parent / "worker-owner.json").read_text())["job"], "job"
            )
            self.assertEqual(ws.restore_worker(3, {"id": "job"}, archive), new)
            with self.assertRaises(SourceChanged):
                ws.archive_worker(3, {"id": "another"})

    def test_coordinator_commits_only_after_owned_clone_and_keeps_user_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()

            def git(*args):
                p = subprocess.run(
                    ["git", "-C", str(source), *args],
                    text=True,
                    capture_output=True,
                    check=True,
                )
                return p.stdout.strip()

            git("init", "-b", "main")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Test")
            (source / "file").write_text("original\n")
            git("add", ".")
            git("commit", "-m", "initial")
            initial = git("rev-parse", "HEAD")
            config = Config("test", root / "state", "r/x", "me", source, root / "queue")
            workspace = Workspace(config)
            path = workspace.worker_ensure(2)
            self.assertTrue((path / ".git").is_dir())
            workspace.worker_git(2, "config", "user.email", "test@example.invalid")
            workspace.worker_git(2, "config", "user.name", "Test")
            workspace.worker_git(2, "checkout", "-b", "mo-bot/work/job")
            (path / "file").write_text("fixed\n")
            candidate = workspace.worker_finalize(2, "fix: causal correction")
            self.assertNotEqual(candidate, initial)
            self.assertEqual(git("rev-parse", "HEAD"), initial)
            self.assertEqual((source / "file").read_text(), "original\n")
            self.assertFalse(workspace.worker_state(2)["status"])
            frozen = workspace.freeze_publication(2, {"id": "job"}, candidate)
            workspace.worker_git(2, "checkout", "--detach", initial)
            (path / "file").write_text("another task working copy\n")
            workspace.verify_publication({"id": "job"}, frozen, candidate)
            self.assertEqual(git("rev-parse", "HEAD"), initial)
            self.assertEqual(git("show", frozen + ":file"), "fixed")
            workspace.publication_git("update-ref", frozen, initial)
            with self.assertRaises(SourceChanged):
                workspace.verify_publication({"id": "job"}, frozen, candidate)
            with self.assertRaises(ValueError):
                workspace.worker_finalize(2, "not conventional")
            (path.parent / "worker-owner.json").write_text("{}")
            with self.assertRaises(SourceChanged):
                workspace.worker_ensure(2)

    def test_local_git_explicit_lease_rejects_concurrent_remote_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            remote = root / "remote.git"
            subprocess.run(
                ["git", "init", "--bare", str(remote)], capture_output=True, check=True
            )
            subprocess.run(
                ["git", "init", "-b", "main", str(repo)],
                capture_output=True,
                check=True,
            )

            def git(*args, check=True):
                p = subprocess.run(
                    ["git", "-C", str(repo), *args],
                    capture_output=True,
                    text=True,
                    check=check,
                )
                return p.stdout.strip() if check else p

            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Test")
            (repo / "file").write_text("a")
            git("add", ".")
            git("commit", "-m", "a")
            old = git("rev-parse", "HEAD")
            git("push", str(remote), old + ":refs/heads/fix/foo")
            (repo / "file").write_text("b")
            git("add", ".")
            git("commit", "-m", "b")
            new = git("rev-parse", "HEAD")
            git("push", str(remote), new + ":refs/heads/fix/foo")
            failed = git(
                "push",
                "--force-with-lease=refs/heads/fix/foo:" + old,
                str(remote),
                old + ":refs/heads/fix/foo",
                check=False,
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn(new, git("ls-remote", str(remote), "refs/heads/fix/foo"))


if __name__ == "__main__":
    unittest.main()
