"""Carry retained validation across worker and review tasks for one commit."""

import hashlib
import json
from pathlib import Path


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
