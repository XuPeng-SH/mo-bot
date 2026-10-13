import os
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from mo_bot.validation import prepare, run


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.phase = self.root / "phase with spaces"
        self.phase.mkdir()
        self.guidance = prepare(self.phase)

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, name, *args):
        return subprocess.run(
            [str(self.phase / name), *args], capture_output=True, text=True, timeout=25
        )

    def test_launcher_preserves_arguments_and_exit_status(self):
        result = self.invoke(
            "check-run",
            "--",
            sys.executable,
            "-c",
            "import sys; print(sys.argv[1]); sys.exit(7)",
            "spaces;$(not-a-shell)",
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout.strip(), "spaces;$(not-a-shell)")
        receipts = [
            json.loads(p.read_text()) for p in (self.phase / "checks").glob("*.json")
        ]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["exit_code"], 7)
        self.assertEqual(receipts[0]["command"][-1], "spaces;$(not-a-shell)")
        self.assertEqual(receipts[0]["kind"], "check")

    def test_real_namespace_preflight_and_test_binary_isolation(self):
        preflight = self.invoke("check-run", "--preflight")
        if preflight.returncode and any(
            reason in preflight.stderr
            for reason in ("Operation not permitted", "isolation tools unavailable")
        ):
            self.skipTest("host does not support test isolation: " + preflight.stderr)
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        marker = "mo-bot-validation-" + str(os.getpid())
        probe = (
            "import os,pathlib,socket; "
            "assert os.environ['TMPDIR']=='/tmp'; "
            f"pathlib.Path('/tmp/{marker}').write_text('private'); "
            "s=socket.socket(); s.bind(('127.0.0.1',0)); s.close(); print('executed')"
        )
        result = self.invoke("test-exec", sys.executable, "-c", probe)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "executed")
        self.assertFalse(Path("/tmp", marker).exists())
        self.assertEqual(list((self.phase / "tmp/runtime").iterdir()), [])

    def test_preflight_cannot_silently_skip_a_command(self):
        result = self.invoke("check-run", "--preflight", "--", "false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not execute a command", result.stderr)

    def test_timeout_reaps_owned_child_without_touching_other_processes(self):
        pidfile = self.root / "child.pid"
        code = (
            "import subprocess,time,pathlib,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(60)"
        )
        result = self.invoke(
            "check-run", "--timeout", "1", "--", sys.executable, "-c", code
        )
        self.assertEqual(result.returncode, 124, result.stderr)
        receipt = json.loads(next((self.phase / "checks").glob("*.json")).read_text())
        self.assertEqual(receipt["exit_code"], 124)
        pid = pidfile.read_text()
        for _ in range(50):
            status = Path("/proc", pid, "stat")
            if not status.exists() or status.read_text().split()[2] == "Z":
                break
            time.sleep(0.02)
        else:
            self.fail("owned child still running after timeout")

    def test_cancellation_cleans_process_group(self):
        with patch("mo_bot.validation.subprocess.Popen") as launch:
            process = launch.return_value
            process.pid = 987654
            process.wait.side_effect = [KeyboardInterrupt, 0]
            process.poll.return_value = 0
            with patch("mo_bot.validation.os.killpg") as kill:
                with self.assertRaises(KeyboardInterrupt):
                    run(["command"], 1)
            self.assertEqual(kill.call_count, 2)

    def test_receipt_fingerprints_new_test_contents_and_separates_setup_failure(self):
        repo = self.root / "checkout"
        repo.mkdir()

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(repo), *args], capture_output=True, check=True
            )

        git("init")
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        )
        probe = repo / "regression.py"
        fingerprints = []
        for contents in ("first", "changed"):
            probe.write_text(contents)
            result = subprocess.run(
                [str(self.phase / "check-run"), "--", "true"],
                cwd=repo,
                capture_output=True,
                timeout=25,
            )
            self.assertEqual(result.returncode, 0)
            latest = sorted((self.phase / "checks").glob("*.json"))[-1]
            fingerprints.append(
                json.loads(latest.read_text())["source"]["delta_sha256"]
            )
        self.assertNotEqual(*fingerprints)
        result = self.invoke("check-run", "--", str(self.root / "missing-command"))
        self.assertEqual(result.returncode, 125)
        receipt = json.loads(
            sorted((self.phase / "checks").glob("*.json"))[-1].read_text()
        )
        self.assertEqual(receipt["exit_code"], 125)
        self.assertIn("setup_error", receipt)

    def test_actual_launcher_cancellation_retains_a_nonpassing_receipt(self):
        marker = self.root / "started"
        script = f"import pathlib,time; pathlib.Path({str(marker)!r}).touch(); time.sleep(60)"
        process = subprocess.Popen(
            [str(self.phase / "check-run"), "--", sys.executable, "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while (
                not marker.exists()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            process.terminate()
            self.assertEqual(process.wait(timeout=10), 130)
            receipt = json.loads(
                next((self.phase / "checks").glob("*.json")).read_text()
            )
            self.assertEqual(receipt["exit_code"], 130)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
