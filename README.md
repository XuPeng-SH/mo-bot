# mo-bot

A PR review and repair server with a SQLite queue, reusable Codex sessions and a REST API. `mo-bot` installs/configures/runs the server; `mo-botctl` operates it.

Each task turn includes shared validation launchers. `check-run --preflight`
checks Linux user/mount/network isolation before compiling tests;
`go test -exec=/absolute/turn/test-exec` runs each binary with a dedicated private
`/tmp` and loopback. `check-run --timeout SECONDS -- COMMAND` bounds foreground
checks and reaps their owned process group on cancellation/timeout. Build caches
stay with the task; logs and validation evidence stay outside disposable scratch.
Unavailable isolation is reported as setup failure and never as a test pass.
`check-run` records argv, checkout fingerprint, timing and exit status under the
turn's `checks/` directory, outside scratch. Reviewers match validation claims to
these receipts and logs; preflight is recorded separately from actual checks.

## Install and start

Requires Linux, Python 3.11+, Git, authenticated GitHub CLI (`gh`) and Codex CLI with app-server support. No runtime Python dependencies or external skills are required. Authenticate with `gh auth login` and `codex login`, and clone the repository whose PRs the bot will handle.

From this checkout:

```bash
python3 scripts/install.py
export PATH="$HOME/.local/bin:$PATH"
mo-bot init --repository OWNER/REPO --source-dir /path/to/source
mo-bot doctor
mo-bot serve
```

The reviewer defaults to the authenticated GitHub user. `init --reviewer LOGIN` selects another reviewer's queue; `--worker-sessions 2` enables repairs for the authenticated owner. Existing config is never overwritten. The default config is `~/.config/mo-bot/config.toml`; `--config PATH` works before or after the server subcommand. The old `mo-bot --config PATH` server invocation remains supported.

Alternatively, install a Python package with `python3 -m pip install .` (or `-e .` for development), then run `mo-bot install` to install its skills. Wheels include the operator, reviewer, worker and shared issue-to-pr skills, their invocation metadata and API reference. Source installs link CLI/skills to the checkout, so keep that checkout in place. Package installs link skills to the installed package. No source compilation is required.

Skills install into both `~/.codex/skills` and `~/.claude/skills` by default. Use `--agent codex`, `--agent claude`, or repeated `--skill-dir PATH` for custom agent directories. Restart/reload the agent's skill discovery after installation. The `mo-bot-service` skill tells an agent how to configure, preflight, start and operate the service. Its API reference and the worker/reviewer policy ship with the same package.

Installation is repeatable, updates owned links after checkout relocation, and rejects foreign or modified paths before changing CLI/skills. An installation manifest records ownership and lets an interrupted install be retried. `mo-bot uninstall` removes only unchanged installer-owned files/links; it preserves configuration, SQLite, credentials, native histories and modified/foreign paths. With a pip install, use pip to remove the Python package itself.

Systemd is optional:

```bash
mo-bot install --systemd --start
systemctl --user status mo-bot.service
journalctl --user -u mo-bot.service -f
```

`--start` requires passing preflight. `--systemd` alone installs/reloads the unit without restarting running work. User systemd needs a working user manager; use foreground `serve` otherwise. Environment overrides such as outbound proxy settings may be placed in `~/.config/mo-bot/environment`. For persistence after logout, the host administrator may need to enable user lingering.

For upgrades, run tests, drain the server with `mo-botctl drain`, wait for `dispatch_control.drained=true`, update the package/checkout and repeat installation, restart, inspect status, then `mo-botctl resume-service`. Config and state remain intact. `docs/design.md` describes lifecycle and recovery boundaries; the maintained operator skill owns the detailed API contract.

```bash
mo-botctl status
mo-botctl sessions
mo-botctl jobs
mo-botctl job JOB_ID
mo-botctl progress SLOT
```

The service retains its marked PR status comment and submits a commit-pinned formal review: `request_changes` → `REQUEST_CHANGES`, `approve` → `APPROVE`, and `needs_validation` → `COMMENT`. PRs authored by the configured reviewer or authenticated account receive `COMMENT` for every assessment. Own PRs without an authenticated, marked bot review on the current head are admitted without waiting for CI, while draft/exclusion/mergeability gates still apply. A review POST is fenced durably; uncertain responses are reconciled by authenticated markers, never blindly repeated. Existing completed jobs are not republished. Reviews run against detached frozen checkouts; only diagnostic directories are writable by the model. It is a trusted local automation using the owner's identities, not a multi-tenant security boundary.

