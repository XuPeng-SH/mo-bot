from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import tomllib

CLASSIFIER_MODEL = "gpt-6-luna"
CLASSIFIER_EFFORT = "xhigh"


@dataclass(frozen=True)
class Config:
    service_id: str
    state_dir: Path
    repository: str
    reviewer: str
    source_dir: Path
    queue_script: Path | None = None
    sessions: int = 2
    poll_seconds: int = 60
    model: str = "gpt-6-astra"
    reasoning_effort: str = "xhigh"
    exclude: tuple[int, ...] = (26145,)
    stale_ci_days: int = 14
    max_recoveries: int = 2
    job_timeout_seconds: int = 7200
    command_timeout_seconds: int = 45
    minimum_free_gib: int = 20
    codex: str = "codex"
    gh: str = "gh"
    session_overrides: tuple = ()
    nodes: tuple = ()
    api_host: str = "127.0.0.1"
    api_port: int = 8989
    worker_sessions: int = 0
    classifier_sessions: int = 0
    worker_model: str = "gpt-6.1-sol"
    worker_effort: str = "low"
    issue_skill: Path | None = None
    worker_tasks_per_head: int = 4
    worker_reruns_per_head: int = 2
    worker_scan_limit: int = 20
    worker_allowed_head_repos: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = tomllib.loads(path.read_text())
        service, review = data["service"], data["review"]
        worker = data.get("worker", {})
        cfg = cls(
            service_id=service["id"],
            state_dir=Path(service["state_dir"]).expanduser().resolve(),
            repository=review["repository"],
            reviewer=review["reviewer"],
            source_dir=Path(review["source_dir"]).expanduser().resolve(),
            queue_script=(
                Path(review["queue_script"]).expanduser().resolve()
                if review.get("queue_script")
                else None
            ),
            **{
                k: v
                for k, v in review.items()
                if k
                not in {
                    "repository",
                    "reviewer",
                    "source_dir",
                    "queue_script",
                    "exclude",
                }
            },
            exclude=tuple(review.get("exclude", [26145])),
            poll_seconds=service.get("poll_seconds", 60),
            session_overrides=tuple(data.get("session", [])),
            nodes=tuple(data.get("node", [])),
            api_host=data.get("api", {}).get("host", "127.0.0.1"),
            api_port=data.get("api", {}).get("port", 8989),
            worker_sessions=worker.get("sessions", 0),
            classifier_sessions=worker.get("classifier_sessions", 1),
            worker_model=worker.get("model", "gpt-6.1-sol"),
            worker_effort=worker.get("reasoning_effort", "low"),
            issue_skill=(
                Path(worker["issue_skill"]).expanduser().resolve()
                if worker.get("issue_skill")
                else None
            ),
            worker_tasks_per_head=worker.get("tasks_per_head", 4),
            worker_reruns_per_head=worker.get("reruns_per_head", 2),
            worker_scan_limit=worker.get("scan_limit", 20),
            worker_allowed_head_repos=tuple(worker.get("allowed_head_repos", [])),
        )
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", cfg.service_id):
            raise ValueError(
                "service.id must be a stable identifier, not a per-process UUID"
            )
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", cfg.repository):
            raise ValueError("repository must be owner/name")
        if not 1 <= cfg.sessions <= 8 or cfg.poll_seconds < 10:
            raise ValueError("sessions must be 1..8 and poll_seconds >= 10")
        if not 0 <= cfg.worker_sessions <= 8:
            raise ValueError("worker.sessions must be 0..8")
        if cfg.classifier_sessions not in (0, 1):
            raise ValueError("worker.classifier_sessions must be 0 or 1")
        if any(
            type(x) is not int or x < 1
            for x in (
                cfg.worker_tasks_per_head,
                cfg.worker_reruns_per_head,
                cfg.worker_scan_limit,
            )
        ):
            raise ValueError("worker budgets must be positive integers")
        if cfg.worker_sessions and cfg.issue_skill and not cfg.issue_skill.is_file():
            raise ValueError(f"issue-to-pr skill missing: {cfg.issue_skill}")
        node_names = {"local"}
        for node in cfg.nodes:
            if (
                set(node) - {"name", "ssh", "codex", "env"}
                or any(
                    not isinstance(node.get(key), str)
                    for key in ("name", "ssh", "codex")
                )
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", node.get("name", ""))
                or node["name"] in node_names
                or not re.fullmatch(r"[A-Za-z0-9_.@-]+", node.get("ssh", ""))
                or node["ssh"].startswith("-")
                or not Path(node.get("codex", "")).is_absolute()
            ):
                raise ValueError(
                    "node requires unique name, SSH host/user@host and absolute codex path"
                )
            env = node.get("env", {})
            if (
                not isinstance(env, dict)
                or set(env)
                - {
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                    "no_proxy",
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "NO_PROXY",
                }
                or any(not isinstance(value, str) for value in env.values())
            ):
                raise ValueError("node.env only supports proxy environment strings")
            node_names.add(node["name"])
        if cfg.nodes and shutil.which("ssh") is None:
            raise ValueError("ssh executable unavailable")
        seen = set()
        for override in cfg.session_overrides:
            if (
                set(override) - {"slot", "role", "model", "reasoning_effort", "node"}
                or type(override.get("slot")) is not int
            ):
                raise ValueError(
                    "session override requires slot and optional model/reasoning_effort"
                )
            role = override.get("role", "review")
            if override.get("node", "local") not in node_names:
                raise ValueError("session references unknown node")
            count = {
                "review": cfg.sessions,
                "worker": cfg.worker_sessions,
                "classifier": cfg.classifier_sessions,
            }.get(role, 0)
            key = (role, override["slot"])
            if (
                role not in ("review", "worker", "classifier")
                or key in seen
                or not 0 <= override["slot"] < count
            ):
                raise ValueError("duplicate or out-of-range session slot")
            if role == "classifier" and override.get("node", "local") != "local":
                raise ValueError("semantic classifier session must run locally")
            if role == "classifier" and (
                override.get("model", CLASSIFIER_MODEL) != CLASSIFIER_MODEL
                or override.get("reasoning_effort", CLASSIFIER_EFFORT)
                != CLASSIFIER_EFFORT
            ):
                raise ValueError(
                    f"semantic classifier is pinned to {CLASSIFIER_MODEL}/{CLASSIFIER_EFFORT}"
                )
            seen.add(key)
        if cfg.api_host != "127.0.0.1" or not 1 <= cfg.api_port <= 65535:
            raise ValueError(
                "API must bind loopback with a valid port; use SSH tunneling for remote access"
            )
        if (
            cfg.max_recoveries < 0
            or cfg.command_timeout_seconds < 1
            or cfg.job_timeout_seconds < 30
        ):
            raise ValueError("invalid recovery/timeout limits")
        if cfg.minimum_free_gib < 0 or cfg.stale_ci_days < 0:
            raise ValueError("disk and CI age limits cannot be negative")
        for executable in (cfg.codex, cfg.gh):
            if shutil.which(executable) is None:
                raise ValueError(f"executable unavailable: {executable}")
        if not cfg.source_dir.is_dir() or (
            cfg.queue_script and not cfg.queue_script.is_file()
        ):
            raise ValueError("source_dir or authoritative queue_script unavailable")
        return cfg
