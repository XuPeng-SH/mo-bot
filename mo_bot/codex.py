import os
import json
import hashlib
from pathlib import Path
import signal
import subprocess
import time


class SourceChanged(RuntimeError):
    pass


def completed_result(directory, session_id):
    """Recover only the completed native turn that owns this artifact directory."""
    directory = Path(directory)
    provenance = json.loads((directory / "turn.json").read_text())
    if provenance["thread"] != session_id:
        raise ValueError("recovery session mismatch")
    with (directory / "events.jsonl").open() as stream:
        for line in stream:
            event = json.loads(line)
            params = event.get("params", {})
            if (
                event.get("method") == "turn/completed"
                and params.get("threadId") == session_id
                and params.get("turn", {}).get("id") == provenance["turn"]
                and params.get("turn", {}).get("status") == "completed"
            ):
                return json.loads((directory / "result.json").read_text()), provenance
    raise ValueError("no durable completed native turn")


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


def stop_owned(owner, grace=5, *, descendants=False):
    if not isinstance(owner, dict) or not isinstance(owner.get("pid"), int):
        return
    if process_identity(owner["pid"]) != owner:
        return
    children = []
    if descendants:
        parents = {}
        for path in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = path.read_text().rsplit(")", 1)[1].split()
                parents.setdefault(int(fields[1]), []).append(int(path.parent.name))
            except (OSError, ValueError, IndexError):
                continue
        pending = [owner["pid"]]
        while pending:
            pid = pending.pop()
            for child in parents.get(pid, []):
                identity = process_identity(child)
                if identity:
                    children.append(identity)
                    pending.append(child)
    try:
        if os.getpgid(owner["pid"]) != owner["pid"]:
            raise RuntimeError("saved worker is not its process-group leader")
        os.killpg(owner["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and process_identity(owner["pid"]) == owner:
        time.sleep(0.05)
    if process_identity(owner["pid"]) == owner:
        try:
            os.killpg(owner["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
    for identity in reversed(children):
        if process_identity(identity["pid"]) == identity:
            try:
                os.kill(identity["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
    if children:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            alive = []
            for identity in children:
                if process_identity(identity["pid"]) == identity:
                    try:
                        state = (
                            Path(f"/proc/{identity['pid']}/stat")
                            .read_text()
                            .rsplit(")", 1)[1]
                            .split()[0]
                        )
                        if state != "Z":
                            alive.append(identity)
                    except OSError:
                        pass
            if not alive:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError(
                "owned command descendants did not stop; retaining recovery fence"
            )


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

    def worker_path(self, slot):
        return self.config.state_dir / "slots" / str(slot) / "worker-source"

    def worker_ensure(self, slot):
        path = self.worker_path(slot)
        owner = path.parent / "worker-owner.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        identity = {"service": self.config.service_id, "slot": slot, "path": str(path)}
        if not path.exists():
            self.git("clone", "--no-hardlinks", str(self.config.source_dir), str(path))
            owner.write_text(json.dumps(identity))
        if (
            path.is_symlink()
            or not owner.is_file()
            or any(
                json.loads(owner.read_text()).get(k) != v for k, v in identity.items()
            )
        ):
            raise SourceChanged("worker checkout ownership mismatch")
        if not (path / ".git").is_dir() or (path / ".git").is_symlink():
            raise SourceChanged("worker Git metadata must be independent")
        if (
            Path(self.worker_git(slot, "rev-parse", "--show-toplevel")).resolve()
            != path
            or (path / ".git/objects/info/alternates").exists()
        ):
            raise SourceChanged("worker Git ownership/objects are not independent")
        self.worker_git(slot, "config", "core.hooksPath", "/dev/null")
        return path

    def worker_git(self, slot, *args, check=True):
        return self.owned_git(self.worker_path(slot), *args, check=check)

    def publication_git(self, *args, check=True):
        return self.owned_git(self.config.source_dir, *args, check=check)

    def owned_git(self, path, *args, check=True):
        env = {
            **os.environ,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EDITOR": "true",
        }
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(path),
                *args,
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
        )
        if check and result.returncode:
            raise RuntimeError(
                result.stderr.strip()[:2000] or result.stdout.strip()[:2000]
            )
        return result.stdout.strip() if check else result

    def publication_ref(self, job):
        return f"refs/mo-bot/{self.config.service_id}/delivery/{job['id']}"

    def freeze_publication(self, slot, job, candidate):
        ref = self.publication_ref(job)
        existing = self.publication_git("rev-parse", "--verify", ref, check=False)
        if existing.returncode == 0:
            if existing.stdout.strip() != candidate:
                raise SourceChanged("frozen publication ref changed")
            return ref
        state = self.worker_state(slot)
        if (
            state["head"] != candidate
            or state["branch"] != "mo-bot/work/" + job["id"]
            or state["status"]
        ):
            raise SourceChanged("reviewed candidate changed before publication freeze")
        self.worker_ensure(slot)
        self.publication_git(
            "fetch", "--no-tags", str(self.worker_path(slot)), candidate + ":" + ref
        )
        if self.publication_git("rev-parse", ref) != candidate:
            raise SourceChanged("publication freeze identity mismatch")
        return ref

    def verify_publication(self, job, ref, candidate):
        if (
            ref != self.publication_ref(job)
            or self.publication_git("rev-parse", ref) != candidate
        ):
            raise SourceChanged("frozen publication candidate changed")

    def archive_worker(self, slot, job):
        destination = self.config.state_dir / "jobs" / job["id"] / "saved-source"
        if destination.exists():
            owner = json.loads(
                (destination.parent / "saved-source-owner.json").read_text()
            )
            if (
                owner.get("service") != self.config.service_id
                or owner.get("job") != job["id"]
            ):
                raise SourceChanged("archived source ownership mismatch")
            return str(destination)
        path = self.worker_path(slot)
        owner = json.loads((path.parent / "worker-owner.json").read_text())
        if (
            owner.get("job") != job["id"]
            or owner.get("service") != self.config.service_id
            or owner.get("path") != str(path)
        ):
            raise SourceChanged(
                "worker source belongs to another task; refusing cleanup"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        (destination.parent / "saved-source-owner.json").write_text(json.dumps(owner))
        path.rename(destination)
        return str(destination)

    def restore_worker(self, slot, job, archive):
        expected = self.config.state_dir / "jobs" / job["id"] / "saved-source"
        if Path(archive) != expected or expected.is_symlink():
            raise SourceChanged("invalid archived source path")
        owner = json.loads((expected.parent / "saved-source-owner.json").read_text())
        if (
            owner.get("service") != self.config.service_id
            or owner.get("job") != job["id"]
        ):
            raise SourceChanged("archived source ownership mismatch")
        path = self.worker_path(slot)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not expected.exists():
            # Recover a crash after rename but before ownership/checkpoint write.
            if (
                path.is_symlink()
                or not (path / ".git").is_dir()
                or self.worker_git(slot, "branch", "--show-current")
                != "mo-bot/work/" + job["id"]
            ):
                raise SourceChanged(
                    "preserved source missing; no restoration authorized"
                )
            (path.parent / "worker-owner.json").write_text(
                json.dumps(
                    {
                        "service": self.config.service_id,
                        "slot": slot,
                        "path": str(path),
                        "job": job["id"],
                    }
                )
            )
            return path
        if path.exists():
            self.worker_ensure(slot)
            path.rename(path.parent / f"worker-quarantine-{time.time_ns()}")
        expected.rename(path)
        (path.parent / "worker-owner.json").write_text(
            json.dumps(
                {
                    "service": self.config.service_id,
                    "slot": slot,
                    "path": str(path),
                    "job": job["id"],
                }
            )
        )
        return path

    def worker_prepare(self, slot, job):
        path = self.worker_ensure(slot)
        owner = path.parent / "worker-owner.json"
        data = json.loads(owner.read_text())
        meta = json.loads(job["metadata"])
        if data.get("job") == job["id"]:
            return path
        if self.worker_git(slot, "status", "--porcelain"):
            # An independent clone can be moved without corrupting worktree registration.
            destination = path.parent / f"worker-quarantine-{time.time_ns()}"
            path.rename(destination)
            path = self.worker_ensure(slot)
            data = json.loads(owner.read_text())
        url = "https://github.com/" + job["repo"] + ".git"
        if job["target_type"] == "pr":
            self.worker_git(
                slot,
                "fetch",
                "--no-tags",
                url,
                f"+refs/pull/{job['pr']}/head:refs/mo-bot/input",
            )
            if self.worker_git(slot, "rev-parse", "refs/mo-bot/input") != job["head"]:
                raise SourceChanged("PR changed during worker fetch")
        else:
            self.worker_git(slot, "fetch", "--no-tags", url, job["head"])
        base = meta["identity"]["base_sha"]
        if self.worker_git(
            slot, "cat-file", "-e", base + "^{commit}", check=False
        ).returncode:
            self.worker_git(slot, "fetch", "--no-tags", url, base)
        self.worker_git(slot, "checkout", "-B", "mo-bot/work/" + job["id"], job["head"])
        data["job"] = job["id"]
        owner.write_text(json.dumps(data))
        return path

    def worker_state(self, slot):
        path = self.worker_path(slot)
        return {
            "head": self.worker_git(slot, "rev-parse", "HEAD"),
            "branch": self.worker_git(slot, "branch", "--show-current"),
            "status": self.worker_git(slot, "status", "--porcelain"),
            "index": hashlib.sha256(
                self.worker_git(slot, "ls-files", "--stage").encode()
            ).hexdigest(),
            "config": hashlib.sha256((path / ".git/config").read_bytes()).hexdigest(),
            "source": hashlib.sha256(
                self.worker_git(slot, "diff", "HEAD", "--binary").encode()
            ).hexdigest(),
        }

    def worker_rebase(self, slot, base):
        result = self.worker_git(slot, "rebase", base, check=False)
        if result.returncode and not self.worker_git(
            slot, "diff", "--name-only", "--diff-filter=U"
        ):
            raise RuntimeError(result.stderr.strip()[:2000])
        return result.returncode == 0

    def worker_finalize(self, slot, message):
        path = self.worker_path(slot)
        if (
            "\n" in message
            or len(message) > 200
            or not message.startswith(
                ("fix:", "fix(", "feat:", "feat(", "test:", "test(")
            )
        ):
            raise ValueError("worker commit needs a Conventional Commit title")
        self.worker_git(slot, "add", "--all")
        if (path / ".git/rebase-merge").exists() or (
            path / ".git/rebase-apply"
        ).exists():
            result = self.worker_git(slot, "rebase", "--continue", check=False)
            if result.returncode:
                if self.worker_git(slot, "diff", "--name-only", "--diff-filter=U"):
                    return None
                raise RuntimeError(result.stderr.strip()[:2000])
        if self.worker_git(slot, "diff", "--cached", "--name-only"):
            self.worker_git(slot, "commit", "-m", message)
        if self.worker_git(slot, "status", "--porcelain"):
            raise SourceChanged("worker candidate is not clean")
        return self.worker_git(slot, "rev-parse", "HEAD")


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
            "no rollout found",
            "session not found",
            "thread not found",
            "invalid session",
            "failed to load rollout",
            "failed to parse rollout",
            "conversation changed identity",
            "review result identity mismatch",
        )
    )
