"""Local operator client; never place bearer tokens in command-line arguments."""

import argparse
import json
from pathlib import Path
import sys
import tomllib
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from urllib.parse import urlencode


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path.home() / ".config/mo-bot/config.toml"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in [
        "status",
        "sessions",
        "jobs",
        "models",
        "nodes",
        "alerts",
        "events",
        "pools",
    ]:
        commands.add_parser(name)
    p = commands.add_parser("resize")
    p.add_argument("role", choices=("review", "worker", "classifier"))
    p.add_argument("sessions", type=int)
    p = commands.add_parser("job")
    p.add_argument("job_id")
    p = commands.add_parser("enqueue")
    p.add_argument("pr", type=int)
    for name in ["repair", "issue"]:
        p = commands.add_parser(name)
        p.add_argument("number", type=int)
        p.add_argument(
            "--hint", help="initial task guidance; use guide for a started task"
        )
    p = commands.add_parser("reconcile")
    p.add_argument("job_id")
    for name in ["pause", "resume", "retry", "cleanup", "rerun"]:
        p = commands.add_parser(name)
        p.add_argument("job_id")
        if name in ("resume", "retry"):
            p.add_argument(
                "--hint", help="task guidance for the next worker turn; empty clears it"
            )
        if name == "rerun":
            p.add_argument("--run-id", type=int, required=True)
        if name == "retry":
            p.add_argument(
                "--from-input",
                action="store_true",
                help="retain diagnostics and restart a failed worker from its frozen source",
            )
    for name in ["drain", "resume-service"]:
        p = commands.add_parser(name)
        p.add_argument("--reason", default="operator request")
    p = commands.add_parser("guide")
    p.add_argument("job_id")
    p.add_argument("text")
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
        if name == "history":
            p.add_argument("--cursor")
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
    if args.command == "resize":
        endpoint = f"/api/pools/{args.role}"
        body = {"sessions": args.sessions}
    elif args.command in ("drain", "resume-service"):
        endpoint = "/api/service/" + ("drain" if args.command == "drain" else "resume")
        body = {"reason": args.reason}
    elif args.command in ("pause", "resume", "retry", "cleanup", "rerun"):
        endpoint = f"/api/jobs/{args.job_id}/{args.command}"
        body = {"run_id": args.run_id} if args.command == "rerun" else {}
        if args.command == "retry" and args.from_input:
            body["from_input"] = True
        if args.command in ("resume", "retry") and args.hint is not None:
            body["hint"] = args.hint
    elif args.command == "guide":
        endpoint = f"/api/jobs/{args.job_id}/guide"
        body = {"text": args.text}
    elif args.command == "job":
        endpoint = f"/api/jobs/{args.job_id}"
    elif args.command == "enqueue":
        endpoint = "/api/jobs"
        body = {"pr": args.pr, "force": True}
    elif args.command == "reconcile":
        endpoint = f"/api/jobs/{args.job_id}/reconcile"
        body = {}
    elif args.command in ("repair", "issue"):
        endpoint = "/api/jobs"
        body = {
            "kind": "work",
            "target_type": "pr" if args.command == "repair" else "issue",
            "number": args.number,
        }
        if args.hint is not None:
            body["hint"] = args.hint
    elif args.command == "progress":
        endpoint = f"/api/sessions/{args.slot}/messages"
        body = {
            "expected_turn_id": session["turn_id"],
            "text": "请仅以简短 commentary 汇报当前任务的目标、阶段和 PR/commit、已完成的检查和证据、正在做什么、剩余验证和阻塞点。不要改变任务目标，不要终止任务，不要重复已完成的工作，汇报后继续当前任务。",
        }
    elif args.command == "chat":
        endpoint = f"/api/sessions/{args.slot}/messages"
        body = {"text": args.text}
    elif args.command == "model":
        endpoint = f"/api/sessions/{args.slot}/model"
        body = {"model": args.model, "reasoning_effort": args.effort}
    elif args.command in ["history", "interrupt"]:
        endpoint = f"/api/sessions/{args.slot}/{args.command}"
        if args.command == "history" and args.cursor:
            endpoint += "?" + urlencode({"cursor": args.cursor})
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


def main():
    try:
        _main()
    except (OSError, ValueError, KeyError, URLError) as error:
        print(
            f"mo-botctl: {error}. Check config and start mo-bot serve before using the API.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
