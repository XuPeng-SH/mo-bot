# Service API

Default address: `http://127.0.0.1:8989`. Every request requires `Authorization: Bearer <token>` from the configured state directory's `api-token` file (mode 0600). Use the repository client to keep tokens out of argv/logs. Local requests must bypass outbound proxies. Remote access: SSH tunnel to the loopback port; there is no public listener or CORS access.

| Request | Result |
|---|---|
| `GET /api/status` | Timestamped scheduler snapshot, backend health, session model/turn/job, recent jobs, counts, discovery/backoff timing |
| `GET /api/jobs` | Compact latest 200 durable jobs plus active/affine jobs, states/errors/result summaries, admission/backoff and head SHA |
| `GET /api/jobs/JOB_ID` | Full metadata, frozen evidence, worker data and saved assessment on demand |
| `GET /api/sessions` | Pool slots including disabled history, thread ID, generation, selected `model`/`effort`, running `active_model`/`active_effort` |
| `GET /api/pools` | Enabled capacity and draining slot count per role |
| `POST /api/pools/review` with `{"sessions":3}` | Resize review pool online; worker endpoint is `/api/pools/worker` |
| `GET /api/models` | Native model catalog and supported reasoning efforts |
| `GET /api/nodes` | Transport/endpoint, connectivity or cleanup fence, remote model catalogs |
| `GET /api/alerts` | Publication errors/manual fences and native inactivity warnings |
| `POST /api/service/drain` / `resume` with `{"reason":"…"}` | Persisted dispatch/publication pause; active turns finish into checkpoints |
| `POST /api/jobs/JOB_ID/pause` / `resume` / `retry` with `{}` | Checkpoint pause/resume or reconcile saved publication; worker resume/retry accept optional `{"hint":"…"}` for the next model turn |
| `POST /api/jobs/JOB_ID/retry` with `{"fresh_session":true}` | Retry a stopped failed unpublished review on the unchanged open head; rotate an idle review thread and reserve it for the same job/comment. Refuses publication uncertainty or existing reviews. CLI: `retry JOB_ID --fresh-session` |
| `POST /api/jobs/JOB_ID/cleanup` with `{}` | Cancel stopped task, free resources, retain evidence; uncertain effects are refused |
| `POST /api/jobs/JOB_ID/rerun` with `{"run_id":123}` | Frozen approved current-head Actions rerun, with cause issues and outbox/budget guards |
| `POST /api/jobs/JOB_ID/guide` with `{"text":"…"}` | Durable worker task guidance through shared Luna xhigh harness; inspect `data.harness_request` / `harness_result` for eventual action, question or fallback |
| `GET /api/sessions/0/history` | Latest 20 native history items; continue using `?cursor=NEXT_CURSOR` (bounded pagination) |
| `GET /api/events?after=CURSOR&thread=THREAD_ID` | SSE native text/tool/status notifications, sequence IDs and heartbeat |
| `POST /api/jobs` with `{"pr":123,"force":true}` | Force admission of current open PR/head; `enqueued:false` and existing job for duplicates |
| `POST /api/sessions/0/messages` with `{"text":"..."}` | Steer active turn or start idle operator chat in the same thread |
| `POST /api/sessions/0/model` with `{"model":"gpt-6.1-sol","reasoning_effort":"medium"}` | Interrupt/continue active work with the new model, preserve job/comment and history/session ID |
| `POST /api/sessions/0/interrupt` with `{}` | Interrupt active turn; its review task becomes superseded |

Slot IDs are zero-based. Creation defaults come from `[review] model/reasoning_effort`; optional `[[session]]` blocks override individual slots. Later API selection is stored in SQLite and survives restart, taking precedence over creation defaults. A busy turn is interrupted and, after native completion confirms cancellation, its same logical review continues with the new model. An idle session starts a short confirmation turn. The API returns `native_turn_started` and the new turn ID; failed interruption cannot start overlapping work. Invalid model/effort is rejected before persisting it.

SSE keeps 1024 events, with individual payloads capped at 64 KiB. Missing buffered events cause an explicit `gap`; oversized payloads produce `event/oversize`. Reload thread history for full content. SSE is not durable conversation storage. Reconnect with `Last-Event-ID` or `after`; event sequence uses a wall-time epoch to distinguish service restarts.

Status is a snapshot: inspect `updated_at`. Network/git preparation can delay scheduler command admission; status and native output streaming remain served on HTTP threads. The command queue is bounded at 64, HTTP connections at 32. `504` reports whether a command was canceled before execution. If already running, reconcile status before retrying; admission remains idempotent. API failures are 400 (invalid input), 401 (authentication), 404 (unknown endpoint), 503 (unavailable/full queue), 504 (timeout).

