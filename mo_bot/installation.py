"""Portable setup; installation owns only recorded files and skill links."""

import getpass
import fcntl
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from .config import Config

PACKAGE = Path(__file__).resolve().parent
DEFAULT_CONFIG = Path.home() / ".config/mo-bot/config.toml"


def initialize(path, repository, source_dir, reviewer=None, worker_sessions=0):
    source_dir = Path(source_dir).expanduser().resolve()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must be owner/name")
    subprocess.run(
        ["git", "-C", str(source_dir), "rev-parse", "--show-toplevel"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=15,
    )
    if not reviewer:
        reviewer = subprocess.check_output(
            ["gh", "api", "user", "--jq", ".login"], text=True, timeout=30
        ).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", reviewer):
        raise ValueError("reviewer must be a GitHub login")
    if not 0 <= worker_sessions <= 8:
        raise ValueError("worker sessions must be 0..8")
    path = Path(path).expanduser().absolute()
    state = Path.home() / ".local/state/mo-bot"

    def quote(value):
        return json.dumps(str(value), ensure_ascii=False)

    service_id = "mo-bot-" + re.sub(r"[^A-Za-z0-9_.-]", "-", getpass.getuser())[:60]
    content = f"""[service]
id = {quote(service_id)}
state_dir = {quote(state)}
poll_seconds = 300

[review]
repository = {quote(repository)}
reviewer = {quote(reviewer)}
source_dir = {quote(source_dir)}
sessions = 2
model = "gpt-6-astra"
reasoning_effort = "xhigh"
exclude = []
minimum_free_gib = 20
codex = "codex"
gh = "gh"

[worker]
sessions = {worker_sessions}
classifier_sessions = {1 if worker_sessions else 0}
model = "gpt-6.1-sol"
reasoning_effort = "low"

[api]
host = "127.0.0.1"
port = 8989
"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(content)
    return path


def doctor(path):
    """Read-only bounded preflight; do not launch turns or write GitHub."""
    checks = []
    try:
        config = Config.load(Path(path).expanduser())
    except (ValueError, TypeError, KeyError, OSError) as error:
        return [{"check": "configuration", "ok": False, "detail": str(error)}]
    checks.append({"check": "configuration", "ok": True})
    for name, command in [
        (
            "source checkout",
            ["git", "-C", str(config.source_dir), "rev-parse", "--verify", "HEAD"],
        ),
        ("GitHub authentication", [config.gh, "api", "user", "--jq", ".login"]),
        ("Codex authentication", [config.codex, "login", "status"]),
        ("Codex app-server", [config.codex, "app-server", "--help"]),
    ]:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=min(config.command_timeout_seconds, 30),
            )
            ok = result.returncode == 0
            if ok and name == "GitHub authentication" and config.worker_sessions:
                ok = result.stdout.strip().casefold() == config.reviewer.casefold()
            checks.append(
                {
                    "check": name,
                    "ok": ok,
                    "detail": "ready"
                    if ok
                    else "unavailable or wrong account; check login/configuration",
                }
            )
        except (OSError, subprocess.TimeoutExpired):
            checks.append(
                {"check": name, "ok": False, "detail": "command missing or timed out"}
            )
    location = config.state_dir
    while not location.exists():
        location = location.parent
    available = shutil.disk_usage(location).free / 1024**3
    checks.append(
        {
            "check": "state disk space",
            "ok": available >= config.minimum_free_gib,
            "detail": f"{available:.1f} GiB free; requires {config.minimum_free_gib} GiB",
        }
    )
    for name in ("mo-bot-service", "mo-bot-review", "mo-bot-worker"):
        checks.append(
            {
                "check": name + " packaged skill",
                "ok": (PACKAGE / "skills" / name / "SKILL.md").is_file(),
            }
        )
    return checks


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".mo-bot-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def fingerprint(path):
    if path.is_symlink():
        return {"link": os.readlink(path)}
    if path.is_file():
        return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return None


def owned(record, actual):
    return actual is not None and (
        actual == record or (isinstance(record, list) and actual in record)
    )


def manifest_path(prefix):
    return Path(prefix).expanduser().resolve() / "share/mo-bot/install.json"


def load_manifest(prefix):
    path = manifest_path(prefix)
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if value.get("format") != 1 or not isinstance(value.get("files"), dict):
        raise ValueError("invalid mo-bot installation manifest")
    return value["files"]


def systemd_value(value):
    value = str(value)
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError("systemd path contains a control character")
    return value.replace("%", "%%")


def systemd_quote(value):
    value = systemd_value(value)
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def serialized(operation):
    @wraps(operation)
    def run(prefix, *args, **kwargs):
        lock = manifest_path(prefix).with_suffix(".lock")
        lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return operation(prefix, *args, **kwargs)

    return run


@serialized
def install(
    prefix, skill_dirs, config_path, *, systemd=False, start=False, unit_dir=None
):
    prefix = Path(prefix).expanduser().resolve()
    config_path = Path(config_path).expanduser().resolve()
    previous = load_manifest(prefix)
    plan = {}
    root = PACKAGE.parent
    # In a wheel, pip owns the CLI entry points. A source install links the checkout.
    for name in ("mo-bot", "mo-botctl"):
        source = root / "scripts" / name
        if source.is_file():
            plan[prefix / "bin" / name] = {"link": str(source)}
    for directory in skill_dirs:
        directory = Path(directory).expanduser().resolve()
        for name in ("mo-bot-service", "mo-bot-review", "mo-bot-worker"):
            source = PACKAGE / "skills" / name
            if not (source / "SKILL.md").is_file():
                raise ValueError(f"packaged skill missing: {name}")
            plan[directory / name] = {"link": str(source)}
    systemd = systemd or start
    if systemd:
        if shutil.which("systemctl") is None:
            raise ValueError("systemctl unavailable; use mo-bot serve")
        # Confirm the user manager exists before installing an unusable unit.
        subprocess.run(
            ["systemctl", "--user", "show-environment"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        unit_dir = Path(unit_dir) if unit_dir else Path.home() / ".config/systemd/user"
        env = config_path.parent / "environment"
        executable = " ".join(
            systemd_quote(p)
            for p in (
                sys.executable,
                "-m",
                "mo_bot.cli",
                "serve",
                "--config",
                config_path,
            )
        )
        plan[unit_dir / "mo-bot.service"] = {
            "text": f"""[Unit]
Description=Reusable Codex PR review service
After=network-online.target

[Service]
Type=simple
WorkingDirectory={systemd_value(root)}
ExecStart={executable}
Environment={systemd_quote("PATH=" + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))}
EnvironmentFile=-{systemd_value(env)}
Restart=on-failure
RestartSec=10
KillMode=control-group
TimeoutStopSec=180
UMask=0077

