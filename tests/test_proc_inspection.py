import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mo_bot import proc_inspection as inspection


class TestProcInspection(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "jobs"
        self.root.mkdir(mode=0o700)
        (self.root / "a").mkdir()
        (self.root / "b").mkdir()

    def test_helper_accepts_only_existing_owned_task_directories(self):
        request = {"root": str(self.root), "ids": ["a", "b"]}
        self.assertEqual(
            inspection._validate_request(request, os.getuid()), (self.root, {"a", "b"})
        )
        with self.assertRaises(ValueError):
            inspection._validate_request(request, os.getuid() + 1)
        with self.assertRaises(ValueError):
            inspection._validate_request(
                {"root": str(self.root), "ids": ["../a"]}, os.getuid()
            )
        (self.root / "link").symlink_to(self.root / "a", target_is_directory=True)
        with self.assertRaises(ValueError):
            inspection._validate_request(
                {"root": str(self.root), "ids": ["link"]}, os.getuid()
            )
        self.root.chmod(0o777)
        with self.assertRaises(ValueError):
            inspection._validate_request(request, os.getuid())
        self.root.chmod(0o700)
        alias = self.root.parent / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            inspection._validate_request(
                {"root": str(alias), "ids": ["a"]}, os.getuid()
            )

    def test_helper_can_inspect_owned_detached_trash(self):
        trash = self.root.parent / "scratch-trash"
        trash.mkdir(mode=0o700)
        (trash / "old-tree").mkdir()
        request = {"root": str(trash), "ids": ["old-tree"]}
        self.assertEqual(
            inspection._validate_request(request, os.getuid()), (trash, {"old-tree"})
        )
        with self.assertRaises(ValueError):
            inspection._validate_request(request, os.getuid() + 1)

    def test_helper_result_is_limited_to_requested_jobs(self):
        helper = Path(inspection.__file__)
        response = json.dumps({"root": str(self.root), "referenced": ["a"]})
        with (
            patch.object(inspection, "_trusted_helper", return_value=True),
            patch.object(
                inspection.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, response),
            ) as run,
        ):
            self.assertEqual(
                inspection._privileged_scan(self.root, {"a", "b"}, helper), {"a"}
            )
            self.assertEqual(
                run.call_args.args[0], ["/usr/bin/sudo", "-n", str(helper)]
            )
            self.assertEqual(run.call_args.kwargs["cwd"], "/")
            payload = json.loads(run.call_args.kwargs["input"])
            self.assertEqual(set(payload["ids"]), {"a", "b"})
            run.return_value.stdout = json.dumps(
                {"root": str(self.root), "referenced": ["foreign"]}
            )
            with self.assertRaisesRegex(OSError, "unknown jobs"):
                inspection._privileged_scan(self.root, {"a", "b"}, helper)
            run.return_value = subprocess.CompletedProcess(
                [], 1, json.dumps({"error": "process inspection timed out"})
            )
            with self.assertRaisesRegex(OSError, "timed out"):
                inspection._privileged_scan(self.root, {"a", "b"}, helper)

    def test_unavailable_or_untrusted_inspector_fails_closed(self):
        helper = Path(inspection.__file__)
        with (
            patch.object(inspection, "_trusted_helper", return_value=False),
            self.assertRaisesRegex(OSError, "not root-owned"),
        ):
            inspection._privileged_scan(self.root, {"a"}, helper)
        with self.assertRaisesRegex(OSError, "cannot enumerate"):
            inspection._scan(self.root, {"a"}, self.root / "missing-proc")
        with (
            patch.object(inspection, "_trusted_helper", return_value=True),
            patch.object(inspection, "SOURCE_DIGEST", None),
            self.assertRaisesRegex(OSError, "does not match"),
        ):
            inspection._privileged_scan(self.root, {"a"}, helper)

    def test_helper_protocol_exposes_only_task_ids(self):
        request = json.dumps({"root": str(self.root), "ids": ["a", "b"]}).encode()
        stdin = io.TextIOWrapper(io.BytesIO(request))
        output = io.StringIO()
        with (
            patch.object(inspection.os, "geteuid", return_value=0),
            patch.dict(inspection.os.environ, {"SUDO_UID": "1001"}),
            patch.object(inspection.sys, "stdin", stdin),
            patch.object(inspection.sys, "stdout", output),
            patch.object(inspection.signal, "signal"),
            patch.object(inspection.signal, "alarm"),
            patch.object(
                inspection, "_validate_request", return_value=(self.root, {"a", "b"})
            ),
            patch.object(inspection, "_scan", return_value={"a"}),
        ):
            self.assertEqual(inspection._main(), 0)
        self.assertEqual(
            json.loads(output.getvalue()), {"root": str(self.root), "referenced": ["a"]}
        )

    def test_overall_timeout_is_not_swallowed_by_systemd_fallback(self):
        process = self.root.parent / "proc/123"
        process.mkdir(parents=True)
        (process / "cgroup").write_text("0::/system.slice/example.service\n")
        fields = [
            "1",
            "0",
            "8:2",
            "/home",
            "/home",
            "ro",
            "-",
            "ext4",
            "/dev/sda",
            "rw",
        ]
        with (
            patch.object(
                inspection.subprocess,
                "run",
                side_effect=inspection.InspectionTimeout("timeout"),
            ),
            self.assertRaises(inspection.InspectionTimeout),
        ):
            inspection._systemd_home_mount(process, fields, {})

    def test_process_text_matches_overlapping_task_ids(self):
        proc = self.root.parent / "proc/123"
        proc.mkdir(parents=True)
        (proc / "cmdline").write_text(f"program\0{self.root}/ab/cache\0")
        self.assertEqual(
            inspection._scan(self.root, {"a", "ab", "b"}, proc.parent), {"a", "ab"}
        )

    def test_root_directory_handle_protects_all_children(self):
        proc = self.root.parent / "proc/123"
        (proc / "fd").mkdir(parents=True)
        handle = proc / "fd/3"
        handle.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(
            inspection._scan(self.root, {"a", "b"}, proc.parent), {"a", "b"}
        )
        handle.unlink()
        handle.symlink_to(self.root.parent, target_is_directory=True)
        self.assertEqual(
            inspection._scan(self.root, {"a", "b"}, proc.parent), {"a", "b"}
        )
        handle.unlink()
        handle.symlink_to(self.root.parent.parent, target_is_directory=True)
        self.assertEqual(inspection._scan(self.root, {"a", "b"}, proc.parent), set())

    def test_state_directory_cwd_protects_all_children(self):
        proc = self.root.parent / "proc/123"
        proc.mkdir(parents=True)
        (proc / "cwd").symlink_to(self.root.parent, target_is_directory=True)
        self.assertEqual(
            inspection._scan(self.root, {"a", "b"}, proc.parent), {"a", "b"}
        )

    def test_locked_service_cwd_does_not_hide_real_references(self):
        proc = self.root.parent / "proc/123"
        (proc / "fd").mkdir(parents=True)
        (proc / "fdinfo").mkdir()
        lock = self.root.parent / "service.lock"
        lock.touch()
        (proc / "cwd").symlink_to(self.root.parent, target_is_directory=True)
        (proc / "fd/3").symlink_to(lock)
        info = proc / "fdinfo/3"
        st = lock.stat()
        identity = f"{os.major(st.st_dev):x}:{os.minor(st.st_dev):x}:{st.st_ino}"
        info.write_text(f"lock:\t1: FLOCK ADVISORY WRITE 123 {identity} 0 EOF\n")
        self.assertEqual(inspection._scan(self.root, {"a", "b"}, proc.parent), set())

        # An explicit directory fd is still a reference, even for the owner.
        (proc / "fd/4").symlink_to(self.root.parent, target_is_directory=True)
        self.assertEqual(
            inspection._scan(self.root, {"a", "b"}, proc.parent), {"a", "b"}
        )
        (proc / "fd/4").unlink()

        # Merely opening the lock file, or another PID holding the lock, does
        # not make the process an authorized coordinator.
        for contents in (
            "",
            f"lock:\t1: FLOCK ADVISORY WRITE 124 {identity} 0 EOF\n",
            f"lock:\t1: FLOCK ADVISORY READ 123 {identity} 0 EOF\n",
            "lock:\t1: FLOCK ADVISORY WRITE 123 0:0:0 0 EOF\n",
        ):
            info.write_text(contents)
            self.assertEqual(
                inspection._scan(self.root, {"a", "b"}, proc.parent), {"a", "b"}
            )

        info.write_text(f"lock:\t1: FLOCK ADVISORY WRITE 123 {identity} 0 EOF\n")
        other = proc.parent / "124"
        other.mkdir()
        (other / "cwd").symlink_to(self.root.parent, target_is_directory=True)
        self.assertEqual(
            inspection._scan(self.root, {"a", "b"}, proc.parent), {"a", "b"}
        )

    def test_zombie_leader_inspects_surviving_threads(self):
        proc = self.root.parent / "proc"
        leader = proc / "123"
        (leader / "task/123").mkdir(parents=True)
        (leader / "status").write_text("State:\tZ (zombie)\n")
        (leader / "task/123/status").write_text("State:\tZ (zombie)\n")
        worker = leader / "task/124"
        (worker / "fd").mkdir(parents=True)
        (worker / "status").write_text("State:\tS (sleeping)\n")
        (worker / "fd/3").symlink_to(self.root / "a/cache/open-file")
        self.assertEqual(inspection._scan(self.root, {"a", "b"}, proc), {"a"})
        (worker / "status").write_text("State:\tZ (zombie)\n")
        self.assertEqual(inspection._scan(self.root, {"a", "b"}, proc), set())

    def test_zombie_with_uninspectable_thread_list_is_not_assumed_safe(self):
        proc = self.root.parent / "proc"
        leader = proc / "123"
        leader.mkdir(parents=True)
        (leader / "status").write_text("State:\tZ (zombie)\n")
        with self.assertRaisesRegex(OSError, "cannot inspect"):
            inspection._scan(self.root, {"a"}, proc)

    def test_live_leader_does_not_hide_thread_with_private_fd_table(self):
        proc = self.root.parent / "proc"
        leader = proc / "123"
        (leader / "task/123").mkdir(parents=True)
        (leader / "status").write_text("State:\tS (sleeping)\n")
        (leader / "task/123/status").write_text("State:\tS (sleeping)\n")
        worker = leader / "task/124"
        (worker / "fd").mkdir(parents=True)
        (worker / "status").write_text("State:\tS (sleeping)\n")
        (worker / "fd/3").symlink_to(self.root / "a/cache/open-file")
        self.assertEqual(inspection._scan(self.root, {"a", "b"}, proc), {"a"})

    def test_hidden_process_mount_cannot_be_used_as_an_empty_snapshot(self):
        plain = "25 31 0:23 / /proc rw,nosuid - proc proc rw\n"
        inspection._require_visible_proc(plain)
        for option in (
            "hidepid=1",
            "hidepid=2",
            "hidepid=invisible",
            "hidepid=ptraceable",
        ):
            with (
                self.subTest(option=option),
                self.assertRaisesRegex(OSError, "hidepid"),
            ):
                inspection._require_visible_proc(
                    plain.replace("proc proc rw", f"proc proc rw,{option}")
                )
        with self.assertRaisesRegex(OSError, "cannot verify"):
            inspection._require_visible_proc("")

    def test_bind_mounts_are_matched_relative_to_a_separate_home_filesystem(self):
        root = Path("/home/user/.local/state/mo-bot/jobs")
        proc = self.root.parent / "proc"
        (proc / "self/ns").mkdir(parents=True)
        (proc / "self/ns/mnt").symlink_to("mnt:[1]")
        (proc / "self/mountinfo").write_text(
            "10 0 8:2 / / rw - ext4 /dev/root rw\n"
            "20 10 8:3 / /home rw - ext4 /dev/home rw\n"
        )
        process = proc / "123"
        (process / "ns").mkdir(parents=True)
        (process / "ns/mnt").symlink_to("mnt:[1]")
        mount = process / "mountinfo"
        # Merely seeing the original /home filesystem does not pin all tasks.
        mount.write_text("20 10 8:3 / /home rw - ext4 /dev/home rw\n")
        self.assertEqual(inspection._scan(root, {"one", "two"}, proc), set())
        # A container's bind of a single task is relative to the filesystem,
        # so its mount root does not contain the host's /home prefix.
        mount.write_text(
            "30 10 8:3 /user/.local/state/mo-bot/jobs/one /work rw - ext4 /dev/home rw\n"
        )
        self.assertEqual(inspection._scan(root, {"one", "two"}, proc), {"one"})
        mount.write_text("30 10 8:3 / /work ro - ext4 /dev/home rw\n")
        self.assertEqual(inspection._scan(root, {"one", "two"}, proc), {"one", "two"})
        mount.write_text("30 10 8:4 / /work ro - ext4 /dev/other rw\n")
        self.assertEqual(inspection._scan(root, {"one", "two"}, proc), set())

    def test_foreign_namespace_root_bind_protects_all_tasks(self):
        root = Path("/home/user/.local/state/mo-bot/jobs")
        proc = self.root.parent / "proc"
        (proc / "self/ns").mkdir(parents=True)
        (proc / "self/ns/mnt").symlink_to("mnt:[1]")
        (proc / "self/mountinfo").write_text("10 0 8:2 / / rw - ext4 /dev/root rw\n")
        process = proc / "123"
        (process / "ns").mkdir(parents=True)
        (process / "ns/mnt").symlink_to("mnt:[1]")
        mount = process / "mountinfo"
        mount.write_text("10 0 8:2 / / rw - ext4 /dev/root rw\n")
        self.assertEqual(inspection._scan(root, {"one", "two"}, proc), set())
        (process / "ns/mnt").unlink()
        (process / "ns/mnt").symlink_to("mnt:[2]")
        mount.write_text("20 0 8:2 / / ro - ext4 /dev/root rw\n")
        with self.assertRaisesRegex(OSError, "cannot verify foreign root mount"):
            inspection._scan(root, {"one", "two"}, proc)


if __name__ == "__main__":
    unittest.main()
