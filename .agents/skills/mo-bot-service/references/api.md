# Service API

Default address: `http://127.0.0.1:8989`. Every request requires `Authorization: Bearer <token>` from the configured state directory's `api-token` file (mode 0600). Use the repository client to keep tokens out of argv/logs. Local requests must bypass outbound proxies. Remote access: SSH tunnel to the loopback port; there is no public listener or CORS access.

| Request | Result |
|---|---|
| `GET /api/status` | Timestamped scheduler snapshot, backend health, session model/turn/job, recent jobs, counts, discovery/backoff timing |
| `GET /api/jobs` | Latest 200 durable jobs, states/errors/results and head SHA |
| `GET /api/sessions` | Fixed pool slots, thread ID, generation, selected `model`/`effort`, running `active_model`/`active_effort` |
| `GET /api/models` | Native model catalog and supported reasoning efforts |
| `GET /api/sessions/0/history` | Native thread history; may grow with a long-lived conversation |
| `GET /api/events?after=CURSOR&thread=THREAD_ID` | SSE native text/tool/status notifications, sequence IDs and heartbeat |
| `POST /api/jobs` with `{"pr":123,"force":true}` | Force admission of current open PR/head; `enqueued:false` and existing job for duplicates |
| `POST /api/sessions/0/messages` with `{"text":"..."}` | Steer active turn or start idle operator chat in the same thread |
| `POST /api/sessions/0/model` with `{"model":"gpt-6.1-sol","reasoning_effort":"medium"}` | Persist selection for the next turn, preserve history/session ID |
| `POST /api/sessions/0/interrupt` with `{}` | Interrupt active turn; its review task becomes superseded |

Slot IDs are zero-based. Creation defaults come from `[review] model/reasoning_effort`; optional `[[session]]` blocks override individual slots. Later API selection is stored in SQLite and survives restart, taking precedence over creation defaults. A busy turn retains its original model and effort. Start another turn after completion to use the new selection. Invalid model/effort is rejected before persisting it.

SSE keeps 1024 events, with individual payloads capped at 64 KiB. Missing buffered events cause an explicit `gap`; oversized payloads produce `event/oversize`. Reload thread history for full content. SSE is not durable conversation storage. Reconnect with `Last-Event-ID` or `after`; event sequence uses a wall-time epoch to distinguish service restarts.

Status is a snapshot: inspect `updated_at`. Network/git preparation can delay scheduler command admission; status and native output streaming remain served on HTTP threads. The command queue is bounded at 64, HTTP connections at 32. `504` reports whether a command was canceled before execution. If already running, reconcile status before retrying; admission remains idempotent. API failures are 400 (invalid input), 401 (authentication), 404 (unknown endpoint), 503 (unavailable/full queue), 504 (timeout).

Automatic admission reuses the installed review-queue skill predicate: open, non-draft, non-self-authored, mergeable PR, green latest CI attempts, fresh CI evidence, and missing decisive review or newer commits after requested changes. Default PR 26145 is excluded. Forced admission bypasses those filters but requires an open PR and cannot reset any existing commit task. Head changes cancel older work. Publication rechecks head; review comments include full SHA, stable service ID and ownership marker. Only comments by the authenticated GitHub user count.

One durable job/comment does not guarantee exactly-once physical model execution after an opaque crash: bounded recovery may continue/recompute interrupted work. Completed checkpoints are published without another model turn. Never run this stable identity against different state databases simultaneously; local lock and SQLite fences do not provide a distributed GitHub lock.
