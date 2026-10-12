import argparse
import json
import logging
from pathlib import Path
import subprocess

from .config import Config
from .installation import DEFAULT_CONFIG, doctor, initialize, install, uninstall


def main():
    parser = argparse.ArgumentParser(description="Durable reusable Codex PR reviewer")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="command")
    for name in ("serve", "init", "doctor", "install", "uninstall"):
        child = commands.add_parser(name)
        child.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        if name == "init":
            child.add_argument("--repository", required=True)
            child.add_argument("--source-dir", type=Path, required=True)
            child.add_argument("--reviewer", help="defaults to authenticated gh user")
            child.add_argument("--worker-sessions", type=int, default=0)
        if name in ("install", "uninstall"):
            child.add_argument("--prefix", type=Path, default=Path.home() / ".local")
        if name == "install":
            child.add_argument("--agent", choices=("codex", "claude"), action="append")
            child.add_argument(
                "--skill-dir",
                type=Path,
                action="append",
                help="explicit skill root instead of agent home directories",
            )
            child.add_argument(
                "--systemd", action="store_true", help="install optional user unit"
            )
            child.add_argument(
                "--start", action="store_true", help="preflight and start user service"
            )
    args = parser.parse_args()
    try:
        if args.command == "init":
            path = initialize(
                args.config,
                args.repository,
                args.source_dir,
                args.reviewer,
                args.worker_sessions,
            )
            print(f"Created {path}; run mo-bot doctor --config {path}")
        elif args.command == "doctor":
            checks = doctor(args.config)
            print(json.dumps(checks, indent=2))
            if not all(item["ok"] for item in checks):
                raise SystemExit(1)
        elif args.command == "install":
            roots = args.skill_dir or [
                Path.home() / ("." + agent) / "skills"
                for agent in (args.agent or ["codex", "claude"])
            ]
            files = install(
                args.prefix, roots, args.config, systemd=args.systemd, start=args.start
            )
            print(
                json.dumps(
                    {
                        "installed": files,
                        "config": str(args.config),
                        "next": "mo-bot init, then mo-bot doctor, then mo-bot serve",
                    },
                    indent=2,
                )
            )
        elif args.command == "uninstall":
            retained = uninstall(args.prefix)
            print(
                json.dumps(
                    {
                        "retained_modified_files": retained,
                        "config_and_state": "preserved",
                    },
                    indent=2,
                )
            )
            if retained:
                raise SystemExit(1)
        else:
            from .service import Service

            logging.basicConfig(
                level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
            )
            config_path = args.config.expanduser().resolve()
            Service(Config.load(config_path), config_path).run()
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        subprocess.SubprocessError,
    ) as error:
        parser.exit(1, f"mo-bot: {error}\n")


if __name__ == "__main__":
    main()