User systemd deployments need their outbound proxy environment explicitly configured when required. `KillMode=control-group` prevents orphaned model/tool work on restart. To survive the last logout, user lingering must be enabled by the machine administrator (`loginctl enable-linger USER`); the current machine denied this operation for xupeng.

Automatic review skips a new head when an authenticated marked bot review covers
an earlier head and every subsequent first-parent commit is a clean merge from
the repository's default branch (normally `main`). Git recomputes each merge tree
to verify that no edits were added inside the merge. Conflict resolutions, new
feature commits, merges from other branches, and unavailable proof keep normal
review admission. Discovery reports `merge-only-reviewed` and the original
`reviewed_head`; it does not publish a review claiming validation of the new SHA.
Explicit `mo-botctl enqueue PR` bypasses this filter too.

## Verification

```bash
python3 -m pip install -e '.[dev]'
python3 -m unittest discover -s tests -v
ruff check mo_bot tests
ruff format --check mo_bot tests
```

Tests exercise SQLite uniqueness/fences, remote marker recovery, ambiguous writes, saved publication checkpoints, head changes, result validation, native JSON-RPC turn reuse/replacement/disconnection, live message steering, model persistence and HTTP authentication. Fake GitHub tests never write to real PRs. Real native session/restart probes are separate operational validation.

Each new review reads the maintained [mo-bot-review skill](.agents/skills/mo-bot-review/SKILL.md) at launch: design/ownership, systemic closure, all comment concerns, senior QA, unhappy paths, performance and implementation/test/documentation deltas; subagents are prohibited.

Review guidance edits take effect for the next assigned review without restarting the service. Already-started/model-switched turns retain their frozen prompt. The installed Codex skill links to the same repository source. Software changes require restarting the service; use idle slots when possible.

## Delivery workers

`[worker] sessions = 2` enables a separate reusable delivery pool (default `0`
keeps existing deployments review-only). `classifier_sessions = 1` enables one
local semantic grouping session; it is pinned to `gpt-6-luna` / `xhigh`. Its
default is `gpt-6.1-sol` / `low`.
The existing five-minute scan automatically admits only the configured reviewer's
open non-draft, non-excluded PRs with new review feedback, conflicts or failed CI.
It does not require green CI. Other authors' PRs remain review-only.

Every task uses the bundled `issue-to-pr` workflow and `mo-bot-worker` assignment
contract. The installer provides both skills; no file under an operator's home is
required. An optional `issue_skill` configuration supplies additional custom
instructions; omit it for the bundled workflow. Existing external skills are not
overwritten by installation: preserve or relocate them before migrating to the
bundled skill. Triage → actual `gpt-6.1-terra/max` design → writable
implementation → actual `gpt-6.1-terra/max` overall review run as native turns in
one retained session. Blocked work backs off without a lifetime failure; mandatory gates cannot be lowered
by changing the session default. Unknown CI causes and unavailable validation
block delivery. Proven environment/existing flaky failures retain causal evidence
before a bounded approved current-head Actions rerun; cause issues are opt-in. Rebase is reserved for
real conflicts/dependencies or an explicit operator request; a push starts fresh CI and never reruns old-head CI.

Explicit assignments use the same queue:

```bash
mo-botctl repair 29422 --hint "Understand comments and CI failures; rebase, repair and validate"
mo-botctl issue 12345
mo-botctl sessions
mo-botctl jobs
```

Explicit repair can assign another author's open non-draft PR; automatic discovery still selects only the configured owner. The scheduler freezes source identity and initial guidance independently of publication permissions. External repairs use an owned fork branch and a marked integration PR targeting the original author's source branch, preserving the original upstream PR as the task target. Workers run the same triage/design/implementation/final-review phases and never write directly to the author's fork. Saved reviews awaiting admission do not block repair execution; active turns and uncertain writes retain their PR fences.

Issue jobs require an existing personal fork and create an owned feature branch
and marked PR. Automatic issue intake is limited to open issues assigned to the
configured reviewer with the `bot-ready` label. The Luna classifier may merge a
new issue into a compatible queued or very early triage/design task only when
confidence is high; uncertain matches stay separate. Groups are limited to
three issues, and classifier/model failure also queues the issue separately.
Review/worker jobs share per-PR fences. Worker stages retain their slot and dirty
independent clone across restarts; pool resizing preserves global slot IDs.
`[[session]] role = "worker", slot = 0` sets creation defaults by pool index.

