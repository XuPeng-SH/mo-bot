#!/usr/bin/python3 -I
"""Inspect process references for task scratch cleanup."""

import errno
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
from pathlib import Path

HELPER = Path("/usr/local/libexec/mo-bot-proc-scan")
MAX_REQUEST = 4 * 1024 * 1024
TRUST_FILE = "scratch-inspection-trust.json"
MAX_TRUST_BYTES = 1024 * 1024
TRUST_FIELDS = ("pid", "start_ticks", "namespace", "cgroup", "mount_id", "device")
try:
    SOURCE_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).digest()
except OSError:
    # Cleanup is optional maintenance; an unavailable source file must not
    # prevent the rest of the service from starting.
    SOURCE_DIGEST = None


class InspectionTimeout(Exception):
    pass


def _proc_error(path, error):
    # Processes and file descriptors may disappear while /proc is traversed.
    # Permission failures, however, are not evidence that a task is unused.
    if error.errno in (errno.ENOENT, errno.ESRCH):
        return
    raise OSError(f"cannot inspect {path}: {error.strerror or error}") from error


def _proc_link(path):
    try:
        return os.readlink(path)
    except OSError as error:
        _proc_error(path, error)
        return ""


def _proc_text(path):
    try:
        return path.read_bytes().decode(errors="replace")
    except OSError as error:
        _proc_error(path, error)
        return ""


