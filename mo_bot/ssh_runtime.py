"""SSH stdio supervisor, sent as code to the remote host; no workflow state."""

import fcntl
import json
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
import threading
import time

HEARTBEAT_SECONDS = 30
LOCK_SECONDS = 35


def main():
    service, node, executable, network_env = sys.argv[1:]
    root = Path.home() / ".local/state/mo-bot-runtime" / service / node
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "runtime.lock").open("a+") as lock:
        # A disconnected owner has at most 30 seconds to reap its child first.
        deadline = time.monotonic() + LOCK_SECONDS
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("remote runtime is still owned")
                time.sleep(0.1)
        child = subprocess.Popen(
            [executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            cwd=root,
            start_new_session=True,
            env={**os.environ, **json.loads(network_env)},
            bufsize=0,
        )
        closed = threading.Event()
        last_input = [time.monotonic()]
        os.set_blocking(child.stdin.fileno(), False)
        os.set_blocking(sys.stdout.fileno(), False)

        def write(fd, data):
            data = memoryview(data)
            while data and not closed.is_set():
                if not select.select([], [fd], [], 0.2)[1]:
                    continue
                try:
                    data = data[os.write(fd, data) :]
                except BlockingIOError:
                    continue

        def forward_input():
            try:
                pending = b""
                fd = sys.stdin.fileno()
                while not closed.is_set():
                    if not select.select([fd], [], [], 0.2)[0]:
                        continue
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        if len(line) > 8 * 1024**2:
                            raise ValueError("input frame limit exceeded")
                        last_input[0] = time.monotonic()
                        if json.loads(line).get("method") != "mo_bot/heartbeat":
                            write(child.stdin.fileno(), line + b"\n")
                    if len(pending) > 8 * 1024**2:
                        raise ValueError("input frame limit exceeded")
            except (OSError, ValueError):
                pass
            finally:
                closed.set()

        def forward_output():
            try:
                while not closed.is_set():
                    if not select.select([child.stdout.fileno()], [], [], 0.2)[0]:
                        continue
                    chunk = os.read(child.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    write(sys.stdout.fileno(), chunk)
            except OSError:
                pass
            finally:
                closed.set()

        def stop(signum, frame):
            closed.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        threads = [
            threading.Thread(target=f, daemon=True)
            for f in (forward_input, forward_output)
        ]
        for thread in threads:
            thread.start()
        try:
            while not closed.wait(0.2):
                if time.monotonic() - last_input[0] > HEARTBEAT_SECONDS:
                    break
        finally:
            closed.set()
            # Keep the lock until the owned process group is gone, even if its leader exited.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(child.pid, sig)
                except ProcessLookupError:
                    break
                if sig == signal.SIGTERM:
                    time.sleep(0.5)
            child.wait(timeout=10)
            for thread in threads:
                thread.join(timeout=2)
            child.stdin.close()
            child.stdout.close()


if __name__ == "__main__":
    main()
