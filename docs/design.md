# Reusable PR review service

## Contract and responsibility

A deterministic service discovers eligible PRs and owns a SQLite queue. A fixed configurable pool of Codex conversation IDs performs reviews; One owned Codex app-server process runs native thread/turn operations; threads are reused across PRs. Each slot has a stable detached Git worktree. Codex owns conversation persistence; the service only records IDs and invokes thread/start, thread/resume and turn/start.

SQLite is the sole workflow authority. A unique repository/PR/head key prevents repeated enqueue. Partial unique indexes fence active PRs and session slots. The coordinator holds an OS file lock. GitHub eligibility is imported from the existing review-queue skill, which remains its single owner. A stable service ID is bound to the database so a configuration rename cannot accidentally repeat work.

## Workflow and publication

queued → reviewing → publishing → done. Failed tasks remain the same job and may be recovered within a bounded attempt budget. canceling retains the active slot/PR fence until native turn cancellation completes, or the owned backend process group is gone, then becomes superseded. At most one active revision per PR; superseded queued revisions never run.

Before model execution, create one PR issue comment containing the full commit SHA, service ID and deterministic job marker. Persist the comment ID and update that same comment for progress/final output. Only markers authored by the authenticated GitHub identity count. An existing remote marker without a matching local job prevents automatic re-enqueue after database loss. An uncertain comment creation is reconciled by reading markers before another write; unresolved ambiguity blocks the task rather than starting a duplicate review.

Final output must match the job ID/head and review schema; the source worktree must remain clean. Recheck current PR head before publication. An outdated report is marked superseded and cannot be presented as a review of the new commit. Publication intent is persisted before the idempotent PATCH; a crash then resumes publication from the saved report, without another model run. This guarantees one logical task/comment, not exactly-once physical model computation across opaque remote failures.

## Session recovery

A healthy slot resumes its recorded session. Reused turns explicitly replace the previous PR scope and require current frozen sources, comments and evidence. A missing/corrupt session or changed session identity is retired and replaced in the same slot; the original logical job and comment stay intact. Transport/provider failures do not imply a broken conversation. Attempts, execution duration and log size are bounded; exhaustion leaves a visible failed task for operator action. Source contamination quarantines the owned worktree and retires its session instead of discarding edits.

On shutdown, cancel only owned process groups and retain recoverable workflow state. PID start time and boot ID protect recovery from signaling an unrelated reused PID. With systemd KillMode=control-group, orphaned child work is also terminated on coordinator restart. Local CLI execution supports the same ownership checks.

## Validation

Use SQLite transactions and fake GitHub and native JSON-RPC processes to test duplicate scans, two reusable slots across multiple PRs, lost sessions, restart during review/publication, ambiguous comment writes, source/head changes, timeouts and shutdown. Exercise real Codex session creation/resume and current GitHub discovery separately. No test posts to a real PR. Live service publication is authorized by the requested journey and begins only after these checks.

Configuration selects slot count, model, discovery interval and limits. A token-authenticated loopback REST API exposes status, queue, sessions, models, force admission, conversation history, live messages and interruption; SSE streams bounded native notifications. Force admission bypasses eligibility filters, never commit uniqueness. Default local deployment: two sessions, one-minute discovery, gpt-6.1-sol/xhigh. No broker, generic plugin framework or second state machine is needed.

## REST and turn admission

Only the scheduler mutates SQLite and starts/steers turns. HTTP producers submit bounded commands with futures; status reads a locked snapshot. A manual idle conversation occupies its slot; messages to an active slot use turn/steer with the current turn ID. Native thread history remains the conversation authority. SSE uses a bounded 1024-event buffer, heartbeats and explicit gap notification; clients reload history after gaps.

Review turns run with only their diagnostic directory writable, source outside it read-only. Network access permits dependency downloads; this is an authenticated local tool running with the owner's Codex/gh identity, not an isolation boundary suitable for adversarial repositories or credentials. Source/comments are explicitly untrusted evidence. No GitHub review event, approval, merge, push or CI rerun is issued by the coordinator.

Ambiguous initial comment creation is fenced durably before the POST. If marker reconciliation cannot find it, the job fails closed for manual inspection; it does not repeat the POST. Completed results remain in SQLite during GitHub outages. A fixed service ID must be used by only one deployment with this state directory; GitHub comment APIs do not provide a distributed compare-and-swap across independently running instances.