def _systemd_home_mount(process, fields, units, proc=None):
    """Recognize only a verified systemd protection mount, not a task bind."""
    if not (fields[4] == "/home" and "ro" in fields[5].split(",")):
        return False
    try:
        cgroup = (process / "cgroup").read_text(errors="replace").strip()
        match = re.fullmatch(
            r"0::/system\.slice/([A-Za-z0-9][A-Za-z0-9_.:@-]*\.service)", cgroup
        )
        if not match:
            return False
        unit = match.group(1)
        if unit not in units:
            # Cache failures too: a single unavailable unit must not cause a
            # systemctl invocation for each of its processes.
            units[unit] = None
            names = (
                "MainPID",
                "ProtectHome",
                "BindPaths",
                "BindReadOnlyPaths",
                "RootDirectory",
                "RootImage",
                "ReadOnlyPaths",
                "ReadWritePaths",
                "ExecPaths",
                "NoExecPaths",
                "InaccessiblePaths",
                "TemporaryFileSystem",
                "PrivateUsers",
            )
            result = subprocess.run(
                [
                    "/usr/bin/systemctl",
                    "--no-pager",
                    "show",
                    *(f"--property={name}" for name in names),
                    "--",
                    unit,
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=2,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
            )
            values = dict(
                line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
            )
            pid = values.get("MainPID", "")
            empty = names[2:-1]
            if not (
                pid.isdecimal()
                and int(pid) > 0
                and values.get("ProtectHome") == "read-only"
                and values.get("PrivateUsers") == "no"
                and all(values.get(name) == "" for name in empty)
            ):
                units[unit] = None
            else:
                main = (proc or process.parent) / pid
                status = (main / "status").read_text(errors="replace")
                caps = next(
                    line.split()[1]
                    for line in status.splitlines()
                    if line.startswith("CapBnd:")
                )
                # A service able to create new mounts cannot be classified
                # from its original systemd settings alone.
                if (
                    int(caps, 16) & (1 << 21)
                    or (main / "cgroup").read_text(errors="replace").strip() != cgroup
                ):  # CAP_SYS_ADMIN
                    units[unit] = None
                else:
                    mount_ids = set()
                    for line in (
                        (main / "mountinfo").read_text(errors="replace").splitlines()
                    ):
                        main_fields = line.split()
                        if (
                            len(main_fields) > 5
                            and main_fields[3:5] == fields[3:5]
                            and "ro" in main_fields[5].split(",")
                        ):
                            mount_ids.add(main_fields[0])
                    units[unit] = (os.readlink(main / "ns/mnt"), mount_ids)
        if not units[unit]:
            return False
        namespace, mount_ids = units[unit]
        return os.readlink(process / "ns/mnt") == namespace and fields[0] in mount_ids
    except (OSError, ValueError, IndexError, StopIteration, subprocess.SubprocessError):
        # An unverified mount remains a task-protection fence.
        return False


def _require_visible_proc(mountinfo):
    mounts = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 10 or fields[4] != "/proc" or "-" not in fields:
            continue
        separator = fields.index("-")
        if separator + 3 >= len(fields) or fields[separator + 1] != "proc":
            continue
        mounts.append(fields[5].split(",") + fields[separator + 3].split(","))
    if not mounts:
        raise OSError("cannot verify /proc mount visibility")
    if any(
        option.startswith("hidepid=") and option not in ("hidepid=0", "hidepid=off")
        for options in mounts
        for option in options
    ):
        raise OSError("/proc hidepid may hide processes from the inspector")


def _unescape_mount_path(value):
    return re.sub(r"\\(040|011|012|134)", lambda m: chr(int(m[1], 8)), value)


def _filesystem_mappings(root, mountinfo):
    """Map the host jobs path to paths relative to its mounted filesystem."""
    matches = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 6:
            continue
        point = Path(_unescape_mount_path(fields[4]))
        if not point.is_absolute() or (point != root and point not in root.parents):
            continue
        origin = Path(_unescape_mount_path(fields[3]))
        if not origin.is_absolute():
            continue
        matches.append(
            (
                len(point.parts),
                fields[2],
                origin / root.relative_to(point),
                origin,
                point,
                fields[0],
            )
        )
    if not matches:
        raise OSError(f"cannot map jobs directory to a filesystem: {root}")
    depth = max(item[0] for item in matches)
    return [item[1:] for item in matches if item[0] == depth]


def _process_views(process, require_status):
    """Inspect every live thread, including independent fd/fs namespaces."""

    def state(path):
        text = _proc_text(path / "status")
        if not text:
            if require_status and path.exists():
                raise OSError(f"cannot inspect {path}/status")
            return None
        for line in text.splitlines():
            if line.startswith("State:"):
                parts = line.split()
                if len(parts) >= 2:
                    return parts[1]
                break
        if require_status:
            raise OSError(f"cannot determine process state from {path}/status")
        return None

    leader_state = state(process)
    task_dir = process / "task"
    try:
        with os.scandir(task_dir) as entries:
            threads = [task_dir / e.name for e in entries if e.name.isdecimal()]
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ESRCH) and not process.exists():
            return []
        if error.errno == errno.ENOENT and not require_status and leader_state != "Z":
            # Lightweight synthetic proc fixtures may only provide a leader.
            return [process]
        raise OSError(
            f"cannot inspect {task_dir}: {error.strerror or error}"
        ) from error
    if not threads and process.exists():
        raise OSError(f"cannot enumerate live threads in {task_dir}")
    live = []
    for thread in threads:
        thread_state = state(thread)
        if thread_state is None:
            if thread.exists():
                raise OSError(f"cannot determine process state from {thread}/status")
            continue
        if thread_state not in ("Z", "X", "x"):
            live.append(process if thread.name == process.name else thread)
    return live


def _owns_state_lock(root, process, view, descriptors):
    """Recognize only the coordinator holding this state's exclusive lock."""
    lock = root.parent / "service.lock"
    candidates = [name for name, target in descriptors if target == str(lock)]
    if not candidates:
        return False
    try:
        lock_info = lock.lstat()
        root_info = root.lstat()
    except OSError as error:
        _proc_error(lock, error)
        return False
    if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != root_info.st_uid:
        return False
    expected = (
        os.major(lock_info.st_dev),
        os.minor(lock_info.st_dev),
        lock_info.st_ino,
    )
    for name in candidates:
        info = _proc_text(view / "fdinfo" / name)
        for line in info.splitlines():
            fields = line.split()
            if (
                len(fields) < 7
                or fields[0] != "lock:"
                or fields[2:5] != ["FLOCK", "ADVISORY", "WRITE"]
                or fields[5] != process.name
            ):
                continue
            try:
                major, minor, inode = fields[6].split(":")
                actual = (int(major, 16), int(minor, 16), int(inode))
            except ValueError:
                continue
            if actual == expected:
                return True
    return False


