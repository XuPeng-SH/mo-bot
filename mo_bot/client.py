"""Local operator client; never place bearer tokens in command-line arguments."""

import argparse
import json
from pathlib import Path
import sys
import tomllib
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path.home() / ".config/mo-bot/config.toml"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ["status", "sessions", "jobs", "models", "events"]:
        commands.add_parser(name)
    p = commands.add_parser("enqueue")
    p.add_argument("pr", type=int)
    p = commands.add_parser("chat")
    p.add_argument("slot", type=int)
    p.add_argument("text")
    p = commands.add_parser("model")
    p.add_argument("slot", type=int)
    p.add_argument("model")
    p.add_argument("--effort", default="xhigh")
    for name in ["history", "interrupt", "progress"]:
        p = commands.add_parser(name)
        p.add_argument("slot", type=int)
    args = parser.parse_args()
    config = tomllib.loads(args.config.expanduser().read_text())
    api = config.get("api", {})
    base = f"http://{api.get('host', '127.0.0.1')}:{api.get('port', 8989)}"
    token = (
        (Path(config["service"]["state_dir"]).expanduser() / "api-token")
        .read_text()
        .strip()
    )
    opener = build_opener(ProxyHandler({}))
    if args.command == "progress":
        query = Request(
            base + "/api/sessions", headers={"Authorization": "Bearer " + token}
        )
        with opener.open(query, timeout=10) as response:
            sessions = json.load(response)
        session = next((s for s in sessions if s["id"] == args.slot), None)
        if not session:
            parser.error("unknown session slot")
        if not session["turn_id"]:
            print(
                json.dumps(
                    {
                        "session": session,
                        "message": "No running turn; no model call started.",
                    },
                    indent=2,
                )
            )
            return
    endpoint = "/api/" + args.command
    body = None
    if args.command == "enqueue":
        endpoint = "/api/jobs"
        body = {"pr": args.pr, "force": True}
    elif args.command == "progress":
        endpoint = f"/api/sessions/{args.slot}/messages"
        body = {
            "expected_turn_id": session["turn_id"],
            "text": "请仅以简短 commentary 汇报当前任务的 PR/commit、已完成的检查和证据、正在做什么、剩余验证和阻塞点。不要改变评审目标，不要终止评审，不要重复已完成的工作，汇报后继续当前评审。",
        }
    elif args.command == "chat":
        endpoint = f"/api/sessions/{args.slot}/messages"
        body = {"text": args.text}
    elif args.command == "model":
        endpoint = f"/api/sessions/{args.slot}/model"
        body = {"model": args.model, "reasoning_effort": args.effort}
    elif args.command in ["history", "interrupt"]:
        endpoint = f"/api/sessions/{args.slot}/{args.command}"
        if args.command == "interrupt":
            body = {}
    request = Request(
        base + endpoint,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        data=None if body is None else json.dumps(body).encode(),
    )
    # Local API traffic must bypass the machine's outbound HTTP proxy.
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=100) as response:
            if args.command == "events":
                for line in response:
                    sys.stdout.buffer.write(line)
                    sys.stdout.flush()
            else:
                print(json.dumps(json.load(response), indent=2, ensure_ascii=False))
    except HTTPError as error:
        print(error.read().decode(), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