[Install]
WantedBy=default.target
"""
        }
    if start:
        checks = doctor(config_path)
        if not all(item["ok"] for item in checks):
            raise ValueError(
                "preflight failed; run mo-bot doctor --config " + str(config_path)
            )
    # Preflight the entire footprint before changing any path. Do not follow symlinks.
    for path, wanted in plan.items():
        actual = fingerprint(path)
        is_owned = owned(previous.get(str(path)), actual)
        same_link = (
            "link" in wanted
            and path.is_symlink()
            and path.resolve() == Path(wanted["link"]).resolve()
        )
        if (path.exists() or path.is_symlink()) and not (is_owned or same_link):
            raise ValueError(f"refusing to replace unowned or modified path: {path}")
    records = dict(previous)
    intent = dict(previous)
    for path, wanted in plan.items():
        expected = (
            {"link": wanted["link"]}
            if "link" in wanted
            else {"sha256": hashlib.sha256(wanted["text"].encode()).hexdigest()}
        )
        records[str(path)] = expected
        actual = fingerprint(path)
        intent[str(path)] = (
            [actual, expected] if actual and actual != expected else expected
        )
    # Persist ownership intent first: interrupted installation can be retried.
    manifest = manifest_path(prefix)
    atomic_write(manifest, json.dumps({"format": 1, "files": intent}, indent=2))
    for path, wanted in plan.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if "link" in wanted:
            if path.is_symlink() and os.readlink(path) == wanted["link"]:
                continue
            if path.exists() or path.is_symlink():
                path.unlink()
            path.symlink_to(
                wanted["link"], target_is_directory=Path(wanted["link"]).is_dir()
            )
        else:
            atomic_write(path, wanted["text"])
    atomic_write(manifest, json.dumps({"format": 1, "files": records}, indent=2))
    if systemd:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15)
    if start:
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "mo-bot.service"],
            check=True,
            timeout=30,
        )
    return list(map(str, plan))


@serialized
def uninstall(prefix):
    records = load_manifest(prefix)
    retained = {}
    units = [
        Path(p)
        for p, expected in records.items()
        if Path(p).name == "mo-bot.service" and owned(expected, fingerprint(Path(p)))
    ]
    if units:
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "mo-bot.service"],
            check=True,
            timeout=30,
        )
    for name, expected in records.items():
        path = Path(name)
        if not path.exists() and not path.is_symlink():
            continue
        if owned(expected, fingerprint(path)):
            path.unlink()
        else:
            retained[name] = expected
    manifest = manifest_path(prefix)
    if retained:
        atomic_write(manifest, json.dumps({"format": 1, "files": retained}, indent=2))
    else:
        manifest.unlink(missing_ok=True)
    if units:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15)
    return list(retained)