def _service_cgroup(value):
    if not isinstance(value, str) or not value.startswith("0::/") or len(value) > 4096:
        return False
    parts = value[4:].split("/")
    return (
        all(
            part not in (".", "..") and re.fullmatch(r"[A-Za-z0-9_.:@\\-]+", part)
            for part in parts
        )
        and any(part.endswith(".service") for part in parts)
        and not any(part.endswith(".scope") for part in parts)
    )


def _trust_key(record):
    if not isinstance(record, dict) or set(record) != set(TRUST_FIELDS):
        raise ValueError("invalid namespace trust entry")
    if any(
        type(record[key]) is not int or record[key] <= 0
        for key in ("pid", "start_ticks", "namespace", "mount_id")
    ):
        raise ValueError("invalid namespace trust identity")
    if (
        not _service_cgroup(record["cgroup"])
        or not isinstance(record["device"], str)
        or not re.fullmatch(r"[0-9]+:[0-9]+", record["device"])
    ):
        raise ValueError("namespace trust requires an exact service cgroup and device")
    return tuple(record[key] for key in TRUST_FIELDS)


def _boot_id(proc):
    value = _proc_text(proc / "sys/kernel/random/boot_id").strip()
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value):
        raise OSError("cannot verify host boot identity")
    return value


def _load_trust(root, uid, proc):
    """Read only an owner-controlled, non-symlinked and generation-bound policy."""
    state_fd = os.open(
        root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:
        state = os.fstat(state_fd)
        if state.st_uid != uid or state.st_mode & 0o022:
            raise OSError("scratch trust requires an owner-controlled state directory")
        try:
            fd = os.open(
                TRUST_FILE,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=state_fd,
            )
        except FileNotFoundError:
            return frozenset()
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != uid
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size > MAX_TRUST_BYTES
            ):
                raise OSError(
                    "scratch trust policy has unsafe ownership or permissions"
                )
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(MAX_TRUST_BYTES + 1)
            if len(raw) > MAX_TRUST_BYTES:
                raise OSError("scratch trust policy exceeds size limit")
            after = os.fstat(fd)
            current = os.stat(TRUST_FILE, dir_fd=state_fd, follow_symlinks=False)
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(
                getattr(info, name) != getattr(after, name)
                or getattr(info, name) != getattr(current, name)
                for name in fields
            ):
                raise OSError("scratch trust policy changed during inspection")
        finally:
            os.close(fd)
    finally:
        os.close(state_fd)

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate policy field")
            result[key] = value
        return result

    try:
        policy = json.loads(raw, object_pairs_hook=unique_object)
        if (
            not isinstance(policy, dict)
            or set(policy) != {"version", "boot_id", "entries"}
            or type(policy["version"]) is not int
            or policy["version"] != 1
            or not isinstance(policy["entries"], list)
            or len(policy["entries"]) > 4096
        ):
            raise ValueError("invalid policy format")
        if policy["boot_id"] != _boot_id(proc):
            raise ValueError("policy belongs to another boot")
        entries = [_trust_key(entry) for entry in policy["entries"]]
        if len(set(entries)) != len(entries):
            raise ValueError("duplicate policy entry")
        return frozenset(entries)
    except (TypeError, ValueError) as error:
        raise OSError(f"invalid scratch trust policy: {error}") from error


def _generation(process, view):
    text = _proc_text(process / "stat")
    try:
        if int(text.split(" ", 1)[0]) != int(process.name):
            raise ValueError("wrong pid")
        start = int(text.rsplit(")", 1)[1].split()[19])
    except (ValueError, IndexError) as error:
        raise OSError(f"cannot verify process identity for {process}") from error
    namespace = _proc_link(view / "ns/mnt")
    match = re.fullmatch(r"mnt:\[([0-9]+)\]", namespace)
    cgroup = _proc_text(view / "cgroup").strip()
    if start <= 0 or not match or not cgroup or "\n" in cgroup:
        raise OSError(f"cannot verify namespace identity for {view}")
    return start, int(match.group(1)), cgroup


