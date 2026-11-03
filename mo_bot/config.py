from dataclasses import dataclass, field
from pathlib import Path
import re
import shutil
import tomllib

CLASSIFIER_MODEL = "gpt-6-luna"
CLASSIFIER_EFFORT = "xhigh"
DELIVERY_GATE_MODEL = "gpt-6.1-terra"
DELIVERY_GATE_EFFORT = "max"
PYTHON_SDK_REVIEW_MODEL = "gpt-6.1-sol"
PYTHON_SDK_REVIEW_EFFORT = "xhigh"


def accepted_delivery_gate(model, effort):
    """Recognize current gates and completed gates retained from before the change."""
    return (model, effort) in {
        (DELIVERY_GATE_MODEL, DELIVERY_GATE_EFFORT),
        ("gpt-6.1-sol", "xhigh"),
    }


@dataclass(frozen=True)
class Config:
    service_id: str
    state_dir: Path
    repository: str
    reviewer: str
    source_dir: Path
    queue_script: Path | None = None
    sessions: int = 2
    batch_size: int = 1
    batch_max_patch_bytes: int = 120000
    poll_seconds: int = 60
    model: str = "gpt-6-astra"
    reasoning_effort: str = "xhigh"
    python_sdk_review_slot: int | None = None
    python_sdk_path_prefixes: tuple[str, ...] = ()
    author_review_delays: dict[str, int] = field(default_factory=dict)
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
    worker_cause_issues: bool = False
    worker_allowed_head_repos: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = tomllib.loads(path.read_text())
        service, review = data["service"], data["review"]
        worker = data.get("worker", {})
        python_sdk = review.get("python_sdk", {})
        if not isinstance(python_sdk, dict) or set(python_sdk) - {
            "slot",
            "path_prefixes",
        }:
            raise ValueError("review.python_sdk must be a table")
        if python_sdk and "slot" not in python_sdk:
            raise ValueError("review.python_sdk.slot is required")
        if "path_prefixes" in python_sdk and not isinstance(
            python_sdk["path_prefixes"], list
        ):
            raise ValueError("review.python_sdk.path_prefixes must be an array")
        raw_delays = review.get("author_review_delays", {})
        if not isinstance(raw_delays, dict):
            raise ValueError(
                "review.author_review_delays must be a table of login to minutes"
            )
        author_review_delays = {}
        for login, minutes in raw_delays.items():
            if (
                not isinstance(login, str)
                or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login)
                or type(minutes) is not int
                or not 0 <= minutes <= 10080
            ):
                raise ValueError(
                    "review.author_review_delays entries require a GitHub login and 0..10080 minutes"
                )
            key = login.casefold()
            if key in author_review_delays:
                raise ValueError("review.author_review_delays has duplicate logins")
            author_review_delays[key] = minutes
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
                    "author_review_delays",
                    "python_sdk",
                    "exclude",
                }
            },
            author_review_delays=author_review_delays,
            python_sdk_review_slot=python_sdk.get("slot"),
            python_sdk_path_prefixes=tuple(
                python_sdk.get("path_prefixes", ["clients/python/"])
                if python_sdk
                else ()
            ),
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
            worker_cause_issues=worker.get("cause_issues", False),
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
        if cfg.python_sdk_review_slot is not None:
            prefixes = cfg.python_sdk_path_prefixes
            if (
                type(cfg.python_sdk_review_slot) is not int
                or not 0 <= cfg.python_sdk_review_slot < cfg.sessions
                or not prefixes
                or any(
                    not isinstance(prefix, str)
                    or not prefix
                    or prefix.startswith("/")
                    or ".." in Path(prefix).parts
                    or "\\" in prefix
                    or not prefix.endswith("/")
                    for prefix in prefixes
                )
                or len({prefix.casefold() for prefix in prefixes}) != len(prefixes)
            ):
                raise ValueError(
                    "review.python_sdk requires a valid review slot and unique relative directory prefixes"
                )
        if type(cfg.batch_size) is not int or cfg.batch_size not in (1, 2):
            raise ValueError("review.batch_size must be 1 or 2")
        if type(cfg.batch_max_patch_bytes) is not int or cfg.batch_max_patch_bytes < 1:
            raise ValueError("review.batch_max_patch_bytes must be positive")
        if not 0 <= cfg.worker_sessions <= 8:
            raise ValueError("worker.sessions must be 0..8")
        if type(cfg.worker_cause_issues) is not bool:
            raise ValueError("worker.cause_issues must be a boolean")
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
        if cfg.python_sdk_review_slot is not None and any(
            override.get("role", "review") == "review"
            and override.get("slot") == cfg.python_sdk_review_slot
            and override.get("node", "local") != "local"
            for override in cfg.session_overrides
        ):
            raise ValueError("Python SDK review slot must use the local model catalog")
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
