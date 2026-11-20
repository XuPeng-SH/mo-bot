"""Reclaim disposable task scratch without deleting source or review evidence."""

import errno
import logging
import os
import select
import stat
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .proc_inspection import referenced_jobs

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
        "GOPATH": str(cache / "go"),
        "GOWORK": "off",
    }


def _mount_id(fd):
    # Unlike st_dev, the kernel mount ID also distinguishes bind mounts on
    # the same filesystem. The descriptor remains anchored across renames.
    for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
        key, _, value = line.partition(":")
        if key == "mnt_id" and value.strip().isdecimal():
            return int(value.strip())
    raise OSError(f"cannot determine mount ID for fd {fd}")


def remove_scratch(path, stopping):
    """Remove a detached tree with bounded fds and without crossing mounts."""
    path = Path(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def open_checked(parent_fd, name, expected_mount, identity=None):
        fd = os.open(name, flags, dir_fd=parent_fd)
        try:
            if _mount_id(fd) != expected_mount:
                raise OSError(errno.EXDEV, "scratch cleanup refuses a mountpoint", name)
            info = os.fstat(fd)
            actual = (info.st_dev, info.st_ino)
            if identity is not None and actual != identity:
                raise OSError(errno.ESTALE, "scratch directory changed", name)
            return fd, actual
        except BaseException:
            os.close(fd)
            raise

    # Mount changes in this process's namespace invalidate the cached parent
    # chain, even if a bind mount exposes the same inode at the same pathname.
    mount_fd = os.open("/proc/thread-self/mounts", os.O_RDONLY | os.O_CLOEXEC)
    state_fd = None
    try:
        mount_poll = select.poll()
        mount_poll.register(mount_fd, select.POLLPRI)
        state_fd = os.open(path.parent.parent, flags)
        expected_mount = _mount_id(state_fd)
        state_info = os.fstat(state_fd)
        state_identity = (state_info.st_dev, state_info.st_ino)
        trash_fd = os.open(path.parent.name, flags, dir_fd=state_fd)
        try:
            if _mount_id(trash_fd) != expected_mount:
                raise OSError(
                    errno.EXDEV,
                    "scratch cleanup refuses a mounted trash root",
                    str(path.parent),
                )
            if stopping.is_set():
                return False
            current, identity = open_checked(trash_fd, path.name, expected_mount)
            try:
                trash_info = os.fstat(trash_fd)
                trash_identity = (trash_info.st_dev, trash_info.st_ino)
                trash_path = os.readlink(f"/proc/self/fd/{trash_fd}")
                current_path = os.path.join(trash_path, path.name)
                stack = [
                    {
                        "name": path.name,
                        "identity": identity,
                        "pending": [],
                    }
                ]

                def checked_parent(frame):
                    parent = os.open("..", flags, dir_fd=current)
                    try:
                        info = os.fstat(parent)
                        expected_parent = (
                            stack[-2]["identity"] if len(stack) > 1 else trash_identity
                        )
                        if (
                            _mount_id(parent) != expected_mount
                            or (info.st_dev, info.st_ino) != expected_parent
                        ):
                            raise OSError(
                                errno.ESTALE, "scratch directory changed", current_path
                            )
                        probe, _ = open_checked(
                            parent, frame["name"], expected_mount, frame["identity"]
                        )
                        os.close(probe)
                        return parent
                    except BaseException:
                        os.close(parent)
                        raise

                def check_visible_chain():
                    # Reopen the state and trash roots as well: either can be
                    # overmounted after their original descriptors were opened.
                    # The state path may also have been moved and reached via
                    # a new symlink in an intermediate component. Inode and
                    # mount identity alone cannot distinguish that case.
                    if os.readlink(f"/proc/self/fd/{trash_fd}") != trash_path:
                        raise OSError(
                            errno.ESTALE, "scratch directory changed", trash_path
                        )
                    ancestor = os.open(path.parent.parent, flags)
                    try:
                        info = os.fstat(ancestor)
                        if (
                            _mount_id(ancestor) != expected_mount
                            or (info.st_dev, info.st_ino) != state_identity
                        ):
                            raise OSError(
                                errno.ESTALE, "scratch directory changed", str(path)
                            )
                        child, _ = open_checked(
                            ancestor, path.parent.name, expected_mount, trash_identity
                        )
                        os.close(ancestor)
                        ancestor = child
                        for part in stack:
                            if stopping.is_set():
                                return False
                            child, _ = open_checked(
                                ancestor,
                                part["name"],
                                expected_mount,
                                part["identity"],
                            )
                            os.close(ancestor)
                            ancestor = child
                        return True
                    finally:
                        os.close(ancestor)

                def check_mount_events():
                    # Acknowledge before rewalking so another change during
                    # the walk is observed on the next poll. A mount storm is
                    # deferred instead of blocking shutdown indefinitely.
                    for _ in range(3):
                        events = mount_poll.poll(0)
                        if not events:
                            return True
                        if any(
                            mask & (select.POLLHUP | select.POLLNVAL)
                            for _, mask in events
                        ):
                            raise OSError("cannot monitor scratch mount changes")
                        os.lseek(mount_fd, 0, os.SEEK_SET)
                        if not os.read(mount_fd, 1):
                            raise OSError("cannot monitor scratch mount changes")
                        if not check_visible_chain():
                            return False
                    if mount_poll.poll(0):
                        raise OSError(errno.EAGAIN, "scratch mounts keep changing")
                    return True

                def check_current_path():
                    try:
                        actual = os.readlink(f"/proc/self/fd/{current}")
                    except OSError as error:
                        if error.errno != errno.ENAMETOOLONG:
                            raise
                        # procfs cannot render paths longer than PATH_MAX even
                        # though openat can traverse them.
                        if not check_visible_chain():
                            return False
                        return check_mount_events()
                    if actual != current_path:
                        raise OSError(
                            errno.ESTALE, "scratch directory changed", current_path
                        )
                    return check_mount_events()

                while stack:
                    if stopping.is_set():
                        return False
                    frame = stack[-1]
                    if not check_current_path():
                        return False
                    # Verify the current parent and its visible mount before any
                    # changes. Handles bind mounts created after the last scan.
                    parent = checked_parent(frame)
                    os.close(parent)
                    if not check_mount_events():
                        return False
                    os.fchmod(current, 0o700)
                    if not frame["pending"]:
                        with os.scandir(current) as entries:
                            for entry in entries:
                                if stopping.is_set():
                                    return False
                                info = entry.stat(follow_symlinks=False)
                                if stat.S_ISDIR(info.st_mode):
                                    frame["pending"].append(
                                        (entry.name, (info.st_dev, info.st_ino))
                                    )
                                    if len(frame["pending"]) >= 64:
                                        break
                                else:
                                    if not check_mount_events():
                                        return False
                                    os.unlink(entry.name, dir_fd=current)
                    if frame["pending"]:
                        name, expected = frame["pending"].pop()
                        child, child_id = open_checked(
                            current, name, expected_mount, expected
                        )
                        child_path = os.path.join(current_path, name)
                        os.close(current)
                        current = child
                        current_path = child_path
                        stack.append(
                            {
                                "name": name,
                                "identity": child_id,
                                "pending": [],
                            }
                        )
                        continue
                    if stopping.is_set():
                        return False
                    if not check_mount_events():
                        return False
                    parent = checked_parent(frame)
                    try:
                        os.rmdir(frame["name"], dir_fd=parent)
                    except BaseException:
                        os.close(parent)
                        raise
                    os.close(current)
                    current = parent
                    stack.pop()
                    current_path = os.path.dirname(current_path)
                return True
            finally:
                os.close(current)
        finally:
            os.close(trash_fd)
    finally:
        if state_fd is not None:
            os.close(state_fd)
        os.close(mount_fd)


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
        try:
            protected = referenced_jobs(self.root, set(candidates))
        except OSError as error:
            errors.append(f"scratch cleanup deferred: {error}")
            LOG.warning("%s", errors[0])
            protected = None
        if protected is not None:
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

        # Detached trees can still be held by processes missed by an older
        # scanner or opened after detachment. Check their current paths before
        # deletion; a failed jobs scan does not imply that this scan will fail.
        trash = {
            p.name for p in self.trash.iterdir() if not p.is_symlink() and p.is_dir()
        }
        self.status["errors"] = errors[:10]
        if trash:
            self.status["running"] = True
            self.future = self.executor.submit(self._delete, errors, trash)

    def _delete(self, errors, names):
        removed = 0
        if self.stopping.is_set():
            return {"removed": removed, "errors": errors[:10]}
        try:
            protected = referenced_jobs(self.trash, names)
        except OSError as error:
            errors.append(f"scratch trash cleanup deferred: {error}")
            LOG.warning("%s", errors[-1])
            return {"removed": removed, "errors": errors[:10]}
        for name in names - protected:
            if self.stopping.is_set():
                break
            try:
                # A previous tree can take a long time to delete. Renew the
                # reference check immediately before handling this tree.
                if name in referenced_jobs(self.trash, {name}):
                    continue
            except OSError as error:
                errors.append(f"scratch trash cleanup deferred: {error}")
                LOG.warning("%s", errors[-1])
                break
            path = self.trash / name
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
