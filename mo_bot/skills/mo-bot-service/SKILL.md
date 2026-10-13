---
name: mo-bot-service
description: "Install, configure and operate the mo-bot reusable Codex PR review service: inspect queue and sessions, force enqueue a PR, chat with a session, change its model, or diagnose recovery. Use for mo-bot service operations, not standalone PR reviews."
---

# mo-bot service

## Install or configure a new machine

Use the checkout's `python3 scripts/install.py`, or `mo-bot install` after a pip installation. Both use the same installer and install the operator, review and worker skills into Codex and Claude by default. Select `--agent codex`/`--agent claude` or `--skill-dir PATH` when requested. Keep a source-install checkout in place; installed skills link to it. Reload agent skill discovery after installation.

Require Linux, Python 3.11+, Git, gh and Codex CLI with app-server support. Ask the user for the GitHub repository and source checkout if they are unavailable. Use `mo-bot init --repository OWNER/REPO --source-dir PATH`; it uses the authenticated gh user unless `--reviewer LOGIN` is supplied, and never overwrites an existing config. Enable delivery with `--worker-sessions COUNT` when requested. No external review-queue or issue-to-pr skill is needed; existing explicit overrides remain supported.

Run `mo-bot doctor` (or `--config PATH`) to check config, source, gh/Codex login, packaged skills and free disk before starting. Report failed checks accurately. Login needs the user's own account; do not copy credentials from another machine or claim a login check proves a real model turn works. Use `mo-bot serve` for foreground execution. For an authorized user service, `mo-bot install --systemd --start` performs preflight and installs/starts the optional systemd unit. Default installation does not start or restart running jobs.

Repeat installation for updates; conflicts with foreign/modified paths are reported rather than overwritten. `mo-bot uninstall` removes only recorded unchanged installer files, stops an owned user unit, and preserves config/state/history. Pip-installed code must be removed with pip separately. Use the existing drain/restart/resume procedure below for upgrades of running services.

Use the installed `mo-botctl` client from any working directory; it reads the API bearer token locally without printing it:

```bash
mo-botctl status
mo-botctl sessions
mo-botctl jobs
mo-botctl models
mo-botctl nodes
```

Default config is `~/.config/mo-bot/config.toml`. Override with `--config PATH` before the operation. Read [the API contract](references/api.md) for endpoints, SSE/history semantics and remote access. The service is `systemctl --user status mo-bot.service`; logs are `journalctl --user -u mo-bot.service`.

## Review policy

Configured-owner PRs with `do-not-merge/wip` are excluded from both review and
repair. Admission, task launch and publication recheck the policy; explicit
enqueue/repair does not bypass it. An active task waits with its source and
assessment retained, and label removal allows normal admission again. Operator
paused tasks still need the existing explicit resume control.

Review commentary and assessment reports are written in English, as required by the repository review/worker skills. Existing published assessments are not rewritten solely to change language.

The repository's [mo-bot-review skill](../mo-bot-review/SKILL.md) owns reviewer prompt guidance and the decision/publication contract. The coordinator reads it for every new assigned review and snapshots the prompt for recovery. Edit that skill to change review guidance; do not maintain a parallel prompt in this operator skill. `scripts/install.py` installs both repository skills into agent discovery paths.

The service submits `REQUEST_CHANGES` for confirmed findings, `APPROVE` for a supported approval, and `COMMENT` for material validation gaps. Own PRs without a published formal review on the current head are admitted without waiting for CI; draft/exclusion/mergeability gates still apply. Reviews on older heads do not suppress a new commit. Own PRs receive `COMMENT` regardless of assessment. A completed failed check/status admits repair while sibling checks/workflows are running, subject to the existing target fence and evidence deduplication. A completed legacy comment-only job is not replayed. Each new result produces at most one marked formal review plus its existing status comment.

## Review status and history

Each new turn receives shared `check-run` and `test-exec` launchers in its
diagnostic directory. Isolation preflight checks private `/tmp` identity and
writes plus loopback before test compilation. Tests bind only a dedicated
runtime leaf; task caches remain shared across phases. Timeout/cancellation
reaps the invocation's process group, and scratch follows existing task GC.
Setup failures retain source/evidence and are distinct from product-test
failures. Use the normal task continuation controls after environment recovery.
`check-run` stores command, source fingerprint, timing and exit receipts in the
turn's `checks/` directory. Correlate those and retained logs with claimed UT,
race and static checks; do not judge execution from artifact filenames alone.

`status.scratch_cleanup` reports automatic task scratch reclamation and errors.
Terminal task `tmp` and shared Go `cache` are detached only after live-turn and
process/bind-mount checks, then removed in the background. Failures retry every
minute and after restart, including while dispatch is drained. Source, task
logs/results/evidence, SQLite and reusable session history are retained. Keep
needed validation logs outside scratch; failed-task reentry rebuilds caches.

