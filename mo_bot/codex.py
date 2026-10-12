import os
from pathlib import Path
import signal
import subprocess
import time


class SourceChanged(RuntimeError):
    pass


def process_identity(pid):
    """Linux process identity; never signal a PID solely because it was saved."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {
            "pid": pid,
            "start_ticks": fields[19],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except (OSError, IndexError):
        return None


def stop_owned(owner, grace=5):
    if not isinstance(owner, dict) or not isinstance(owner.get("pid"), int):
        return
    if process_identity(owner["pid"]) != owner:
        return
    try:
        if os.getpgid(owner["pid"]) != owner["pid"]:
            raise RuntimeError("saved worker is not its process-group leader")
        os.killpg(owner["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and process_identity(owner["pid"]) == owner:
        time.sleep(0.05)
    if process_identity(owner["pid"]) == owner:
        try:
            os.killpg(owner["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass


class Workspace:
    def __init__(self, config):
        self.config = config

    def git(self, *args, cwd=None):
        result = subprocess.run(
            ["git", "-C", str(cwd or self.config.source_dir), *args],
            text=True,
            capture_output=True,
            timeout=120,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[:2000])
        return result.stdout.strip()

    def path(self, slot):
        return self.config.state_dir / "slots" / str(slot) / "source"

    def clean(self, slot):
        path = self.path(slot)
        return not path.exists() or not self.git("status", "--porcelain", cwd=path)

    def ensure(self, slot, head=None):
        path = self.path(slot)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self.git("worktree", "add", "--detach", str(path), head or "HEAD")
        if Path(self.git("rev-parse", "--show-toplevel", cwd=path)).resolve() != path:
            raise RuntimeError("slot worktree ownership mismatch")
        if not self.clean(slot):
            raise SourceChanged(
                "slot contains source changes; preserving it for inspection"
            )
        if head:
            self.git("checkout", "--detach", head, cwd=path)
        return path

    def prepare(self, slot, job, pr, directory):
        remote = "https://github.com/" + job["repo"] + ".git"
        ref = f"refs/mo-bot/{self.config.service_id}/pr/{job['pr']}"
        self.git("fetch", "--no-tags", remote, f"+refs/pull/{job['pr']}/head:{ref}")
        if self.git("rev-parse", ref) != job["head"]:
            raise SourceChanged("PR head changed during fetch")
        base = pr["base"]["sha"]
        try:
            self.git("cat-file", "-e", base + "^{commit}")
        except RuntimeError:
            self.git("fetch", "--no-tags", remote, base)
        path = self.ensure(slot, job["head"])
        merge_base = self.git("merge-base", job["head"], base, cwd=path)
        (directory / "change.patch").write_text(
            self.git("diff", "--find-renames", merge_base, job["head"], cwd=path)
        )
        (directory / "change-stat.txt").write_text(
            self.git("diff", "--stat", merge_base, job["head"], cwd=path)
        )
        return path, merge_base

    def quarantine(self, slot):
        path = self.path(slot)
        if path.exists():
            destination = path.parent / f"quarantine-{time.time_ns()}"
            self.git("worktree", "move", str(path), str(destination))


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "job_id": {"type": "string"},
        "head_sha": {"type": "string"},
        "decision": {
            "type": "string",
            "enum": ["approve", "request_changes", "needs_validation"],
        },
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "trigger": {"type": "string"},
                    "impact": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": [
                    "priority",
                    "file",
                    "line",
                    "trigger",
                    "impact",
                    "evidence",
                ],
            },
        },
        "validation": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "job_id",
        "head_sha",
        "decision",
        "summary",
        "findings",
        "validation",
        "limitations",
    ],
}


def broken_session(message):
    lower = message.lower()
    return any(
        text in lower
        for text in (
            "no session found",
            "session not found",
            "thread not found",
            "invalid session",
            "failed to load rollout",
            "failed to parse rollout",
            "conversation changed identity",
            "review result identity mismatch",
        )
    )