def _root_identity(process, view, fields, before):
    """Bind an approval to a stable process generation and mount namespace."""
    mounts = _proc_text(view / "mountinfo")
    if not any(
        len(parts) > 4 and parts[0] == fields[0] and parts[2:5] == fields[2:5]
        for parts in (line.split() for line in mounts.splitlines())
    ):
        raise OSError(f"mount changed while inspecting {view}")
    if _generation(process, view) != before:
        raise OSError(f"namespace changed while inspecting {view}")
    try:
        return {
            "pid": int(process.name),
            "start_ticks": before[0],
            "namespace": before[1],
            "cgroup": before[2],
            "mount_id": int(fields[0]),
            "device": fields[2],
        }
    except ValueError as error:
        raise OSError(f"invalid mount identity for {view}") from error


def _scan(root, ids, proc, *, trusted=frozenset(), blockers=None):
    """Protect detached local tools and container bind mounts as well as turns."""
    if not ids:
        return set()
    mountinfo = _proc_text(proc / "self/mountinfo")
    if proc == Path("/proc"):
        _require_visible_proc(mountinfo)
    if mountinfo:
        mappings = _filesystem_mappings(root, mountinfo)
    elif proc != Path("/proc"):
        # Synthetic proc fixtures without mount metadata use host-style paths.
        mappings = [(None, root, Path("/"), Path("/"), None)]
    else:
        raise OSError("cannot read process mount information")
    host_namespace = _proc_link(proc / "self/ns/mnt") if mountinfo else ""
    # Look for the common jobs prefix once per text, then match the task id.
    # A naive scan of every full path for every process becomes expensive as
    # the retained job history grows.
    prefix = str(root).rstrip("/") + "/"
    root_refs = {str(root)}
    if root.name in ("jobs", "scratch-trash"):
        root_refs.add(str(root.parent))
    trie = {}
    for key in ids:
        node = trie
        for char in key:
            node = node.setdefault(char, {})
        node[None] = key
    used = set()
    units = {}
    try:
        with os.scandir(proc) as entries:
            processes = [
                proc / entry.name for entry in entries if entry.name.isdecimal()
            ]
    except OSError as error:
        raise OSError(f"cannot enumerate {proc}: {error.strerror or error}") from error
    for process in processes:
        for view in _process_views(process, proc == Path("/proc")):
            mounts = _proc_text(view / "mountinfo")
            # Capture the generation before inspecting handles and cwd. If a
            # PID or namespace changes during that inspection, an approval of
            # the replacement must not be applied to the earlier snapshot.
            generation = None
            if trusted or blockers is not None:
                for line in mounts.splitlines():
                    fields = line.split()
                    if len(fields) <= 5 or fields[3:5] != ["/", "/"]:
                        continue
                    if any(
                        (device is None or device == fields[2])
                        and not (
                            fields[0] == host_id
                            and host_namespace
                            and _proc_link(view / "ns/mnt") == host_namespace
                        )
                        for device, _, _, _, host_id in mappings
                    ):
                        generation = _generation(process, view)
                        break
            cwd = _proc_link(view / "cwd")
            texts = [cwd, _proc_link(view / "exe")]
            fd_dir = view / "fd"
            descriptors = []
            try:
                with os.scandir(fd_dir) as entries:
                    descriptors = [
                        (entry.name, _proc_link(fd_dir / entry.name))
                        for entry in entries
                    ]
            except OSError as error:
                _proc_error(fd_dir, error)
            texts.extend(target for _, target in descriptors)
            # A handle or cwd on either the tree root or its dedicated state
            # directory can access all children through relative paths. More
            # distant ancestors such as /home are not task ownership evidence.
            # The coordinator's own inherited cwd is not independent task
            # use, but its explicit directory fds remain protection fences.
            coordinator_cwd = (
                cwd == str(root.parent)
                and root.name in ("jobs", "scratch-trash")
                and _owns_state_lock(root, process, view, descriptors)
            )
            if (cwd in root_refs and not coordinator_cwd) or any(
                text in root_refs for text in texts[1:]
            ):
                used.update(ids)
            for name in ("cmdline", "environ", "maps", "mountinfo"):
                text = mounts if name == "mountinfo" else _proc_text(view / name)
                if name == "mountinfo":
                    text = _unescape_mount_path(text)
                texts.append(text)
            for text in texts:
                offset = 0
                while (position := text.find(prefix, offset)) != -1:
                    index = position + len(prefix)
                    node = trie
                    while index < len(text) and text[index] in node:
                        node = node[text[index]]
                        if None in node:
                            used.add(node[None])
                        index += 1
                    offset = position + len(prefix)
            # A container may bind the entire jobs tree, rather than one task.
            for line in mounts.splitlines():
                fields = line.split()
                if len(fields) <= 5:
                    continue
                mount_root = Path(_unescape_mount_path(fields[3]))
                mount_point = Path(_unescape_mount_path(fields[4]))
                for device, jobs_root, _, _, host_id in mappings:
                    if device is not None and fields[2] != device:
                        continue
                    # Only the very same mount in the host namespace is known
                    # to be ordinary host visibility. A foreign root can be a
                    # real bind of the entire filesystem.
                    if (
                        fields[0] == host_id
                        and host_namespace
                        and _proc_link(view / "ns/mnt") == host_namespace
                    ):
                        continue
                    if mount_root == jobs_root or mount_root in jobs_root.parents:
                        if mount_root == mount_point == Path("/"):
                            if trusted or blockers is not None:
                                if generation is None:
                                    raise OSError(
                                        f"cannot verify process identity for {view}"
                                    )
                                identity = _root_identity(
                                    process, view, fields, generation
                                )
                                try:
                                    key = _trust_key(identity)
                                except ValueError:
                                    key = None
                                if key is not None and key in trusted:
                                    continue
                                if blockers is not None:
                                    entry = {**identity, "eligible": key is not None}
                                    if entry not in blockers:
                                        if len(blockers) >= 4096:
                                            raise OSError(
                                                "too many foreign root mounts to diagnose"
                                            )
                                        blockers.append(entry)
                                    used.update(ids)
                                    continue
                            raise OSError(
                                f"cannot verify foreign root mount for {view}"
                            )
                        if _systemd_home_mount(view, fields, units, proc):
                            continue
                        used.update(ids)
                    elif jobs_root in mount_root.parents:
                        key = mount_root.relative_to(jobs_root).parts[0]
                        if key in ids:
                            used.add(key)
    return used


