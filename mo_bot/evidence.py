"""Carry retained validation across worker and review tasks for one commit."""

import hashlib
import json
from pathlib import Path
import re


def completed_checks(directory):
    """Identify new successful executions for retry pacing, never for approval."""
    completed = set()
    for path in (Path(directory) / "checks").glob("*.json"):
        try:
            if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                continue
            receipt = json.loads(path.read_text())
            source = receipt.get("source", {})
            command = receipt.get("command")
            if (
                type(receipt.get("exit_code")) is not int
                or receipt["exit_code"] != 0
                or not receipt.get("finished_at")
                or receipt.get("setup_error")
                or not source.get("head")
                or not source.get("delta_sha256")
                or not isinstance(command, list)
                or any(not isinstance(arg, str) for arg in command)
            ):
                continue
            # Slot and turn-local executors do not make rerunning a check progress.
            command = [
                re.sub(
                    r"/(?:triage|design|implement|final_review)-\d+(?=/|$)",
                    "/turn",
                    arg.replace(receipt.get("cwd") or "\0", "$SOURCE"),
                )
                for arg in command
            ]
            value = [receipt.get("kind"), command, source]
            completed.add(
                hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
            )
        except (OSError, ValueError, AttributeError, TypeError):
            # Missing/malformed receipts cannot reset stalled work or prove a pass.
            continue
    return completed


def local_validation(store, state_dir, job):
    sources = []
    for previous in store.candidate_jobs(job["repo"], job["head"]):
        data = json.loads(previous["data"])
        gate = data.get("final_review", {})
        result = gate.get("result", {})
        if (
            result.get("candidate_head") != job["head"]
            or result.get("accepted") is not True
            or result.get("validation_complete") is not True
            or gate.get("effort") != "xhigh"
        ):
            continue
        root = Path(state_dir) / "jobs" / previous["id"]
        source = {
            "job_id": previous["id"],
            "candidate_head": job["head"],
            "final_gate": gate,
            "artifacts": [],
            "unavailable": [],
        }
        # Store-owned stage paths are the discovery authority. Do not search
        # arbitrary jobs, caches or artifact contents for apparent passing tests.
        for phase in ("implement", "final_review"):
            stage = data.get(phase, {}).get("artifact")
            if not stage:
                source["unavailable"].append(f"{phase}: no retained artifact path")
                continue
            directory = Path(stage)
            try:
                if root.is_symlink() or not directory.resolve().is_relative_to(
                    root.resolve()
                ):
                    source["unavailable"].append(f"{phase}: artifact outside task")
                    continue
                files = list(directory.iterdir())
                checks = directory / "checks"
                if checks.is_dir() and not checks.is_symlink():
                    files.extend(checks.glob("*.json"))
                selected = sorted(
                    p for p in files if p.suffix in (".json", ".log", ".md", ".sh")
                )
                if len(selected) > 256:
                    source["unavailable"].append(
                        f"{phase}: inventory exceeds 256 files"
                    )
                for path in selected[:256]:
                    if path.is_symlink() or not path.is_file():
                        source["unavailable"].append(str(path))
                        continue
                    size = path.stat().st_size
                    item = {"path": str(path), "size": size}
                    # Hash small manifests/receipts; large test logs stay on disk
                    # for the reviewer to reconcile with the gate's ledger.
                    if size <= 1024 * 1024 and path.suffix != ".log":
                        item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                    source["artifacts"].append(item)
            except (OSError, RuntimeError) as error:
                source["unavailable"].append(f"{phase}: {error}")
        sources.append(source)
    return sources