The coordinator owns Git metadata, commits and all GitHub writes. It pins the
actor, repository identities, original head and ref, rejects every destination
default branch, reports URL/source/destination/default before each push and uses
an explicit expected-SHA (or absent-ref) lease. Shared head repositories need
explicit `allowed_head_repos`; default owned fork delivery needs no allowlist.
Issue/PR creation, reruns and pushes have durable attempted-write checkpoints and
independent reconciliation. Read reconciliation uses bounded backoff; manual-only
errors stop background polling. `mo-botctl reconcile JOB_ID` explicitly rechecks
the same frozen outbox without bypassing write fences. Uncertain absent effects remain blocked in
`publishing`, retain their target/write fences and require manual reconciliation;
reviewed commits are preserved in private Git refs so model slots are released.
there are no blind POST or push retries. Delivery done means the accepted commit
and effects were verified, not that newly started CI is green.

This is a trusted local service with selected GitHub credentials and network
access. Worker prompts delegate remote mutations to the coordinator; they do not
constitute an independent credential/security boundary. Source is writable only
in assigned implementation turns; Git metadata/default/ref checks are also
verified by the coordinator. Do not deploy with unrelated credentials.

Mixed CI failures receive per-failure causal dispositions. A PR regression is
fixed while independently proven environment/flaky causes retain evidence for
approved delivery/rerun. Omitting a failure disposition blocks the design gate.
Detailed PR evidence is gathered by bounded read-only child processes so review
turn completion and local commands keep running. Semantic input changes fence
stale same-head delivery; handled feedback follows only the bot's exact candidate.

Discovery runs in bounded background children, keeping session commands responsive to scan timeouts. Complete admission snapshots reconcile stale tasks; transient waits remain visible. Job retries and publication failures are isolated, native turn recovery verifies exact provenance, and a process lock is acquired before database migration. Status responses omit bulk immutable evidence; fetch full job details when needed. Synchronous GitHub/git task preparation and delivery still have explicit deadlines and can delay scheduler write-command admission; they are not advertised as fully asynchronous.

## SSH Codex sessions

An optional `[[node]]` declares `name`, `ssh` (host or user@host) and an absolute
remote `codex` executable. Bind a review/worker pool index using
`[[session]] role = "worker", slot = 1, node = "idc55"`; see config.example.toml.
Unassigned slots remain local. Restart the service to apply transport bindings.

Only the Codex session/app-server and a small stdio supervisor run remotely.
SQLite, queue, source, builds, diagnostics, Git/GitHub delivery and REST API stay
on the coordinator. Remote native shell tools are disabled; the `host_exec`
dynamic tool runs bounded commands through the existing local Codex sandbox.
Implementation may write assigned source; review and other phases may only write
diagnostics. No filesystem synchronization, remote SQLite or second bot install
is required. This does not move build/CPU load to the remote host.

SSH uses BatchMode, verified known_hosts, no agent forwarding, no PTY and bounded
connection/keepalive deadlines. The remote host needs Python 3 and an authenticated
Codex CLI supporting experimental dynamic tools and native thread resume
(validated with 0.162.1). Credentials stay on their respective hosts. Optional
`[node.env]` accepts proxy variables only; noninteractive SSH does not inherit
interactive proxy settings. Use trusted hosts/accounts: task source and tool
results reach the remote model session; this is not a security isolation boundary.

```bash
mo-botctl nodes
mo-botctl sessions
mo-botctl history 3
mo-botctl progress 3
mo-botctl model 3 gpt-6.1-sol --effort low
```

History/steering/model selection route to the thread's original node. One node's
outage does not restart other nodes' sessions. Reconnect resumes healthy IDs;
corrupt/missing IDs use the existing bounded replacement policy. Nodes have
60-second reconnect backoff and their own model catalogs. A remote heartbeat
timeout reaps Codex and releases its runtime lock within approximately 31 seconds;
clean shutdown sends EOF and waits for cleanup first.

