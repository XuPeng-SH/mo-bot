import os
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