def _trusted_helper(path):
    # The sudo rule names this exact file. Neither it nor any parent may be
    # replaceable by the unprivileged bot user.
    for current in (path, *path.parents):
        info = current.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            return False
        if current == path:
            if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
                return False
        elif not stat.S_ISDIR(info.st_mode):
            return False
    return True


def _privileged_scan(root, ids, helper=HELPER, *, diagnose=False):
    try:
        if not _trusted_helper(helper):
            raise OSError("process inspection helper is not root-owned and protected")
        helper_hash = hashlib.sha256(helper.read_bytes()).digest()
        if SOURCE_DIGEST is None or SOURCE_DIGEST != helper_hash:
            raise OSError("process inspection helper does not match the running code")
        payload = {"root": str(root), "ids": sorted(ids)}
        if diagnose:
            payload["diagnose"] = True
        request = json.dumps(payload)
        if len(request.encode()) > MAX_REQUEST:
            raise OSError("process inspection request is too large")
        completed = subprocess.run(
            ["/usr/bin/sudo", "-n", str(helper)],
            input=request,
            capture_output=True,
            text=True,
            # The inspector and sudo must not pin every task merely because
            # their coordinator was started from the state directory.
            cwd="/tmp" if root.parent == Path("/") else "/",
            timeout=12,
            check=False,
        )
        if completed.returncode:
            try:
                error = json.loads(completed.stdout).get("error")
            except (ValueError, AttributeError):
                error = None
            raise OSError(
                f"privileged process inspection unavailable: {str(error)[:300] if error else 'check helper installation and sudo policy'}"
            )
        if len(completed.stdout.encode()) > MAX_REQUEST:
            raise OSError("process inspection response is too large")
        response = json.loads(completed.stdout)
        if not isinstance(response, dict) or response.get("root") != str(root):
            raise OSError("invalid process inspection response")
        if response.get("error"):
            raise OSError(str(response["error"]))
        found = response.get("referenced")
        if not isinstance(found, list) or not all(isinstance(x, str) for x in found):
            raise OSError("invalid process inspection result")
        found = set(found)
        if not found <= ids:
            raise OSError("process inspection returned unknown jobs")
        if diagnose:
            blocked, boot_id = response.get("blocked"), response.get("boot_id")
            if (
                set(response) != {"root", "referenced", "blocked", "boot_id"}
                or not isinstance(boot_id, str)
                or not re.fullmatch(
                    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot_id
                )
                or not isinstance(blocked, list)
                or len(blocked) > 4096
            ):
                raise OSError("invalid process inspection diagnostic")
            for item in blocked:
                if (
                    not isinstance(item, dict)
                    or set(item) != set(TRUST_FIELDS) | {"eligible"}
                    or type(item["eligible"]) is not bool
                ):
                    raise OSError("invalid process inspection diagnostic entry")
                if item["eligible"]:
                    _trust_key({name: item[name] for name in TRUST_FIELDS})
            return response
        return found
    except (ValueError, subprocess.SubprocessError) as error:
        raise OSError(f"privileged process inspection unavailable: {error}") from error


