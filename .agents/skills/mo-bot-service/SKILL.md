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

## Review status and history

For “在 review 什么 / 进展 / 之前 review 了哪些 / 结果”, use `mo-botctl status` and `mo-botctl jobs` first. Map each session's `job_id` to job `pr/head/state`; report selected versus active model. `updated_at` is snapshot freshness, not proof that tools are advancing.

To ask a RUNNING session directly, use `mo-botctl progress SLOT`. This steers the current turn to report evidence, work remaining and blockers as commentary, then continue review. It does not cancel the turn or create an idle conversation; a stale turn ID is rejected. Read the actual reply via history/events: the steer acknowledgement alone is not a progress report.

For detail, use `mo-botctl history SLOT` for native conversation and `mo-botctl events` for current output. Distinguish facts already established from planned work, running checks and missing evidence; do not invent progress percentages. History spans multiple PRs because sessions are reused: bind observations to exact current job/head and turn ID.

For completed jobs, report `result.assessment.decision`, summary/findings, actual validation and limitations, plus `comment_id`. `publishing` has a saved result awaiting publication; `done` is published, while failed/superseded are not successful reviews. Expose errors for failed tasks. The current API lists the latest 200 jobs; disclose that limit instead of claiming an exhaustive history. Status operations are read-only and do not authorize enqueue, interruption, model changes or review publication.

## Requested operations

- Enqueue: `mo-botctl enqueue PR_NUMBER`. This is force admission: it bypasses CI/draft/author/exclusion filters and can start a review and publish a PR comment. Execute when requested; a status request does not authorize enqueue. Same PR/head returns the existing job and does not repeat a review.
- Converse: `mo-botctl chat SLOT 'message'`. Idle chat occupies the slot; active chat steers its existing turn. Inspect job/head first to target the intended review. `events` streams live output; `history SLOT` retrieves the native conversation.
- Select model: inspect `models`, then `mo-botctl model SLOT MODEL --effort xhigh`. Change preserves thread/history and job/comment identity. A busy turn is interrupted and the same task continues via a native turn/start with the new model; idle switching starts a brief confirmation turn. Success means the native turn was started, not merely a database setting. In-flight requests are interrupted rather than hot-swapped.
- Interrupt: `mo-botctl interrupt SLOT` only when requested. It cancels that turn and supersedes its current review task; it is not a transparent session recovery action.

## Recovery invariants

SQLite is the workflow authority; Codex owns native thread history. Healthy IDs are resumed; missing/corrupt threads are replaced in their existing slot, retaining job/comment identity and bounded attempts. Do not delete the database, change service ID or remove GitHub markers to force recovery: this can destroy duplicate protection. Do not launch two deployments with the same service identity and separate databases.

A failed/ambiguous initial comment POST is deliberately fenced. Inspect the job error and authenticated-author marker on the PR before proposing manual reconciliation; do not blindly retry writes or force another review. `publishing` means a saved assessment awaits idempotent comment update; GitHub outages must not trigger a fresh model review. An old head is superseded, never treated as a review of the current commit.

Use `updated_at`, queue counts and backend/session state to distinguish stale status, eligibility wait, model failure and actual execution. SSE is bounded and emits gaps: reload native history when events are missing. Do not describe green CI or an idle session as proof of a successful review. Report exact job/head, session/model, validation evidence and limits.

If `mo-botctl` is missing from PATH, try `~/.local/bin/mo-botctl`. If neither exists, report installation missing; do not guess a running service or silently start a replacement daemon. The installer is `python3 scripts/install.py` in the mo-bot source checkout.
