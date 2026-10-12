"""Remote model execution; all host tools use the existing local Codex sandbox."""

import base64
import hashlib
from dataclasses import replace
import json
from pathlib import Path
import shlex
import subprocess
import threading
import time
import uuid

from .rpc import Backend
from .codex import stop_owned


class CleanupPending(RuntimeError):
    """The turn/workspace must stay leased until tool cleanup is confirmed."""


HOST_TOOL = {
    "type": "function",
    "name": "host_exec",
    "description": "Run a bounded shell command on the coordinator host. All source files and diagnostics are on that host. Use this for reading/editing files, searches, tests and local analysis. No background jobs. Never use remote filesystem tools for task evidence.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
        },
        "required": ["command", "cwd", "timeout_seconds"],
        "additionalProperties": False,
    },
}


def ssh_command(config, node):
    script = Path(__file__).with_name("ssh_runtime.py").read_bytes()
    bootstrap = (
        "import base64;exec(compile(base64.b64decode("
        + repr(base64.b64encode(script).decode())
        + "),'<mo-bot-ssh>','exec'))"
    )
    remote = shlex.join(
        [
            "python3",
            "-u",
            "-c",
            bootstrap,
            config.service_id,
            node["name"],
            node["codex"],
            json.dumps(node.get("env", {})),
        ]
    )
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=3",
        node["ssh"],
        remote,
    ]


class HostTools:
    """One live command per turn, with a cleanup fence before workspace reuse."""

    def __init__(self, remote, turn, policy, workspace):
        self.remote, self.turn = remote, turn
        self.policy, self.workspace = policy, Path(workspace).resolve()
        self.lock = threading.Lock()
        self.cleanup_lock = threading.Lock()
        self.backend = None
        self.closed = False
        self.request_running = False
        self.active = None
        self.uncertain = False
        self.done = threading.Event()
        self.done.set()

    def execute(self, args):
        if (
            not isinstance(args, dict)
            or set(args) != {"command", "cwd", "timeout_seconds"}
            or not isinstance(args["command"], str)
            or not args["command"].strip()
            or len(args["command"]) > 65536
            or not isinstance(args["cwd"], str)
            or type(args["timeout_seconds"]) is not int
            or not 1 <= args["timeout_seconds"] <= 600
        ):
            raise ValueError("invalid host_exec arguments")
        cwd = Path(args["cwd"]).resolve()
        if not Path(args["cwd"]).is_absolute():
            raise ValueError("host_exec cwd must be absolute")
        directory = self.turn.directory.resolve()
        if not cwd.is_relative_to(directory) and not cwd.is_relative_to(self.workspace):
            raise ValueError(
                "host_exec cwd must be inside the assigned source or diagnostic directory"
            )
        if (
            time.monotonic() - self.turn.started
            >= self.remote.host_config.job_timeout_seconds
        ):
            raise TimeoutError("turn execution budget exhausted")
        with self.lock:
            if self.closed or self.active:
                raise RuntimeError("host tools closed or another command is running")
            if self.backend is None:
                state = (
                    self.remote.host_config.state_dir
                    / "host-tools"
                    / hashlib.sha256(self.turn.session_id.encode()).hexdigest()[:24]
                )
                state.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.backend = Backend(
                    replace(self.remote.host_config, state_dir=state),
                    self.remote.events,
                    experimental=True,
                )
            process = "mo-bot-" + uuid.uuid4().hex
            self.active = process
            self.uncertain = False
            self.done.clear()
            backend = self.backend
        budget = max(
            1,
            min(
                args["timeout_seconds"],
                int(
                    self.remote.host_config.job_timeout_seconds
                    - (time.monotonic() - self.turn.started)
                ),
            ),
        )
        finished = False
        try:
            result = backend.call(
                "command/exec",
                {
                    "command": ["/bin/sh", "-c", args["command"]],
                    "cwd": str(cwd),
                    "env": {
                        "TMPDIR": str(directory / "tmp"),
                        "GOTMPDIR": str(directory / "tmp"),
                        "GOWORK": "off",
                    },
                    "timeoutMs": budget * 1000,
                    "outputBytesCap": 65536,
                    "processId": process,
                    "sandboxPolicy": self.policy,
                },
                timeout=budget + 15,
            )
            finished = True
            return {
                "contentItems": [{"type": "inputText", "text": json.dumps(result)}],
                "success": result.get("exitCode") == 0,
            }
        finally:
            with self.lock:
                self.uncertain = not finished
                if finished:
                    self.active = None
                    self.done.set()

    def close(self):
        with self.cleanup_lock:
            self._close()

    def _close(self):
        with self.lock:
            self.closed = True
            backend = self.backend
        if not backend:
            return
        # Registration and command startup can race interruption. Retry terminate
        # until the command response proves it stopped; never release on an unknown ID.
        deadline = time.monotonic() + 12
        while not self.done.is_set() and time.monotonic() < deadline:
            with self.lock:
                process = self.active
            if process:
                try:
                    backend.call(
                        "command/exec/terminate", {"processId": process}, timeout=2
                    )
                    with self.lock:
                        if self.uncertain:
                            self.active = None
                            self.done.set()
                except Exception:
                    pass
            self.done.wait(0.1)
        if not self.done.is_set():
            if not isinstance(backend.owner, dict):
                raise RuntimeError(
                    "host command cleanup unconfirmed; node remains fenced"
                )
            # A missing response or broken RPC cannot confirm termination. This
            # backend owns tools for this turn only, so reap its entire owned tree.
            stop_owned(backend.owner, grace=1, descendants=True)
        backend.close()
        with self.lock:
            self.active = None
            self.done.set()
        (backend.config.state_dir / "backend-owner.json").unlink(missing_ok=True)


