from concurrent.futures import Future
import queue
import threading
import fcntl
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import sys
import time

from .codex import (
    SCHEMA,
    SourceChanged,
    Workspace,
    broken_session,
    stop_owned,
    completed_result,
)
from .rpc import Backend, Events
from .nodes import Nodes
from .remote import CleanupPending, recover_host_tools
from .harness import Harness
from .github import GitHub
from .store import Store
from .worker import Worker
from .discovery import Discovery
from .config import CLASSIFIER_EFFORT, CLASSIFIER_MODEL


LOG = logging.getLogger("mo-bot")
REVIEW_SKILL = Path(__file__).parent / "skills/mo-bot-review/SKILL.md"


def review_instructions():
    # Read per launch: policy edits apply to the next assigned review without a
    # service restart. Each native turn persists its actual prompt for recovery.
    text = REVIEW_SKILL.read_text()
    if not text.startswith("---\n"):
        raise ValueError("review skill frontmatter missing")
    return text.split("---", 2)[2].strip()


WARMUP_SCHEMA = {
    "type": "object",
    "properties": {"ready": {"type": "boolean"}},
    "required": ["ready"],
    "additionalProperties": False,
}


def report_text(envelope):
    result = envelope["assessment"]
    lines = [
        f"Assessment: **{result['decision']}**",
        result["summary"],
        f"Model: `{envelope.get('model')}` / `{envelope.get('effort')}`",
        f"Session: `{envelope['session_id']}` · slot {envelope['slot']} · generation {envelope['generation']}",
    ]
    for finding in result["findings"]:
        lines.append(
            f"- **{finding['priority']}** `{finding['file']}:{finding['line']}`: "
            f"{finding['trigger']}\n  Impact: {finding['impact']}\n  Evidence: {finding['evidence']}"
        )
    if result["validation"]:
        lines += ["Validation:"] + [f"- {item}" for item in result["validation"]]
    if result["limitations"]:
        lines += ["Limitations:"] + [f"- {item}" for item in result["limitations"]]
    text = "\n\n".join(lines)
    if len(text) > 60000:
        raise ValueError(
            "review exceeds GitHub comment budget; refusing silent truncation"
        )
    return text


