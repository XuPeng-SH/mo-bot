import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mo_bot import proc_inspection as inspection
from mo_bot import scratch_control


class TestScratchTrust(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.root = self.state / "jobs"
        self.root.mkdir(mode=0o700)
        (self.root / "a").mkdir()
        (self.root / "b").mkdir()
        self.proc = self.state / "proc"
        (self.proc / "self/ns").mkdir(parents=True)
        (self.proc / "self/ns/mnt").symlink_to("mnt:[1]")
        (self.proc / "self/mountinfo").write_text(
            "10 0 8:2 / / rw - ext4 /dev/root rw\n"
        )
        self.process = self.proc / "123"
        (self.process / "ns").mkdir(parents=True)
        (self.process / "fd").mkdir()
        (self.process / "ns/mnt").symlink_to("mnt:[2]")
        (self.process / "cgroup").write_text("0::/system.slice/test.service\n")
        (self.process / "stat").write_text("123 (service) S " + "0 " * 18 + "42\n")
        self.mounts = self.process / "mountinfo"
        self.mounts.write_text("20 0 8:2 / / rw - ext4 /dev/root rw\n")
        self.boot_id = "12345678-1234-1234-1234-123456789abc"
        boot = self.proc / "sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        boot.write_text(self.boot_id)
        self.entry = {
            "pid": 123,
            "start_ticks": 42,
            "namespace": 2,
            "cgroup": "0::/system.slice/test.service",
            "mount_id": 20,
            "device": "8:2",
        }
        self.trusted = frozenset({inspection._trust_key(self.entry)})

    def _write(self, value):
        path = self.state / inspection.TRUST_FILE
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def _policy(self, entries=None):
        return {"version": 1, "boot_id": self.boot_id, "entries": entries or []}

    def test_exact_service_identity_only_ignores_its_root_mount(self):
        with self.assertRaisesRegex(OSError, "foreign root mount"):
            inspection._scan(self.root, {"a", "b"}, self.proc)
        blocked = []
        self.assertEqual(
            inspection._scan(self.root, {"a", "b"}, self.proc, blockers=blocked),
            {"a", "b"},
        )
        self.assertEqual(blocked, [{**self.entry, "eligible": True}])
        self.assertEqual(
            inspection._scan(self.root, {"a", "b"}, self.proc, trusted=self.trusted),
            set(),
        )
        self.mounts.write_text(
            "20 0 8:2 / / rw - ext4 /dev/root rw\n"
            f"21 20 8:2 {self.root}/a /work rw - ext4 /dev/root rw\n"
        )
        self.assertEqual(
            inspection._scan(self.root, {"a", "b"}, self.proc, trusted=self.trusted),
            {"a"},
        )
        self.mounts.write_text("20 0 8:2 / / rw - ext4 /dev/root rw\n")
        (self.process / "fd/3").symlink_to(self.root / "b/cache")
        self.assertEqual(
            inspection._scan(self.root, {"a", "b"}, self.proc, trusted=self.trusted),
            {"b"},
        )
        (self.process / "fd/3").unlink()
        (self.process / "cwd").symlink_to(self.root)
        self.assertEqual(
            inspection._scan(self.root, {"a", "b"}, self.proc, trusted=self.trusted),
            {"a", "b"},
        )

    def test_every_identity_field_and_generation_is_required(self):
        changes = {
            "pid": 124,
            "start_ticks": 43,
            "namespace": 3,
            "cgroup": "0::/system.slice/another.service",
            "mount_id": 21,
            "device": "8:3",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                key = inspection._trust_key({**self.entry, field: value})
                with self.assertRaisesRegex(OSError, "foreign root mount"):
                    inspection._scan(self.root, {"a"}, self.proc, trusted={key})
        before = (42, 2, self.entry["cgroup"])
        after = (43, 2, self.entry["cgroup"])
        with (
            patch.object(inspection, "_generation", side_effect=[before, after]),
            self.assertRaisesRegex(OSError, "changed while inspecting"),
        ):
            inspection._scan(self.root, {"a"}, self.proc, trusted=self.trusted)

    def test_trust_still_scans_each_thread_and_rejects_another_namespace(self):
        (self.process / "status").write_text("State:\tS (sleeping)\n")
        leader = self.process / "task/123"
        leader.mkdir(parents=True)
        (leader / "status").write_text("State:\tS (sleeping)\n")
        worker = self.process / "task/124"
        (worker / "fd").mkdir(parents=True)
        (worker / "ns").mkdir()
        (worker / "status").write_text("State:\tS (sleeping)\n")
        (worker / "cgroup").write_text("0::/system.slice/test.service\n")
        (worker / "mountinfo").write_text(self.mounts.read_text())
        (worker / "ns/mnt").symlink_to("mnt:[2]")
        (worker / "fd/4").symlink_to(self.root / "b/cache")
        self.assertEqual(
            inspection._scan(self.root, {"a", "b"}, self.proc, trusted=self.trusted),
            {"b"},
        )
        (worker / "ns/mnt").unlink()
        (worker / "ns/mnt").symlink_to("mnt:[3]")
        with self.assertRaisesRegex(OSError, "foreign root mount"):
            inspection._scan(self.root, {"a"}, self.proc, trusted=self.trusted)

    def test_container_scopes_and_unrecognized_cgroups_cannot_be_trusted(self):
        for cgroup in (
            "0::/system.slice/docker-abc.scope",
            "0::/user.slice/user@1000.service/app.slice/docker.scope",
            "0::/system.slice/a.service/../other",
            "0::/system.slice/a.service/with space",
        ):
            with self.subTest(cgroup=cgroup):
                self.assertFalse(inspection._service_cgroup(cgroup))
                with self.assertRaises(ValueError):
                    inspection._trust_key({**self.entry, "cgroup": cgroup})
        (self.process / "cgroup").write_text("0::/system.slice/docker-abc.scope\n")
        blocked = []
        inspection._scan(self.root, {"a"}, self.proc, blockers=blocked)
        self.assertEqual(len(blocked), 1)
        self.assertFalse(blocked[0]["eligible"])
        with self.assertRaisesRegex(OSError, "foreign root mount"):
            inspection._scan(self.root, {"a"}, self.proc, trusted=self.trusted)

    def test_policy_requires_valid_owner_permissions_boot_and_complete_entries(self):
        self.assertEqual(
            inspection._load_trust(self.root, os.getuid(), self.proc), set()
        )
        path = self._write(self._policy([self.entry]))
        self.assertEqual(
            inspection._load_trust(self.root, os.getuid(), self.proc), self.trusted
        )
        for policy in (
            {**self._policy([self.entry]), "boot_id": "old-boot"},
            self._policy([self.entry, self.entry]),
            self._policy([{**self.entry, "cgroup": "0::/container.scope"}]),
            {**self._policy(), "unknown": True},
        ):
            with self.subTest(policy=policy):
                self._write(policy)
                with self.assertRaises(OSError):
                    inspection._load_trust(self.root, os.getuid(), self.proc)
        path.write_text('{"version":1,"version":1,"boot_id":"x","entries":[]}')
        with self.assertRaisesRegex(OSError, "duplicate"):
            inspection._load_trust(self.root, os.getuid(), self.proc)
        self._write(self._policy([self.entry]))
        path.chmod(0o644)
        with self.assertRaisesRegex(OSError, "ownership or permissions"):
            inspection._load_trust(self.root, os.getuid(), self.proc)
        path.chmod(0o600)
        self.state.chmod(0o777)
        try:
            with self.assertRaisesRegex(OSError, "owner-controlled"):
                inspection._load_trust(self.root, os.getuid(), self.proc)
        finally:
            self.state.chmod(0o700)

    def test_policy_refuses_symlink_hardlink_and_fifo_without_blocking(self):
        path = self._write(self._policy([self.entry]))
        other = self.state / "other"
        os.link(path, other)
        with self.assertRaisesRegex(OSError, "ownership or permissions"):
            inspection._load_trust(self.root, os.getuid(), self.proc)
        path.unlink()
        path.symlink_to(other)
        with self.assertRaises(OSError):
            inspection._load_trust(self.root, os.getuid(), self.proc)
        path.unlink()
        os.mkfifo(path, 0o600)
        with self.assertRaisesRegex(OSError, "ownership or permissions"):
            inspection._load_trust(self.root, os.getuid(), self.proc)

    def test_policy_replacement_during_read_fails_closed(self):
        path = self._write(self._policy([self.entry]))
        original_stat = inspection.os.stat

        def swap_before_stat(name, *args, **kwargs):
            replacement = self.state / "replacement"
            replacement.write_text(json.dumps(self._policy()))
            replacement.chmod(0o600)
            os.replace(replacement, path)
            return original_stat(name, *args, **kwargs)

        with (
            patch.object(inspection.os, "stat", side_effect=swap_before_stat),
            self.assertRaisesRegex(OSError, "changed during inspection"),
        ):
            inspection._load_trust(self.root, os.getuid(), self.proc)

    def test_diagnostic_helper_only_returns_minimal_identities(self):
        request = {"root": str(self.root), "ids": ["a"], "diagnose": True}
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(request).encode()))
        output = io.StringIO()

        def scan(root, ids, proc, *, blockers):
            blockers.append({**self.entry, "eligible": True})
            return {"a"}

        with (
            patch.object(inspection.os, "geteuid", return_value=0),
            patch.dict(inspection.os.environ, {"SUDO_UID": str(os.getuid())}),
            patch.object(inspection.sys, "stdin", stdin),
            patch.object(inspection.sys, "stdout", output),
            patch.object(inspection.signal, "signal"),
            patch.object(inspection.signal, "alarm"),
            patch.object(inspection, "_scan", side_effect=scan),
        ):
            self.assertEqual(inspection._main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["blocked"], [{**self.entry, "eligible": True}])
        self.assertEqual(set(result), {"root", "referenced", "boot_id", "blocked"})
        for invalid in (False, 1, "yes"):
            with self.assertRaises(ValueError):
                inspection._validate_request(
                    {**request, "diagnose": invalid}, os.getuid()
                )

    def test_operator_selection_rechecks_exact_entries_and_replaces_policy(self):
        item = {**self.entry, "eligible": True}
        item["entry_id"] = scratch_control._entry_id(self.boot_id, item)
        report = {"boot_id": self.boot_id, "blocked": [item]}
        with (
            patch.object(scratch_control, "diagnose", return_value=report),
            patch.object(scratch_control, "_write_policy") as write,
        ):
            self.assertEqual(
                scratch_control.set_trust(self.state, [item["entry_id"]]), [self.entry]
            )
            write.assert_called_once_with(self.state, self.boot_id, [self.entry])
            with self.assertRaisesRegex(ValueError, "changed"):
                scratch_control.set_trust(self.state, ["f" * 64])
            with self.assertRaisesRegex(ValueError, "at most once"):
                scratch_control.set_trust(self.state, [item["entry_id"]] * 2)

    def test_operator_policy_is_atomic_private_and_can_be_cleared(self):
        boot = inspection._boot_id(Path("/proc"))
        scratch_control._write_policy(self.state, boot, [self.entry])
        path = self.state / inspection.TRUST_FILE
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            inspection._load_trust(self.root, os.getuid(), Path("/proc")), self.trusted
        )
        scratch_control.set_trust(self.state, [])
        self.assertEqual(
            inspection._load_trust(self.root, os.getuid(), Path("/proc")), set()
        )


if __name__ == "__main__":
    unittest.main()
