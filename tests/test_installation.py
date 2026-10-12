import os
import json
import socket
import time
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mo_bot.config import Config
from mo_bot.installation import doctor, initialize, install, manifest_path, uninstall
from mo_bot.review_queue import Queue


class TestInstallation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / "prefix"
        self.skills = self.root / "agent-skills"
        self.config = self.root / "config.toml"
        self.source = self.root / "source with spaces"
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.source),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "--allow-empty",
                "-qm",
                "test",
            ],
            check=True,
        )

    def deploy(self, **kwargs):
        return install(self.prefix, [self.skills], self.config, **kwargs)

    def test_config_initialization_is_portable_and_never_overwrites(self):
        with patch("pathlib.Path.home", return_value=self.root):
            initialize(self.config, "owner/repo", self.source, "me", 1)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("/home/xupeng", self.config.read_text())
        with patch("mo_bot.config.shutil.which", return_value="/fake/executable"):
            config = Config.load(self.config)
        self.assertIsNone(config.queue_script)
        self.assertIsNone(config.issue_skill)
        self.assertEqual(config.source_dir, self.source)
        previous = self.config.read_bytes()
        with self.assertRaises(FileExistsError):
            initialize(self.config, "owner/repo", self.source, "other")
        self.assertEqual(previous, self.config.read_bytes())

    def test_reinstall_and_uninstall_preserve_config_and_state(self):
        self.config.write_text("user config")
        state = self.root / "state.sqlite3"
        state.write_bytes(b"user state")
        installed = self.deploy()
        with patch("mo_bot.installation.subprocess.run") as command:
            self.assertEqual(installed, self.deploy())
            command.assert_not_called()
        for name in ("mo-bot-service", "mo-bot-review", "mo-bot-worker", "issue-to-pr"):
            self.assertTrue((self.skills / name / "SKILL.md").is_file())
        self.assertTrue((self.skills / "mo-bot-service/references/api.md").is_file())
        self.assertTrue((self.skills / "issue-to-pr/agents/openai.yaml").is_file())
        self.assertEqual(uninstall(self.prefix), [])
        self.assertTrue(all(not Path(name).is_symlink() for name in installed))
        self.assertEqual(self.config.read_text(), "user config")
        self.assertEqual(state.read_bytes(), b"user state")
        self.assertEqual(uninstall(self.prefix), [])

    def test_foreign_skill_conflict_does_not_partially_install(self):
        foreign = self.skills / "mo-bot-worker"
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("custom instructions")
        with self.assertRaisesRegex(ValueError, "unowned"):
            self.deploy()
        self.assertFalse((self.prefix / "bin/mo-bot").exists())
        self.assertFalse(manifest_path(self.prefix).exists())
        self.assertEqual((foreign / "SKILL.md").read_text(), "custom instructions")

    def test_foreign_issue_workflow_is_preserved_without_partial_install(self):
        foreign = self.skills / "issue-to-pr"
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("operator workflow")
        with self.assertRaisesRegex(ValueError, "unowned"):
            self.deploy()
        self.assertFalse((self.prefix / "bin/mo-bot").exists())
        self.assertFalse((self.skills / "mo-bot-worker").exists())
        self.assertFalse(manifest_path(self.prefix).exists())
        self.assertEqual((foreign / "SKILL.md").read_text(), "operator workflow")

    def test_interrupted_installation_can_be_retried(self):
        original = Path.symlink_to
        calls = 0

        def interrupt(path, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("interrupted")
            return original(path, *args, **kwargs)

        with patch.object(Path, "symlink_to", interrupt):
            with self.assertRaisesRegex(OSError, "interrupted"):
                self.deploy()
        self.assertTrue(manifest_path(self.prefix).is_file())
        self.deploy()
        self.assertTrue((self.skills / "mo-bot-service/SKILL.md").is_file())
        self.assertEqual(uninstall(self.prefix), [])

    def test_uninstall_keeps_replaced_foreign_skill(self):
        self.deploy()
        path = self.skills / "mo-bot-review"
        path.unlink()
        path.mkdir()
        (path / "SKILL.md").write_text("user replacement")
        self.assertEqual(uninstall(self.prefix), [str(path)])
        self.assertEqual((path / "SKILL.md").read_text(), "user replacement")

    def test_interrupted_unit_update_accepts_the_old_owned_version_on_retry(self):
        from mo_bot.installation import atomic_write

        units = self.root / "units"
        with (
            patch("mo_bot.installation.shutil.which", return_value="systemctl"),
            patch("mo_bot.installation.subprocess.run"),
        ):
            self.deploy(systemd=True, unit_dir=units)
            self.config = self.root / "changed-config.toml"

            def interrupt(path, text):
                if path.name == "mo-bot.service":
                    raise OSError("unit update interrupted")
                return atomic_write(path, text)

            with patch("mo_bot.installation.atomic_write", side_effect=interrupt):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    self.deploy(systemd=True, unit_dir=units)
            self.deploy(systemd=True, unit_dir=units)
        self.assertIn("changed-config.toml", (units / "mo-bot.service").read_text())

    def test_concurrent_installer_is_rejected_before_mutation(self):
        import fcntl

        lock = manifest_path(self.prefix).with_suffix(".lock")
        lock.parent.mkdir(parents=True)
        with lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.deploy()
        self.assertFalse(self.skills.exists())

    def test_init_refuses_a_broken_config_symlink(self):
        destination = self.root / "absent-config"
        self.config.symlink_to(destination)
        with self.assertRaises(FileExistsError):
            initialize(self.config, "owner/repo", self.source, "me")
        self.assertFalse(destination.exists())

    def test_checkout_relocation_updates_owned_links(self):
        self.deploy()
        package = self.root / "relocated/mo_bot"
        from mo_bot.installation import PACKAGE

        shutil.copytree(PACKAGE / "skills", package / "skills")
        scripts = package.parent / "scripts"
        scripts.mkdir()
        for name in ("mo-bot", "mo-botctl"):
            (scripts / name).write_text("new launcher")
        with patch("mo_bot.installation.PACKAGE", package):
            self.deploy()
        self.assertEqual(
            (self.skills / "mo-bot-service").resolve(),
            package / "skills/mo-bot-service",
        )
        self.assertEqual((self.prefix / "bin/mo-bot").resolve(), scripts / "mo-bot")

    def test_systemd_is_optional_and_absence_does_not_write_outputs(self):
        with patch("mo_bot.installation.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "systemctl unavailable"):
                self.deploy(systemd=True)
        self.assertFalse(self.skills.exists())
        self.assertFalse(manifest_path(self.prefix).exists())

    def test_systemd_unit_quotes_paths_and_reinstall_does_not_restart(self):
        from mo_bot.installation import PACKAGE

        package = self.root / "package space%/mo_bot"
        shutil.copytree(PACKAGE / "skills", package / "skills")
        with (
            patch("mo_bot.installation.PACKAGE", package),
            patch("mo_bot.installation.shutil.which", return_value="systemctl"),
            patch("mo_bot.installation.subprocess.run") as command,
        ):
            self.config = self.root / "config space%/bot.toml"
            units = self.root / "units"
            self.deploy(systemd=True, unit_dir=units)
            self.deploy(systemd=True, unit_dir=units)
        text = (units / "mo-bot.service").read_text()
        self.assertIn("config space%%/bot.toml", text)
        self.assertIn('"serve" "--config"', text)
        self.assertTrue(
            all("enable" not in call.args[0] for call in command.call_args_list)
        )
        analyzer = shutil.which("systemd-analyze")
        if analyzer:
            verified = subprocess.run(
                [analyzer, "verify", str(units / "mo-bot.service")],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertNotIn("ignoring", verified.stderr)

    def test_doctor_checks_authentication_without_turns_or_secret_output(self):
        with patch("pathlib.Path.home", return_value=self.root):
            initialize(self.config, "owner/repo", self.source, "me", 1)
        self.config.write_text(
            self.config.read_text().replace(
                "minimum_free_gib = 20", "minimum_free_gib = 0"
            )
        )
        binaries = self.root / "bin"
        binaries.mkdir()
        for name, body in (
            ("gh", "printf 'me\\n'\n"),
            (
                "codex",
                "if [ \"$1 $2\" = 'login status' ] || [ \"$1 $2\" = 'app-server --help' ]; then exit 0; fi; exit 91\n",
            ),
        ):
            path = binaries / name
            path.write_text("#!/bin/sh\n" + body)
            path.chmod(0o755)
        with patch.dict(
            os.environ, {"PATH": str(binaries) + os.pathsep + os.environ["PATH"]}
        ):
            checks = doctor(self.config)
        self.assertTrue(all(item["ok"] for item in checks), checks)
        (binaries / "gh").write_text("#!/bin/sh\nprintf 'other\\n'\n")
        with patch.dict(
            os.environ, {"PATH": str(binaries) + os.pathsep + os.environ["PATH"]}
        ):
            checks = doctor(self.config)
        self.assertFalse(
            next(c for c in checks if c["check"] == "GitHub authentication")["ok"]
        )
        self.assertFalse((self.root / ".local/state/mo-bot").exists())

    def test_real_source_cli_install_init_and_uninstall(self):
        repo = Path(__file__).resolve().parents[1]
        script = repo / "scripts/install.py"
        subprocess.run(
            [
                sys.executable,
                str(script),
                "--prefix",
                str(self.prefix),
                "--skill-dir",
                str(self.skills),
            ],
            check=True,
            capture_output=True,
        )
        launcher = self.prefix / "bin/mo-bot"
        subprocess.run(
            [
                str(launcher),
                "init",
                "--config",
                str(self.config),
                "--repository",
                "owner/repo",
                "--source-dir",
                str(self.source),
                "--reviewer",
                "me",
            ],
            check=True,
            capture_output=True,
        )
        again = subprocess.run(
            [
                str(launcher),
                "init",
                "--config",
                str(self.config),
                "--repository",
                "owner/repo",
                "--source-dir",
                str(self.source),
                "--reviewer",
                "me",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(again.returncode, 0)
        self.assertNotIn("Traceback", again.stderr)
        subprocess.run(
            [str(launcher), "uninstall", "--prefix", str(self.prefix)],
            check=True,
            capture_output=True,
        )
        self.assertTrue(self.config.is_file())

    def test_real_server_and_client_with_bundled_policy(self):
        with patch("pathlib.Path.home", return_value=self.root):
            initialize(self.config, "owner/repo", self.source, "me")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        gh = self.root / "gh"
        gh.write_text(
            f"#!{sys.executable}\n"
            + """
import json,sys
endpoint=sys.argv[2]
if endpoint=='user': result={'login':'me','id':1}
elif endpoint=='graphql':
 result={'data':{'repository':{'pullRequests':{'nodes':[],'pageInfo':{'hasNextPage':False,'endCursor':None}}}}}
elif '/issues?' in endpoint: result=[]
else: sys.exit(92)
print(json.dumps(result))
"""
        )
        codex = self.root / "codex"
        codex.write_text(
            f"#!{sys.executable}\n"
            + """
import json,sys,time
if sys.argv[1:]==['login','status']: sys.exit(0)
if sys.argv[1:]!=['app-server','--stdio']: sys.exit(93)
for line in sys.stdin:
 request=json.loads(line)
 if 'id' not in request: continue
 method=request.get('method'); params=request.get('params',{}); result={}
 if method=='model/list': result={'data':[],'nextCursor':None}
 if method in ('thread/start','thread/resume'):
  result={'thread':{'id':params.get('threadId','thread-'+str(request['id']))}}
 if method=='turn/start': result={'turn':{'id':'turn-'+str(request['id'])}}
 print(json.dumps({'id':request['id'],'result':result}),flush=True)
 if method=='turn/start':
  time.sleep(.02)
  tid=result['turn']['id']; thread=params['threadId']
  print(json.dumps({'method':'item/completed','params':{'threadId':thread,'turnId':tid,'item':{'type':'agentMessage','text':'{"ready":true}','phase':'final_answer'}}}),flush=True)
  print(json.dumps({'method':'turn/completed','params':{'threadId':thread,'turn':{'id':tid,'status':'completed'}}}),flush=True)
"""
        )
        gh.chmod(0o755)
        codex.chmod(0o755)
        text = self.config.read_text().replace(
            'codex = "codex"', "codex = " + json.dumps(str(codex))
        )
        text = text.replace('gh = "gh"', "gh = " + json.dumps(str(gh)))
        text = text.replace("port = 8989", f"port = {port}").replace(
            "minimum_free_gib = 20", "minimum_free_gib = 0"
        )
        self.config.write_text(text)
        repo = Path(__file__).resolve().parents[1]
        with (self.root / "server.log").open("w") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "mo_bot.cli",
                    "serve",
                    "--config",
                    str(self.config),
                ],
                cwd=repo,
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 10
                status = None
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail((self.root / "server.log").read_text())
                    probe = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "mo_bot.client",
                            "--config",
                            str(self.config),
                            "status",
                        ],
                        cwd=repo,
                        capture_output=True,
                        text=True,
                        timeout=3,
                    )
                    if probe.returncode == 0:
                        status = json.loads(probe.stdout)
                        if (
                            status["sessions"]
                            and all(x["ready"] for x in status["sessions"])
                            and status["discovery"]["last_success_at"]
                        ):
                            break
                    time.sleep(0.1)
                self.assertIsNotNone(status, (self.root / "server.log").read_text())
                self.assertTrue(all(x["ready"] for x in status["sessions"]), status)
                self.assertEqual(status["jobs"], [])
                self.assertTrue(status["discovery"]["last_success_at"])
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                    from mo_bot.codex import stop_owned

                    state = self.root / ".local/state/mo-bot/backend-owner.json"
                    if state.is_file():
                        stop_owned(json.loads(state.read_text()))
        self.assertEqual(process.returncode, 0)


class TestBundledDiscovery(unittest.TestCase):
    def test_pagination_uses_shared_github_client(self):
        from unittest.mock import Mock

        gh = Mock()
        gh.api.side_effect = [
            {
                "data": {
                    "repository": {
                        "pullRequests": {
                            "nodes": [{"number": 1}],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor"},
                        }
                    }
                }
            },
            {
                "data": {
                    "repository": {
                        "pullRequests": {
                            "nodes": [{"number": 2}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            },
        ]
        self.assertEqual(
            Queue(gh).all_prs("owner", "repo"), [{"number": 1}, {"number": 2}]
        )
        self.assertEqual(
            gh.api.call_args.kwargs["body"]["variables"]["endCursor"], "cursor"
        )

    def test_partial_graphql_and_stuck_cursor_never_mean_empty_queue(self):
        from unittest.mock import Mock

        gh = Mock()
        gh.api.return_value = {"errors": [{"message": "quota"}], "data": {}}
        with self.assertRaisesRegex(RuntimeError, "GraphQL errors"):
            Queue(gh).all_prs("o", "r")
        gh.api.return_value = {
            "data": {
                "repository": {
                    "pullRequests": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": True, "endCursor": "same"},
                    }
                }
            }
        }
        with self.assertRaisesRegex(RuntimeError, "did not advance"):
            Queue(gh).all_prs("o", "r")


if __name__ == "__main__":
    unittest.main()
