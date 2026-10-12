"""A bounded read-only discovery child, shared by review and delivery pools."""

import json
import subprocess
import time

from .codex import process_identity, stop_owned


class Discovery:
    def __init__(self, config, name):
        self.config = config
        self.path = config.state_dir / name
        self.owner_path = config.state_dir / f"{name}-owner.json"
        self.process = None

    def recover(self):
        if self.owner_path.exists():
            stop_owned(json.loads(self.owner_path.read_text()))

    def start(self, args):
        if self.process is not None:
            raise RuntimeError("discovery already running")
        self.path.mkdir(exist_ok=True)
        try:
            self.output = (self.path / "snapshot.json").open("w")
            self.errors = (self.path / "error.txt").open("w")
            self.process = subprocess.Popen(
                args, stdout=self.output, stderr=self.errors, start_new_session=True
            )
            self.owner = process_identity(self.process.pid)
            temporary = self.owner_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.owner))
            temporary.replace(self.owner_path)
            self.started = time.monotonic()
        except Exception:
            self.close()
            raise

    def poll(self):
        if self.process is None:
            return None
        over_limit = (self.path / "snapshot.json").stat().st_size > 64 * 1024**2 or (
            self.path / "error.txt"
        ).stat().st_size > 1024**2
        timed_out = (
            time.monotonic() - self.started >= self.config.command_timeout_seconds
        )
        if self.process.poll() is None and not timed_out and not over_limit:
            return None
        returncode = self.process.poll()
        self.close()
        if over_limit:
            raise RuntimeError("discovery output limit exceeded")
        if timed_out and returncode is None:
            raise RuntimeError("discovery timed out")
        if returncode:
            raise RuntimeError(
                (self.path / "error.txt").read_text()[:2000] or "discovery failed"
            )
        result = json.loads((self.path / "snapshot.json").read_text())
        if not isinstance(result, dict):
            raise ValueError("discovery must return a JSON object")
        return result

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                stop_owned(getattr(self, "owner", None), grace=0.2)
            self.process.wait(timeout=2)
            self.process = None
        for stream in (getattr(self, "output", None), getattr(self, "errors", None)):
            if stream:
                stream.close()
