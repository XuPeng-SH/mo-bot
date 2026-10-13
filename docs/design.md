# Reusable PR review service

## Contract and responsibility

A deterministic service discovers eligible PRs and owns a SQLite queue. A fixed configurable pool of Codex conversation IDs performs reviews; an owned Codex app-server per configured node runs native thread/turn operations, and threads are reused across PRs. Each slot has a stable local detached Git worktree. Codex owns conversation persistence; the service records IDs and invokes thread/start, thread/resume and turn/start.

SQLite is the sole workflow authority. A unique repository/PR/head key prevents repeated enqueue. Partial unique indexes fence active PRs and session slots. The coordinator holds an OS file lock. GitHub eligibility uses the bundled policy adopted from the existing review-queue skill; an explicitly configured script can replace it. The bot additionally admits self-authored PRs for COMMENT-only reviews when their current head has no published formal review, without waiting for CI. Old-head formal reviews do not suppress a new commit; pending/dismissed reviews and issue comments are not formal completion. Mergeability, draft and exclusion gates still apply. A potentially truncated review window cannot prove absence. Repair admission uses individual completed check/status failures, so a running parent workflow does not delay diagnosis. Target fences serialize review and repair. A stable service ID is bound to the database so a configuration rename cannot accidentally repeat work.

## Workflow and publication

queued → reviewing → publishing → done. Failed tasks remain the same job and may be recovered within a bounded attempt budget. canceling retains the active slot/PR fence until native turn cancellation completes, or the owned backend process group is gone, then becomes superseded. At most one active revision per PR; superseded queued revisions never run.

Before model execution, create one PR issue comment containing the full commit SHA, service ID and deterministic job marker. Persist the comment ID and update that same comment for progress/final output. Only markers authored by the authenticated GitHub identity count. An existing remote marker without a matching local job prevents automatic re-enqueue after database loss. An uncertain comment creation is reconciled by reading markers before another write; unresolved ambiguity blocks the task rather than starting a duplicate review.

Final output must match the job ID/head and review schema; the source worktree must remain clean. Recheck current PR head before publication. An outdated report is marked superseded and cannot be presented as a review of the new commit. Publication intent is persisted before the formal review POST and status-comment PATCH. Assessments map to APPROVE, REQUEST_CHANGES or COMMENT; self-authored PRs always use COMMENT. The review includes an authenticated service/job/head marker and exact commit_id. SQLite persists review_event, review_attempted and review_id. An uncertain POST is reconciled by marker before any retry; a missing or inconsistent marker fails closed for manual reconciliation instead of posting again. A saved review resumes status-comment publication even if its decisive review removes the PR from discovery eligibility. A crash resumes from the saved report without another model run. Completed legacy jobs are not replayed. This guarantees one logical task/comment, not exactly-once physical model computation across opaque remote failures.

## Session recovery

A healthy slot resumes its recorded session. Reused turns explicitly replace the previous PR scope and require current frozen sources, comments and evidence. A missing/corrupt session or changed session identity is retired and replaced in the same slot; the original logical job and comment stay intact. Transport/provider failures do not imply a broken conversation. Attempts, execution duration and log size are bounded; exhaustion leaves a visible failed task for operator action. Source contamination quarantines the owned worktree and retires its session instead of discarding edits.

On shutdown, cancel only owned process groups and retain recoverable workflow state. PID start time and boot ID protect recovery from signaling an unrelated reused PID. With systemd KillMode=control-group, orphaned child work is also terminated on coordinator restart. Local CLI execution supports the same ownership checks.

## Validation

Use SQLite transactions and fake GitHub and native JSON-RPC processes to test duplicate scans, two reusable slots across multiple PRs, lost sessions, restart during review/publication, ambiguous comment writes, source/head changes, timeouts and shutdown. Exercise real Codex session creation/resume and current GitHub discovery separately. No test posts to a real PR. Live service publication is authorized by the requested journey and begins only after these checks.

Configuration selects slot count, model, discovery interval and limits. A token-authenticated loopback REST API exposes status, queue, sessions, models, force admission, conversation history, live messages and interruption; SSE streams bounded native notifications. Force admission bypasses eligibility filters, never commit uniqueness. Default local deployment: two sessions, one-minute discovery, gpt-6-astra/xhigh. No broker, generic plugin framework or second state machine is needed.

## REST and turn admission

Only the scheduler mutates SQLite and starts/steers turns. HTTP producers submit bounded commands with futures; status reads a locked snapshot. A manual idle conversation occupies its slot; messages to an active slot use turn/steer with the current turn ID. Native thread history remains the conversation authority. SSE uses a bounded 1024-event buffer, heartbeats and explicit gap notification; clients reload history after gaps.

Review turns run with only their diagnostic directory writable, source outside it read-only. Network access permits dependency downloads; this is an authenticated local tool running with the owner's Codex/gh identity, not an isolation boundary suitable for adversarial repositories or credentials. Source/comments are explicitly untrusted evidence. Only the coordinator submits GitHub review events under the maintained review skill. For review jobs, neither worker nor coordinator merges, pushes or reruns CI. The worker prompt is loaded from the packaged repository skill for each new review and persisted in the turn artifacts; policy edits do not alter already-started turns.

