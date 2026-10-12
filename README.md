# mo-bot

A long-running PR review service with a SQLite queue and a fixed pool of reusable Codex sessions. Ordinary code discovers PRs; models only run assigned reviews or operator conversations.

## Skills ship with the repository

The complete installable skill lives in `.agents/skills/mo-bot-service/`, including `SKILL.md` and `references/api.md`. It is versioned with the service; nothing essential lives only in this machine's home directory.

On another machine, obtain this repository, then run `python3 scripts/install.py`. The installer links that checkout's skill into both Codex and Claude discovery paths and installs the client. Edit the machine-specific config before starting the service. Alternatively, copy the complete skill directory into either agent's skill path; install `mo-botctl` on that machine as well. Start/reload the agent after installation so it discovers `$mo-bot-service`.

## Local installation

Requirements: Python 3.11+, Git, authenticated `gh`, authenticated Codex app-server (validated with CLI 0.162.0), a local source repository, and the configured review-queue skill script. No runtime Python packages are required.

```bash
python3 scripts/install.py
# Edit ~/.config/mo-bot/config.toml for this machine.
systemctl --user enable --now mo-bot.service
mo-botctl status
```

The installer links the same operator skill into `~/.codex/skills/mo-bot-service` and `~/.claude/skills/mo-bot-service`, and installs `~/.local/bin/mo-botctl`. A new agent discovers the skill and operates the existing service from any directory. For agents already running, reload skill discovery/start a new session. Keep `~/.local/bin` on PATH. Installation preserves existing config and refuses to overwrite conflicting CLI/skill paths.

Edit paths/service ID/model as needed. Keep state on disk, not `/tmp`. A stable service ID belongs to one deployment and must not be changed casually. The local installation at `/home/xupeng/github/mo-bot` has a user unit `mo-bot.service`; stop the unit before running the foreground command against its state.

```bash
systemctl --user status mo-bot.service
journalctl --user -u mo-bot.service -f
mo-botctl status
mo-botctl enqueue 12345
mo-botctl chat 0 'Explain the current review progress'
mo-botctl model 0 gpt-6.1-sol --effort medium
mo-botctl events
```

Read [API behavior](docs/api.md), [design and failure boundaries](docs/design.md), and the repo-local [operator skill](.agents/skills/mo-bot-service/SKILL.md). Defaults: two sessions, 60-second discovery, gpt-6-astra/xhigh. Per-slot creation models and dynamic selection are supported.

The service creates/updates PR issue comments with commit/service markers. It does not submit GitHub approvals or requested-changes review events. Reviews run against detached frozen checkouts; only diagnostic directories are writable by the model. It is a trusted local automation using the owner's identities, not a multi-tenant security boundary.

User systemd deployments need their outbound proxy environment explicitly configured when required. `KillMode=control-group` prevents orphaned model/tool work on restart. To survive the last logout, user lingering must be enabled by the machine administrator (`loginctl enable-linger USER`); the current machine denied this operation for xupeng.

## Verification

```bash
python3 -m unittest discover -s tests -v
ruff check mo_bot tests
ruff format --check mo_bot tests
```

Tests exercise SQLite uniqueness/fences, remote marker recovery, ambiguous writes, saved publication checkpoints, head changes, result validation, native JSON-RPC turn reuse/replacement/disconnection, live message steering, model persistence and HTTP authentication. Fake GitHub tests never write to real PRs. Real native session/restart probes are separate operational validation.