For “在 review 什么 / 进展 / 之前 review 了哪些 / 结果”, use `mo-botctl status` and `mo-botctl jobs` first. These return compact summaries; use `mo-botctl job JOB_ID` for full immutable evidence, data and assessment. Map each session's `job_id` to job `pr/head/state`; report selected versus active model. `updated_at` is snapshot freshness, not proof that tools are advancing.

To ask a RUNNING session directly, use `mo-botctl progress SLOT`. This steers the current turn to report evidence, work remaining and blockers as commentary, then continue review. It does not cancel the turn or create an idle conversation; a stale turn ID is rejected. Read the actual reply via history/events: the steer acknowledgement alone is not a progress report.

For detail, use `mo-botctl history SLOT` for native conversation and `mo-botctl events` for current output. Distinguish facts already established from planned work, running checks and missing evidence; do not invent progress percentages. History spans multiple PRs because sessions are reused: bind observations to exact current job/head and turn ID.

For completed jobs, report `result.assessment.decision`, summary/findings, actual validation and limitations from the full job detail, plus `comment_id`, `review_id` and `review_event`. `publishing` has a saved result awaiting publication; `done` is published, while failed/superseded are not successful reviews. Expose errors for failed tasks. The summary API lists the latest 200 jobs plus active/affine jobs; disclose that limit instead of claiming an exhaustive history. `queued` is not proof of admission: inspect `dispatchable` and `wait_reason`, and the discovery success timestamp/error. CI/mergeability waits retain the job; an automatic same-head task already decisively reviewed becomes `superseded` with reason `already-reviewed`. A failed/incomplete discovery never implies PR closure. Status operations are read-only and do not authorize enqueue, interruption, model changes or review publication.

## Requested operations

- Enqueue: `mo-botctl enqueue PR_NUMBER`. This is force admission: it bypasses CI/draft/author/exclusion filters and can start a review and publish a status comment and a formal GitHub review under the review policy. Execute when requested; a status request does not authorize enqueue. Same PR/head returns the existing job and does not repeat a review.
- Converse: `mo-botctl chat SLOT 'message'`. Idle chat occupies the slot; active chat steers its existing turn. Inspect job/head first to target the intended review. `events` streams live output; `history SLOT` retrieves the latest 20 native items; use `history SLOT --cursor CURSOR` with the returned `nextCursor` for older items.
- Select model: inspect `models`, then `mo-botctl model SLOT MODEL --effort xhigh`. Change preserves thread/history and job/comment identity. A busy turn is interrupted and the same task continues via a native turn/start with the new model; idle switching starts a brief confirmation turn. Success means the native turn was started, not merely a database setting. In-flight requests are interrupted rather than hot-swapped.
- Interrupt: `mo-botctl interrupt SLOT` only when requested. It cancels that turn and supersedes its current review task; it is not a transparent session recovery action.

## Recovery invariants

SQLite is the workflow authority; Codex owns native thread history. Healthy IDs are resumed; missing/corrupt threads are replaced in their existing slot, retaining job/comment identity and bounded attempts. Do not delete the database, change service ID or remove GitHub markers to force recovery: this can destroy duplicate protection. Do not launch two deployments with the same service identity and separate databases.

A failed/ambiguous initial comment POST is deliberately fenced. Inspect the job error and authenticated-author marker on the PR before proposing manual reconciliation; do not blindly retry writes or force another review. `publishing` means a saved assessment awaits formal review/status-comment publication; GitHub outages must not trigger a fresh model review. An old head is superseded, never treated as a review of the current commit. Formal review POSTs are fenced before submission. After an uncertain response, reconcile the authenticated exact-head review marker; an absent, duplicated, dismissed or mismatched marker requires manual reconciliation, never a blind second POST. Inspect both review and comment identities before proposing recovery.

Use `updated_at`, queue counts and backend/session state to distinguish stale status, eligibility wait, model failure and actual execution. SSE is bounded and emits gaps: reload the paginated native history when events are missing. Do not describe green CI or an idle session as proof of a successful review. Report exact job/head, session/model, validation evidence and limits.

New same-head feedback joins the existing unattempted worker task as pending evidence, without changing a live turn's input. At a checkpoint, the existing harness may dismiss informational comments with a retained rationale; failed CI and formal reviews cannot be dismissed. Relevant feedback reopens the necessary design/review gate on the preserved source/candidate. Model failure, stale advice or timeout never discards feedback or creates a replacement task; conservative worker continuation remains available. Inspect `data.pending_evidence_count` and the full job's `pending_input`. A changed head still supersedes the old task, and a previously attempted effect keeps its reconciliation fence.

