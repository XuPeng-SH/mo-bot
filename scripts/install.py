#!/usr/bin/python3
"""Install local CLI launchers, one shared skill and a user service unit."""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys


def link(target, source):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if target.is_symlink() and target.resolve() == source.resolve():
            return
        raise RuntimeError(f"refusing to replace existing {target}")
    target.symlink_to(source, target_is_directory=source.is_dir())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start", action="store_true", help="enable and start the installed service"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    home = Path.home()
    for name in ["mo-bot", "mo-botctl"]:
        link(home / ".local/bin" / name, root / "scripts" / name)
    skill = root / ".agents/skills/mo-bot-service"
    for parent in [home / ".codex/skills", home / ".claude/skills"]:
        link(parent / "mo-bot-service", skill)
    config = home / ".config/mo-bot/config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    if not config.exists():
        shutil.copyfile(root / "config.example.toml", config)
    unit = home / ".config/systemd/user/mo-bot.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    # Preserve the local environment file, if present (e.g. outbound proxy settings).
    env = config.parent / "environment"
    content = f"""[Unit]
Description=Reusable Codex PR review service
After=network-online.target

[Service]
Type=simple
WorkingDirectory={root}
ExecStart={sys.executable} -m mo_bot.cli --config {config}
Environment=PATH={home}/.local/bin:/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=-{env}
Restart=on-failure
RestartSec=10
KillMode=control-group
TimeoutStopSec=30
UMask=0077

[Install]
WantedBy=default.target
"""
    unit.write_text(content)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if args.start:
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "mo-bot.service"], check=True
        )
    print("Installed mo-bot/mo-botctl and mo-bot-service skill for Codex and Claude.")
    print(f"Config: {config}. Start: systemctl --user enable --now mo-bot.service")


if __name__ == "__main__":
    main()
