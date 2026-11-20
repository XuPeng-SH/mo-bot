import errno
import os
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mo_bot.artifacts import (
    Artifacts,
    _mount_id,
    referenced_jobs,
    remove_scratch,
    scratch_env,
)


class TestArtifacts(unittest.TestCase):
    def test_go_verification_cache_is_task_owned_and_shared_across_phases(self):
        root = Path("/state")
        first = scratch_env(root, root / "jobs/a/design-1")
        later = scratch_env(root, root / "jobs/a/implement-2")
        other = scratch_env(root, root / "jobs/b/implement-1")
        self.assertEqual(first["GOPATH"], later["GOPATH"])
        self.assertNotEqual(first["GOPATH"], other["GOPATH"])
        self.assertEqual(first["GOPATH"], "/state/jobs/a/cache/go")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gc = Artifacts(self.root)
        self.addCleanup(self.gc.close)

    def job(self, key, state):
        root = self.root / "jobs" / key
        scratch = root / "implement-1" / "tmp"
        scratch.mkdir(parents=True)
        (scratch / "large-build").write_text("disposable")
        (root / "implement-1" / "events.jsonl").write_text("evidence")
        (root / "implement-1" / "result.json").write_text("result")
        (root / "source.patch").write_text("source")
        (root / "cache" / "go-mod").mkdir(parents=True)
        return {"id": key, "state": state}, root

    def sweep(self, jobs, live=()):
        self.gc.next_scan = 0
        self.gc.poll(jobs, set(live))
        if self.gc.future:
            self.gc.future.result(timeout=5)
            self.gc.poll(jobs, set(live))

    def test_terminal_scratch_only_and_live_cleanup_fence(self):
        jobs = []
        for state in (
            "done",
            "failed",
            "superseded",
            "reviewing",
            "publishing",
            "queued",
            "canceling",
        ):
            job, _ = self.job(state, state)
            jobs.append(job)
        source = self.root / "jobs/done/saved-source/tmp"
        source.mkdir(parents=True)
        (source / "user-file").write_text("source data")
        with patch("mo_bot.artifacts.referenced_jobs", return_value=set()):
            self.sweep(jobs, live={"failed"})
        for job in jobs:
            root = self.root / "jobs" / job["id"]
            should_keep = job["state"] not in ("done", "superseded")
            self.assertEqual((root / "implement-1/tmp").exists(), should_keep)
            self.assertEqual((root / "cache").exists(), should_keep)
            self.assertEqual(
                (root / "implement-1/events.jsonl").read_text(), "evidence"
            )
            self.assertTrue((root / "source.patch").exists())
            self.assertTrue((root / "implement-1/result.json").exists())
        self.assertEqual((source / "user-file").read_text(), "source data")

    def test_readonly_module_cache_symlinks_and_retry_new_scratch(self):
        job, root = self.job("done", "done")
        outside = self.root / "valuable"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        scratch = root / "implement-1/tmp"
        (scratch / "outside").symlink_to(outside, target_is_directory=True)
        readonly = scratch / "gomod/pkg/module"
        readonly.mkdir(parents=True)
        (readonly / "file").write_text("cache")
        readonly.chmod(0o500)
        readonly.parent.chmod(0o500)
        entered, proceed = threading.Event(), threading.Event()
        original = remove_scratch

        def delayed(path, stopping):
            entered.set()
            self.assertTrue(proceed.wait(5))
            return original(path, stopping)

        with (
            patch("mo_bot.artifacts.referenced_jobs", return_value=set()),
            patch("mo_bot.artifacts.remove_scratch", side_effect=delayed),
        ):
            self.gc.poll([job], set())
            self.assertTrue(entered.wait(5))
            # Reentry can create fresh scratch after detachment; background GC
            # has no access to the replacement, even at an identical pathname.
            scratch.mkdir()
            (scratch / "new-build").write_text("new")
            proceed.set()
            self.gc.future.result(timeout=5)
        self.assertEqual((outside / "keep").read_text(), "keep")
        self.assertEqual((scratch / "new-build").read_text(), "new")

    def test_delete_failure_retried_after_restart(self):
        job, root = self.job("done", "done")
        with (
            patch("mo_bot.artifacts.referenced_jobs", return_value=set()),
            patch(
                "mo_bot.artifacts.remove_scratch",
                side_effect=PermissionError("injected"),
            ),
        ):
            self.sweep([job])
        self.assertFalse((root / "implement-1/tmp").exists())
        self.assertTrue(self.gc.status["errors"])
        self.assertTrue(list(self.gc.trash.iterdir()))
        self.gc.close()
        replacement = Artifacts(self.root)
        self.addCleanup(replacement.close)
        with patch("mo_bot.artifacts.referenced_jobs", return_value=set()):
            replacement.poll([], set())
            replacement.future.result(timeout=5)
        self.assertFalse(list(replacement.trash.iterdir()))
        self.assertTrue((root / "source.patch").exists())

    def test_open_file_blocks_gc_until_released(self):
        job, root = self.job("open", "done")
        scratch = root / "implement-1/tmp"
        # Observe real /proc fd links without unrelated host/container mounts
        # of the test runner's /tmp influencing this isolated task.
        proc = self.root / "proc" / str(os.getpid())
        proc.mkdir(parents=True)
        (proc / "fd").symlink_to(Path("/proc/self/fd"), target_is_directory=True)
        with patch(
            "mo_bot.artifacts.referenced_jobs",
            side_effect=lambda root, ids: referenced_jobs(root, ids, proc.parent),
        ):
            with (scratch / "large-build").open() as handle:
                self.sweep([job])
                self.assertFalse(handle.closed)
                self.assertTrue(scratch.exists())
            self.sweep([job])
        self.assertFalse(scratch.exists())

    def test_unexpected_background_error_does_not_stick_and_retries(self):
        job, root = self.job("worker-error", "done")
        with (
            patch("mo_bot.artifacts.referenced_jobs", return_value=set()),
            patch(
                "mo_bot.artifacts.remove_scratch",
                side_effect=RuntimeError("worker failed"),
            ),
        ):
            self.gc.poll([job], set())
            with self.assertRaisesRegex(RuntimeError, "worker failed"):
                self.gc.future.result(timeout=5)
            self.gc.poll([job], set())
        self.assertIsNone(self.gc.future)
        self.assertIn("worker failed", self.gc.status["errors"])
        with patch("mo_bot.artifacts.referenced_jobs", return_value=set()):
            self.sweep([job])
        self.assertFalse(list(self.gc.trash.iterdir()))
        self.assertTrue((root / "source.patch").exists())

    def test_container_mounts_environment_and_ancestor_bind_are_protected(self):
        root = self.root / "jobs"
        proc = self.root / "proc/123"
        proc.mkdir(parents=True)
        (proc / "environ").write_text(f"TMPDIR={root}/one/implement-1/tmp\0")
        (proc / "mountinfo").write_text(
            f"1 0 8:0 {root}/two /work rw - ext4 /dev/sda rw\n"
        )
        self.assertEqual(
            referenced_jobs(root, {"one", "two", "three"}, proc.parent), {"one", "two"}
        )
        (proc / "mountinfo").write_text(
            f"1 0 8:0 {self.root} /work rw - ext4 /dev/sda rw\n"
        )
        self.assertEqual(
            referenced_jobs(root, {"one", "two", "three"}, proc.parent),
            {"one", "two", "three"},
        )

    def systemd_home_fixture(self):
        proc = self.root / "proc"
        for pid in ("123", "124"):
            process = proc / pid
            (process / "ns").mkdir(parents=True)
            (process / "ns/mnt").symlink_to("mnt:[42]")
            (process / "cgroup").write_text("0::/system.slice/NetworkManager.service\n")
            (process / "mountinfo").write_text(
                "269 187 8:2 /home /home ro,relatime - ext4 /dev/sda2 rw\n"
            )
        (proc / "123/status").write_text("CapBnd:\t00000000200534e2\n")
        properties = (
            "MainPID=123\nProtectHome=read-only\nBindPaths=\nBindReadOnlyPaths=\n"
            "RootDirectory=\nRootImage=\nReadOnlyPaths=\nReadWritePaths=\n"
            "ExecPaths=\nNoExecPaths=\nInaccessiblePaths=\n"
            "TemporaryFileSystem=\nPrivateUsers=no\n"
        )
        return proc, properties

    def test_verified_systemd_home_mount_and_helper_do_not_pin_all_jobs(self):
        root = Path("/home/user/.local/state/mo-bot/jobs")
        proc, properties = self.systemd_home_fixture()
        with patch(
            "mo_bot.proc_inspection.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, properties),
        ) as systemctl:
            self.assertEqual(referenced_jobs(root, {"one", "two"}, proc), set())
            self.assertEqual(systemctl.call_count, 1)
            # Real task references remain protected for both main and helper.
            (proc / "123/environ").write_text(f"TMPDIR={root}/one/cache\0")
            (proc / "124/fd").mkdir()
            (proc / "124/fd/3").symlink_to(root / "two/cache/open-file")
            self.assertEqual(
                referenced_jobs(root, {"one", "two"}, proc), {"one", "two"}
            )

    def test_unverified_home_mounts_and_other_namespaces_remain_protected(self):
        root = Path("/home/user/.local/state/mo-bot/jobs")
        proc, properties = self.systemd_home_fixture()
        cases = (
            (
                "explicit bind",
                properties.replace(
                    "BindReadOnlyPaths=\n", "BindReadOnlyPaths=/home:/home\n"
                ),
            ),
            ("missing property", properties.replace("PrivateUsers=no\n", "")),
            (
                "different protection",
                properties.replace("ProtectHome=read-only", "ProtectHome=no"),
            ),
        )
        for name, output in cases:
            with (
                self.subTest(name=name),
                patch(
                    "mo_bot.proc_inspection.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, output),
                ),
            ):
                self.assertEqual(
                    referenced_jobs(root, {"one", "two"}, proc), {"one", "two"}
                )
        with patch(
            "mo_bot.proc_inspection.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, properties),
        ):
            (proc / "124/ns/mnt").unlink()
            (proc / "124/ns/mnt").symlink_to("mnt:[43]")
            self.assertEqual(
                referenced_jobs(root, {"one", "two"}, proc), {"one", "two"}
            )
            (proc / "124/ns/mnt").unlink()
            (proc / "124/ns/mnt").symlink_to("mnt:[42]")
            (proc / "123/status").write_text("CapBnd:\t0000000000200000\n")
            self.assertEqual(
                referenced_jobs(root, {"one", "two"}, proc), {"one", "two"}
            )

    def test_other_verified_systemd_services_are_not_hard_coded(self):
        root = Path("/home/user/.local/state/mo-bot/jobs")
        proc, properties = self.systemd_home_fixture()
        for pid in ("123", "124"):
            (proc / pid / "cgroup").write_text("0::/system.slice/example.service\n")
        with patch(
            "mo_bot.proc_inspection.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, properties),
        ):
            self.assertEqual(referenced_jobs(root, {"one", "two"}, proc), set())

    def test_inaccessible_process_references_defer_cleanup(self):
        job, root = self.job("done", "done")
        proc = self.root / "proc/123"
        (proc / "fd").mkdir(parents=True)
        (proc / "fd/3").symlink_to(root / "cache/active-file")
        scandir = os.scandir

        def deny_fd(path):
            if Path(path) == proc / "fd":
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return scandir(path)

        with (
            patch("mo_bot.artifacts.os.scandir", side_effect=deny_fd),
            patch(
                "mo_bot.artifacts.referenced_jobs",
                side_effect=lambda root, ids: referenced_jobs(root, ids, proc.parent),
            ),
        ):
            self.sweep([job])
        self.assertTrue((root / "cache").is_dir())
        self.assertIn("cannot inspect", self.gc.status["errors"][0])

    def test_inspection_failure_does_not_block_detached_trash_retry(self):
        job, root = self.job("done", "done")
        old = self.gc.trash / "previous-cleanup"
        old.mkdir(parents=True)
        (old / "cache").write_text("discardable")

        def inspect(root, ids):
            if root.name == "jobs":
                raise OSError("inspection unavailable")
            return set()

        with patch("mo_bot.artifacts.referenced_jobs", side_effect=inspect):
            self.sweep([job])
        self.assertTrue((root / "cache").is_dir())
        self.assertFalse(old.exists())
        self.assertIn("inspection unavailable", self.gc.status["errors"][0])

    def test_trash_with_live_directory_fd_is_retained_until_released(self):
        old = self.gc.trash / "previous-cleanup"
        old.mkdir(parents=True)
        (old / "needed").write_text("keep")
        proc = self.root / "proc" / str(os.getpid())
        proc.mkdir(parents=True)
        (proc / "fd").symlink_to(Path("/proc/self/fd"), target_is_directory=True)
        with patch(
            "mo_bot.artifacts.referenced_jobs",
            side_effect=lambda root, ids: referenced_jobs(root, ids, proc.parent),
        ):
            fd = os.open(old, os.O_RDONLY | os.O_DIRECTORY)
            try:
                self.sweep([])
                self.assertEqual((old / "needed").read_text(), "keep")
            finally:
                os.close(fd)
            self.sweep([])
        self.assertFalse(old.exists())

    def test_trash_root_fd_protects_children_until_released(self):
        old = self.gc.trash / "previous-cleanup"
        old.mkdir(parents=True)
        (old / "needed").write_text("keep")
        proc = self.root / "proc" / str(os.getpid())
        proc.mkdir(parents=True)
        (proc / "fd").symlink_to(Path("/proc/self/fd"), target_is_directory=True)
        with patch(
            "mo_bot.artifacts.referenced_jobs",
            side_effect=lambda root, ids: referenced_jobs(root, ids, proc.parent),
        ):
            fd = os.open(self.gc.trash, os.O_RDONLY | os.O_DIRECTORY)
            try:
                self.sweep([])
                child = os.open("previous-cleanup/needed", os.O_RDONLY, dir_fd=fd)
                os.close(child)
            finally:
                os.close(fd)
            self.sweep([])
        self.assertFalse(old.exists())

    def test_state_directory_fd_protects_trash_until_released(self):
        old = self.gc.trash / "previous-cleanup"
        old.mkdir(parents=True)
        (old / "needed").write_text("keep")
        proc = self.root / "proc" / str(os.getpid())
        proc.mkdir(parents=True)
        (proc / "fd").symlink_to(Path("/proc/self/fd"), target_is_directory=True)
        with patch(
            "mo_bot.artifacts.referenced_jobs",
            side_effect=lambda root, ids: referenced_jobs(root, ids, proc.parent),
        ):
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                self.sweep([])
                child = os.open(
                    "scratch-trash/previous-cleanup/needed", os.O_RDONLY, dir_fd=fd
                )
                os.close(child)
            finally:
                os.close(fd)
            self.sweep([])
        self.assertFalse(old.exists())

    def test_reference_opened_during_detach_is_rechecked_in_trash(self):
        job, root = self.job("done", "done")
        proc = self.root / "proc" / str(os.getpid())
        proc.mkdir(parents=True)
        (proc / "fd").symlink_to(Path("/proc/self/fd"), target_is_directory=True)
        held = []

        def inspect(root, ids):
            if root.name == "jobs" and ids:
                held.append(os.open(root / "done/cache", os.O_RDONLY | os.O_DIRECTORY))
                return set()
            return referenced_jobs(root, ids, proc.parent)

        with patch("mo_bot.artifacts.referenced_jobs", side_effect=inspect):
            try:
                self.sweep([job])
                self.assertEqual(len(held), 1)
                child = os.open("go-mod", os.O_RDONLY | os.O_DIRECTORY, dir_fd=held[0])
                os.close(child)
                self.assertTrue(list(self.gc.trash.iterdir()))
            finally:
                for fd in held:
                    os.close(fd)
            self.sweep([job])
        self.assertFalse(list(self.gc.trash.iterdir()))

    def test_trash_is_retained_when_its_reference_check_fails(self):
        old = self.gc.trash / "previous-cleanup"
        old.mkdir(parents=True)
        (old / "needed").write_text("keep")

        def inspect(root, ids):
            if root.name == "scratch-trash" and ids:
                raise OSError("trash references unavailable")
            return set()

        with patch("mo_bot.artifacts.referenced_jobs", side_effect=inspect):
            self.sweep([])
        self.assertEqual((old / "needed").read_text(), "keep")
        self.assertIn("trash references unavailable", self.gc.status["errors"][0])

    def test_trash_reference_check_runs_in_background(self):
        old = self.gc.trash / "previous-cleanup"
        old.mkdir(parents=True)
        (old / "file").write_text("discardable")
        entered, proceed = threading.Event(), threading.Event()

        def inspect(root, ids):
            if root.name == "scratch-trash" and ids:
                entered.set()
                if not proceed.wait(5):
                    raise RuntimeError("reference check blocked coordinator")
            return set()

        with patch("mo_bot.artifacts.referenced_jobs", side_effect=inspect):
            self.gc.poll([], set())
            self.assertTrue(entered.wait(5))
            self.assertTrue(old.exists())
            proceed.set()
            self.gc.future.result(timeout=5)
        self.assertFalse(old.exists())

    def test_later_trash_tree_is_rechecked_after_slow_deletion(self):
        for name in ("a", "b"):
            tree = self.gc.trash / name
            tree.mkdir(parents=True)
            (tree / "needed").write_text("keep")
        proc = self.root / "proc" / str(os.getpid())
        proc.mkdir(parents=True)
        (proc / "fd").symlink_to(Path("/proc/self/fd"), target_is_directory=True)
        entered, proceed = threading.Event(), threading.Event()
        first = []
        original = remove_scratch

        def slow(path, stopping):
            if not first:
                first.append(path.name)
                entered.set()
                if not proceed.wait(5):
                    raise RuntimeError("test deletion did not resume")
            return original(path, stopping)

        with (
            patch(
                "mo_bot.artifacts.referenced_jobs",
                side_effect=lambda root, ids: referenced_jobs(root, ids, proc.parent),
            ),
            patch("mo_bot.artifacts.remove_scratch", side_effect=slow),
        ):
            self.gc.poll([], set())
            self.assertTrue(entered.wait(5))
            later = "b" if first[0] == "a" else "a"
            fd = os.open(self.gc.trash / later, os.O_RDONLY | os.O_DIRECTORY)
            try:
                proceed.set()
                self.gc.future.result(timeout=5)
                child = os.open("needed", os.O_RDONLY, dir_fd=fd)
                os.close(child)
                self.assertTrue((self.gc.trash / later / "needed").exists())
            finally:
                proceed.set()
                os.close(fd)

    def test_per_tree_reference_check_failure_retains_trash(self):
        old = self.gc.trash / "previous-cleanup"
        old.mkdir(parents=True)
        (old / "needed").write_text("keep")
        checks = []

        def inspect(root, ids):
            if root.name == "scratch-trash":
                checks.append(ids)
                if len(checks) == 2:
                    raise OSError("recheck unavailable")
            return set()

        with patch("mo_bot.artifacts.referenced_jobs", side_effect=inspect):
            self.sweep([])
        self.assertEqual(len(checks), 2)
        self.assertEqual((old / "needed").read_text(), "keep")
        self.assertIn("recheck unavailable", self.gc.status["errors"][0])

    def test_detached_trash_does_not_cross_same_device_mount(self):
        job, root = self.job("done", "done")
        nested = self.gc.trash / "previous-cleanup/nested"
        nested.mkdir(parents=True)
        protected = nested / "external-data"
        protected.write_text("keep")
        original_mount_id = _mount_id

        def mount_id(fd):
            if Path(os.readlink(f"/proc/self/fd/{fd}")) == nested:
                return original_mount_id(fd) + 1
            return original_mount_id(fd)

        def inspect(root, ids):
            if root.name == "jobs":
                raise OSError("inspection unavailable")
            return set()

        with (
            patch("mo_bot.artifacts._mount_id", side_effect=mount_id),
            patch("mo_bot.artifacts.referenced_jobs", side_effect=inspect),
        ):
            self.sweep([job])
        self.assertEqual(protected.read_text(), "keep")
        self.assertTrue((root / "cache").is_dir())
        self.assertTrue(
            any("mountpoint" in error for error in self.gc.status["errors"])
        )

    def test_new_mount_on_distant_ancestor_is_checked_before_deleting(self):
        old = self.gc.trash / "old"
        outer = old / "outer"
        inner = outer / "b/c/d"
        inner.mkdir(parents=True)
        payload = inner / "needed"
        payload.write_text("keep")
        original_mount_id = _mount_id
        mounted = False
        before_fds = len(os.listdir("/proc/self/fd"))

        def mount_id(fd):
            nonlocal mounted
            result = original_mount_id(fd)
            target = Path(os.readlink(f"/proc/self/fd/{fd}"))
            if target == inner:
                mounted = True
            if target == outer and mounted:
                return result + 1
            return result

        class Poller:
            def __init__(self):
                self.fd = None
                self.reported = False

            def register(self, fd, _events):
                self.fd = fd

            def poll(self, _timeout):
                if mounted and not self.reported:
                    self.reported = True
                    return [(self.fd, select.POLLPRI | select.POLLERR)]
                return []

        with (
            patch("mo_bot.artifacts._mount_id", side_effect=mount_id),
            patch("mo_bot.artifacts.select.poll", return_value=Poller()),
            self.assertRaisesRegex(OSError, "mountpoint"),
        ):
            remove_scratch(old, threading.Event())
        self.assertTrue(mounted)
        self.assertEqual(payload.read_text(), "keep")
        self.assertEqual(len(os.listdir("/proc/self/fd")), before_fds)

    def test_mount_monitor_failure_preserves_tree(self):
        old = self.gc.trash / "old"
        old.mkdir(parents=True)
        payload = old / "needed"
        payload.write_text("keep")
        before_fds = len(os.listdir("/proc/self/fd"))

        class Poller:
            def register(self, fd, _events):
                self.fd = fd

            def poll(self, _timeout):
                return [(self.fd, select.POLLHUP)]

        with (
            patch("mo_bot.artifacts.select.poll", return_value=Poller()),
            self.assertRaisesRegex(OSError, "cannot monitor"),
        ):
            remove_scratch(old, threading.Event())
        self.assertEqual(payload.read_text(), "keep")
        self.assertEqual(len(os.listdir("/proc/self/fd")), before_fds)

    def test_mount_change_storm_is_bounded_and_preserves_tree(self):
        old = self.gc.trash / "old"
        old.mkdir(parents=True)
        payload = old / "needed"
        payload.write_text("keep")
        before_fds = len(os.listdir("/proc/self/fd"))

        class Poller:
            def register(self, fd, _events):
                self.fd = fd

            def poll(self, _timeout):
                return [(self.fd, select.POLLPRI | select.POLLERR)]

        with (
            patch("mo_bot.artifacts.select.poll", return_value=Poller()),
            self.assertRaisesRegex(OSError, "mounts keep changing"),
        ):
            remove_scratch(old, threading.Event())
        self.assertEqual(payload.read_text(), "keep")
        self.assertEqual(len(os.listdir("/proc/self/fd")), before_fds)

    def test_deep_trash_cleanup_uses_bounded_descriptors(self):
        old = self.gc.trash / "deep"
        old.mkdir(parents=True)
        current = old
        for _ in range(96):
            current = current / "d"
            current.mkdir()
        (current / "file").write_text("discardable")
        script = (
            "import resource,sys,threading; from pathlib import Path; "
            "from mo_bot.artifacts import remove_scratch; "
            "soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE); "
            "limit=64 if hard==resource.RLIM_INFINITY else min(64,hard); "
            "resource.setrlimit(resource.RLIMIT_NOFILE,(limit,hard)); "
            "assert remove_scratch(Path(sys.argv[1]),threading.Event())"
        )
        subprocess.run([sys.executable, "-c", script, str(old)], check=True, timeout=30)
        self.assertFalse(old.exists())

    def test_cleanup_handles_paths_longer_than_procfs_limit(self):
        old = self.gc.trash / "long"
        old.mkdir(parents=True)
        name = "d" * 30
        fd = os.open(old, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for _ in range(140):
                os.mkdir(name, dir_fd=fd)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
            with self.assertRaises(OSError) as error:
                os.readlink(f"/proc/self/fd/{fd}")
            self.assertEqual(error.exception.errno, errno.ENAMETOOLONG)
        finally:
            os.close(fd)
        self.assertTrue(remove_scratch(old, threading.Event()))
        self.assertFalse(old.exists())

    def test_long_path_fallback_does_not_follow_moved_tree(self):
        old = self.gc.trash / "long"
        old.mkdir(parents=True)
        rescued = self.root / "rescued"
        name = "d" * 30
        fd = os.open(old, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for _ in range(140):
                os.mkdir(name, dir_fd=fd)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
            file_fd = os.open("needed", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd)
            os.close(file_fd)
            original_readlink = os.readlink
            moved = False

            def move_after_path_limit(path):
                nonlocal moved
                try:
                    return original_readlink(path)
                except OSError as error:
                    if error.errno == errno.ENAMETOOLONG and not moved:
                        moved = True
                        old.rename(rescued)
                        old.mkdir()
                    raise

            with (
                patch(
                    "mo_bot.artifacts.os.readlink", side_effect=move_after_path_limit
                ),
                self.assertRaisesRegex(OSError, "directory changed"),
            ):
                remove_scratch(old, threading.Event())
            self.assertTrue(moved)
            file_fd = os.open("needed", os.O_RDONLY, dir_fd=fd)
            os.close(file_fd)
        finally:
            os.close(fd)
        self.assertTrue(remove_scratch(rescued, threading.Event()))

    def test_long_path_fallback_rejects_symlinked_state_ancestor(self):
        parent = self.root / "live"
        old = parent / "state/scratch-trash/old"
        old.mkdir(parents=True)
        rescued = self.root / "rescued-parent"
        name = "d" * 30
        fd = os.open(old, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for _ in range(140):
                os.mkdir(name, dir_fd=fd)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
            file_fd = os.open("needed", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd)
            os.close(file_fd)
            original_readlink = os.readlink
            moved = False

            def move_after_path_limit(path):
                nonlocal moved
                try:
                    return original_readlink(path)
                except OSError as error:
                    if error.errno == errno.ENAMETOOLONG and not moved:
                        moved = True
                        parent.rename(rescued)
                        parent.symlink_to(rescued, target_is_directory=True)
                    raise

            with (
                patch(
                    "mo_bot.artifacts.os.readlink", side_effect=move_after_path_limit
                ),
                self.assertRaisesRegex(OSError, "directory changed"),
            ):
                remove_scratch(old, threading.Event())
            self.assertTrue(moved)
            file_fd = os.open("needed", os.O_RDONLY, dir_fd=fd)
            os.close(file_fd)
        finally:
            os.close(fd)
        self.assertTrue(
            remove_scratch(rescued / "state/scratch-trash/old", threading.Event())
        )

    def test_replaced_directory_is_not_followed_after_reopen(self):
        old = self.gc.trash / "old"
        nested = old / "nested"
        nested.mkdir(parents=True)
        (nested / "original").write_text("keep")
        rescued = self.root / "rescued"
        swapped = False

        def mount_id(fd):
            nonlocal swapped
            result = _mount_id(fd)
            if not swapped and Path(os.readlink(f"/proc/self/fd/{fd}")) == nested:
                swapped = True
                nested.rename(rescued)
                nested.mkdir()
                (nested / "replacement").write_text("keep")
            return result

        with (
            patch("mo_bot.artifacts._mount_id", side_effect=mount_id),
            self.assertRaisesRegex(OSError, "directory changed"),
        ):
            remove_scratch(old, threading.Event())
        self.assertEqual((rescued / "original").read_text(), "keep")
        self.assertEqual((nested / "replacement").read_text(), "keep")

    def test_moved_ancestor_is_not_followed(self):
        old = self.gc.trash / "old"
        outer = old / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        (inner / "original").write_text("keep")
        rescued = self.root / "rescued"
        swapped = False
        before_fds = len(os.listdir("/proc/self/fd"))

        def mount_id(fd):
            nonlocal swapped
            result = _mount_id(fd)
            if not swapped and Path(os.readlink(f"/proc/self/fd/{fd}")) == inner:
                swapped = True
                outer.rename(rescued)
                outer.mkdir()
                (outer / "replacement").write_text("keep")
            return result

        with (
            patch("mo_bot.artifacts._mount_id", side_effect=mount_id),
            self.assertRaisesRegex(OSError, "directory changed"),
        ):
            remove_scratch(old, threading.Event())
        self.assertEqual((rescued / "inner/original").read_text(), "keep")
        self.assertEqual((outer / "replacement").read_text(), "keep")
        self.assertEqual(len(os.listdir("/proc/self/fd")), before_fds)

    def test_symlinked_trash_refused_and_unknown_jobs_preserved(self):
        _, root = self.job("unknown", "done")
        self.sweep([])
        self.assertTrue((root / "implement-1/tmp").exists())
        self.gc.trash.rmdir()
        outside = self.root / "outside"
        outside.mkdir()
        self.gc.trash.symlink_to(outside, target_is_directory=True)
        self.gc.next_scan = 0
        with self.assertRaisesRegex(ValueError, "symlinked"):
            self.gc.poll([], set())

    def test_shutdown_preserves_unfinished_detached_tree(self):
        path = self.root / "scratch-trash/old"
        path.mkdir(parents=True)
        (path / "cache").write_text("cache")
        stopping = threading.Event()
        stopping.set()
        self.assertFalse(remove_scratch(path, stopping))
        self.assertTrue((path / "cache").exists())
        self.assertTrue(remove_scratch(path, threading.Event()))
        self.assertFalse(path.exists())

    def test_shared_cache_is_job_scoped_and_tmp_is_attempt_scoped(self):
        a = scratch_env(self.root, self.root / "jobs/a/design-1")
        b = scratch_env(self.root, self.root / "jobs/a/implement-2")
        other = scratch_env(self.root, self.root / "jobs/b/design-1")
        self.assertEqual(a["GOCACHE"], b["GOCACHE"])
        self.assertEqual(a["GOMODCACHE"], b["GOMODCACHE"])
        self.assertNotEqual(a["GOCACHE"], other["GOCACHE"])
        self.assertNotEqual(a["TMPDIR"], b["TMPDIR"])