If `mo-botctl` is missing from PATH, try `~/.local/bin/mo-botctl`. If neither exists, report installation missing; do not guess a running service or silently start a replacement daemon. The source installer is `python3 scripts/install.py`; packaged installs use `mo-bot install`.

## Exceptional coordination and task intent

The existing local `classifier` slot shares its reusable `gpt-6-luna/xhigh` session between issue grouping and harness advice; it is not another worker or approval authority. Normal worker phases do not call it. Blocked checkpoints, repeated execution/protocol errors and five minutes without native activity may request advice. Silence alone never authorizes interruption; an inactivity decision is rate limited to ten minutes per task. Requests bind job/head/phase/attempt/native turn, expire after three minutes and survive restart. Queued tasks await a pending decision only until that deadline. Timeout, unavailable model, disabled pool or invalid advice releases that wait and preserves diagnostics and original guidance. Repeated execution failures retain source and use backoff rather than exhausting a lifetime task budget; ownership/cleanup/write uncertainty remains fenced.

Use `mo-botctl guide JOB_ID 'continue the missing validation / revise the invalid plan / pause …'` for task-level natural-language guidance. `POST /api/jobs/JOB_ID/guide` accepts `{"text":"…"}`. It applies to queued, running or failed worker tasks, never publishing/terminal completed tasks or attempted remote effects. Existing `chat SLOT`, `pause`, `resume`, `retry --hint` remain direct controls; prefer them for an urgent deterministic operation or when the semantic model is unavailable.

Harness may steer the exact active turn, suggest concrete continuation, return an invalidated plan to the mandatory read-only xhigh design gate, ask for missing input, or route explicit operator pause/resume through existing controls. Its `wait` action identifies an unavailable external prerequisite and a readiness check. At a preserved, unleased checkpoint it uses the existing queued state and durable retry deadline (5 minutes to 1 hour), retaining source/evidence and releasing model capacity. Active work must checkpoint before waiting. `data.recovery_wait`, `wait_reason`, `next_retry_at` and alerts expose the prerequisite. New feedback, operator `guide`/resume/retry, or deadline expiry can resume the same task; a dependency wait is not an operator pause or permanent failure. Recover supported local setup before using wait. Automatic advice cannot pause; no recommendation approves, publishes, reruns CI or changes a frozen target. Low-confidence advice asks instead of acting. Blocked questions receive a bounded wait; active workers can continue independent work without assuming an answer. Check `job.data.harness_request` and `harness_result` for IDs, reason, deadline, action, explanation and execution status, also exposed in status summaries and `harness/result` events. An acknowledgement only queues advice; it is not proof that an action ran. Uncertain steering acknowledgements are reported without replaying the message.

Independent environment/flaky failures do not create GitHub issues by default (`worker.cause_issues=false`). This includes transient size-label GitHub API socket resets and proven flaky TLS/network failures. Keep logs, concrete control evidence, PR/task explanation and bounded approved reruns. An existing attempted cause-issue write still needs reconciliation; disabling new writes never drops an uncertainty fence. Do not enable cause-issue publication unless the operator explicitly requests it.

## Delivery pool

Operate the shared worker queue using `mo-botctl repair PR` (configured owner's
PR only) and `mo-botctl issue NUMBER` (explicit issue assignment). The service
also admits configured-owner issues assigned to them with `bot-ready`; one
optional local classifier uses pinned `gpt-6-luna/xhigh` and groups only clear
same-root-cause matches into a compatible, early task (maximum three issues).
Uncertain matches and classifier/model failures remain separate issue jobs.
Inspect role, phase, error and native session in jobs/sessions.
`[worker] sessions` controls delivery capacity; its default model is
gpt-6.1-sol/low. Worker assignments live in repo-owned mo-bot-worker; the bundled
issue-to-pr workflow owns shared requirements and mandatory xhigh design/final
gates. Do not bypass default-ref guards
or retry uncertain GitHub writes manually.

## SSH session nodes

Inspect `nodes` and each session's `node` before diagnosing remote execution.
`GET /api/nodes` shows transport, endpoint, connectivity/cleanup errors and each
remote account's model catalog; `/api/models` is the local catalog. History,
progress, chat and model commands use the global slot ID and route to its original
host. Worker mandatory design/final xhigh gates remain in force.

Only Codex runs remotely. Queue/SQLite/source/tests/Git/GitHub and API stay local;
remote tools use `host_exec` back to the coordinator sandbox. Do not install a
second coordinator/database or copy credentials. Nodes need verified known_hosts,
noninteractive SSH, Python 3 and compatible signed-in Codex. Explicit proxy-only
`[node.env]` may be needed. Repo config.example.toml documents node/slot bindings.