Host commands are limited to one concurrent request per turn, 600 seconds each,
the remaining job budget and 64 KiB output. Requests must match the current
thread/turn; stale calls and cwd escapes are rejected. Interrupt/disconnect must
stop local tools before lease/workspace reuse. Ambiguous RPC termination escalates
to cleanup of the identity-verified owned tool tree; unconfirmed cleanup fences
the node. Restart recovery reaps saved tool owners before consuming results. Do
not delete owner records or release leases to bypass a fence. Endpoint/binding
changes replace idle thread identity and refuse leased slots. Native setup still
uses bounded synchronous calls; scheduling is not advertised as zero-latency.

The installed systemd unit allows 180 seconds for stop: the former 30-second
budget was shorter than the default 45-second in-flight command deadline. Hard
termination still invokes control-group cleanup and the remote heartbeat watchdog;
the durable queue and native IDs are recovered on the next startup.

Review launch carries accepted worker validation for the same repository and exact candidate commit in `evidence.json.local_validation`. It includes retained gate records and artifact inventories; reviewers reconcile receipts, logs, fingerprints and coverage rather than inherit the old verdict. Missing or foreign artifacts remain explicit gaps. Go module verification uses task-owned GOPATH alongside the existing task caches, avoiding shared host sumdb state.

## Recovery and deployment

`mo-botctl alerts` exposes blocked jobs with error, retry time and manual-action
requirements, plus five-minute native inactivity warnings (not automatic verdicts
of a hang). Status shows last native activity. Publishing holds the target/outbox
fence and immutable commit, while releasing model capacity. Cause issues use a
bounded marker search, verify canonical ownership, then use saved numbers for
direct reconciliation; successful POST receipts survive search-index lag.

Use `pause JOB_ID`, `resume JOB_ID`, `retry JOB_ID`, `cleanup JOB_ID` and
`rerun JOB_ID --run-id RUN_ID` through mo-botctl. Pause waits for native/tool stop,
preserves dirty source and Git/rebase state, and frees the slot; resume may use
another free worker session with the same checkpoint/job/comment. Cleanup cancels
a stopped task without deleting evidence or bypassing uncertain-write fences.
Rerun follows the approved frozen plan, cause issues and current-head/latest-run
budget checks. Repeated publication/Actions operations reconcile prior effects.

Configured-owner PRs labelled `do-not-merge/wip` are excluded from automatic
review and repair. Explicit review enqueue overrides review admission filters
(WIP, draft, exclusions, CI and already-reviewed) through launch and publication.
Repair retains its WIP restriction. Automatic tasks wait through the existing
source-preserving retry path; removing the label permits normal admission.
Operator-paused tasks still require explicit resume. Existing active review jobs
are promoted to forced admission without duplicating work; completed reviews and
uncertain publication are not replayed. New or edited nonempty human discussion
can admit a separate review round on the same head; unchanged discussion coalesces.
The reviewer receives all paginated PR comments, inline replies and formal reviews
with a chronological index. Before accepting a decision, the coordinator verifies
a discussion-assessment ledger covering every nonempty item, including dispositions,
rationale and evidence for substantive conclusions. Authors and previous reviewers
are held to the same evidence standard. Documented design choices are evaluated
against their actual contracts, rather than treating the previous bot verdict as
authority. Before an unattempted formal publication, changed human discussion
supersedes the saved assessment and queues a fresh context round. Each round has
separate markers and publication fences; bot output cannot cause review loops.

All phases and retries of a task share its Go build/module caches. Stage `tmp`
and task `cache` are disposable: save validation logs, reports and evidence in
the stage directory outside them. Once a job is done, failed or superseded and
has no live turn, process reference or bind mount, the coordinator detaches its
scratch and deletes it in the background. Source checkpoints, native histories,
events and results are retained; retries rebuild discarded caches. Maintenance
runs every minute, including during dispatch drain and low-disk admission waits.
Deletion failures remain in `scratch-trash` for retry after restart; status exposes
`scratch_cleanup` activity and errors. Interrupted cleanup stops between files.

Rebase design pins the actual target branch tip separately from the PR's historical base SHA. Implementation fetches that exact commit and verifies either a stopped rebase onto it or its ancestry in the candidate; an old `rebased=true` flag is never sufficient.

Failed workers preserve owned source for `retry`/`resume`. For legacy failures without a recoverable source, `mo-botctl retry JOB_ID --from-input` retains diagnostics and restarts the same task from its frozen input, requiring new design and validation. It rejects active tools, completed tasks and attempted remote effects. Explicit reentry renews bounded attempt capacity while keeping lifetime counters; explicit repair is not blocked by the automatic admission budget.

