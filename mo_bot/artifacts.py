"""Reclaim disposable task scratch without deleting source or review evidence."""

from concurrent.futures import ThreadPoolExecutor
import logging
import os
from pathlib import Path
import threading
import time
import uuid


LOG = logging.getLogger("mo-bot")
TERMINAL = {"done", "failed", "superseded"}


def scratch_env(state_dir, directory):
    directory = Path(directory)
    temp = directory / "tmp"
    try:
        parts = directory.relative_to(Path(state_dir) / "jobs").parts
    except ValueError:
        parts = ()
    # A job owns one cache shared by all its phases/retries. Other native turns
    # (warmup/chat/classifier) keep their own disposable scratch.
    cache = directory.parent / "cache" if len(parts) == 2 else temp / "cache"
    return {
        "TMPDIR": str(temp),
        "GOTMPDIR": str(temp),
        "GOCACHE": str(cache / "go-build"),
        "GOMODCACHE": str(cache / "go-mod"),
        "GOWORK": "off",
    }


def referenced_jobs(root, ids, proc=Path("/proc")):
    """Protect detached local tools and container bind mounts as well as turns."""
    if not ids:
        return set()
    needles = {str(root / key): key for key in ids}
    used = set()
    for process in proc.glob("[0-9]*"):
        texts = []
        mounts = ""
        for name in ("cwd", "exe"):
            try:
                texts.append(os.readlink(process / name))
            except OSError:
                pass
        for fd in (process / "fd").glob("*"):
            try:
                texts.append(os.readlink(fd))
            except OSError:
                pass
        for name in ("cmdline", "environ", "maps", "mountinfo"):
            try:
                text = (process / name).read_bytes().decode(errors="replace")
                if name == "mountinfo":
                    mounts = text
                    text = text.replace(r"\040", " ").replace(r"\011", "\t")
                texts.append(text)
            except OSError:
                # Other users' private memory/fds are not task ownership proof;
                # visible records still apply. Exiting/zombie processes may also
                # return ESRCH/EINVAL for maps/mountinfo during this snapshot.
                pass
        for text in texts:
            for path, key in needles.items():
                if path in text:
                    used.add(key)
        # A container may bind the entire jobs tree, rather than one task.
        for line in mounts.splitlines():
            fields = line.split()
            if len(fields) > 5 and fields[3] != "/":
                mount_root = Path(
                    fields[3].replace(r"\040", " ").replace(r"\011", "\t")
                )
                if mount_root == root or mount_root in root.parents:
                    used.update(ids)
    return used


def remove_scratch(path, stopping):
    # Go module directories are read-only. Make only directories in the
    # detached scratch tree writable; never follow a symlink into user data.
    def fail(error):
        raise error

    for current, dirs, files in os.walk(
        path, topdown=False, followlinks=False, onerror=fail
    ):
        if stopping.is_set():
            return False
        os.chmod(current, 0o700)
        for name in files:
            if stopping.is_set():
                return False
            (Path(current) / name).unlink()
        for name in dirs:
            child = Path(current) / name
            if child.is_symlink():
                child.unlink()
            else:
                child.rmdir()
    path.rmdir()
    return True


class Artifacts:
    def __init__(self, state_dir):
        self.root = Path(state_dir) / "jobs"
        self.trash = Path(state_dir) / "scratch-trash"
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="scratch-gc"
        )
        self.future = None
        self.stopping = threading.Event()
        self.next_scan = 0
        self.status = {"running": False, "removed": 0, "errors": [], "last_scan": None}

    def due(self):
        return self.future.done() if self.future else time.monotonic() >= self.next_scan

    def poll(self, jobs, live_jobs):
        if self.future:
            if not self.future.done():
                return
            try:
                result = self.future.result()
                self.status.update(running=False, errors=result["errors"])
                self.status["removed"] += result["removed"]
            except Exception as error:
                self.status.update(running=False, errors=[str(error)])
                LOG.warning("scratch cleanup worker failed: %s", error)
            finally:
                self.future = None
        if time.monotonic() < self.next_scan:
            return
        self.next_scan = time.monotonic() + 60
        self.status["last_scan"] = time.time()
        if self.root.is_symlink() or self.trash.is_symlink():
            raise ValueError("scratch cleanup refuses symlinked jobs/trash root")
        eligible = {j["id"] for j in jobs if j["state"] in TERMINAL} - live_jobs
        self.trash.mkdir(exist_ok=True, mode=0o700)
        errors = []
        candidates = {}
        # Import lazily to reuse the worker's phase authority without coupling
        # native RPC module initialization to the delivery coordinator.
        from .worker import PHASES

        for key in eligible:
            if Path(key).name != key or key in (".", ".."):
                continue
            root = self.root / key
            if root.is_symlink() or not root.is_dir():
                continue
            paths = [root / "cache"]
            for child in root.iterdir():
                phase, _, number = child.name.rpartition("-")
                if (
                    phase in ("attempt", *PHASES)
                    and number.isdecimal()
                    and not child.is_symlink()
                    and child.is_dir()
                ):
                    paths.append(child / "tmp")
            disposable = [p for p in paths if not p.is_symlink() and p.is_dir()]
            if disposable:
                candidates[key] = disposable
        protected = referenced_jobs(self.root, set(candidates))
        for key, paths in candidates.items():
            if key in protected:
                continue
            for path in paths:
                try:
                    # Detach synchronously under the coordinator's ownership.
                    # A retry may now create fresh scratch at the original path.
                    path.rename(self.trash / uuid.uuid4().hex)
                except OSError as error:
                    errors.append(f"{path}: {error}")
        self.status["errors"] = errors[:10]
        if any(self.trash.iterdir()):
            self.status["running"] = True
            self.future = self.executor.submit(self._delete, errors)

    def _delete(self, errors):
        removed = 0
        for path in self.trash.iterdir():
            if path.is_symlink() or not path.is_dir():
                continue
            try:
                if not remove_scratch(path, self.stopping):
                    break
                removed += 1
            except OSError as error:
                errors.append(f"{path}: {error}")
                LOG.warning("scratch cleanup deferred: %s: %s", path, error)
        return {"removed": removed, "errors": errors[:10]}

    def close(self):
        # Stop between files; a partially deleted detached tree is retried after
        # restart. Never make service shutdown wait for an entire large cache.
        self.stopping.set()
        self.executor.shutdown(wait=True)