`recovering` means a connection/resume wait, not a failed review. `fenced` means
tool cleanup is unconfirmed: retain leases/artifacts and inspect the exact error.
Do not delete ownership records or start a duplicate service. Nodes retry with
backoff; one remote outage does not restart other sessions. Changing endpoints
refuses active/affine slots and replaces idle identity. Restart is required for
transport configuration changes.

## Blocked tasks and controlled recovery

Use `mo-botctl alerts` for repair-admission budget/discovery failures, publication failures, manual reconciliation and native
inactivity over five minutes; inactivity alone does not prove a model is hung.
Jobs expose the error and next retry time. Publication releases model slots after
preserving reviewed commits in private coordinator Git refs; target/write fences
remain. A GitHub outage must not consume another model review.

Missing final validation continues in final_review with the accepted design retained. Only source/design findings or an explicit replan reopen earlier phases. Retry pacing uses source, frozen input and successful execution receipts rather than the wording of limitations; repeating the same check does not reset a stalled task.

Task controls use exact durable job IDs:

```bash
mo-botctl pause JOB_ID
mo-botctl resume JOB_ID
mo-botctl retry JOB_ID
mo-botctl cleanup JOB_ID
mo-botctl rerun JOB_ID --run-id RUN_ID
```

For a running turn, `mo-botctl chat SLOT 'guidance'` steers it immediately. For a stopped worker, use `mo-botctl retry JOB_ID --hint 'guidance'` or `resume JOB_ID --hint 'guidance'`. The latest hint stays with that durable task, appears in subsequent worker prompts and never carries into other jobs sharing the slot; an empty hint clears it. The coordinator also includes its previous rejection reason in the next worker prompt. Hints do not change the frozen target or bypass publication/write fences. Publishing has no model turn: reconcile uncertain effects or allow fresh-input work to replace a safely unpublished stale assessment.

Pause interrupts the turn, waits for tool cleanup, archives owned worker source
including uncommitted edits/rebase metadata, then releases the slot. Resume restores
the checkpoint in a free slot. Failed source without a preserved owned checkpoint
cannot be blindly restored. Publication retry reconciles the existing outbox;
it does not repeat the model task. Cleanup cancels a stopped task and frees its
lease/ref, retaining diagnostics/source evidence. It refuses uncertain remote
effects; reconcile first. Completed/superseded tasks cannot be replayed. Ordinary `retry` restores a preserved failed source and retains the current phase. If an older failure lacks a recoverable source checkpoint, explicitly use `mo-botctl retry JOB_ID --from-input`: it retains prior diagnostics/source archives, restarts the same job/comment at design (or triage) on the frozen input, and invalidates subsequent implementation/validation. It refuses attempted remote effects and live tools. Explicit retries reset interruption recovery counts while preserving lifetime attempt numbers. Incomplete worker assessments do not terminate delivery: blocked triage advances to the design gate, while blocked design/implementation continues its current phase with retained gaps and source. Unknown causes remain unapproved; missing validation must be investigated or executed, never treated as a pass. Worker correction and lifetime turn counts do not terminate delivery. An identical incomplete assessment for the same phase, source and gaps enters exponential backoff (one minute to one hour), preserves source, releases its model slot and reports an alert; the normal queue restores it when capacity is available. A changed candidate or rejection resets backoff. Explicit resume/retry can queue preserved source even when all worker slots are busy. Automatic admission remains bounded. An explicit `repair PR` can reconsider input after the automatic admission budget is exhausted and reuses an already active matching task. Rerun
requires the actual frozen xhigh gate, approved run ID, verified causal evidence,
current head/latest attempt and budget; repeated requests reconcile the same effect.

Before deployment, `mo-botctl drain --reason 'deployment …'` freezes new turns and
publication attempts. Existing turns settle into durable checkpoints. Wait for
`status.dispatch_control.drained=true`, restart, inspect sessions/jobs, then
`mo-botctl resume-service --reason 'deployment verified'`. Drain survives restart.
Session interrupt retains its cancel/supersede contract; use job pause for resume.
Signals and control reasons are logged. Status is a snapshot: check updated_at and
use history/progress before interrupting work on an inactivity alert alone.


## Online pool resizing

Use `mo-botctl pools` and `mo-botctl resize review COUNT` / `mo-botctl resize worker COUNT`
when requested (integer 0..8). This sets pool capacity, not a delta. SQLite saves
counts across restart, taking precedence over TOML defaults. Enlargement creates
or reactivates stable slots; confirm native readiness via sessions. Shrink stops
new tasks on higher pool indices and allows assigned work to drain without
interruption. Disabled slots remain visible and retain native identity/history,
source and model/node selection. No deletion or forced task cancellation.
Report enabled capacity separately from draining work; temporary active work can
exceed the new enabled capacity. Reenable before idle chat/model changes; active
progress/steering remains available. Global deployment drain still blocks launches.
