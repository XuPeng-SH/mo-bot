---
name: issue-to-pr
description: Explicit-only general issue-resolution workflow with evidence reuse, gpt-6.1-terra/max design and final review, implementation, validation, and PR delivery. Use only when the user explicitly invokes $issue-to-pr; ordinary issue, bug, review, or optimization requests do not activate it.
---

# Issue to PR

Apply after an explicit `$issue-to-pr` invocation or an explicit task assignment from the mo-bot coordinator. Continue it through follow-up messages on the same task; do not carry activation into unrelated tasks. Works across repositories and problem domains; no MatrixOne-specific assumptions.

## Coordinator-assigned mode

For a mo-bot task, the bundled [mo-bot-worker](../mo-bot-worker/SKILL.md) defines the assignment, allowed source paths, phase and result schema. Use this workflow's evidence, design, QA and validation requirements within that assignment. The coordinator invokes the separate design and final-review model turns and owns Git metadata, rebase, commits and every GitHub write. The worker implements only in its assigned implementation phase and returns delivery data; do not delegate or perform delivery yourself. Reuse the coordinator's existing task/checkpoint and diagnostic directories instead of creating another task record. Report an unmet gate as a blocked result for the coordinator; do not request an exception or change the task through an independent approval flow.

For direct user invocation, the agent owns the authorized workflow and delivery as described below. These direct-mode ownership and delegation instructions do not apply to coordinator-assigned tasks.

## Roles and scope

- In direct mode, the current agent owns investigation, reproduction, implementation, QA challenges, validation, and authorized delivery. Coordinator-assigned ownership follows the worker contract above.
- **The `gpt-6.1-terra` model with `max` reasoning must produce the solution/design before implementation and perform the final overall review before submission.** These gates apply to every task using this skill, not only complex issues.
- Use a supported delegation or CLI interface that explicitly selects the model and reasoning effort. Record the actual model, effort, reviewed revision, and review artifact/session ID. Do not infer model selection from a prompt or claim that current-agent self-review satisfies a separately requested model review.
- Discover an available invocation mechanism once and reuse it. If `gpt-6.1-terra` with `max` reasoning is unavailable, report the exact missing capability and request an alternative; continue independent investigation but do not silently bypass either gate.
- Honor the user's existing authorization. Invocation does not authorize unrelated changes, deployment, merging, or destructive operations. A fix-to-PR request includes ordinary commits, pushes, and PR creation; a diagnosis-only request ends with evidence and recommendations.
- Use repository instructions and relevant domain skills for build, platform prerequisites, testing, and PR templates. Do not create new universal requirements from one repository's conventions.

## Durable task record

At first use, create or reuse one compact task record outside tracked source unless the repository provides a designated location. Report its path. Prefer a persistent location under `~/.codex/issue-to-pr/<repo>/<task-id>/`. Reuse it after compaction, interruptions, or model handoffs.

Keep only actionable state:

- Scope, acceptance criteria, branch/worktree, base revision, current revision or dirty-diff fingerprint, and relevant environment/configuration/data versions.
- Confirmed facts, root-cause evidence, rejected hypotheses with reasons, unresolved questions, and existing fixes/PRs.
- Accepted design and invariants, model design/review artifacts, and finding dispositions.
- Evidence ledger: `ID | question/risk | command or experiment | source/test fingerprint | environment/data | result | conclusion | artifact | invalidation condition`.
- Current stage, next concrete action, and delivery links. Update after meaningful discoveries or changes, not every shell command.

Use IDs to reference existing evidence in handoffs and reviews rather than copying logs. Never store credentials in the record.

## Workflow

### 1. Understand and reproduce

Read the issue, relevant discussions, linked changes, and existing task evidence. Check whether an active or merged fix already covers the behavior. Identify product intent before deciding that a compatibility difference is a bug.

Reproduce through the public interface and trace the real code boundary where feasible. Use an appropriate reference implementation or documented contract. Compare passing controls and closely related cases that test the same invariant. Clearly distinguish an executed reproduction from source-level inference; if execution is unavailable, record the missing evidence rather than claiming root cause is proven.

Each experiment must answer an unresolved question or challenge a specific assumption. Stop exploration once the causal explanation and acceptance boundary are sufficient for design. Do not reopen rejected hypotheses without new evidence.

### 2. gpt-6.1-terra / max solution and design

Provide the model with scope, exact source revision, relevant code, reproduced behavior, controls, confirmed facts, rejected hypotheses, evidence IDs, and unresolved questions. Ask it to design from this evidence and inspect the causal code; do not ask it to repeat completed discovery.

Require an implementable design covering:

- Root cause, expected behavior, and invariants.
- Smallest coherent correction and affected component boundaries; avoid per-example patches.
- Applicable performance, compatibility, failure/cancellation/rollback, ownership, and bounded-resource concerns.
- A risk-to-validation map, acceptance criteria, and justified remaining limitations.

For small fixes, a short design is sufficient. Challenge assumptions before implementation, then treat the accepted design as stable until new evidence invalidates it. Scope unrelated findings separately instead of expanding the task automatically.

### 3. Implement and validate

