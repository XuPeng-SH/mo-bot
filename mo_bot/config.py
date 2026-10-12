from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import tomllib


@dataclass(frozen=True)
class Config:
    service_id: str
    state_dir: Path
    repository: str
    reviewer: str
    source_dir: Path
    queue_script: Path
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
    api_host: str = "127.0.0.1"
    api_port: int = 8989

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = tomllib.loads(path.read_text())
        service, review = data["service"], data["review"]
        cfg = cls(
            service_id=service["id"],
            state_dir=Path(service["state_dir"]).expanduser().resolve(),
            repository=review["repository"],
            reviewer=review["reviewer"],
            source_dir=Path(review["source_dir"]).expanduser().resolve(),
            queue_script=Path(review["queue_script"]).expanduser().resolve(),
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
            api_host=data.get("api", {}).get("host", "127.0.0.1"),
            api_port=data.get("api", {}).get("port", 8989),
        )
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", cfg.service_id):
            raise ValueError(
                "service.id must be a stable identifier, not a per-process UUID"
            )
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", cfg.repository):
            raise ValueError("repository must be owner/name")
        if not 1 <= cfg.sessions <= 8 or cfg.poll_seconds < 10:
            raise ValueError("sessions must be 1..8 and poll_seconds >= 10")
        seen = set()
        for override in cfg.session_overrides:
            if (
                set(override) - {"slot", "model", "reasoning_effort"}
                or type(override.get("slot")) is not int
            ):
                raise ValueError(
                    "session override requires slot and optional model/reasoning_effort"
                )
            if override["slot"] in seen or not 0 <= override["slot"] < cfg.sessions:
                raise ValueError("duplicate or out-of-range session slot")
            seen.add(override["slot"])
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
        if not cfg.source_dir.is_dir() or not cfg.queue_script.is_file():
            raise ValueError("source_dir or authoritative queue_script unavailable")
        return cfg