Ambiguous initial comment creation is fenced durably before the POST. If marker reconciliation cannot find it, the job fails closed for manual inspection; it does not repeat the POST. Completed results remain in SQLite during GitHub outages. A fixed service ID must be used by only one deployment with this state directory; GitHub comment APIs do not provide a distributed compare-and-swap across independently running instances.

## Native model switching and progress

A loaded thread/resume can ignore model overrides (verified with CLI 0.162.0), so its response is not used to overwrite the selected model. Dynamic switching waits for interruption completion, retains thread/job/comment identity, and submits the continued task through turn/start with explicit model/effort. Idle switching starts a bounded confirmation turn. Success reports a native turn ID. Interruption failure fences replacement; opaque start failures use backend recovery. SQLite records the actual turn artifact directory for crash checkpoint recovery.

Operator progress queries use turn/steer with expected turn ID. The review continues, and a commentary reply is available in native history/SSE; acknowledgement is not treated as evidence of progress.

## Shared delivery worker contract

The `work` job kind extends the same Store, scheduler, native backend and role
slots; no second lifecycle owner. Targets are `pr` or explicitly assigned `issue`.
`target_number` is canonical; issue `pr` stays NULL until marked PR creation is
independently verified. Migration rebuilds the legacy unconditional PR uniqueness
atomically while preserving review IDs, markers, results and native sessions.
Partial indexes enforce review identity, semantic worker identity, cross-kind
active PR ownership and active issue ownership. Assigned queued work retains its
target and slot fences. Publishing retains its target/write fences while releasing
the model slot after preserving the reviewed commit. Disabled slots drain retained
work; role/pool index maps to stable global IDs.

Inputs pin actor and repository IDs, author/head/ref and issue version/base.
Fully paginated review, unresolved thread, status, check and Actions evidence is
classified into semantic atoms, never PR updatedAt. Dispositions and task/rerun
budgets bound rediscovery. Worker-generated status/cause output is excluded; own
completed bot request_changes COMMENT is valid feedback. Unknown/truncated
inputs remain indeterminate. Automatic PR scope is the configured owner. Issue
intake is limited to open issues assigned to that owner and tagged `bot-ready`;
issue discovery errors do not block PR discovery. One optional local
`gpt-6-luna/xhigh` classifier compares bounded issue evidence against at most
eight compatible recent tasks. Only a high-confidence same-root-cause match can
join a queued or very early triage/design task, with a three-issue cap. Uncertain
matches, missing model capability and classifier failures enqueue a separate
issue task. The model/effort pair is pinned in slot defaults and each turn so
status and actual execution stay aligned.

SQLite `phase`/`data`, native turn provenance and per-job task records own
triage/design/implement/final_review recovery. Design/final gates select actual
6.1-sol/xhigh, irrespective of the selected slot default. Only implementation
adds the independent worker clone to native writable roots. Coordinator checks
Git/source snapshots, invokes rebase/continuations, commits Conventionally, then
freezes the exact clean candidate for final review. Rebase conflict rounds,
corrections do not fail on a lifetime turn count. Validation-only final rejections
retain final_review and its accepted design; actionable findings return to the
responsible phase. Retry pacing derives progress from source, frozen input and
successful check receipts, independent of paraphrased limitations or duplicate
executions. Unchanged blocked checkpoints archive source, release slots and back
off. Harness `wait` advice identifies an external prerequisite and reuses queued
state/retry deadlines (5 minutes to 1 hour), with visible reasons and alerts.
New evidence or authenticated guidance wakes that same task early; deadlines
survive restart. Active tools checkpoint before waiting, and missing validation
never becomes approval. Incomplete native turns retain source and slot;
completed exact-thread/turn results can advance from durable artifacts.

`effects` persists immutable intent and attempted status before non-idempotent
writes. Authenticated cause/PR markers, remote SHA and Actions attempts verify
outcomes; absent uncertain results are never retried. Cause keys exclude PR/head/
run/time; reruns are bounded to latest terminal failed associated current-head
runs. Push uses canonical owned/allowlisted repository URL, checked feature ref,
explicit old-SHA/absent lease and exact reviewed candidate. Every default branch
is forbidden. Issue PR creation attaches a verified PR number to the shared fence.
Publishing freezes the exact reviewed commit in a private coordinator Git ref;
slot clones can then serve other tasks without changing the delivery candidate.
Successful write receipts are persisted before independent reconciliation. Cause
lookup uses bounded search with canonical marker/actor verification, or direct
GET by a saved issue number; uncertain absent writes remain fenced. New-head CI
remains pending and is not treated as delivery validation evidence.

