# multi-model-agentflow

[English](README.md) | [简体中文](README.zh-CN.md) | [Español](README.es.md) | [Português](README.pt.md)

`multi-model-agentflow` is a platform-agnostic, domain-agnostic, risk-aware control plane for coordinating multiple AI models across planning, authorization, execution, verification, review, cost tracking, privacy checks, and recovery.

The project is built around one idea: models should generate, implement, and review within explicit boundaries, while deterministic software owns authorization, state, budgets, safety gates, evidence, and recovery.

## Why This Exists

Complex agent workflows can fail in ways that are hard to audit:

- a plan changes, but the authorization boundary does not;
- one model writes, tests, reviews, and declares its own work complete;
- remote models receive more context or sensitive data than needed;
- paid calls are retried after uncertain failures;
- long-running tasks are interrupted without clear recovery state;
- human edits invalidate some results, but not necessarily all of them;
- state is scattered across chat, terminals, logs, model outputs, and Git diffs.

`multi-model-agentflow` tries to make those workflows explicit, inspectable, and recoverable.

## Current Status

The local MVP has been implemented and validated with automated tests. It covers the core loop:

```text
plan -> authorize -> implement -> self-test -> deterministic gates
-> independent review -> revise/rereview -> approve
```

It also covers safe pause, immediate freeze, human takeover, resumable execution, idempotent call tracking, cost evidence, file-boundary checks, and review evidence.

OpenCode-based remote roles are included in the current implementation scope:

- remote Reviewer: read-only, packet-only, no repository workspace access;
- remote Worker: implementation/revision role, only when explicitly allowed by the task contract;
- remote Worker execution happens inside a minimal staging sandbox;
- remote Worker inputs are declared through `input_artifacts` and verified by SHA-256;
- remote Worker outputs are synchronized back only to `allowed_files`, with all-or-nothing boundaries;
- remote calls are constrained by plan hash, authorization snapshot, budget, privacy policy, file scope, role permissions, and network policy.

Default development and validation use no-cost test doubles, local model paths, mocked processes, and stub OpenCode executables. Real paid provider calls and real remote smoke tests require a separate plan, displayed hash, explicit approval, and authorization.

## Core Guarantees

- **Explicit activation**: automatic Skill discovery may suggest AgentFlow, but it is not authorization to start models.
- **Plan-hash authorization**: authorization binds to canonical plan JSON and a SHA-256 content hash.
- **Scope invalidation**: changing models, files, budget, data exposure, permissions, or side effects requires fresh authorization.
- **Two-axis risk**: business importance uses B0-B3; operational safety uses S0-S3.
- **Privacy gates**: data is classified as D0-D3; D3 data and secrets never go remote.
- **Role separation**: implementation, self-test, and review cannot collapse into one self-approved model path.
- **Independent review**: Reviewers use fresh, read-only context; important or critical work requires cross-family review.
- **Remote Reviewer isolation**: remote Reviewers receive only a minimal Review Packet, not the repository workspace.
- **Remote Worker sandboxing**: remote Workers run in a minimal staging sandbox with network denied by default.
- **Input snapshots**: remote Worker inputs are project-relative files verified by SHA-256.
- **Atomic output sync**: remote Worker outputs are copied back only within authorized files and only when sync checks pass.
- **Review acceptance policy**: tasks can use `block_p0_p1` or `zero_findings`.
- **Cost honesty**: calls record tokens, duration, cost, unavailable-cost state, and idempotency keys.
- **Deterministic state**: events, projections, calls, tests, reviews, and evidence live in one SQLite state source.
- **Supervisor checkpoints**: deterministic wake events can create bounded supervisor checkpoints for low-token oversight.
- **Pause and recovery**: runs can be safely paused, frozen, taken over, and resumed without repeating completed paid calls.
- **Optional control plane**: AgentFlow can be stopped; Git worktrees, diffs, and evidence remain available for manual work.

## Quick Start

Run tests:

```bash
pytest
```

Show the current plan:

```bash
agentflow plan show
```

Authorize a displayed plan hash:

```bash
agentflow plan authorize --hash <sha256>
```

Start a run:

```bash
agentflow start <plan-id>
```

Inspect status:

```bash
agentflow status <run-id>
```

Follow status and logs:

```bash
agentflow status <run-id> --watch
agentflow logs <run-id> --follow
```

Inspect cost evidence:

```bash
agentflow cost <run-id>
```

Read or record supervisor checkpoints:

```bash
agentflow supervisor-next <run-id>
agentflow supervisor-record <run-id> <checkpoint-id> --decision '<json>'
```

Pause, freeze, take over, resolve, resume, or cancel:

```bash
agentflow pause <run-id>
agentflow pause <run-id> --immediate
agentflow takeover <run-id>
agentflow handoff <run-id>
agentflow resolve-call <run-id> <call-id>
agentflow resume <run-id>
agentflow cancel <run-id>
```

Run against an explicit project root:

```bash
agentflow --project <root> status <run-id>
```

## Typical Workflow

1. Create or generate `.agentflow/plan.json`.
2. Run `agentflow plan show`.
3. Review the normalized plan, effective scope, models, files, budget, privacy policy, mode, expiry, and SHA-256.
4. Explicitly approve the displayed hash.
5. Run `agentflow plan authorize --hash <sha256>`.
6. Run `agentflow start <plan-id>`.
7. Observe through `status`, `logs`, `cost`, and `supervisor-next`.
8. Use safe pause, freeze, takeover, or resume when human intervention is needed.
9. Approve completion only after deterministic gates and independent review pass.