class RemoteBackend(Backend):
    def __init__(self, config, events, node):
        self.host_config, self.node = config, node
        self.tools = {}
        self.heartbeat_stop = threading.Event()
        self.cleanup_error = None
        state = config.state_dir / "nodes" / node["name"]
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        super().__init__(
            replace(config, state_dir=state),
            events,
            command=ssh_command(config, node),
            experimental=True,
        )
        self.heartbeat = threading.Thread(
            target=self._heartbeat, name="ssh-heartbeat", daemon=True
        )
        self.heartbeat.start()

    def _heartbeat(self):
        while not self.heartbeat_stop.wait(5):
            try:
                self.send({"method": "mo_bot/heartbeat"})
            except Exception:
                self.close()
                return

    def session_params(self, params):
        params["cwd"] = "."
        params["config"]["features.shell_tool"] = False
        params["config"]["features.unified_exec"] = False
        params["dynamicTools"] = [HOST_TOOL]
        params["developerInstructions"] += (
            " You run on an SSH model host. The source and diagnostic paths in each task exist ONLY on the coordinator. Use host_exec for ALL filesystem, search, edit, test and shell work. Do not use native remote filesystem/shell tools, remote credentials or remote GitHub tools. Do not start background processes."
        )
        return params

    def turn_params(self, turn, params):
        self.tools[turn.session_id] = HostTools(
            self, turn, params["sandboxPolicy"], turn.workspace
        )
        params["cwd"] = "."
        params["sandboxPolicy"] = {"type": "readOnly", "networkAccess": False}
        return params

    def server_request(self, event):
        if event.get("method") != "item/tool/call":
            return False
        params = event.get("params", {})
        with self.lock:
            turn = self.turns.get(params.get("threadId"))
            bridge = self.tools.get(params.get("threadId"))

        def run():
            try:
                deadline = time.monotonic() + self.config.command_timeout_seconds
                while (
                    turn
                    and turn.turn_id is None
                    and not self.dead
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                if (
                    not turn
                    or not bridge
                    or self.dead
                    or turn.completed
                    or turn.stopping_at
                    or params.get("turnId") != turn.turn_id
                    or params.get("tool") != "host_exec"
                    or params.get("namespace") not in (None, "")
                ):
                    raise RuntimeError("stale or unauthorized host tool call")
                result = bridge.execute(params.get("arguments"))
            except Exception as error:
                result = {
                    "contentItems": [{"type": "inputText", "text": str(error)}],
                    "success": False,
                }
            try:
                self.send({"id": event["id"], "result": result})
            except Exception:
                pass

        # Admission is bounded by one request per live turn; reject floods without threads.
        if not bridge or bridge.request_running:
            self.send(
                {
                    "id": event["id"],
                    "result": {
                        "contentItems": [
                            {
                                "type": "inputText",
                                "text": "host tool unavailable or busy",
                            }
                        ],
                        "success": False,
                    },
                }
            )
        else:
            bridge.request_running = True

            def bounded_run():
                try:
                    run()
                finally:
                    bridge.request_running = False

            threading.Thread(target=bounded_run, daemon=True, name="host-tool").start()
        return True

    def before_release(self, turn):
        bridge = self.tools.get(turn.session_id)
        if bridge:
            try:
                bridge.close()
            except Exception as error:
                self.cleanup_error = str(error)
                raise CleanupPending(self.cleanup_error) from error
            self.tools.pop(turn.session_id, None)
            if not self.tools:
                self.cleanup_error = None

    def disconnected(self):
        self.heartbeat_stop.set()
        for bridge in list(self.tools.values()):
            try:
                bridge.close()
            except Exception as error:
                self.cleanup_error = str(error)
        if self.cleanup_error:
            # Keep turns leased. The coordinator cannot consume/retry until cleanup proves safe.
            self.events.emit(
                {
                    "method": "node/fenced",
                    "params": {"node": self.node["name"], "error": self.cleanup_error},
                }
            )
            while self.cleanup_error:
                time.sleep(1)
                try:
                    for bridge in list(self.tools.values()):
                        bridge.close()
                    self.cleanup_error = None
                except Exception:
                    pass

    def close(self):
        self.heartbeat_stop.set()
        for bridge in list(self.tools.values()):
            try:
                bridge.close()
            except Exception as error:
                self.cleanup_error = str(error)
                raise CleanupPending(self.cleanup_error) from error
        # Deliver EOF first so the remote supervisor reaps Codex and releases its
        # lock before reconnect. Killing SSH alone only proves local transport exit.
        if self.process and self.process.poll() is None:
            with self.write_lock:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        super().close()


def recover_host_tools(config):
    """Reap only proven owned local tool trees before recovering workflow state."""
    for path in (config.state_dir / "host-tools").glob("*/backend-owner.json"):
        owner = json.loads(path.read_text())
        stop_owned(owner, grace=1, descendants=True)
        path.unlink()