Correction invariants: decisive requests survive later COMMENT reviews; thread
resolution/outdated flags and author identity never replace source triage. Bot
output exclusion requires authenticated valid service markers. Handled atoms
carry only to the exact attempted/verified candidate, including lost responses.
Same-head new semantic evidence fences design/final/publication. Successful
phases do not consume independent error recovery counts. Disabled worker pools
drain retained affine tasks without discovering new work. Mixed failures require
per-atom dispositions and issue-first action plans, not one lossy classification.
Unresolved issue PR creation effects fence possibly-created PR heads/refs.
Publication reads have durable bounded backoff/manual suspension and an explicit
reconcile API; this never grants permission to repeat uncertain writes.

## Queue and unhappy-path hardening

The existing review predicate remains the admission owner. Discovery now includes every open PR's classification/reason; completed snapshots reconcile already-reviewed/explicitly-excluded automatic tasks into superseded, while temporary CI/mergeability gaps remain queued with a wait reason. Forced tasks and uncertain publication retain their fences. No result is discarded/recomputed because an idempotent publication update failed.

Review and worker discovery share one bounded child lifecycle: disk-backed output, captured PID identity, atomic ownership file, timeout/output budgets and cleanup. A scan failure clears automatic review admission until success, without blocking commands or known worker phases. Per-job durable retry deadlines isolate I/O failures; a review publishing result frees model capacity, but a worker's source/delivery affinity remains fenced.

The singleton lock precedes SQLite migration/pool resizing. Command futures are failed on shutdown. Artifact recovery is shared across pools and matches session ID, turn ID and completion status; late previous-turn notifications are buffered until the new native turn ID is known, then rejected. Stalled stdio writes have deadlines and oversized/malformed RPC frames fence the backend. Initial spawn failures release open logs.

Status is a compact SQL projection over recent plus active jobs; complete evidence stays in SQLite and is read on demand. Admission state, retry deadline and scan freshness are visible separately from queue state. This avoids parsing/emitting historical CI/comment/diagnostic blobs on every scheduler tick.

## SSH session transport

Node routing owns transport and native catalog/thread routing only. It does not
own jobs, publication, retries, task phases or source state: those remain in the
existing coordinator/Store/Workspace chain. A slot persists its node so thread
identity cannot accidentally resume on a different endpoint. Binding/endpoint
changes clear idle IDs and reject active/affine migrations.

An SSH stdio supervisor locks one service/node runtime, relays protocol without
interpreting workflow, and reaps its process group on EOF, signal or heartbeat
loss. Nonblocking pipes avoid daemon-thread shutdown locks and output deadlock.
Remote shell tools are disabled; one dynamic host_exec callback per turn invokes
the local native command executor with the existing phase sandbox. It verifies
exact thread/turn, bounds cwd/time/output and rejects late calls. The host tool
executor owns its native process and saved identity; cancellation/transport loss
must finish cleanup before results or leases can be consumed. Unknown command
responses escalate to owned-tree cleanup. Unconfirmed cleanup retains the fence;
restart reaps stored tool owners before ordinary workflow recovery.

Node outages have independent backoff and cannot restart healthy other nodes.
Selected models are checked against that node's catalog; mandatory worker xhigh
design/final gates still use actual native turn parameters. No code, credentials,
SQLite, build cache or GitHub mutation engine is replicated on the remote host.


## Online pool resizing

SQLite slot enabled flags remain the admission authority; a role-specific saved
capacity sets startup counts when explicitly resized online. One scheduler command
transaction disables excess indices, reactivates existing identities and creates
missing indices with role defaults/node overrides. No secondary capacity manager.
Assigned worker phases retain affinity on disabled slots until completion;
new claims require enabled slots. Status includes disabled identities and draining
work. Native initialization retries are isolated per slot and bounded by existing
command timeouts/backoff. Global drain blocks launches independently of resize.

## Installation ownership

Source and wheel installs share the CLI installer. Runtime policy and all three skills are package resources; external queue/workflow paths are optional compatibility overrides. Init generates user-local paths and refuses overwrites; doctor validates existing config without starting native turns or writing GitHub. Systemd is opt-in.

Installer-owned paths are recorded in a manifest under the installation prefix. A nonblocking file lock serializes installation/uninstallation. Preflight refuses foreign or modified destinations; durable ownership intent allows interrupted writes to be retried. Units use quoted interpreter/config/source paths and do not restart existing jobs unless explicitly started. Uninstall retains config/state, and modified destinations remain recorded for reconciliation.

## Unresolved repair evidence

Failed, blocked or superseded evidence remains actionable while its owning task is terminal. Admission retries those atoms with a deterministic identity derived from the prior terminal tasks; enqueue atomically transfers their queued ownership. Handled/delivered evidence and evidence owned by live tasks stay deduplicated.

The configured tasks-per-head limit bounds active/completed tasks and separately bounds executed attempts for an identical frozen input. Failed/superseded task records do not consume completion capacity. A changed input can be investigated again; exhausted admission reports a worker discovery alert instead of silently returning no task. Noop or independent-CI diagnoses cannot bypass a frozen, still-unresolved merge conflict at design or final review.