## Remote Roles

### Reviewer

A remote Reviewer may only perform `review` or `rereview`.

It is read-only by default, does not receive the repository workspace, and receives only the minimal Review Packet generated by AgentFlow. The Review Packet is subject to privacy, budget, authorization, and independence gates.

A remote Reviewer must not edit files, write to disk, run shell commands, access external directories, browse the web, invoke Skills, start tasks/subagents, or request interactive permission upgrades.

Reviewer output must be one protocol-valid JSON object. Non-JSON prose, fenced JSON, missing fields, invalid types, invalid severity values, or contradictory approval cannot approve a task.

### Worker

A remote Worker may perform `implementation` or `revision` only when the task contract explicitly sets:

```yaml
allow_remote_implementation: true
```

The Worker runs inside a minimal staging sandbox containing only:

- hash-verified read-only `input_artifacts`;
- authorized `allowed_files`;
- a generated task briefing.

Network access is denied by default. Host allowlists cannot currently be safely expressed through the OpenCode permission layer, so allowlist mode fails closed.

Worker outputs are synchronized back only to `allowed_files`. Sync uses baseline and manifest checks and follows an all-or-nothing boundary. Input mutation, out-of-scope output, destination conflict, damaged manifest, or failed rollback blocks integration.

## Task Contract

A task contract describes the execution boundary before any model runs:

```yaml
task_id:
objective:
risk_level:
allowed_files:
forbidden_actions:
acceptance_criteria:
data_sensitivity:
implementation_model:
review_model:
fallback_model:
max_remote_cost:
max_retry_count:
escalation_conditions:
expected_outputs:
implementation_max_steps:
implementation_timeout_seconds:
implementation_max_continuations:
allow_remote_implementation:
remote_worker_network_mode:
remote_worker_allowed_hosts:
remote_worker_max_steps:
remote_worker_timeout_seconds:
input_artifacts:
review_acceptance_policy:
```

`risk_level` contains both business importance and operational safety. The task contract is part of the canonical plan JSON and authorization hash.

## Operating Modes

- **Managed**: the approved plan may run within its authorized scope without additional confirmations.
- **Supervised**: each implementation, review, revision, and rereview call requires confirmation.
- **Adaptive**: confirmations are determined only by the plan's static B/S thresholds and critical-node markers.

Adaptive mode does not perform learning-based routing or silently change strategy.

## State and Recovery

AgentFlow stores run state in a local SQLite database. Events and current-state projections are updated in the same transaction, so recovery can distinguish completed work, failed work, unknown calls, pending review, and human intervention states.

If a paid or remote call has an uncertain result, AgentFlow records it as `UNKNOWN` and pauses for reconciliation. It must not retry speculatively.

If a local implementation or revision call reaches a known step limit, it is recorded as a known incomplete failure. It may continue only as an authorized same-session segment and only within `implementation_max_continuations`. Remote Reviewers and remote Workers do not continue this way.

## Documentation Map

- `docs/requirements.md`: normative product requirements with stable IDs and acceptance evidence.
- `docs/mvp.md`: MVP scope, non-goals, acceptance scenarios, and validation status.
- `docs/architecture-decisions.md`: accepted, tentative, and pending architecture decisions.
- `task_plan.md`: implementation milestones and current work plan.
- `progress.md`: progress notes and validation records.
- `skills/multi-model-agentflow/SKILL.md`: Codex Skill entry point.
- `AGENTS.md`: repository collaboration rules and safety boundaries.

## Safety Boundaries

By default, this project does not:

- download or install models;
- register provider accounts;
- purchase credits;
- collect or store provider secrets;
- call models outside plan authorization;
- send D3 data, secrets, tokens, or private user data to remote models;
- treat configured/discoverable OpenCode models as callable-verified models;
- let remote Reviewers edit files, run shells, browse, or read the repository workspace;
- let remote Workers use network access, shell access, external directories, Skills, tasks, subagents, or interactive upgrades;
- use model prose as a substitute for tests, review records, database evidence, or Git diffs;
- use AI models to implement the state machine, locks, budget checks, waiting logic, or deterministic gates;
- generate API cost without explicit authorization.

## Development Notes

The generic core must remain independent of model provider, agent engine, platform, and application domain. Domain-specific policy belongs in project configuration, project documentation, or caller-provided strategy.

New requirements should be added to `docs/requirements.md` with stable IDs, observable behavior, and acceptance evidence. MVP scope changes should be reflected in `docs/mvp.md`. Architecture decisions and open questions should be recorded in `docs/architecture-decisions.md`.

Implementation changes should preserve evidence for tests, review, cost, authorization, state, and recovery. Test doubles must be clearly labeled and must not be represented as real model runs.

## Disclaimer

This project is experimental software for research and development workflows. It is provided as-is, without warranty of any kind.

It does not provide legal, financial, security, compliance, or other professional advice. Users are responsible for reviewing plans, permissions, model outputs, costs, privacy boundaries, provider behavior, and downstream effects before using it on real projects or with real model providers.

Real remote provider calls, paid model usage, sensitive-data processing, and production deployment require separate review and explicit authorization.

## License

This project is licensed under the Apache License 2.0. See `LICENSE` for details.
