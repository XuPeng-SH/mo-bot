---
name: mo-bot-service
description: "Operate the mo-bot reusable Codex PR review service: inspect queue and sessions, force enqueue a PR, chat with a session, change its model, or diagnose recovery. Use for mo-bot service operations, not standalone PR reviews."
---

# mo-bot service

Use the installed `mo-botctl` client from any working directory; it reads the API bearer token locally without printing it:

```bash
mo-botctl status
mo-botctl sessions
mo-botctl jobs
mo-botctl models
```

Default config is `~/.config/mo-bot/config.toml`. Override with `--config PATH` before the operation. Read [the API contract](references/api.md) for endpoints, SSE/history semantics and remote access. The service is `systemctl --user status mo-bot.service`; logs are `journalctl --user -u mo-bot.service`.

## Requested operations

- Enqueue: `mo-botctl enqueue PR_NUMBER`. This is force admission: it bypasses CI/draft/author/exclusion filters and can start a review and publish a PR comment. Execute when requested; a status request does not authorize enqueue. Same PR/head returns the existing job and does not repeat a review.
- Converse: `mo-botctl chat SLOT 'message'`. Idle chat occupies the slot; active chat steers its existing turn. Inspect job/head first to target the intended review. `events` streams live output; `history SLOT` retrieves the native conversation.
- Select model: inspect `models`, then `mo-botctl model SLOT MODEL --effort xhigh`. Change preserves thread/history, persists in SQLite and applies on the next turn. Report both selected model and active turn model when busy; an ongoing inference is not hot-swapped.
- Interrupt: `mo-botctl interrupt SLOT` only when requested. It cancels that turn and supersedes its current review task; it is not a transparent session recovery action.

## Recovery invariants

SQLite is the workflow authority; Codex owns native thread history. Healthy IDs are resumed; missing/corrupt threads are replaced in their existing slot, retaining job/comment identity and bounded attempts. Do not delete the database, change service ID or remove GitHub markers to force recovery: this can destroy duplicate protection. Do not launch two deployments with the same service identity and separate databases.

A failed/ambiguous initial comment POST is deliberately fenced. Inspect the job error and authenticated-author marker on the PR before proposing manual reconciliation; do not blindly retry writes or force another review. `publishing` means a saved assessment awaits idempotent comment update; GitHub outages must not trigger a fresh model review. An old head is superseded, never treated as a review of the current commit.

Use `updated_at`, queue counts and backend/session state to distinguish stale status, eligibility wait, model failure and actual execution. SSE is bounded and emits gaps: reload native history when events are missing. Do not describe green CI or an idle session as proof of a successful review. Report exact job/head, session/model, validation evidence and limits.

If `mo-botctl` is missing from PATH, try `~/.local/bin/mo-botctl`. If neither exists, report installation missing; do not guess a running service or silently start a replacement daemon. The installer is `python3 scripts/install.py` in the mo-bot source checkout.
