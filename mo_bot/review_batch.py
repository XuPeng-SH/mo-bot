"""Share a native review turn while retaining each job's existing lifecycle."""

import json
import logging
from pathlib import Path
import time

from .artifacts import scratch_env
from .codex import SCHEMA, SourceChanged, broken_session
from .remote import CleanupPending
from .validation import prepare as prepare_validation


LOG = logging.getLogger("mo-bot")
BATCH_SCHEMA = {
    "type": "object",
    "properties": {"completed_job_ids": {"type": "array", "items": {"type": "string"}}},
    "required": ["completed_job_ids"],
    "additionalProperties": False,
}


def turn_jobs(turn, key):
    return turn.__dict__.get("review_jobs") or ([key] if key else [])


def batchable(job):
    meta = json.loads(job["metadata"])
    return (
        job["kind"] == "review"
        and job["attempts"] == 0
        and not meta.get("forced")
        and not meta.get("context_review")
    )


class ReviewBatch:
    def __init__(self, service):
        self.service = service

    def launch(self, slot, job):
        s = self.service
        if s.config.batch_size != 2 or slot["node"] != "local" or not batchable(job):
            return s.launch(slot, job)
        eligible = {
            j["id"]
            for j in s.store.jobs(("queued",))
            if j["id"] in s.eligible and j["repo"] == job["repo"] and batchable(j)
        }
        if not eligible:
            return s.launch(slot, job)
        first = s.prepare_review(slot, job)
        if not first:
            return
        if not self.small(first):
            return s.start_review(slot, first)
        peer = s.store.claim(slot["id"], eligible, companion_of=job["id"])
        if peer is None:
            return s.start_review(slot, first)
        source_slot = f"{slot['id']}/batch"
        try:
            second = s.prepare_review(slot, peer, source_slot)
            if second and not self.small(second):
                s.store.update(peer["id"], state="queued", slot_id=None, error=None)
                second = None
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            s.store.update(
                peer["id"],
                state="superseded" if isinstance(error, SourceChanged) else "queued",
                slot_id=None,
                error=str(error),
                retry_at=time.time() + 60,
            )
            LOG.warning(
                "batch companion preparation deferred job=%s: %s", peer["id"], error
            )
            second = None
        if second is None:
            return s.start_review(slot, first)

        try:
            self.start(slot, first, second, source_slot)
        except Exception:
            if slot["id"] not in s.turns:
                for key in (first["job"]["id"], second["job"]["id"]):
                    s.store.update(
                        key,
                        state="queued",
                        slot_id=None,
                        retry_at=time.time() + 60,
                        error="batch preparation/start failed; retry after backend recovery",
                    )
            raise

    def start(self, slot, first, second, source_slot):
        s = self.service
        from .service import review_instructions

        items = [first, second]
        keys = [item["job"]["id"] for item in items]
        directory = first["directory"]
        prompt = (
            review_instructions(batch=True)
            + "\n\nBATCH ASSIGNMENT: apply the two-PR batch contract to exactly the independent scopes below.\n"
            + "Each review-result.json must satisfy this assessment schema: "
            + json.dumps(SCHEMA, separators=(",", ":"))
            + "\n"
        )
        for item in items:
            prompt += (
                f"\nIndependent source checkout: {item['workspace']}\n" + item["prompt"]
            )
        # The second member has its own receipts and disposable cache. Executor
        # instructions are shared; only the member-specific paths differ.
        environment = scratch_env(s.config.state_dir, second["directory"])
        cache = Path(environment["GOCACHE"]).parent
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        prepare_validation(second["directory"], environment)
        prompt += (
            f"\nFor PR {second['job']['pr']}, use {second['directory']}/check-run and "
            f"{second['directory']}/test-exec; they select that PR's cache/runtime and save its receipts.\n"
        )
        # Membership is durable before turn/start; restart can recover each member
        # without replaying a sibling that already reached publication.
        with s.store.db:
            for item, source in zip(items, (slot["id"], source_slot)):
                data = json.loads(item["job"]["data"])
                data["review_batch"] = {
                    "job_ids": keys,
                    "directory": str(directory),
                    "session_id": slot["session_id"],
                    "source_slot": source,
                }
                s.store.db.execute(
                    "UPDATE jobs SET data=?,turn_dir=?,updated=? WHERE id=?",
                    (
                        json.dumps(data),
                        str(item["directory"]),
                        time.time(),
                        item["job"]["id"],
                    ),
                )
        native_slot = {
            **dict(slot),
            "review_directories": [str(second["directory"]), str(cache)],
        }
        turn = s.backend.start(
            native_slot, directory, first["workspace"], prompt, BATCH_SCHEMA
        )
        turn.review_jobs = keys
        s.turns[slot["id"]] = (turn, keys[0])
        s.refresh_status()
        LOG.info(
            "started batch slot=%s jobs=%s PRs=%s",
            slot["id"],
            keys,
            [item["job"]["pr"] for item in items],
        )

    def small(self, prepared):
        return (
            prepared["directory"] / "change.patch"
        ).stat().st_size <= self.service.config.batch_max_patch_bytes

    def checkpoint(self, job):
        path = Path(job["turn_dir"]) / "review-result.json"
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise ValueError("invalid review checkpoint file")
        return json.loads(path.read_text())

    def save(self, job, slot, result, model, effort):
        s = self.service
        source_slot = json.loads(job["data"])["review_batch"]["source_slot"]
        if not s.workspace.clean(source_slot, head=job["head"]):
            raise SourceChanged("batch reviewer modified source")
        s.save_result(job, slot, result, model, effort)

    def settle_error(self, job, error):
        s = self.service
        data = json.loads(job["data"])
        if data.get("paused"):
            state, message = "queued", "paused by operator"
        elif job["state"] == "canceling":
            state, message = "superseded", job["error"] or str(error)
        else:
            state = (
                "queued"
                if (
                    not isinstance(error, SourceChanged)
                    and job["attempts"] - data.get("review_recovery_base", 0)
                    <= s.config.max_recoveries
                )
                else "failed"
            )
            message = str(error)
        s.store.update(
            job["id"],
            state=state,
            slot_id=None,
            error=message,
            retry_at=time.time() + 60 if state == "queued" else 0,
        )

    def finish(self, slot_id, turn, key):
        s = self.service
        slot = s.store.db.execute(
            "SELECT * FROM slots WHERE id=?", (slot_id,)
        ).fetchone()
        error = None
        if turn.completed:
            try:
                turn.result()
            except CleanupPending:
                return
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                error = exc
        reset = False
        for member in turn_jobs(turn, key):
            job = s.store.job(member)
            if job["state"] not in ("reviewing", "canceling"):
                continue
            admission = s.admission.get(str(job["pr"]), {})
            if job["state"] == "canceling" or (
                admission.get("head") == job["head"]
                and admission.get("reason") == "owner-wip"
                and not json.loads(job["metadata"]).get("forced")
            ):
                if turn.completed:
                    self.settle_error(
                        job, error or RuntimeError("review admission changed")
                    )
                continue
            try:
                # A private, atomic checkpoint can survive later sibling/model
                # failure. Missing/partial files are not completion evidence.
                result = self.checkpoint(job)
                self.save(job, slot, result, turn.model, turn.effort)
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                if turn.completed:
                    if isinstance(exc, SourceChanged):
                        s.workspace.quarantine(
                            json.loads(job["data"])["review_batch"]["source_slot"]
                        )
                    self.settle_error(
                        job, exc if isinstance(exc, SourceChanged) else error or exc
                    )
                elif isinstance(exc, SourceChanged):
                    turn.error = str(exc)
                    turn.stop()
                reset |= isinstance(exc, SourceChanged)
        if not turn.completed:
            return
        if reset or (error and broken_session(str(error))):
            s.store.slot_update(
                slot_id, session_id=None, ready=0, generation=slot["generation"] + 1
            )
        else:
            s.store.slot_update(slot_id, ready=1)
        turn.release()
        s.turns.pop(slot_id, None)

    def recover(self, job, slot):
        data = json.loads(job["data"])["review_batch"]
        directory = Path(data["directory"])
        provenance = json.loads((directory / "turn.json").read_text())
        if (
            provenance["thread"] != data["session_id"]
            or slot["session_id"] != data["session_id"]
        ):
            raise ValueError("batch recovery session mismatch")
        result = self.checkpoint(job)
        self.save(job, slot, result, provenance["model"], provenance["effort"])