For deployment, drain first and wait for `dispatch_control.drained=true`:

```bash
mo-botctl drain --reason 'deploy verified service changes'
mo-botctl status
systemctl --user restart mo-bot.service
mo-botctl sessions
mo-botctl resume-service --reason 'restart and retained sessions verified'
```

Drain is persisted across restart and blocks new model/publication operations;
existing turns finish into checkpoints. Signals and operator control reasons are
logged. Archived interrupted source is kept for recovery and consumes disk until
explicit retention cleanup; source without a proven owned checkpoint is not
silently recreated or overwritten. Saved results are published without a fresh
model task. A done/superseded job is not replayed by retry/resume.


## Online pool capacity

```bash
mo-botctl pools
mo-botctl resize review 3
mo-botctl resize worker 4
```

Both pools accept 0..8 slots independently without restarting. Higher indices
stop taking new tasks when shrunk; assigned turns and worker phases finish normally.
Disabled slots keep their IDs, histories, source and selected models for reuse.
SQLite persists online capacity over TOML defaults on restart. New slots initialize
asynchronously; check sessions for readiness/errors. Global drain still takes precedence.
REST: `GET /api/pools`, `POST /api/pools/review` or `/api/pools/worker` with `{"sessions":3}`.


## Own-PR admission

An own PR without an authenticated, marked bot review on its current commit can enter review
while CI is pending, missing or failed. A new head starts a new review identity;
reviews on previous commits do not suppress it. Draft/exclusion/mergeability gates
remain. Own assessments publish formal COMMENT reviews with exact-head markers.
Individual failed checks/statuses can enqueue repair before the overall workflow
finishes. Review and repair queue independently but the shared PR fence prevents
concurrent execution; failure evidence prevents repeated enqueue. Pending CI is
never reported as passed, and actual reruns still require a terminal failed run.

### Harness guidance

The optional classifier pool shares one reusable `gpt-6-luna/xhigh` session for
issue grouping and exceptional worker coordination. Normal tasks run directly;
blocked checkpoints, repeated execution failures and native inactivity may ask
for a bounded next-step recommendation. SQLite retains requests and outcomes.
Model failure or timeout releases the coordination wait without approving work.

```bash
mo-botctl resize classifier 1
mo-botctl guide JOB_ID 'Continue from the preserved source and execute missing SQL checks'
mo-botctl job JOB_ID
```

Task guidance can steer a running turn, resume a failed/paused checkpoint, or
return an invalid implementation plan to the independent design gate. Only an
explicit operator request can pause a task. Direct `chat`, `pause`, `resume` and
`retry --hint` remain available. Inspect `data.harness_request` and
`data.harness_result`: admission is not execution, stale advice is discarded,
and missing input is reported as an attention alert. No advice bypasses source
ownership, tool cleanup or publication fences.

Missing final validation stays in final_review with the accepted design retained.
Supported local setup recovery and missing checks are concrete continuation work.
For an unavailable external prerequisite, harness `wait` preserves the checkpoint
and releases capacity using the existing queue/retry deadline (5 minutes to 1 hour).
Status exposes `data.recovery_wait`, `wait_reason`, `next_retry_at` and an alert.
New evidence or operator guidance wakes the task early; a deadline retries it
without a permanent failure. Repeated gaps back off regardless of paraphrased
descriptions; source/input changes and new successful execution receipts establish
progress. Lost steering acknowledgements are surfaced without replaying the message.

New same-head PR observations join the existing unattempted repair task rather
than creating another task for each failed job and workflow aggregate. Turn input
stays fixed until a checkpoint. Harness can dismiss informational comments with
an audited reason; new CI failures and formal review evidence require worker
validation. Relevant feedback resumes the preserved candidate through the
necessary gate. Stale decisions, model outages and timeouts conservatively route
feedback back to that task. A changed head or attempted remote write retains its
existing supersession/reconciliation protections.

Independent environment/flaky CI failures (including transient GitHub API and
proven flaky TLS/network failures) do not automatically create GitHub issues.
The default `worker.cause_issues = false` retains causal evidence and bounded
approved reruns; uncertain previous writes still require reconciliation.

Ordinary manual reviews, including `COMMENTED` reviews by the configured reviewer,
do not satisfy bot review admission. Bot review markers must identify the exact
head, a service/job and review state, and be authored by the configured reviewer.
Previously used bot service identities still count.