`worker_discovery_exclusions` reports known PR policy restrictions separately
from `worker_discovery_errors` and outage alerts. Both are refreshed by discovery;
an exclusion is not a permanent task failure or permission to bypass the policy.
A saved review awaiting admission retains `publishing` and exposes `wait_reason`
and `next_retry_at`; it does not consume another model turn.

Automatic admission uses the bundled review-queue policy (or an explicitly configured replacement): open, non-draft, mergeable PR, green latest CI attempts, fresh CI evidence, and missing decisive review or newer commits after requested changes. Self-authored PRs are eligible without waiting for CI when the current head has no published formal review (APPROVED, CHANGES_REQUESTED or COMMENTED); older-head reviews, pending/dismissed reviews and ordinary issue comments do not close this gate. They retain draft/exclusion/mergeability gates and receive COMMENT-only reviews. Incomplete review evidence waits for reliable discovery. Generated config has no default exclusions; legacy config without an exclude list retains its prior default. Forced admission bypasses those filters but requires an open PR and cannot reset any existing commit task. Head changes cancel older work. Publication rechecks head; review comments include full SHA, stable service ID and ownership marker. Only comments by the authenticated GitHub user count.

One durable job/comment does not guarantee exactly-once physical model execution after an opaque crash: bounded recovery may continue/recompute interrupted work. Completed checkpoints are published without another model turn. Never run this stable identity against different state databases simultaneously; local lock and SQLite fences do not provide a distributed GitHub lock.

## Formal review publication

Job status includes `review_event`, `review_attempted` and `review_id`, alongside the existing `comment_id`/`comment_state`. A `done` job published under the current policy has a marked, commit-pinned GitHub review and a final status comment. Legacy completed jobs may have no review ID and are not replayed.

The review policy is maintained in the repository's `mo-bot-review` skill. New assessments choose approve or request_changes: supported approvals map to APPROVE; actionable defects and demonstrated unmet required validation contracts map to REQUEST_CHANGES. Historical needs_validation results map to COMMENT. Own PRs always receive COMMENT while retaining the assessment. Force enqueue can publish both a formal review and the status comment; same-head re-review requires changed nonempty human discussion and receives a new durable context identity. It never replays a completed publication.

A durable `review_attempted=1` fences a non-idempotent POST. If its response is uncertain, the scheduler reads authenticated service/job/head review markers. A matching terminal review resumes the saved transaction; an absent or mismatched marker requires manual reconciliation and is never blindly POSTed again. Status reads do not authorize reconciliation writes. An already-submitted review may remove the PR from discovery eligibility; status-comment completion still proceeds without rerunning the model.

## Online capacity

`mo-botctl pools` inspects capacity. `mo-botctl resize review 3` and
`mo-botctl resize worker 4` set each pool independently (integer 0..8). Requests
are serialized by the scheduler and persisted in SQLite; saved online counts take
precedence over TOML creation counts on restart. Repeating a count is idempotent.
New slots initialize asynchronously; acceptance is not proof of a ready session.
Initialization errors back off per slot without restarting healthy sessions.

Shrinking disables higher pool indices for new tasks without interrupting existing
turns or retained worker phases. Their `draining=true` lasts until their task
releases the lease. Disabled idle slots have `state=disabled`, stay visible and
retain ID/history/model/node for later reactivation. Running turns can still be
steered/interrupted; disabled slots cannot start idle chats/model-switch turns.
No session, source or history is deleted. Count zero stops new pool assignments;
previous worker tasks continue. Global deployment drain still takes precedence.
New slots use configured creation defaults/overrides, local unless a node override
exists. Native model/history operations require a connected session; reenable
archived disabled sessions to reconnect them after restart.

## Delivery assignments

`POST /api/jobs` with `{"kind":"work","target_type":"pr","number":29422}`
queues an explicitly assigned open non-draft PR repair, regardless of author. Optional `hint` (up to 8,000 characters) freezes initial task guidance; repeat requests retain the same task. Use `guide` for changed guidance after work starts. Automatic discovery stays configured-owner only. External PR repairs retain the upstream source PR identity and use an owned fork branch plus a marked integration PR targeting the original author's source branch; no write access to that source fork is required to execute the task. Automatic repairs admit a completed failed check/status even while sibling checks or the workflow remain running; the latest terminal-failed-run requirement still applies to actual reruns. Review and repair may both queue but never execute concurrently for the same PR. `target_type="issue"` explicitly queues an open issue in
the configured repository; automatic issue admission is separately limited to
open issues assigned to the configured owner with the `bot-ready` label. With
one classifier slot enabled, `gpt-6-luna/xhigh` may group an issue into a
compatible queued or very early triage/design task (maximum three issues).
Uncertain matches or classifier/model failures stay separate. CLI equivalents
are `mo-botctl repair NUMBER` and `mo-botctl issue NUMBER`. Worker pool must be
enabled. Explicit repair authorizes source admission, never a default destination ref or an unowned push. Draft/exclusion and publication identity guards remain.

