import json
from pathlib import Path
import subprocess
import tempfile
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mo_bot.config import Config
from mo_bot.nodes import Nodes
from mo_bot.remote import (
    CleanupPending,
    HostTools,
    RemoteBackend,
    recover_host_tools,
    ssh_command,
)
from mo_bot.service import Service
from mo_bot.codex import process_identity, stop_owned
from mo_bot.rpc import Events
from mo_bot.store import Store


NODE = {"name": "idc55", "ssh": "me@host55", "codex": "/opt/codex"}


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = Config(
            "test-ssh",
            self.root,
            "o/r",
            "me",
            self.root,
            self.root / "q",
            nodes=(NODE,),
        )

    def tools(self):
        directory = self.root / "diag"
        directory.mkdir(exist_ok=True)
        turn = SimpleNamespace(
            directory=directory, started=time.monotonic(), session_id="host-thread"
        )
        remote = SimpleNamespace(host_config=self.cfg, events=Events())
        return HostTools(
            remote,
            turn,
            {"type": "workspaceWrite", "writableRoots": [str(directory)]},
            self.root / "source",
        )

    def test_host_tool_uses_local_sandbox_and_bounded_command(self):
        tools = self.tools()
        with patch("mo_bot.remote.Backend") as factory:
            factory.return_value.call.return_value = {
                "exitCode": 0,
                "stdout": "local",
                "stderr": "",
            }
            factory.return_value.config = self.cfg
            result = tools.execute(
                {
                    "command": "hostname",
                    "cwd": str(self.root / "source"),
                    "timeout_seconds": 20,
                }
            )
            method, params = factory.return_value.call.call_args.args
            self.assertEqual(method, "command/exec")
            self.assertEqual(
                params["sandboxPolicy"]["writableRoots"], [str(self.root / "diag")]
            )
            self.assertEqual(params["outputBytesCap"], 65536)
            self.assertEqual(params["timeoutMs"], 20000)
            self.assertTrue(result["success"])
            tools.close()
            factory.return_value.close.assert_called_once()
            with self.assertRaisesRegex(RuntimeError, "closed"):
                tools.execute(
                    {
                        "command": "hostname",
                        "cwd": str(self.root / "source"),
                        "timeout_seconds": 1,
                    }
                )

    def test_outside_cwd_relative_and_symlink_escape_rejected(self):
        tools = self.tools()
        (self.root / "diag" / "escape").symlink_to("/etc")
        with patch("mo_bot.remote.Backend") as backend:
            for cwd in (".", "/etc", str(self.root / "diag" / "escape")):
                with self.subTest(cwd=cwd), self.assertRaises(ValueError):
                    tools.execute({"command": "true", "cwd": cwd, "timeout_seconds": 1})
            backend.assert_not_called()

    def test_bad_arguments_and_exhausted_budget_do_not_spawn(self):
        tools = self.tools()
        with patch("mo_bot.remote.Backend") as backend:
            for args in (
                {},
                {
                    "command": "x",
                    "cwd": str(self.root / "diag"),
                    "timeout_seconds": True,
                },
                {
                    "command": "x",
                    "cwd": str(self.root / "diag"),
                    "timeout_seconds": 601,
                },
            ):
                with self.assertRaises(ValueError):
                    tools.execute(args)
            tools.turn.started -= self.cfg.job_timeout_seconds + 1
            with self.assertRaises(TimeoutError):
                tools.execute(
                    {
                        "command": "x",
                        "cwd": str(self.root / "diag"),
                        "timeout_seconds": 1,
                    }
                )
            backend.assert_not_called()

    def test_interrupt_races_command_registration_and_waits_for_exit(self):
        tools = self.tools()
        started, killed = threading.Event(), threading.Event()
        fake = Mock()
        fake.config = self.cfg
        calls = []

        def rpc(method, params, **kwargs):
            if method == "command/exec":
                started.set()
                self.assertTrue(killed.wait(3))
                return {"exitCode": 143, "stdout": "", "stderr": ""}
            calls.append(params["processId"])
            if len(calls) == 1:
                raise RuntimeError("process registration pending")
            killed.set()
            return {}

        fake.call.side_effect = rpc
        with patch("mo_bot.remote.Backend", return_value=fake):
            thread = threading.Thread(
                target=lambda: tools.execute(
                    {
                        "command": "sleep 100",
                        "cwd": str(self.root / "diag"),
                        "timeout_seconds": 30,
                    }
                )
            )
            thread.start()
            self.assertTrue(started.wait(2))
            tools.close()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertGreaterEqual(len(calls), 2)
            self.assertEqual(len(set(calls)), 1)
            self.assertTrue(tools.done.is_set())

    def test_ambiguous_exec_timeout_requires_confirmed_terminate(self):
        tools = self.tools()
        fake = Mock()
        fake.config = self.cfg
        fake.call.side_effect = [TimeoutError("response lost"), {}]
        with patch("mo_bot.remote.Backend", return_value=fake):
            with self.assertRaises(TimeoutError):
                tools.execute(
                    {
                        "command": "sleep 100",
                        "cwd": str(self.root / "diag"),
                        "timeout_seconds": 30,
                    }
                )
            self.assertFalse(tools.done.is_set())
            self.assertIsNotNone(tools.active)
            tools.close()
            self.assertTrue(tools.done.is_set())
            self.assertEqual(fake.call.call_args.args[0], "command/exec/terminate")

    def test_unconfirmed_cleanup_does_not_release_backend(self):
        tools = self.tools()
        tools.backend = Mock()
        tools.active = "running"
        tools.done.clear()
        tools.backend.call.side_effect = RuntimeError("unknown process")
        with patch("mo_bot.remote.time.monotonic", side_effect=[0, 13]):
            with self.assertRaisesRegex(RuntimeError, "fenced"):
                tools.close()
        tools.backend.close.assert_not_called()
        self.assertFalse(tools.done.is_set())

    def test_native_tools_disabled_and_remote_paths_not_local(self):
        remote = RemoteBackend.__new__(RemoteBackend)
        remote.tools = {}
        remote.host_config, remote.events = self.cfg, Events()
        params = remote.session_params(
            {"cwd": str(self.root), "config": {}, "developerInstructions": "Review"}
        )
        self.assertFalse(params["config"]["features.shell_tool"])
        self.assertFalse(params["config"]["features.unified_exec"])
        self.assertEqual(params["dynamicTools"][0]["name"], "host_exec")
        turn = SimpleNamespace(
            session_id="sid",
            workspace=self.root,
            directory=self.root,
            started=time.monotonic(),
        )
        policy = {"type": "workspaceWrite", "writableRoots": [str(self.root)]}
        result = remote.turn_params(
            turn, {"sandboxPolicy": policy, "cwd": str(self.root)}
        )
        self.assertEqual(
            result["sandboxPolicy"], {"type": "readOnly", "networkAccess": False}
        )
        self.assertIs(remote.tools["sid"].policy, policy)

    def test_old_turn_cannot_execute_host_command(self):
        remote = RemoteBackend.__new__(RemoteBackend)
        remote.lock, remote.config, remote.dead = threading.RLock(), self.cfg, False
        remote.send = Mock()
        turn = SimpleNamespace(turn_id="new", completed=False, stopping_at=None)
        bridge = Mock(request_running=False)
        remote.tools, remote.turns = {"sid": bridge}, {"sid": turn}
        remote.server_request(
            {
                "id": 7,
                "method": "item/tool/call",
                "params": {
                    "threadId": "sid",
                    "turnId": "old",
                    "tool": "host_exec",
                    "arguments": {},
                },
            }
        )
        deadline = time.monotonic() + 2
        while bridge.request_running and time.monotonic() < deadline:
            time.sleep(0.01)
        bridge.execute.assert_not_called()
        self.assertFalse(remote.send.call_args.args[0]["result"]["success"])

    def test_remote_disconnect_does_not_restart_local_backend(self):
        local, remote, replacement = (
            Mock(dead=False),
            Mock(dead=False, cleanup_error=None),
            Mock(dead=False, cleanup_error=None),
        )
        replacement.call.return_value = {"data": [{"model": "remote-model"}]}
        with (
            patch("mo_bot.nodes.Backend", return_value=local),
            patch("mo_bot.nodes.RemoteBackend", side_effect=[remote, replacement]),
        ):
            remote.call.return_value = {"data": []}
            nodes = Nodes(self.cfg, Events())
            slot = {"node": "idc55"}
            self.assertIs(nodes.connect(slot), remote)
            remote.dead = True
            nodes.retry_at.clear()
            self.assertIs(nodes.connect(slot), replacement)
            local.close.assert_not_called()
            self.assertFalse(nodes.dead)
            self.assertEqual(nodes.models_for(slot, []), [{"model": "remote-model"}])
            nodes.close()

    def test_unreachable_remote_backoff_leaves_local_available(self):
        with (
            patch(
                "mo_bot.nodes.Backend",
                return_value=Mock(dead=False, cleanup_error=None),
            ),
            patch(
                "mo_bot.nodes.RemoteBackend",
                side_effect=RuntimeError("SSH unavailable"),
            ) as factory,
        ):
            nodes = Nodes(self.cfg, Events())
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    nodes.connect({"node": "idc55"})
            self.assertEqual(factory.call_count, 1)
            self.assertTrue(nodes.available({"node": "local"}))
            self.assertFalse(nodes.available({"node": "idc55"}))
            self.assertEqual(
                nodes.models_for({"node": "idc55"}, [{"model": "local"}]), []
            )
            self.assertEqual(nodes.status()[1]["error"], "SSH unavailable")
            nodes.close()

    def test_history_routes_to_thread_host(self):
        with patch("mo_bot.nodes.Backend", return_value=Mock(dead=False)):
            nodes = Nodes(self.cfg, Events())
            remote = Mock()
            nodes.threads["remote-sid"] = remote
            nodes.call("thread/read", {"threadId": "remote-sid"})
            remote.call.assert_called_once_with(
                "thread/read", {"threadId": "remote-sid"}
            )
            nodes.local.call.assert_not_called()
            nodes.close()

    def test_local_backend_failure_does_not_restart_remote_sessions(self):
        local = Mock(dead=False, cleanup_error=None)
        replacement = Mock(dead=False, cleanup_error=None)
        replacement.call.return_value = {"data": [{"model": "new-local"}]}
        remote = Mock(dead=False, cleanup_error=None)
        with patch("mo_bot.nodes.Backend", side_effect=[local, replacement]):
            nodes = Nodes(self.cfg, Events())
            nodes.backends["idc55"] = remote
            local.dead = True
            self.assertFalse(nodes.dead)
            self.assertTrue(nodes.available({"node": "idc55"}))
            self.assertIs(nodes.connect({"node": "local"}), replacement)
            remote.close.assert_not_called()
            self.assertEqual(
                nodes.models_for({"node": "local"}, []), [{"model": "new-local"}]
            )
            nodes.close()

    def test_endpoint_change_clears_idle_id_and_blocks_leased_move(self):
        store = Store(self.root / "db", "test", 1, 1)
        self.addCleanup(store.close)
        slot = store.slots()[0]
        store.bind_node(slot, "local", "local")
        store.slot_update(slot["id"], session_id="local-id", ready=1)
        store.bind_node(store.slots()[0], "idc55", json.dumps(NODE))
        updated = store.slots()[0]
        self.assertEqual(updated["node"], "idc55")
        self.assertIsNone(updated["session_id"])
        store.slot_update(updated["id"], ready=1)
        store.enqueue({"repo": "o/r", "number": 1, "headRefOid": "a" * 40})
        store.claim(updated["id"])
        with self.assertRaisesRegex(ValueError, "leased"):
            store.bind_node(store.slots()[0], "idc55", "different-endpoint")
        self.assertEqual(store.slots()[0]["node"], "idc55")

    def test_ssh_is_noninteractive_strict_and_quoted(self):
        command = ssh_command(self.cfg, {**NODE, "codex": "/opt/codex with spaces"})
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("BatchMode=yes", command)
        self.assertIn("-T", command)
        self.assertIn("'/opt/codex with spaces'", command[-1])
        self.assertNotIn("-A", command)

    def test_config_accepts_node_and_rejects_bad_targets(self):
        (self.root / "q").touch()
        config = self.root / "config.toml"
        text = f'''[service]
id="test"
state_dir="{self.root}"
[review]
repository="o/r"
reviewer="me"
source_dir="{self.root}"
queue_script="{self.root}/q"
codex="/bin/true"
gh="/bin/true"
[[node]]
name="idc55"
ssh="me@host55"
codex="/opt/codex"
[[session]]
slot=0
node="idc55"
'''
        config.write_text(text)
        self.assertEqual(Config.load(config).nodes[0]["name"], "idc55")
        for invalid in (
            'ssh="-oProxyCommand=evil"',
            'ssh="host; touch /tmp/x"',
            'codex="codex"',
            'name="local"',
        ):
            original = (
                'codex="/opt/codex"'
                if invalid.startswith("codex")
                else 'name="idc55"'
                if invalid.startswith("name")
                else 'ssh="me@host55"'
            )
            config.write_text(text.replace(original, invalid))
            with self.assertRaises(ValueError):
                Config.load(config)

    def runtime(self, heartbeat=2, lock=2):
        executable = self.root / "fake-codex"
        executable.write_text("""#!/usr/bin/python3
import json, os, sys
print(json.dumps({"child":os.getpid()}),flush=True)
for line in sys.stdin:
 print(line.strip(),flush=True)
""")
        executable.chmod(0o755)
        script = f"""from unittest.mock import patch
from pathlib import Path
from mo_bot import ssh_runtime
ssh_runtime.HEARTBEAT_SECONDS={heartbeat}
ssh_runtime.LOCK_SECONDS={lock}
with patch.object(Path, 'home', return_value=Path({str(self.root)!r})):
 ssh_runtime.main()
"""
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", script, "test", "n", str(executable), "{}"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def cleanup():
            if process.poll() is None:
                process.stdin.close()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(2)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

        self.addCleanup(cleanup)
        return process

    def assert_child_gone(self, pid):
        path = Path(f"/proc/{pid}/stat")
        self.assertFalse(path.exists(), f"child {pid} leaked")

    def test_runtime_eof_reaps_child_before_exiting(self):
        process = self.runtime()
        pid = json.loads(process.stdout.readline())["child"]
        process.stdin.write('{"method":"initialize","id":1}\n')
        process.stdin.flush()
        self.assertEqual(json.loads(process.stdout.readline())["id"], 1)
        process.stdin.close()
        self.assertEqual(process.wait(5), 0, process.stderr.read())
        self.assert_child_gone(pid)

    def test_runtime_heartbeat_loss_reaps_child_even_with_open_stdin(self):
        process = self.runtime(heartbeat=0.3)
        pid = json.loads(process.stdout.readline())["child"]
        self.assertEqual(process.wait(5), 0, process.stderr.read())
        self.assert_child_gone(pid)

    def test_runtime_live_heartbeat_keeps_owner_and_second_owner_is_rejected(self):
        process = self.runtime(heartbeat=3)
        pid = json.loads(process.stdout.readline())["child"]
        competitor = self.runtime(heartbeat=3, lock=0.2)
        self.assertNotEqual(competitor.wait(4), 0)
        self.assertIn("still owned", competitor.stderr.read())
        process.stdin.write(
            '{"method":"mo_bot/heartbeat"}\n{"method":"probe","id":5}\n'
        )
        process.stdin.flush()
        # Heartbeats never enter Codex's JSON-RPC stream.
        self.assertEqual(json.loads(process.stdout.readline())["id"], 5)
        process.stdin.close()
        self.assertEqual(process.wait(5), 0, process.stderr.read())
        self.assert_child_gone(pid)

    def test_crash_recovery_reaps_owned_child_in_other_process_group(self):
        parent = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                "import subprocess,time; child=subprocess.Popen(['sleep','60'],start_new_session=True); print(child.pid,flush=True);time.sleep(60)",
            ],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        child = int(parent.stdout.readline())
        other = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            state = self.root / "host-tools" / "thread"
            state.mkdir(parents=True)
            owner = state / "backend-owner.json"
            owner.write_text(json.dumps(process_identity(parent.pid)))
            recover_host_tools(self.cfg)
            parent.wait(3)
            self.assertFalse(owner.exists())
            self.assertIsNone(other.poll())
            try:
                fields = (
                    Path(f"/proc/{child}/stat").read_text().rsplit(")", 1)[1].split()
                )
                self.assertEqual(fields[0], "Z")
            except FileNotFoundError:
                pass
        finally:
            for process in (parent, other):
                if process.poll() is None:
                    process.kill()
                process.wait(3)
            parent.stdout.close()

    def test_stale_pid_identity_cannot_kill_an_unrelated_process(self):
        process = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            identity = process_identity(process.pid)
            identity["start_ticks"] = "incorrect-generation"
            stop_owned(identity, descendants=True)
            self.assertIsNone(process.poll())
        finally:
            process.kill()
            process.wait(3)

    def test_corrupt_recovery_record_blocks_instead_of_deleting(self):
        root = self.root / "host-tools" / "thread"
        root.mkdir(parents=True)
        owner = root / "backend-owner.json"
        owner.write_text("corrupt")
        with self.assertRaises(ValueError):
            recover_host_tools(self.cfg)
        self.assertTrue(owner.exists())

    def test_cleanup_failure_retains_active_pr_and_slot_without_retry_budget(self):
        store = Store(self.root / "lease-db", "test", 2)
        self.addCleanup(store.close)
        for slot in store.slots():
            store.slot_update(slot["id"], ready=1)
        store.enqueue({"repo": "o/r", "number": 1, "headRefOid": "a" * 40})
        job = store.claim(0)
        service = Service.__new__(Service)
        service.store = store
        turn = Mock(completed=True)
        turn.result.side_effect = CleanupPending("process cleanup unconfirmed")
        service.turns = {0: (turn, job["id"])}
        for _ in range(2):
            service.finish(0, turn, job["id"])
        current = store.job(job["id"])
        self.assertEqual(current["state"], "reviewing")
        self.assertEqual(current["slot_id"], 0)
        self.assertEqual(current["attempts"], 0)
        turn.release.assert_not_called()
        self.assertIn(0, service.turns)
        store.enqueue({"repo": "o/r", "number": 1, "headRefOid": "b" * 40})
        self.assertIsNone(store.claim(1))

    def test_release_sets_and_clears_node_cleanup_fence(self):
        remote = RemoteBackend.__new__(RemoteBackend)
        remote.cleanup_error = None
        bridge = Mock()
        bridge.close.side_effect = [RuntimeError("unconfirmed"), None]
        remote.tools = {"sid": bridge}
        turn = SimpleNamespace(session_id="sid")
        with self.assertRaises(CleanupPending):
            remote.before_release(turn)
        self.assertEqual(remote.cleanup_error, "unconfirmed")
        self.assertIn("sid", remote.tools)
        remote.before_release(turn)
        self.assertIsNone(remote.cleanup_error)
        self.assertFalse(remote.tools)

    def test_missing_remote_thread_replaced_with_dynamic_host_tool(self):
        remote = RemoteBackend.__new__(RemoteBackend)
        remote.config = self.cfg
        remote.call = Mock(
            side_effect=[
                RuntimeError("thread not found"),
                {"thread": {"id": "replacement"}},
            ]
        )
        result, replaced = remote.session(
            {
                "session_id": "missing",
                "model": "gpt-6.1-sol",
                "effort": "low",
                "role": "worker",
            },
            self.root,
        )
        self.assertTrue(replaced)
        self.assertEqual(result["thread"]["id"], "replacement")
        resume, start = remote.call.call_args_list
        self.assertEqual(resume.args[0], "thread/resume")
        self.assertNotIn("dynamicTools", resume.args[1])
        self.assertEqual(start.args[0], "thread/start")
        self.assertEqual(start.args[1]["dynamicTools"][0]["name"], "host_exec")

    def test_provider_error_does_not_retire_remote_thread(self):
        remote = RemoteBackend.__new__(RemoteBackend)
        remote.config = self.cfg
        remote.call = Mock(side_effect=RuntimeError("provider disconnected"))
        with self.assertRaises(RuntimeError):
            remote.session(
                {"session_id": "healthy", "model": "gpt-6.1-sol", "effort": "low"},
                self.root,
            )
        self.assertEqual(remote.call.call_count, 1)

    def test_unknown_thread_never_falls_back_to_local_host(self):
        with patch("mo_bot.nodes.Backend", return_value=Mock(dead=False)):
            nodes = Nodes(self.cfg, Events())
            with self.assertRaisesRegex(RuntimeError, "not bound"):
                nodes.call("thread/read", {"threadId": "unknown-remote"})
            nodes.local.call.assert_not_called()
            nodes.close()


if __name__ == "__main__":
    unittest.main()