def referenced_jobs(root, ids, proc=Path("/proc")):
    if not ids:
        return set()
    root = Path(root)
    if proc == Path("/proc") and os.geteuid() != 0 and HELPER.exists():
        return _privileged_scan(root, ids)
    try:
        return _scan(root, ids, proc)
    except OSError as error:
        if proc == Path("/proc") and os.geteuid() != 0:
            raise OSError(
                f"{error}; configure the privileged process inspector"
            ) from error
        raise


def _validate_request(request, uid):
    if (
        not isinstance(request, dict)
        or set(request) not in ({"root", "ids"}, {"root", "ids", "diagnose"})
        or ("diagnose" in request and request["diagnose"] is not True)
    ):
        raise ValueError("invalid inspection request")
    raw_root, ids = request["root"], request["ids"]
    if (
        not isinstance(raw_root, str)
        or not isinstance(ids, list)
        or len(ids) > 20000
        or (request.get("diagnose") and not ids)
    ):
        raise ValueError("invalid inspection request")
    root = Path(raw_root)
    if (
        not root.is_absolute()
        or root.name not in ("jobs", "scratch-trash")
        or str(root) != raw_root
        or root.resolve(strict=True) != root
    ):
        raise ValueError("jobs root must be an absolute path without symlinks")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
            raise ValueError(
                "jobs root must be owned by the caller and not writable by others"
            )
        for key in ids:
            if not isinstance(key, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,128}", key
            ):
                raise ValueError("invalid task id")
            item = os.stat(key, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(item.st_mode) or item.st_uid != uid:
                raise ValueError("task directories must be owned by the caller")
    finally:
        os.close(fd)
    return root, set(ids)


def _main():
    # This file is installed root-owned and can be invoked through a sudo rule
    # without arguments. Normal scans return only references to the caller's
    # own jobs. Diagnostics expose only bounded namespace/service identities,
    # never process environments, arguments, file handles, or file contents.
    try:

        def timed_out(*_):
            raise InspectionTimeout("process inspection timed out")

        signal.signal(signal.SIGALRM, timed_out)
        signal.alarm(10)
        if os.geteuid() != 0:
            raise ValueError("process inspection helper must run as root")
        uid = int(os.environ.get("SUDO_UID", ""))
        if uid <= 0:
            raise ValueError("process inspection helper requires a sudo caller")
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise ValueError("inspection request is too large")
        request = json.loads(raw)
        root, ids = _validate_request(request, uid)
        proc = Path("/proc")
        if request.get("diagnose"):
            blocked = []
            found = _scan(root, ids, proc, blockers=blocked)
            response = {
                "root": str(root),
                "referenced": sorted(found),
                "boot_id": _boot_id(proc),
                "blocked": sorted(
                    blocked,
                    key=lambda item: (item["pid"], item["namespace"], item["mount_id"]),
                ),
            }
        else:
            trusted = _load_trust(root, uid, proc)
            found = _scan(root, ids, proc, trusted=trusted)
            response = {"root": str(root), "referenced": sorted(found)}
        encoded = json.dumps(response)
        if len(encoded.encode()) > MAX_REQUEST:
            raise ValueError("process inspection response is too large")
        print(encoded)
    except (OSError, ValueError, TypeError, InspectionTimeout) as error:
        # Only return the requested root and a generic error, not process data.
        root = locals().get("root", "")
        print(json.dumps({"root": str(root), "error": str(error)}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