Sessions include `role`, `pool_index`, `enabled`; jobs include `kind`,
`target_type`, `target_number`, `phase` and persisted `data`. Global slot IDs stay
stable when pools resize. Worker model changes require an idle unreserved slot;
actual design/final turns always select 6.1-sol/xhigh. Assigned queued/publishing
worker slots cannot be used for idle chat. Publishing errors retain fences and
are visible in `job.error`; uncertain mutations require manual reconciliation.

`POST /api/jobs/JOB_ID/reconcile` (body `{}`) explicitly rechecks a publishing
worker outbox after manual reconciliation. CLI: `mo-botctl reconcile JOB_ID`.
This resets read backoff only; authenticated ownership, exact candidate, default
ref guards and attempted-write fences remain mandatory. Background manual errors
are not polled again; transient read errors have bounded persisted backoff.

## Discovery and recovery

Saved review assessments awaiting CI/admission release the PR execution fence; active turns and attempted review writes still fence repairs. Review publication waits while repair execution owns the PR. Review and worker discovery use bounded read-only child processes. A complete, validated snapshot is required before reconciliation: timeout, malformed/partial output and provider errors never imply that PRs closed. Same-head automatic tasks already decisively reviewed are superseded; transient CI/mergeability waits retain the job and expose a wait reason. Explicit force admission and already-attempted publication keep their original safety contract. During discovery, automatic review dispatch pauses until fresh success, while operator commands and retained worker tasks remain serviced.

One job's I/O/publication failure sets its persistent `retry_at` and cannot force a global dispatch cooldown. Saved review publication releases its model slot; delivery workers retain affine slots through model phases, then freeze reviewed commits and release slots before publication. Retry deadlines survive restart. Single-instance locking precedes SQLite migration. Recovery accepts only the exact session/turn recorded in the artifact provenance, not any completed event in a reused log. Shutdown fails queued operator commands instead of leaving their futures unresolved.

Status/job lists are summaries; use `mo-botctl job JOB_ID` or the detail endpoint for full validation/findings/limitations. `dispatchable` means admission and retry deadline allow scheduling, subject to capacity and PR ownership fences. Check `discovery.running`, `last_success_at` and `error`; historic `last_error` alone does not prove a current blocker.

Harness `wait` uses the existing queued state and `retry_at` after source is preserved and leases released. `data.recovery_wait` records its prerequisite/readiness guidance; `wait_reason`, `next_retry_at` and alerts expose the wait. The deadline is 5 minutes to 1 hour, survives restart, and new feedback or operator guidance/resume/retry can wake it early. It never sets operator `paused` or waives validation. Active turns must checkpoint before waiting; uncertain steering responses are reported without replay.

Sessions include `node` (`local` by default). Model validation uses the target
node's catalog; `/api/models` stays the local catalog. Global slot APIs route to
the original host's native thread. A disconnected node pauses its dispatch while
other nodes continue. Local tools must be reaped before remote lease release;
unconfirmed cleanup exposes a node fence. Endpoint changes cannot move leased
slots. Transport is stdio over SSH; REST remains loopback local.

For a failed worker, `POST /api/jobs/JOB_ID/retry` with `{"from_input":true}` preserves prior diagnostics/source archives and restarts the same job/comment from its frozen input at design or triage. Existing implementation/final validation is not reused. Active tools, done/superseded tasks and attempted remote effects are rejected. Omit `from_input` to restore a preserved source at its existing phase.

Worker resume/retry accept `hint` (up to 8,000 characters); it is task-scoped, durable across turns, and an empty string clears it. Active turns use session chat/steer. Publishing has no model turn and refuses hints; write reconciliation remains separate. Transport failures retain automatic publication backoff. A new PR head supersedes an unpublished assessment. Same-head observations coalesce into the existing unattempted task and are coordinated at a checkpoint; `data.pending_evidence_count` reports their count in summaries, while the full job retains `pending_input`. Source and reviewed candidate refs survive continuation. Attempted remote effects retain reconciliation fences.

Explicit review enqueue (`force=true`) overrides WIP/draft/exclusion/CI and
already-reviewed/merge-only-reviewed admission through launch and publication. It promotes an active
automatic task to forced admission; it does not replay completed or uncertain
publications. Automatic already-reviewed detection requires an authenticated bot
review marker matching the exact current head; ordinary reviews do not count.
The `merge-only-reviewed` exception instead proves that the authenticated marked
earlier head changed solely by clean default-branch merges, recomputing each tree.
Admission retains `reviewed_head`, without publishing a review on the new SHA.
A new/edited nonempty human comment can admit a distinct same-head contextual round; unchanged discussions coalesce. Per-head/context uniqueness and per-job publication fences remain. Force can revive an unclaimed task skipped solely by this rule; prior attempted
remote effects retain their existing reconciliation fence.