def validate_result(value, schema):
    kind = schema["type"]
    expected = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "boolean": bool,
    }[kind]
    if type(value) is not expected:
        raise ValueError(f"result must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("invalid result enum")
    if kind == "object":
        if set(value) != set(schema["required"]):
            raise ValueError("missing or unexpected result fields")
        for key, child in value.items():
            validate_result(child, schema["properties"][key])
    elif kind == "array":
        for item in value:
            validate_result(item, schema["items"])


class Service:
    def __init__(
        self,
        config,
        config_path=None,
        *,
        github=None,
        workspace=None,
        backend_factory=Backend,
    ):
        self.config = config
        self.config_path = config_path
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (config.state_dir / "service.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._initialize(config, config_path, github, workspace, backend_factory)
        except Exception:
            if hasattr(self, "store"):
                self.store.close()
            self.lock.close()
            raise

    def _initialize(self, config, config_path, github, workspace, backend_factory):
        self.store = Store(
            config.state_dir / "state.sqlite3",
            config.service_id,
            config.sessions,
            config.worker_sessions,
            config.classifier_sessions,
        )
        for slot in self.store.slots():
            override = next(
                (
                    s
                    for s in config.session_overrides
                    if s.get("role", "review") == slot["role"]
                    and s["slot"] == slot["pool_index"]
                ),
                {},
            )
            node = override.get("node", "local")
            definition = next((n for n in config.nodes if n["name"] == node), None)
            if node != "local" and definition is None:
                raise ValueError(f"node unavailable in configuration: {node}")
            self.store.bind_node(
                slot,
                node,
                json.dumps(
                    {key: definition[key] for key in ("name", "ssh", "codex")},
                    sort_keys=True,
                )
                if definition
                else "local",
            )
            if not slot["model"] or slot["role"] == "classifier":
                self.store.slot_update(
                    slot["id"],
                    model=(
                        CLASSIFIER_MODEL
                        if slot["role"] == "classifier"
                        else override.get(
                            "model",
                            config.worker_model
                            if slot["role"] == "worker"
                            else config.model,
                        )
                    ),
                    effort=(
                        CLASSIFIER_EFFORT
                        if slot["role"] == "classifier"
                        else override.get(
                            "reasoning_effort",
                            config.worker_effort
                            if slot["role"] == "worker"
                            else config.reasoning_effort,
                        )
                    ),
                )
        self.github = github or GitHub(config)
        self.workspace = workspace or Workspace(config)
        self.backend_factory = (
            Nodes if config.nodes and backend_factory is Backend else backend_factory
        )
        self.backend = None
        self.events = Events()
        self.commands = queue.Queue(maxsize=64)
        self.status_lock = threading.Lock()
        self.status = {}
        self.models = []
        self.turns = {}  # Only live RPC turn handles; persistent workflow is in SQLite.
        self.eligible = set()
        self.last_error = None
        self.next_scan = 0.0
        self.cooldown = 0.0
        self.stopping = False
        self.worker = Worker(self)
        self.harness = Harness(self)
        self.discovery = Discovery(config, "review-discovery")
        self.admission = {}
        self.scan_error = None
        self.last_scan_at = None
        self.node_session_retry = {}
        control = self.store.db.execute(
            "SELECT value FROM meta WHERE key='dispatch_control'"
        ).fetchone()
        self.dispatch_control = (
            json.loads(control[0])
            if control
            else {"paused": False, "reason": None, "at": None}
        )

    def recover(self):
        recover_host_tools(self.config)
        self.discovery.recover()
        self.worker.discovery_child.recover()
        self.worker.issue_discovery_child.recover()
        owner = self.config.state_dir / "backend-owner.json"
        if owner.exists():
            stop_owned(json.loads(owner.read_text()))
        for job in self.store.jobs(("reviewing", "canceling")):
            if job["kind"] == "work" and job["state"] != "canceling":
                self.worker.recover(job)
                continue
            if job["state"] == "canceling":
                if json.loads(job["data"]).get("paused"):
                    data = json.loads(job["data"])
                    if job["kind"] == "work" and job["slot_id"] is not None:
                        data["workspace_archive"] = self.workspace.archive_worker(
                            job["slot_id"], job
                        )
                    self.store.update(
                        job["id"],
                        state="queued",
                        slot_id=None,
                        data=json.dumps(data),
                        error="paused by operator",
                    )
                    continue
                self.store.update(
                    job["id"],
                    state="superseded",
                    error="old revision canceled during restart",
                )
            else:
                # Recover a fully materialized result before considering another turn.
                directory = (
                    Path(job["turn_dir"])
                    if job["turn_dir"]
                    else self.job_dir(job) / f"attempt-{job['attempts']}"
                )
                slot = self.store.db.execute(
                    "SELECT * FROM slots WHERE id=?", (job["slot_id"],)
                ).fetchone()
                try:
                    if slot is None:
                        raise ValueError("recovery slot missing")
                    result, provenance = completed_result(directory, slot["session_id"])
                    if not self.workspace.clean(slot["id"]):
                        raise ValueError("source changed during recovery")
                    self.save_result(
                        job, slot, result, provenance["model"], provenance["effort"]
                    )
                except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                    self.store.update(
                        job["id"],
                        state="queued",
                        error="recovering interrupted task",
                        slot_id=None,
                    )

    def job_dir(self, job):
        directory = self.config.state_dir / "jobs" / job["id"]
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def scan(self, snapshot=None):
        if snapshot is None:
            if self.discovery.process is None:
                self.eligible.difference_update(
                    j["id"]
                    for j in self.store.jobs(("queued", "reviewing", "publishing"))
                    if j["kind"] == "review"
                    and not json.loads(j["metadata"]).get("forced")
                )
                self.discovery.start(
                    [sys.executable, "-m", "mo_bot.github", str(self.config_path)]
                )
            return
        if not isinstance(snapshot.get("heads"), dict) or not isinstance(
            snapshot.get("candidates"), list
        ):
            raise ValueError("incomplete discovery snapshot")
        admission = snapshot.get("admission", {})
        if not isinstance(admission, dict):
            raise ValueError("invalid discovery admission map")
        heads = snapshot["heads"]
        if any(not isinstance(head, str) for head in heads.values()):
            raise ValueError("invalid discovery head")
        for candidate in snapshot["candidates"]:
            if (
                not isinstance(candidate, dict)
                or candidate.get("repo") != self.config.repository
                or type(candidate.get("number")) is not int
                or heads.get(str(candidate["number"])) != candidate.get("headRefOid")
            ):
                raise ValueError("candidate does not match discovery snapshot")
        for number, status in admission.items():
            if (
                not isinstance(status, dict)
                or status.get("head") != heads.get(number)
                or not isinstance(status.get("reason"), str)
            ):
                raise ValueError("invalid discovery admission entry")
        self.eligible.difference_update(
            j["id"] for j in self.store.jobs() if j["kind"] == "review"
        )
        self.admission = admission
        (self.config.state_dir / "snapshot.json").write_text(
            json.dumps(snapshot, indent=2)
        )
        heads = snapshot["heads"]
        for job in self.store.jobs(("queued", "reviewing", "publishing")):
            if job["kind"] == "work":
                continue  # Worker validates immutable identities per phase/effect.
            forced = json.loads(job["metadata"]).get("forced")
            status = admission.get(str(job["pr"]), {})
            reason = None
            if heads.get(str(job["pr"])) != job["head"]:
                reason = "PR closed or head changed"
            elif (
                not forced
                and status.get("head") == job["head"]
                and status.get("reason") in ("already-reviewed", "explicitly-excluded")
                and not job["review_attempted"]
            ):
                reason = status["reason"]
            if reason:
                self.store.update(
                    job["id"],
                    state="canceling" if job["state"] == "reviewing" else "superseded",
                    error=reason,
                    slot_id=job["slot_id"] if job["state"] == "reviewing" else None,
                )
        for candidate in snapshot["candidates"]:
            key = self.store.key(
                candidate["repo"], candidate["number"], candidate["headRefOid"]
            )
            if not self.store.job(key):
                self.store.enqueue(candidate)
                LOG.info(
                    "discovered PR=%s head=%s job=%s",
                    candidate["number"],
                    candidate["headRefOid"],
                    key,
                )
            self.eligible.add(key)
        self.eligible.update(
            j["id"]
            for j in self.store.jobs(("queued", "reviewing", "publishing"))
            if json.loads(j["metadata"]).get("forced")
            and heads.get(str(j["pr"])) == j["head"]
        )
        self.last_scan_at = time.time()
        self.scan_error = None
        if self.last_error and "discovery" in self.last_error:
            self.last_error = None
        assigned_issues = snapshot.get("assigned_issues", [])
        issue_error = snapshot.get("assigned_issue_error")
        if (
            not isinstance(assigned_issues, list)
            or any(type(number) is not int or number <= 0 for number in assigned_issues)
            or len(set(assigned_issues)) != len(assigned_issues)
            or (issue_error is not None and not isinstance(issue_error, str))
        ):
            raise ValueError("invalid assigned issue discovery evidence")
        self.worker.scan(snapshot.get("own_prs", []), assigned_issues, issue_error)
        LOG.info(
            "scan candidates=%d indeterminate=%s",
            len(snapshot["candidates"]),
            snapshot.get("indeterminate", []),
        )

    def poll_discovery(self):
        if self.discovery.process is None:
            return
        try:
            snapshot = self.discovery.poll()
            if snapshot is not None:
                if snapshot.get("complete") is not True:
                    raise ValueError("discovery snapshot is not complete")
                self.scan(snapshot)
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            self.scan_error = str(error)
            self.next_scan = time.monotonic() + min(60, self.config.poll_seconds)
            LOG.warning("review discovery blocked: %s", error)

    def launch(self, slot, job=None):
        slot_id = slot["id"]
        if job and job["kind"] == "work":
            return self.worker.launch(slot, job)
        if job:
            pr = self.github.pr(job)
            if self.store.uncertain_pr(
                job["repo"],
                pr["head"]["sha"],
                (pr["head"].get("repo") or {}).get("id"),
                pr["head"].get("ref"),
            ):
                self.store.update(
                    job["id"],
                    state="queued",
                    slot_id=None,
                    error="possibly created worker PR awaits issue outbox reconciliation",
                )
                return
            if pr["state"] != "open" or pr["head"]["sha"] != job["head"]:
                self.store.update(
                    job["id"], state="superseded", error="PR changed before dispatch"
                )
                return
            if job["attempts"] > self.config.max_recoveries:
                self.store.update(
                    job["id"], state="failed", error="recovery budget exhausted"
                )
                return
            if not job["comment_id"]:
                comment = self.github.find_comment(job)
                if not comment:
                    if job["claim_attempted"]:
                        self.store.update(
                            job["id"],
                            state="failed",
                            slot_id=None,
                            error="ambiguous comment creation; remote marker absent; manual reconciliation required",
                        )
                        return
                    self.store.update(job["id"], claim_attempted=1)
                    comment = self.github.ensure_comment(job)
                    job = self.store.job(job["id"])
                marker = self.github.owned_marker(comment, job)
                if not marker:
                    raise RuntimeError("created claim has no valid ownership marker")
                if not job["claim_attempted"] and marker["state"] != "done":
                    self.store.update(
                        job["id"],
                        state="failed",
                        slot_id=None,
                        comment_id=comment["id"],
                        comment_state=marker["state"],
                        error="remote claim restored; no duplicate review",
                    )
                    return
                if marker["state"] == "done":
                    self.store.update(
                        job["id"],
                        state="done",
                        comment_id=comment["id"],
                        comment_state="done",
                        slot_id=None,
                    )
                    return
                self.store.update(
                    job["id"], comment_id=comment["id"], comment_state=marker["state"]
                )
            job = self.store.job(job["id"])
            attempt = job["attempts"] + 1
            self.store.update(job["id"], attempts=attempt)
            directory = self.job_dir(job) / f"attempt-{attempt}"
            directory.mkdir(parents=True, exist_ok=True)
            evidence = self.github.evidence(job, pr)
            (directory / "evidence.json").write_text(json.dumps(evidence, indent=2))
            workspace, merge_base = self.workspace.prepare(slot_id, job, pr, directory)
            prompt = (
                review_instructions()
                + f"""\nYou are mo-bot reusable review slot {slot_id}, generation {slot["generation"]}.
CURRENT TASK replaces any previous PR task: job={job["id"]}, repository={job["repo"]},
PR={job["pr"]}, HEAD={job["head"]}, merge-base={merge_base}. Only these frozen sources
and {directory}/evidence.json establish facts for this PR. Earlier PR facts are not evidence.
Read {directory}/change.patch and surrounding owners/callers. Follow applicable
mo-self-review instructions. Review the COMPLETE delta, design/ownership, correctness,
performance, unhappy paths, compatibility, comments and test quality. Reuse valid exact-input
CI/author evidence; run only checks that resolve concrete gaps. Missing evidence is not a pass.
Source must remain unchanged. Put diagnostic artifacts only under {directory}; use its
disk-backed tmp directory. Before native tests verify the current revision's native provenance
through the repository build/test owner; retained artifacts may belong to an earlier PR.
Do not spawn subagents, publish comments/reviews, mutate GitHub, rerun CI, commit, push or merge.
The coordinator owns publication. Findings need location, concrete trigger, impact and evidence.
Return the required JSON with job_id={job["id"]} and head_sha={job["head"]}; no speculative blockers.
This is attempt {attempt} of the SAME logical job; consult earlier attempt artifacts in
{directory.parent} to recover completed evidence instead of repeating it blindly.
"""
            )
            schema = SCHEMA
        else:
            directory = (
                self.config.state_dir
                / "slots"
                / str(slot_id)
                / f"warmup-{slot['generation']}"
            )
            workspace = self.slot_workspace(slot)
            prompt = f"You are persistent mo-bot review slot {slot_id}. Future turns provide a new exact PR scope. Do not inspect files, run tools, or contact GitHub. Reply only with JSON ready=true."
            schema = WARMUP_SCHEMA
            if slot["role"] == "classifier":
                prompt = f"You are persistent mo-bot issue similarity session {slot_id}. Future turns ask whether issue reports describe the same underlying cause and can be fixed together. Do not inspect repositories, use tools, or contact GitHub. Reply only with JSON ready=true."
        if job:
            self.store.update(job["id"], turn_dir=str(directory))
        turn = self.backend.start(slot, directory, workspace, prompt, schema)
        self.turns[slot_id] = (turn, None if job is None else job["id"])
        self.refresh_status()
        LOG.info(
            "started slot=%s session=%s job=%s",
            slot_id,
            slot["session_id"],
            None if job is None else job["id"],
        )

    def save_result(self, job, slot, result, actual_model=None, actual_effort=None):
        validate_result(result, SCHEMA)
        if result.get("job_id") != job["id"] or result.get("head_sha") != job["head"]:
            raise ValueError("review result identity mismatch")
        if result.get("decision") not in (
            "approve",
            "request_changes",
            "needs_validation",
        ):
            raise ValueError("invalid review decision")
        if bool(result["findings"]) != (result["decision"] == "request_changes"):
            raise ValueError(
                "confirmed findings require request_changes; other decisions need no findings"
            )
        envelope = {
            "assessment": result,
            "model": actual_model or slot["model"],
            "effort": actual_effort or slot["effort"],
            "session_id": slot["session_id"],
            "slot": slot["id"],
            "generation": slot["generation"],
        }
        report_text(
            envelope
        )  # Check the publishable shape/budget before checkpointing.
        self.store.update(
            job["id"],
            state="publishing",
            slot_id=None,
            result=json.dumps(envelope),
            error=None,
        )

    def finish(self, slot_id, turn, key):
        slot = next(
            s for s in self.store.slots(include_disabled=True) if s["id"] == slot_id
        )
        job = self.store.job(key) if key else None
        cleanup_pending = False
        try:
            if (
                job
                and job["state"] == "canceling"
                and json.loads(job["data"]).get("paused")
                and turn.completed
            ):
                turn.backend.before_release(turn)
                data = json.loads(job["data"])
                if job["kind"] == "work":
                    data["workspace_archive"] = self.workspace.archive_worker(
                        slot_id, job
                    )
                self.store.update(
                    key,
                    state="queued",
                    slot_id=None,
                    data=json.dumps(data),
                    error="paused by operator",
                )
                return
            result = turn.result()
            if result is None:
                return
            if slot["role"] == "classifier":
                self.worker.classifier_complete(slot_id, result)
                self.store.slot_update(slot_id, ready=1)
                return
            if job and job["kind"] == "work":
                if job["state"] == "canceling":
                    self.store.update(
                        key,
                        state="superseded",
                        slot_id=None,
                        error="worker interrupted",
                    )
                else:
                    self.worker.consume(job, slot, result, turn.model, turn.effort)
                self.store.slot_update(slot_id, ready=1)
                return
            if slot["role"] == "review" and not self.workspace.clean(slot_id):
                raise SourceChanged("review worker modified source")
            if job and job["state"] == "canceling":
                self.store.update(
                    key,
                    state="superseded",
                    slot_id=None,
                    error=job["error"] or "old revision stopped",
                )
            elif job:
                self.save_result(job, slot, result, turn.model, turn.effort)
            elif turn.schema is not None and result != {"ready": True}:
                raise ValueError("warmup failed")
            self.store.slot_update(slot_id, ready=1)
        except CleanupPending:
            cleanup_pending = True
            # Transport cleanup is not a new workflow attempt. Retain the active
            # PR/slot lease and retry cleanup, without consuming recovery budget.
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            message = str(error)
            if not turn.completed:
                turn.stop()
            if (
                slot["role"] == "review"
                and isinstance(error, SourceChanged)
                and "modified" in message
            ):
                self.workspace.quarantine(slot_id)
            if isinstance(error, SourceChanged) or broken_session(message):
                self.store.slot_update(
                    slot_id, session_id=None, ready=0, generation=slot["generation"] + 1
                )
            if job and job["kind"] == "work":
                if isinstance(error, SourceChanged) or job["state"] == "canceling":
                    self.worker.fail(job, error)
                else:
                    self.worker.retry(job, error)
            elif slot["role"] == "classifier":
                self.worker.classifier_failed(slot_id, error)
            elif job:
                state = (
                    "superseded"
                    if job["state"] == "canceling"
                    else (
                        "queued"
                        if job["attempts"] <= self.config.max_recoveries
                        and not isinstance(error, SourceChanged)
                        else "failed"
                    )
                )
                self.store.update(
                    key,
                    state=state,
                    error=message,
                    slot_id=slot_id
                    if job["kind"] == "work" and state == "queued"
                    else None,
                )
                if job["kind"] == "work" and state != "queued":
                    self.store.dispose_evidence(job, state)
            if not job and slot["role"] != "classifier":
                self.cooldown = time.monotonic() + 60
            if job and self.store.job(key)["state"] == "queued":
                self.store.update(
                    key, retry_at=max(self.store.job(key)["retry_at"], time.time() + 60)
                )
            LOG.error("turn failed slot=%s job=%s error=%s", slot_id, key, message)
        finally:
            if turn.completed and not cleanup_pending:
                turn.release()
                self.turns.pop(slot_id, None)

    def publish(self, job):
        if json.loads(job["data"]).get("paused"):
            return
        if job["kind"] == "work":
            return self.worker.publish(job)
        pr = self.github.pr(job)
        if pr["state"] != "open" or pr["head"]["sha"] != job["head"]:
            self.github.patch_comment(
                job,
                "superseded",
                "Review belongs to an older commit; it is not a review of the current head.",
            )
            self.store.update(
                job["id"],
                state="superseded",
                comment_state="superseded",
                error="head changed before publication",
            )
            return
        if job["id"] not in self.eligible and not job["review_attempted"]:
            return  # Keep the completed result; CI refresh must not start another model turn.
        envelope = json.loads(job["result"])
        text = report_text(envelope)
        event = (
            "COMMENT"
            if pr.get("user", {}).get("login")
            in {self.config.reviewer, self.github.author}
            else {
                "approve": "APPROVE",
                "request_changes": "REQUEST_CHANGES",
                "needs_validation": "COMMENT",
            }[envelope["assessment"]["decision"]]
        )
        if job["review_event"] and job["review_event"] != event:
            raise RuntimeError(
                "publication event changed; manual reconciliation required"
            )
        self.store.update(job["id"], review_event=event)
        review = self.github.find_review(job)
        if review:
            self.github.validate_review(review, job, event)
            if job["review_id"] and job["review_id"] != review["id"]:
                raise RuntimeError(
                    "publication review ID changed; manual reconciliation required"
                )
        elif job["review_attempted"] or job["review_id"]:
            self.store.update(
                job["id"],
                state="failed",
                error="ambiguous review submission; remote marker absent; manual reconciliation required",
            )
            return
        else:
            # Persist the fence BEFORE the non-idempotent POST. On an uncertain
            # response, reconcile authenticated markers; never blindly POST again.
            self.store.update(job["id"], review_attempted=1)
            review = self.github.submit_review(job, event, text)
        self.store.update(job["id"], review_id=review["id"], review_attempted=1)
        self.github.patch_comment(job, "done", report_text(json.loads(job["result"])))
        self.store.update(job["id"], state="done", comment_state="done", error=None)
        LOG.info("published PR=%s head=%s job=%s", job["pr"], job["head"], job["id"])

    def slot_workspace(self, slot):
        if slot["role"] == "worker":
            return self.workspace.worker_ensure(slot["id"])
        if slot["role"] == "classifier":
            path = self.config.state_dir / "classifier" / str(slot["id"])
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            return path
        return self.workspace.ensure(slot["id"])

    def open_session(self, slot):
        if time.monotonic() < self.node_session_retry.get(slot["id"], 0):
            return
        try:
            self._open_session(slot)
            self.node_session_retry.pop(slot["id"], None)
        except Exception as error:
            self.store.slot_update(slot["id"], ready=0)
            if slot["role"] == "classifier":
                self.worker.classifier_unavailable(error)
            self.node_session_retry[slot["id"]] = time.monotonic() + 60
            LOG.warning(
                "session blocked slot=%s node=%s: %s", slot["id"], slot["node"], error
            )

    def _open_session(self, slot):
        workspace = self.slot_workspace(slot)
        response, replaced = self.backend.session(slot, workspace)
        self.store.slot_update(
            slot["id"],
            session_id=response["thread"]["id"],
            generation=slot["generation"] + int(replaced),
            ready=1,
            model=slot["model"],
            effort=slot["effort"],
        )
        if response["thread"]["id"] != slot["session_id"]:
            updated = next(s for s in self.store.slots() if s["id"] == slot["id"])
            self.launch(updated)

    def refresh_status(self):
        sessions = []
        for slot in self.store.slots(include_disabled=True):
            current = self.turns.get(slot["id"])
            semantic = self.worker.classifications.get(slot["id"])
            owned = self.store.db.execute(
                "SELECT * FROM jobs WHERE slot_id=? AND kind='work' AND state IN ('queued','reviewing','publishing','canceling') ORDER BY created LIMIT 1",
                (slot["id"],),
            ).fetchone()
            sessions.append(
                {
                    **dict(slot),
                    "draining": bool(not slot["enabled"] and (current or owned)),
                    "state": "disabled"
                    if not slot["enabled"] and not current and not owned
                    else "running"
                    if current
                    else (
                        owned["state"]
                        if owned
                        else "idle"
                        if slot["ready"]
                        and self.backend
                        and not self.backend.dead
                        and (
                            not isinstance(self.backend, Nodes)
                            or self.backend.available(slot)
                        )
                        else "recovering"
                    ),
                    "turn_id": current[0].turn_id if current else None,
                    "active_model": current[0].model if current else None,
                    "active_effort": current[0].effort if current else None,
                    "job_id": current[1]
                    if current
                    else (owned["id"] if owned else None),
                    "semantic_issue": semantic[0]["number"] if semantic else None,
                    "phase": owned["phase"] if owned else None,
                    "last_activity_at": getattr(current[0], "last_activity_at", None)
                    if current
                    else None,
                    "activity_age_seconds": max(
                        0, time.time() - current[0].last_activity_at
                    )
                    if current
                    and isinstance(
                        getattr(current[0], "last_activity_at", None), (int, float)
                    )
                    else None,
                }
            )
        jobs = [dict(j) for j in self.store.summaries()]
        for job in jobs:
            job["metadata"] = json.loads(job["metadata"])
            job["data"] = json.loads(job["data"])
            job["result"] = json.loads(job["result"]) if job["result"] else None
            harness_request = job["data"].get("harness_request") or {}
            harness_result = job["data"].get("harness_result") or {}
            harness_waiting = (
                harness_request.get("status") in ("pending", "running")
                and harness_request.get("expires_at", 0) > time.time()
            )
            job["dispatchable"] = (
                job["state"] == "queued"
                and not self.dispatch_control["paused"]
                and not job["data"].get("paused")
                and not harness_waiting
                and job["id"] in self.eligible
                and job["retry_at"] <= time.time()
            )
            status = self.admission.get(str(job["pr"]), {})
            job["wait_reason"] = None
            if job["state"] == "queued" and not job["dispatchable"]:
                job["wait_reason"] = (
                    "operator paused"
                    if job["data"].get("paused")
                    else "service draining"
                    if self.dispatch_control["paused"]
                    else "semantic harness coordination"
                    if harness_waiting
                    else "retry backoff"
                    if job["retry_at"] > time.time()
                    else status.get("reason", "awaiting successful discovery")
                    if status.get("head") == job["head"]
                    else "awaiting successful discovery"
                )
            job["attention_required"] = bool(
                job["data"].get("paused")
                or (
                    harness_result.get("status") in ("needs_input", "error")
                    and harness_result.get("request_id") == harness_request.get("id")
                )
                or (
                    job["error"]
                    and job["state"] in ("queued", "publishing", "failed", "canceling")
                )
            )
            job["next_retry_at"] = (
                job["data"].get("publication_next_at")
                if job["state"] == "publishing" and job["kind"] == "work"
                else job["retry_at"]
            )
            job["manual_required"] = bool(job["data"].get("manual_reconciliation"))
        alerts = [
            {
                "kind": "job_blocked",
                "job_id": j["id"],
                "pr": j["pr"],
                "state": j["state"],
                "reason": j["error"]
                or (j["data"].get("harness_result") or {}).get("guidance")
                or (j["data"].get("harness_result") or {}).get("rationale"),
                "manual_required": j["manual_required"],
                "next_retry_at": j["next_retry_at"],
            }
            for j in jobs
            if j["attention_required"]
        ]
        alerts += [
            {
                "kind": "worker_discovery_blocked",
                "job_id": None,
                "target": target,
                "pr": int(target) if target.isdigit() else None,
                "reason": reason,
            }
            for target, reason in self.worker.discovery_errors.items()
        ]
        alerts += [
            {
                "kind": "session_inactive",
                "slot": s["id"],
                "job_id": s["job_id"],
                "last_activity_at": s["last_activity_at"],
                "activity_age_seconds": s["activity_age_seconds"],
                "reason": "No native activity for 5 minutes; inspect progress/history before interrupting.",
            }
            for s in sessions
            if s["activity_age_seconds"] is not None
            and s["activity_age_seconds"] >= 300
        ]
        with self.status_lock:
            self.status = {
                "service_id": self.config.service_id,
                "last_error": self.last_error,
                "updated_at": time.time(),
                "backend_alive": bool(self.backend and not self.backend.dead),
                "sessions": sessions,
                "pools": {
                    role: {
                        "sessions": self.store.capacity(role),
                        "draining": sum(
                            s["draining"] for s in sessions if s["role"] == role
                        ),
                    }
                    for role in ("review", "worker", "classifier")
                },
                "alerts": alerts,
                "dispatch_control": {
                    **self.dispatch_control,
                    "drained": bool(self.dispatch_control["paused"] and not self.turns),
                },
                "jobs": jobs,
                "models": self.models_for({"node": "local"}),
                "nodes": self.backend.status(self.models)
                if isinstance(self.backend, Nodes)
                else [
                    {
                        "name": "local",
                        "transport": "local",
                        "state": "connected"
                        if self.backend and not self.backend.dead
                        else "recovering",
                    }
                ],
                "worker_discovery_errors": self.worker.discovery_errors.copy(),
                "discovery": {
                    "running": self.discovery.process is not None,
                    "last_success_at": self.last_scan_at,
                    "error": self.scan_error,
                },
                "queue_counts": dict(
                    self.store.db.execute(
                        "SELECT state, COUNT(*) FROM jobs GROUP BY state"
                    ).fetchall()
                ),
                "next_scan_seconds": max(0, self.next_scan - time.monotonic()),
                "cooldown_seconds": max(0, self.cooldown - time.monotonic()),
            }

    def request(self, action, arguments):
        if self.stopping:
            raise RuntimeError("service is stopping")
        future = Future()
        self.commands.put_nowait((action, arguments, future))
        return future

    def handle_commands(self):
        for _ in range(16):
            try:
                action, args, future = self.commands.get_nowait()
            except queue.Empty:
                return
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = self.command(action, args)
                future.set_result(result)
            except Exception as error:
                future.set_exception(error)

    def command(self, action, args):
        if action == "resize_pool":
            role, count = args.get("role"), args.get("sessions")
            if (
                role not in ("review", "worker", "classifier")
                or type(count) is not int
                or not 0 <= count <= 8
                or (role == "classifier" and count > 1)
            ):
                raise ValueError(
                    "pool must be review|worker and sessions an integer 0..8"
                )
            if (
                role == "worker"
                and count
                and self.config.issue_skill
                and not self.config.issue_skill.is_file()
            ):
                raise ValueError(
                    f"issue-to-pr skill missing: {self.config.issue_skill}"
                )
            defaults = {}
            for index in range(count):
                override = next(
                    (
                        o
                        for o in self.config.session_overrides
                        if o.get("role", "review") == role and o["slot"] == index
                    ),
                    {},
                )
                node = override.get("node", "local")
                definition = next(
                    (n for n in self.config.nodes if n["name"] == node), None
                )
                if node != "local" and definition is None:
                    raise ValueError(f"node unavailable in configuration: {node}")
                defaults[index] = {
                    "model": (
                        CLASSIFIER_MODEL
                        if role == "classifier"
                        else override.get(
                            "model",
                            self.config.worker_model
                            if role == "worker"
                            else self.config.model,
                        )
                    ),
                    "effort": (
                        CLASSIFIER_EFFORT
                        if role == "classifier"
                        else override.get(
                            "reasoning_effort",
                            self.config.worker_effort
                            if role == "worker"
                            else self.config.reasoning_effort,
                        )
                    ),
                    "node": node,
                    "endpoint": json.dumps(
                        {k: definition[k] for k in ("name", "ssh", "codex")},
                        sort_keys=True,
                    )
                    if definition
                    else "local",
                }
            reactivated = [
                s["id"]
                for s in self.store.slots(include_disabled=True)
                if s["role"] == role and not s["enabled"] and s["pool_index"] < count
            ]
            self.store.resize_pool(role, count, defaults)
            for slot_id in reactivated:
                if slot_id not in self.turns:
                    # It may have been disabled during backend/service restart;
                    # resume its native thread before dispatch, retaining identity.
                    self.store.slot_update(slot_id, ready=0)
            self.next_scan = 0.0
            self.refresh_status()
            LOG.warning("pool resized role=%s sessions=%s", role, count)
            return {
                "role": role,
                **self.status["pools"][role],
                "slots": [s for s in self.status["sessions"] if s["role"] == role],
            }
        if action in ("drain", "resume_dispatch"):
            reason = args.get("reason", "operator request")
            if not isinstance(reason, str) or not 1 <= len(reason) <= 500:
                raise ValueError("reason must be 1..500 characters")
            self.dispatch_control = {
                "paused": action == "drain",
                "reason": reason,
                "at": time.time(),
            }
            with self.store.db:
                self.store.db.execute(
                    "INSERT INTO meta(key,value) VALUES('dispatch_control',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(self.dispatch_control),),
                )
            LOG.warning("dispatch control action=%s reason=%s", action, reason)
            return {
                **self.dispatch_control,
                "drained": bool(self.dispatch_control["paused"] and not self.turns),
            }
        if action in (
            "pause_job",
            "resume_job",
            "retry_job",
            "cleanup_job",
            "rerun_job",
        ):
            return self.control_job(action, args)
        if action == "guide_job":
            request = self.harness.request(
                args.get("job_id"), "operator", args.get("text", "")
            )
            return {"job_id": args.get("job_id"), "request": request}
        if action == "job":
            job = self.store.job(args["job_id"])
            if job is None:
                raise ValueError("unknown job")
            row = dict(job)
            for key in ("metadata", "data", "result"):
                row[key] = json.loads(row[key]) if row[key] else None
            return row
        if action == "reconcile":
            job = self.store.job(args.get("job_id"))
            if not job or job["kind"] != "work" or job["state"] != "publishing":
                raise ValueError("reconcile requires a publishing worker job")
            return self.worker.reconcile(job)
        if action == "work":
            return self.worker.enqueue(
                args.get("target_type", "pr"), args.get("number", args.get("pr"))
            )
        if action == "model":
            if self.dispatch_control["paused"]:
                raise ValueError(
                    "service is draining; resume dispatch before starting a model-switch turn"
                )
            slot = next(
                (s for s in self.store.slots() if s["id"] == args["slot"]), None
            )
            if slot is None:
                raise ValueError("unknown session slot")
            if not slot["enabled"]:
                raise ValueError("slot is draining; enable it before changing model")
            model = args.get("model", slot["model"])
            effort = args.get("reasoning_effort", slot["effort"])
            if slot["role"] == "classifier" and (model, effort) != (
                CLASSIFIER_MODEL,
                CLASSIFIER_EFFORT,
            ):
                raise ValueError(
                    f"semantic classifier is pinned to {CLASSIFIER_MODEL}/{CLASSIFIER_EFFORT}"
                )
            entry = next(
                (
                    m
                    for m in self.models_for(slot)
                    if m.get("model", m.get("id")) == model
                ),
                None,
            )
            if not entry:
                raise ValueError("model unavailable; consult GET /api/models")
            supported = [
                e["reasoningEffort"] for e in entry.get("supportedReasoningEfforts", [])
            ]
            if effort not in supported:
                raise ValueError(
                    f"unsupported reasoning effort; available: {supported}"
                )
            if not self.native_available(slot) or not slot["session_id"]:
                raise ValueError("native session unavailable; model was not changed")
            current = self.turns.get(slot["id"])
            if slot["role"] == "worker":
                reserved = self.store.db.execute(
                    "SELECT 1 FROM jobs WHERE slot_id=? AND kind='work' AND state IN ('queued','reviewing','publishing','canceling')",
                    (slot["id"],),
                ).fetchone()
                if current or reserved:
                    raise ValueError(
                        "worker model change requires idle slot; mandatory xhigh gates cannot be lowered"
                    )
                self.store.slot_update(slot["id"], model=model, effort=effort)
                selected = next(s for s in self.store.slots() if s["id"] == slot["id"])
                self.launch(selected)
                return {
                    "slot": slot["id"],
                    "model": model,
                    "reasoning_effort": effort,
                    "effective": "next_worker_task",
                }
            if current and current[0].completed:
                self.finish(slot["id"], *current)
                current = self.turns.get(slot["id"])
            if current and (current[0].model, current[0].effort) == (model, effort):
                return {
                    "slot": slot["id"],
                    "session_id": slot["session_id"],
                    "model": model,
                    "reasoning_effort": effort,
                    "effective": "current_turn",
                    "turn_id": current[0].turn_id,
                }
            job = self.store.job(current[1]) if current and current[1] else None
            if current:
                current[0].stop()
                if not current[0].completion.wait(10):
                    if isinstance(self.backend, Nodes):
                        self.backend.close_slot(slot)
                    else:
                        self.backend.close()
                    raise RuntimeError(
                        "native interruption timed out; no replacement turn started"
                    )
                if not self.native_available(slot):
                    raise RuntimeError(
                        "backend disconnected; no replacement turn started"
                    )
                current[0].release()
                self.turns.pop(slot["id"], None)
            if job:
                pr = self.github.pr(job)
                if (
                    job["state"] == "canceling"
                    or pr["state"] != "open"
                    or pr["head"]["sha"] != job["head"]
                ):
                    self.store.update(
                        job["id"],
                        state="superseded",
                        slot_id=None,
                        error="head changed during model switch",
                    )
                    job = None
            self.store.slot_update(slot["id"], model=model, effort=effort)
            selected = next(s for s in self.store.slots() if s["id"] == slot["id"])
            if job:
                # Same job/head/comment; resume frozen evidence in the SAME thread.
                old = current[0]
                directory = self.job_dir(job) / f"model-switch-{time.time_ns()}"
                prompt = (old.directory / "prompt.txt").read_text() + (
                    f"\nThe coordinator interrupted the previous turn to change model. Continue the SAME job. "
                    f"Recover previous evidence from {old.directory}; do not repeat completed checks unnecessarily."
                )
                self.store.update(job["id"], turn_dir=str(directory))
                try:
                    turn = self.backend.start(
                        selected,
                        directory,
                        self.workspace.ensure(slot["id"]),
                        prompt,
                        SCHEMA,
                    )
                    self.turns[slot["id"]] = (turn, job["id"])
                except Exception as error:
                    self.store.update(
                        job["id"], state="queued", slot_id=None, error=str(error)
                    )
                    raise
            else:
                self.launch(selected)
            turn = self.turns[slot["id"]][0]
            self.refresh_status()
            return {
                "slot": slot["id"],
                "session_id": slot["session_id"],
                "model": model,
                "reasoning_effort": effort,
                "effective": "native_turn_started",
                "turn_id": turn.turn_id,
                "continued_job": job["id"] if job else None,
            }
        if action == "enqueue":
            pr_number = args.get("pr")
            if type(pr_number) is not int or pr_number <= 0:
                raise ValueError("pr must be a positive integer")
            if args.get("force") is not True:
                raise ValueError("manual admission requires explicit force=true")
            pr = self.github.api(f"repos/{self.config.repository}/pulls/{pr_number}")
            if pr["state"] != "open":
                raise ValueError("only open PRs can be queued")
            candidate = {
                "repo": self.config.repository,
                "number": pr_number,
                "headRefOid": pr["head"]["sha"],
                "forced": True,
            }
            key = self.store.key(candidate["repo"], pr_number, candidate["headRefOid"])
            existing_job = self.store.job(key)
            if existing_job:
                return {"enqueued": False, "job": dict(existing_job)}
            remote = self.github.find_comment(
                {
                    "id": key,
                    "repo": candidate["repo"],
                    "pr": pr_number,
                    "head": candidate["headRefOid"],
                }
            )
            self.store.enqueue(candidate)
            if remote:
                job = self.store.job(key)
                state = self.github.owned_marker(remote, job)["state"]
                self.store.update(
                    key,
                    state="done" if state == "done" else "failed",
                    comment_id=remote["id"],
                    comment_state=state,
                    error="remote claim restored; refusing duplicate review",
                )
            else:
                self.eligible.add(key)
            return {"enqueued": not bool(remote), "job": dict(self.store.job(key))}
        if action in ("message", "history", "interrupt"):
            slot_id = args["slot"]
            slot = next(
                (
                    s
                    for s in self.store.slots(include_disabled=True)
                    if s["id"] == slot_id
                ),
                None,
            )
            if not slot or not slot["session_id"] or not self.native_available(slot):
                raise ValueError("session unavailable")
            current = self.turns.get(slot_id)
            if action == "history":
                thread = self.backend.call(
                    "thread/read",
                    {"threadId": slot["session_id"], "includeTurns": False},
                )["thread"]
                page = self.backend.call(
                    "thread/items/list",
                    {
                        "threadId": slot["session_id"],
                        "limit": 20,
                        "sortDirection": "desc",
                        "cursor": args.get("cursor"),
                    },
                )
                turns = {}
                for entry in reversed(page["data"]):
                    turns.setdefault(
                        entry["turnId"], {"id": entry["turnId"], "items": []}
                    )["items"].append(entry["item"])
                return {
                    "thread": {**thread, "turns": list(turns.values())},
                    "nextCursor": page.get("nextCursor"),
                    "history_limited": True,
                }
            if action == "interrupt":
                if current:
                    if current[1]:
                        self.store.update(
                            current[1], state="canceling", error="operator interrupted"
                        )
                    current[0].error = "operator interrupted"
                    current[0].stop()
                return {"interrupted": bool(current)}
            expected = args.get("expected_turn_id")
            if expected is not None and (not current or current[0].turn_id != expected):
                raise ValueError(
                    "active turn changed; no message or idle conversation started"
                )
            text = args.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 16000:
                raise ValueError("text must contain 1..16000 characters")
            if current:
                response = current[0].steer(text)
                return {"steered": True, "session_id": slot["session_id"], **response}
            if not slot["enabled"]:
                raise ValueError("slot disabled; enable it before starting idle chat")
            if self.dispatch_control["paused"]:
                raise ValueError(
                    "service is draining; idle chat cannot start a new turn"
                )
            if (
                slot["role"] == "worker"
                and self.store.db.execute(
                    "SELECT 1 FROM jobs WHERE slot_id=? AND kind='work' AND state IN ('queued','publishing')",
                    (slot_id,),
                ).fetchone()
            ):
                raise ValueError(
                    "worker slot reserved by a task phase/delivery; chat cannot steal it"
                )
            directory = (
                self.config.state_dir
                / "slots"
                / str(slot_id)
                / f"chat-{time.time_ns()}"
            )
            turn = self.backend.start(
                slot,
                directory,
                self.slot_workspace(slot),
                "Operator conversation. Remain read-only; do not mutate GitHub or source.\n"
                + text,
            )
            self.turns[slot_id] = (turn, None)
            return {
                "steered": False,
                "session_id": slot["session_id"],
                "turn_id": turn.turn_id,
            }
        raise ValueError("unsupported action")

    def tick(self):
        self.handle_commands()
        self.harness.poll()
        if self.stopping:
            return
        if not self.backend or self.backend.dead:
            if self.backend:
                for turn, _ in self.turns.values():
                    turn.completed = True
                    turn.error = "Codex backend disconnected"
                for slot_id, (turn, key) in list(self.turns.items()):
                    self.finish(slot_id, turn, key)
                self.backend.close()
            if time.monotonic() < self.cooldown:
                return
            self.backend = self.backend_factory(self.config, self.events)
            try:
                self.models = []
                cursor = None
                while True:
                    page = self.backend.call("model/list", {"cursor": cursor})
                    self.models.extend(page.get("data", []))
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
                for slot in self.store.slots():
                    self.open_session(slot)
            except Exception:
                self.backend.close()
                raise
        if self.backend.dead:
            return
        for slot_id, (turn, key) in list(self.turns.items()):
            if turn.session_id:
                self.store.slot_update(slot_id, session_id=turn.session_id)
            job = self.store.job(key) if key else None
            size = (turn.directory / "events.jsonl").stat().st_size
            role = self.store.db.execute(
                "SELECT role FROM slots WHERE id=?", (slot_id,)
            ).fetchone()[0]
            timeout = 120 if role == "classifier" else self.config.job_timeout_seconds
            if (
                (job and job["state"] == "canceling")
                or turn.error
                or size > 64 * 1024 * 1024
                or time.monotonic() - turn.started > timeout
            ):
                turn.error = (
                    turn.error or "task canceled or execution/log limit exceeded"
                )
                turn.stop()
            self.finish(slot_id, turn, key)
        if self.dispatch_control["paused"] or self.stopping:
            return
        if time.monotonic() >= self.next_scan:
            self.next_scan = time.monotonic() + self.config.poll_seconds
            self.scan()
        self.poll_discovery()
        self.worker.poll()
        for job in self.store.jobs(("publishing",)):
            if self.stopping:
                break
            if job["retry_at"] > time.time():
                continue
            try:
                self.publish(job)
            except Exception as error:
                self.store.update(
                    job["id"], error=str(error), retry_at=time.time() + 60
                )
                LOG.warning("publication blocked job=%s: %s", job["id"], error)
        for job in self.store.jobs(("failed", "superseded")):
            if (
                job["comment_id"]
                and job["comment_state"] != job["state"]
                and job["retry_at"] <= time.time()
            ):
                try:
                    self.github.patch_comment(
                        job,
                        job["state"],
                        job["error"] or "Task stopped without a review verdict.",
                    )
                    self.store.update(job["id"], comment_state=job["state"])
                except Exception as error:
                    self.store.update(job["id"], retry_at=time.time() + 60)
                    LOG.warning("terminal comment blocked job=%s: %s", job["id"], error)
        if (
            shutil.disk_usage(self.config.state_dir).free
            < self.config.minimum_free_gib * 1024**3
        ):
            LOG.warning("dispatch paused: insufficient disk space")
            return
        for slot in self.store.slots():
            if self.backend.dead or self.stopping:
                break
            if slot["id"] in self.turns:
                continue
            if self.dispatch_control["paused"]:
                continue
            if slot["role"] == "classifier":
                if not slot["ready"]:
                    self.open_session(slot)
                else:
                    self.worker.dispatch_classifier(slot)
                continue
            if isinstance(self.backend, Nodes) and not self.backend.available(slot):
                self.store.slot_update(slot["id"], ready=0)
                self.open_session(slot)
                continue
            if not slot["ready"]:
                self.open_session(slot)
            else:
                job = self.store.claim(slot["id"], self.eligible)
                if job:
                    try:
                        self.launch(slot, job)
                    except SourceChanged as error:
                        if job["kind"] == "work":
                            self.store.dispose_evidence(job, "superseded")
                        self.store.update(
                            job["id"],
                            state="superseded",
                            slot_id=None,
                            error=str(error),
                        )
                    except Exception as error:
                        if job["kind"] == "work":
                            # Worker admission/capability failures do not stall the review pool.
                            if isinstance(error, ValueError):
                                self.worker.fail(job, error)
                            else:
                                self.worker.retry(job, error)
                            LOG.warning(
                                "worker dispatch blocked job=%s: %s", job["id"], error
                            )
                            continue
                        if (
                            self.store.job(job["id"])["attempts"]
                            > self.config.max_recoveries
                        ):
                            self.store.update(
                                job["id"],
                                state="failed",
                                slot_id=None,
                                error=str(error),
                            )
                            continue
                        # No model has started. Reconcile any ambiguous remote claim
                        # on the next dispatch before another write or model call.
                        self.store.update(
                            job["id"],
                            state="queued",
                            slot_id=slot["id"] if job["kind"] == "work" else None,
                            error=str(error),
                            retry_at=time.time() + 60,
                        )
                        LOG.warning(
                            "review dispatch blocked job=%s: %s", job["id"], error
                        )

    def models_for(self, slot):
        return (
            self.backend.models_for(slot, self.models)
            if isinstance(self.backend, Nodes)
            else self.models
        )

    def control_job(self, action, args):
        job = self.store.job(args.get("job_id"))
        if job is None:
            raise ValueError("unknown job")
        data = json.loads(job["data"])
        if "hint" in args:
            if action not in ("resume_job", "retry_job") or job["kind"] != "work":
                raise ValueError("hint requires a worker resume or retry")
            if not isinstance(args["hint"], str) or len(args["hint"]) > 8000:
                raise ValueError("hint must be text of at most 8000 characters")
        guidance = args.get("hint", data.get("operator_hint"))
        feedback = (
            job["error"]
            if job["kind"] == "work" and not data.get("paused")
            else data.get("coordinator_feedback")
        )
        current = next(
            (
                (slot, turn)
                for slot, (turn, key) in self.turns.items()
                if key == job["id"]
            ),
            None,
        )
        if current and current[1].completed is True:
            self.finish(current[0], current[1], job["id"])
            if current[0] in self.turns:
                raise ValueError("tool cleanup is unconfirmed; keep the task fence")
            job = self.store.job(job["id"])
            data = json.loads(job["data"])
            current = None
        if "hint" in args and job["state"] == "publishing":
            raise ValueError(
                "publishing has no model turn; reconcile or use a fresh-input task"
            )
        if "from_input" in args and type(args["from_input"]) is not bool:
            raise ValueError("from_input must be a boolean")
        if args.get("from_input"):
            if (
                action != "retry_job"
                or job["kind"] != "work"
                or job["state"] != "failed"
            ):
                raise ValueError("from_input requires a failed worker task")
            if current or job["state"] == "canceling":
                raise ValueError("wait for native/tool cleanup before reentry")
            if self.store.db.execute(
                "SELECT 1 FROM effects WHERE job_id=? AND attempted=1", (job["id"],)
            ).fetchone():
                raise ValueError(
                    "remote effects exist; reconcile them before restarting from input"
                )
            self.worker.check_input(job)
            # Preserve an available owned source, including dirty edits. Legacy
            # failures may have released their clone without archiving it; an
            # explicit from-input retry never borrows another task's source.
            slot_id = data.get("worker_slot")
            if not data.get("workspace_archive") and slot_id is not None:
                path = self.workspace.worker_path(slot_id)
                owner = path.parent / "worker-owner.json"
                if owner.is_file() and path.exists():
                    identity = json.loads(owner.read_text())
                    if identity.get("job") == job["id"]:
                        if slot_id in self.turns:
                            raise ValueError("owned workspace is still in use")
                        data["workspace_archive"] = self.workspace.archive_worker(
                            slot_id, job
                        )
            if data.get("workspace_archive"):
                data["workspace_archive"] = self.workspace.retire_worker_archive(
                    job, data["workspace_archive"]
                )
            record = (
                self.job_dir(job) / f"reentry-{job['attempts']}-{time.time_ns()}.json"
            )
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(json.dumps(data, indent=2))
            triage = data.get("triage", {})
            data = {
                "reentry_record": str(record),
                "recoveries": 0,
                "paused": False,
            }
            if triage.get("result", {}).get("accepted"):
                data["triage"] = triage
            if guidance:
                data["operator_hint"] = guidance
            if feedback:
                data["coordinator_feedback"] = feedback[:4000]
            self.store.update(
                job["id"],
                state="queued",
                slot_id=None,
                phase="design" if data.get("triage") else "triage",
                data=json.dumps(data),
                result=None,
                error=None,
                retry_at=0,
            )
            self.eligible.add(job["id"])
            LOG.warning(
                "job reentry from frozen input job=%s preserved=%s", job["id"], record
            )
            return self.command("job", {"job_id": job["id"]})
        if action == "rerun_job":
            if self.dispatch_control["paused"]:
                raise ValueError("service is draining; resume dispatch before rerun")
            final = data.get("final_review", {})
            approved = final.get("result", {})
            if (
                job["kind"] != "work"
                or not approved.get("accepted")
                or not approved.get("validation_complete")
                or final.get("model") != "gpt-6.1-sol"
                or final.get("effort") != "xhigh"
            ):
                raise ValueError(
                    "rerun requires the accepted frozen xhigh delivery gate"
                )
            run = args.get("run_id")
            permitted = {
                r
                for c in self.worker.causes(data["design"]["result"])
                for r in c["rerun_ids"]
            }
            if type(run) is not int or run not in permitted:
                raise ValueError("rerun is outside the approved frozen action plan")
            for cause in self.worker.causes(data["design"]["result"]):
                self.worker.cause_issue(job, cause)
            return self.worker.rerun(job, run)
        if action == "cleanup_job":
            if job["state"] == "done":
                return self.command("job", {"job_id": job["id"]})
            pending = self.store.db.execute(
                "SELECT 1 FROM effects WHERE job_id=? AND attempted=1 AND result IS NULL",
                (job["id"],),
            ).fetchone()
            if pending:
                raise ValueError(
                    "uncertain remote effects require reconciliation; cleanup cannot remove the target fence"
                )
            if current:
                raise ValueError(
                    "pause the running job and wait for cleanup acknowledgement first"
                )
            if (
                job["kind"] == "work"
                and data.get("worker_slot") is not None
                and job["state"] != "publishing"
                and not data.get("workspace_archive")
            ):
                slot_id = data["worker_slot"]
                if slot_id in self.turns:
                    raise ValueError(
                        "workspace slot is in use; refusing another live session's cleanup"
                    )
                data["workspace_archive"] = self.workspace.archive_worker(slot_id, job)
            if data.get("publication_ref"):
                self.workspace.verify_publication(
                    job, data["publication_ref"], data["candidate"]
                )
                self.workspace.publication_git(
                    "update-ref", "-d", data["publication_ref"], data["candidate"]
                )
                data.pop("publication_ref")
            data.update(paused=True)
            self.store.update(
                job["id"],
                state="superseded",
                slot_id=None,
                data=json.dumps(data),
                error="operator cleanup; artifacts preserved",
            )
        elif action == "pause_job":
            if job["state"] in ("done", "failed", "superseded"):
                raise ValueError("terminal job cannot be paused")
            data["paused"] = True
            if current:
                self.store.update(
                    job["id"],
                    state="canceling",
                    data=json.dumps(data),
                    error="operator pause requested; waiting for tool cleanup",
                )
                current[1].stop()
            else:
                if (
                    job["kind"] == "work"
                    and job["slot_id"] is not None
                    and data.get("worker_slot") is not None
                    and job["state"] != "publishing"
                ):
                    data["workspace_archive"] = self.workspace.archive_worker(
                        job["slot_id"], job
                    )
                self.store.update(
                    job["id"],
                    data=json.dumps(data),
                    slot_id=job["slot_id"] if job["state"] == "publishing" else None,
                    error="paused by operator",
                )
                if job["state"] == "publishing" and job["kind"] == "work":
                    self.worker.publish(self.store.job(job["id"]))
        else:
            if current or job["state"] == "canceling":
                raise ValueError(
                    "wait for the active turn/tool cleanup acknowledgement"
                )
            if job["state"] in ("done", "superseded"):
                raise ValueError("completed or newer-head job cannot be replayed")
            if job["kind"] == "work":
                self.worker.check_input(
                    job, allow_delivered=job["state"] == "publishing"
                )
            else:
                pr = self.github.pr(job)
                if pr["state"] != "open" or pr["head"]["sha"] != job["head"]:
                    raise SourceChanged("PR changed; cannot resume old review")
            if data.get("workspace_archive"):
                free = next(
                    (
                        s
                        for s in sorted(
                            self.store.slots(),
                            key=lambda s: s["id"] != data.get("worker_slot"),
                        )
                        if s["role"] == "worker"
                        and s["enabled"]
                        and s["ready"]
                        and s["id"] not in self.turns
                        and not self.store.db.execute(
                            "SELECT 1 FROM jobs WHERE slot_id=? AND state IN ('queued','reviewing','canceling','publishing') AND id<>?",
                            (s["id"], job["id"]),
                        ).fetchone()
                    ),
                    None,
                )
                if free is not None:
                    # Reserve before mutation; a crash cannot reassign this clone.
                    self.store.update(job["id"], state="queued", slot_id=free["id"])
                    self.workspace.restore_worker(
                        free["id"], job, data["workspace_archive"]
                    )
                    data["worker_slot"] = free["id"]
                    data.pop("workspace_archive")
                else:
                    # Admission does not require free capacity. Keep the archive
                    # until the ordinary claim/launch path acquires a slot.
                    self.store.update(job["id"], state="queued", slot_id=None)
            elif (
                job["kind"] == "work"
                and job["state"] == "failed"
                and data.get("before")
            ):
                raise ValueError(
                    "failed workspace needs explicit preservation/cleanup before resume"
                )
            data.update(paused=False, recoveries=0)
            if guidance:
                data["operator_hint"] = guidance
            else:
                data.pop("operator_hint", None)
            if feedback:
                data["coordinator_feedback"] = feedback[:4000]
            data.pop("reentry_attempts_base", None)
            data.pop("manual_reconciliation", None)
            data.pop("publication_next_at", None)
            data.pop("publication_failures", None)
            self.store.update(
                job["id"],
                state="publishing" if job["state"] == "publishing" else "queued",
                data=json.dumps(data),
                error=None,
                retry_at=0,
            )
            if (
                job["state"] == "publishing"
                and job["kind"] == "work"
                and not self.dispatch_control["paused"]
            ):
                self.worker.publish(self.store.job(job["id"]))
            else:
                self.eligible.add(job["id"])
        LOG.warning(
            "job control action=%s job=%s pr=%s head=%s",
            action,
            job["id"],
            job["pr"],
            job["head"],
        )
        return self.command("job", {"job_id": job["id"]})

    def native_available(self, slot):
        return bool(
            self.backend
            and not self.backend.dead
            and (not isinstance(self.backend, Nodes) or self.backend.available(slot))
        )

    def run(self):
        def stop(signum, frame):
            LOG.warning(
                "shutdown requested signal=%s pid=%s dispatch_control=%s",
                signal.Signals(signum).name,
                os.getpid(),
                self.dispatch_control,
            )
            self.stopping = True

        previous = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            self.recover()
            from .api import API

            api = API(self)
            self.refresh_status()
            api.start()
            while not self.stopping:
                try:
                    self.tick()
                except Exception as error:
                    self.last_error = str(error)[:2000]
                    LOG.exception("service iteration failed; no unchecked dispatch")
                    self.cooldown = time.monotonic() + 60
                self.refresh_status()
                time.sleep(0.2)
        finally:
            if "api" in locals():
                api.close()
            self.close()
            for s, handler in previous.items():
                signal.signal(s, handler)

    def close(self):
        if self.lock.closed:
            return
        self.stopping = True
        while not self.commands.empty():
            _, _, future = self.commands.get_nowait()
            if not future.done():
                future.set_exception(
                    RuntimeError("service stopped before command execution")
                )
        with self.events.condition:
            self.events.condition.notify_all()
        try:
            for turn, _ in list(self.turns.values()):
                try:
                    turn.stop()
                except Exception:
                    LOG.exception("turn stop failed; retaining ownership for recovery")
            for owned in (self.worker, self.discovery, self.backend):
                if owned:
                    try:
                        owned.close()
                    except Exception:
                        LOG.exception(
                            "owned process cleanup failed; retaining recovery record"
                        )
        finally:
            self.store.close()
            self.lock.close()
