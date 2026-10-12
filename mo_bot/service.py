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
import subprocess
import sys
import time

from .codex import SCHEMA, SourceChanged, Workspace, broken_session, stop_owned
from .rpc import Backend, Events
from .github import GitHub
from .store import Store


LOG = logging.getLogger("mo-bot")
REVIEW_PROMPT = Path(__file__).with_name("review_prompt.txt").read_text()
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
        self.store = Store(
            config.state_dir / "state.sqlite3", config.service_id, config.sessions
        )
        for slot in self.store.slots():
            override = next(
                (s for s in config.session_overrides if s["slot"] == slot["id"]), {}
            )
            if not slot["model"]:
                self.store.slot_update(
                    slot["id"],
                    model=override.get("model", config.model),
                    effort=override.get("reasoning_effort", config.reasoning_effort),
                )
        self.github = github or GitHub(config)
        self.workspace = workspace or Workspace(config)
        self.backend_factory = backend_factory
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

    def recover(self):
        owner = self.config.state_dir / "backend-owner.json"
        if owner.exists():
            stop_owned(json.loads(owner.read_text()))
        for job in self.store.jobs(("reviewing", "canceling")):
            if job["state"] == "canceling":
                self.store.update(
                    job["id"],
                    state="superseded",
                    error="old revision canceled during restart",
                )
            else:
                # Recover a fully materialized result before considering another turn.
                result_path = (
                    Path(job["turn_dir"])
                    if job["turn_dir"]
                    else self.job_dir(job) / f"attempt-{job['attempts']}"
                ) / "result.json"
                events_path = result_path.parent / "events.jsonl"
                slot = self.store.db.execute(
                    "SELECT * FROM slots WHERE id=?", (job["slot_id"],)
                ).fetchone()
                try:
                    result = json.loads(result_path.read_text())
                    completed = any(
                        json.loads(line).get("method") == "turn/completed"
                        and json.loads(line)
                        .get("params", {})
                        .get("turn", {})
                        .get("status")
                        == "completed"
                        for line in events_path.read_text().splitlines()
                        if line.startswith("{")
                    )
                    if not completed or not self.workspace.clean(slot["id"]):
                        raise ValueError("no durable completed result")
                    provenance = json.loads(
                        (result_path.parent / "turn.json").read_text()
                    )
                    self.save_result(
                        job, slot, result, provenance["model"], provenance["effort"]
                    )
                except (OSError, ValueError, RuntimeError):
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
        self.eligible.clear()
        if snapshot is None:
            args = [sys.executable, "-m", "mo_bot.github", str(self.config_path)]
            process = subprocess.Popen(
                args,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                out, err = process.communicate(
                    timeout=self.config.command_timeout_seconds
                )
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise RuntimeError("GitHub discovery timed out")
            if process.returncode:
                raise RuntimeError(err.strip()[:2000])
            snapshot = json.loads(out)
        (self.config.state_dir / "snapshot.json").write_text(
            json.dumps(snapshot, indent=2)
        )
        heads = snapshot["heads"]
        for job in self.store.jobs(("queued", "reviewing", "publishing")):
            if heads.get(str(job["pr"])) != job["head"]:
                self.store.update(
                    job["id"],
                    state="canceling" if job["state"] == "reviewing" else "superseded",
                    error="PR closed or head changed",
                )
        for candidate in snapshot["candidates"]:
            key = self.store.key(
                candidate["repo"], candidate["number"], candidate["headRefOid"]
            )
            if not self.store.job(key):
                prospective = {
                    "id": key,
                    "repo": candidate["repo"],
                    "pr": candidate["number"],
                    "head": candidate["headRefOid"],
                }
                existing = self.github.find_comment(prospective)
                self.store.enqueue(candidate)
                job = self.store.job(key)
                if existing:
                    remote_state = self.github.owned_marker(existing, job)["state"]
                    self.store.update(
                        key,
                        state="done" if remote_state == "done" else "failed",
                        comment_id=existing["id"],
                        comment_state=remote_state,
                        error="remote claim restored; no duplicate review",
                    )
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
        LOG.info(
            "scan candidates=%d indeterminate=%s",
            len(snapshot["candidates"]),
            snapshot["indeterminate"],
        )

    def launch(self, slot, job=None):
        slot_id = slot["id"]
        if job:
            pr = self.github.pr(job)
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
                marker = self.github.owned_marker(comment, job)
                if not marker:
                    raise RuntimeError("created claim has no valid ownership marker")
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
                REVIEW_PROMPT
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
            workspace = self.workspace.ensure(slot_id)
            prompt = f"You are persistent mo-bot review slot {slot_id}. Future turns provide a new exact PR scope. Do not inspect files, run tools, or contact GitHub. Reply only with JSON ready=true."
            schema = WARMUP_SCHEMA
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
            job["id"], state="publishing", result=json.dumps(envelope), error=None
        )

    def finish(self, slot_id, turn, key):
        slot = next(s for s in self.store.slots() if s["id"] == slot_id)
        job = self.store.job(key) if key else None
        try:
            result = turn.result()
            if result is None:
                return
            if not self.workspace.clean(slot_id):
                raise SourceChanged("review worker modified source")
            if job and job["state"] == "canceling":
                self.store.update(key, state="superseded", error="old revision stopped")
            elif job:
                self.save_result(job, slot, result, turn.model, turn.effort)
            elif turn.schema is not None and result != {"ready": True}:
                raise ValueError("warmup failed")
            self.store.slot_update(slot_id, ready=1)
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            message = str(error)
            if not turn.completed:
                turn.stop()
            if isinstance(error, SourceChanged) and "modified" in message:
                self.workspace.quarantine(slot_id)
            if isinstance(error, SourceChanged) or broken_session(message):
                self.store.slot_update(
                    slot_id, session_id=None, ready=0, generation=slot["generation"] + 1
                )
            if job:
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
                self.store.update(key, state=state, error=message, slot_id=None)
            self.cooldown = time.monotonic() + 60
            LOG.error("turn failed slot=%s job=%s error=%s", slot_id, key, message)
        finally:
            if turn.completed:
                turn.release()
                self.turns.pop(slot_id, None)

    def publish(self, job):
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
        if job["id"] not in self.eligible:
            return  # Keep the completed result; CI refresh must not start another model turn.
        self.github.patch_comment(job, "done", report_text(json.loads(job["result"])))
        self.store.update(job["id"], state="done", comment_state="done", error=None)
        LOG.info("published PR=%s head=%s job=%s", job["pr"], job["head"], job["id"])

    def open_session(self, slot):
        workspace = self.workspace.ensure(slot["id"])
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
        for slot in self.store.slots():
            current = self.turns.get(slot["id"])
            sessions.append(
                {
                    **dict(slot),
                    "state": "running"
                    if current
                    else (
                        "idle"
                        if slot["ready"] and self.backend and not self.backend.dead
                        else "recovering"
                    ),
                    "turn_id": current[0].turn_id if current else None,
                    "active_model": current[0].model if current else None,
                    "active_effort": current[0].effort if current else None,
                    "job_id": current[1] if current else None,
                }
            )
        jobs = [dict(j) for j in self.store.jobs()[:200]]
        for job in jobs:
            job["metadata"] = json.loads(job["metadata"])
            job["result"] = json.loads(job["result"]) if job["result"] else None
        with self.status_lock:
            self.status = {
                "service_id": self.config.service_id,
                "last_error": self.last_error,
                "updated_at": time.time(),
                "backend_alive": bool(self.backend and not self.backend.dead),
                "sessions": sessions,
                "jobs": jobs,
                "models": self.models,
                "queue_counts": dict(
                    self.store.db.execute(
                        "SELECT state, COUNT(*) FROM jobs GROUP BY state"
                    ).fetchall()
                ),
                "next_scan_seconds": max(0, self.next_scan - time.monotonic()),
                "cooldown_seconds": max(0, self.cooldown - time.monotonic()),
            }

    def request(self, action, arguments):
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
        if action == "model":
            slot = next(
                (s for s in self.store.slots() if s["id"] == args["slot"]), None
            )
            if slot is None:
                raise ValueError("unknown session slot")
            model = args.get("model", slot["model"])
            effort = args.get("reasoning_effort", slot["effort"])
            entry = next(
                (m for m in self.models if m.get("model", m.get("id")) == model), None
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
            if not self.backend or self.backend.dead or not slot["session_id"]:
                raise ValueError("native session unavailable; model was not changed")
            current = self.turns.get(slot["id"])
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
                    self.backend.close()
                    raise RuntimeError(
                        "native interruption timed out; no replacement turn started"
                    )
                if self.backend.dead:
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
            slot = next((s for s in self.store.slots() if s["id"] == slot_id), None)
            if (
                not slot
                or not slot["session_id"]
                or not self.backend
                or self.backend.dead
            ):
                raise ValueError("session unavailable")
            current = self.turns.get(slot_id)
            if action == "history":
                return self.backend.call(
                    "thread/read",
                    {"threadId": slot["session_id"], "includeTurns": True},
                )
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
            directory = (
                self.config.state_dir
                / "slots"
                / str(slot_id)
                / f"chat-{time.time_ns()}"
            )
            turn = self.backend.start(
                slot,
                directory,
                self.workspace.ensure(slot_id),
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
        self.handle_commands()
        if self.backend.dead:
            return
        for slot_id, (turn, key) in list(self.turns.items()):
            if turn.session_id:
                self.store.slot_update(slot_id, session_id=turn.session_id)
            job = self.store.job(key) if key else None
            size = (turn.directory / "events.jsonl").stat().st_size
            if (
                (job and job["state"] == "canceling")
                or turn.error
                or size > 64 * 1024 * 1024
                or time.monotonic() - turn.started > self.config.job_timeout_seconds
            ):
                turn.error = (
                    turn.error or "task canceled or execution/log limit exceeded"
                )
                turn.stop()
            self.finish(slot_id, turn, key)
        if time.monotonic() < self.cooldown:
            return
        if time.monotonic() >= self.next_scan:
            self.next_scan = time.monotonic() + self.config.poll_seconds
            self.scan()
        for job in self.store.jobs(("publishing",)):
            self.publish(job)
        for job in self.store.jobs(("failed", "superseded")):
            if job["comment_id"] and job["comment_state"] != job["state"]:
                self.github.patch_comment(
                    job,
                    job["state"],
                    job["error"] or "Task stopped without a review verdict.",
                )
                self.store.update(job["id"], comment_state=job["state"])
        if (
            shutil.disk_usage(self.config.state_dir).free
            < self.config.minimum_free_gib * 1024**3
        ):
            LOG.warning("dispatch paused: insufficient disk space")
            return
        for slot in self.store.slots():
            if slot["id"] in self.turns:
                continue
            if not slot["ready"]:
                self.open_session(slot)
            else:
                job = self.store.claim(slot["id"], self.eligible)
                if job:
                    try:
                        self.launch(slot, job)
                    except SourceChanged as error:
                        self.store.update(
                            job["id"],
                            state="superseded",
                            slot_id=None,
                            error=str(error),
                        )
                    except Exception as error:
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
                            raise
                        # No model has started. Reconcile any ambiguous remote claim
                        # on the next dispatch before another write or model call.
                        self.store.update(
                            job["id"], state="queued", slot_id=None, error=str(error)
                        )
                        raise

    def run(self):
        lock = (self.config.state_dir / "service.lock").open("a+")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def stop(signum, frame):
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
            for turn, key in list(self.turns.values()):
                try:
                    turn.stop()
                except Exception:
                    LOG.exception("worker stop failed; retaining its ownership record")
            if self.backend:
                self.backend.close()
            for s, handler in previous.items():
                signal.signal(s, handler)
            lock.close()
            self.store.close()