Implement the accepted design with focused regression coverage. Verify platform/build prerequisites once before expensive tests. Preserve other work and running services; use an isolated worktree or test instance when needed.

Choose checks by affected behavior and risk: targeted UT/integration/public-interface tests and relevant compatibility or performance controls. Incremental static analysis and UT with race detection are required by the pre-PR gate below. Repeated runs, prepared execution, distributed tests, and benchmarks remain conditional on the changed paths.

### 4. QA counterexample pass

Challenge the fix using realistic inputs and execution paths that could falsify its invariants: boundaries, NULL/empty/invalid inputs, mixed batches, reuse, state transitions, failure/rollback, and concurrency where relevant. Assert user-visible results and cleanup, not just internal flags or absence of panic.

Cover plausible neighboring defects in the same mechanism. Expand implementation only when a concrete counterexample or reachable contract violation justifies it. Record why an apparent theoretical risk cannot occur when established system invariants exclude it.

### 5. gpt-6.1-terra / max overall review and correction loop

Give `gpt-6.1-terra` with `max` reasoning the complete change map, current diff/revision, accepted design, surrounding production boundaries, QA counterexamples, and evidence ledger. Require an overall review of design, correctness, performance, unhappy paths, compatibility, and test quality. Request actionable findings with location, trigger, impact, evidence, and a proposed correction; distinguish confirmed defects from unproven concerns.

Resolve material findings, explain rejected findings with evidence, and validate affected paths. Return to `gpt-6.1-terra` with `max` reasoning for re-review. Re-review focuses on new changes and invalidated conclusions while reconciling them with the entire design; a delta review must not omit cross-component interactions. Unchanged, still-valid review evidence remains usable.

Converge when acceptance criteria are met, relevant checks pass, and `gpt-6.1-terra` with `max` reasoning reports no unresolved material blocker. Do not invent work to fill review rounds. If the same unresolved finding recurs without new evidence or progress, identify the missing experiment/decision instead of repeating the review blindly.

### 6. Deliver

#### Pre-PR validation gate

Before the final push and PR creation/update, check the complete PR delta against the target branch's merge base, including all implementation and review corrections. Require:

- **Incremental SCA:** run the repository's static analysis for changed code, tests, and affected packages. Use its configured tools and build environment; for Go, include `go vet` and the configured lint/SCA checks. Limit reported new findings to the PR delta without hiding diagnostics from unchanged lines that the change makes relevant. Compare against the base when needed to distinguish existing findings.
- **Incremental UT with race detection:** run the relevant existing and new unit tests for affected packages and directly affected consumers. For Go, use `go test -race` with the repository's required CGo/build configuration. Do not restrict coverage to newly added tests; cover the behavior and lifecycle boundaries changed by the PR. Use bounded timeouts and retain logs and exit codes.
- Record the merge base, source/test fingerprint, package/test scope, commands, environment, results, and unresolved findings in the evidence ledger. Summarize both checks in the PR's validation section. A normal UT run does not substitute for a race run.

Reuse completed checks only when their inputs and affected dependencies still match the final delta. After fixes or a rebase, rerun the checks whose evidence was invalidated; do not rerun unchanged checks merely because this is the delivery stage. Resolve new SCA findings, test failures, and race reports before delivery. An unavailable tool/platform or blocked execution is not a pass: report the exact gap and obtain an explicit user exception before proceeding with an unmet gate. For documentation-only changes or code with no applicable race detector, record the specific not-applicable reason and use applicable validation; do not invent a Go requirement for another ecosystem.

Use the repository PR template. Describe the final problem and behavior, causal fix/design, linked issues, meaningful validation, performance implications, and remaining risks. Distinguish historical evidence, current-code validation, and pending CI. Do not dump the conversational chronology into the PR.

Commit, push, and create/update the PR when within the user's authorized scope. Verify remote revision and publication once. Do not rebase merely to chase the latest base; rebase when requested or needed to resolve a real dependency/conflict. If rebasing, invalidate only evidence affected by changed dependencies.

Report the PR, revision, `gpt-6.1-terra` / `max` design and review evidence, key validation, and remaining checks. Do not claim pending CI passed. Wait or monitor CI only when requested or required by the agreed completion criterion.

## No duplicate exploration or validation

- Before an action, consult the task record: what new fact or changed behavior will this establish?
- Reuse evidence when relevant source, tests, configuration, environment, and data are unchanged. A new commit SHA alone—such as committing already-tested content or changing documentation—does not invalidate tests.
- When code changes, map affected risks and dependencies to the smallest sufficient revalidation set. Broaden only for failures, shared-boundary changes, or unresolved concerns.
- Repeated runs require an explicit purpose and bounded count, such as demonstrating a timing flake or validating a concurrency fix. Record the count in advance.
- Distinguish reproduction, post-fix verification, and final review; do not execute the same check three times solely because the stage name changed.
- Resume from recorded state after compaction or delegation. A new agent is not a reason to rebuild, re-download, rediscover, or retest unchanged artifacts.
- Preserve overall review coverage through a change map and evidence reconciliation, rather than rereading every unchanged line on every round.
- Once the acceptance and review gates are satisfied, deliver. Further work requires a specific new failure, risk, or user requirement.
