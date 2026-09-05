---
name: multi-model-agentflow
description: Plan, authorize, run, review, pause, or recover risk-aware multi-model tasks through the local AgentFlow control plane. Use for bounded multi-model or long-running project work; do not use for ordinary single-model questions that need no persistent execution workflow.
---

# Multi-Model AgentFlow

Use the `agentflow` CLI as the only plan, authorization, execution, and status source. Do not call implementation or review models directly around it.

## Safety boundary

Automatic skill selection may recommend AgentFlow, inspect local status, or draft a plan. It is never authorization to start a model.

Before the first model call:

1. Read the target project's `AGENTS.md` and `.agentflow/project.toml` when present.
2. Classify business importance (B0-B3), operational safety (S0-S3), and data sensitivity (D0-D3) separately.
3. Put finite tasks, exact allowed files, forbidden actions, acceptance criteria, role models, budget, retries, and escalation conditions in `.agentflow/plan.json`.
4. Set an explicit finite `implementation_max_steps` (1-32) for local tasks that must read multiple evidence files, e.g. 12-16. The default 8 only exists for backward compatibility. Set an explicit `implementation_timeout_seconds` (60-14400) for long-running local tasks; the default 900 only exists for backward compatibility. Set `implementation_max_continuations` (0-8) only when the local implementation may outgrow a single step budget and is authorized to continue in the same OpenCode session; the default 0 disables continuation.
5. Run `agentflow --project <root> plan show` and present the exact SHA-256 plus the effective scope, models, file boundaries, remote budget, privacy policy, mode, and expiry.
6. Wait for explicit approval of that plan hash. Only then run `agentflow --project <root> plan authorize --hash <sha256>` and `agentflow --project <root> start <plan-id>`.

Any plan, model, file, budget, remote-data, permission, or side-effect expansion invalidates the prior approval. Show the changed scope and obtain a new authorization snapshot.

## Model selection

- Prefer models that are already loaded, callable, allowed, and sufficiently trusted for the task.
- Discover local availability with AgentFlow/OpenCode/LM Studio status; treat installed-but-unloaded or unverified models as unavailable for immediate routing.
- Treat model names and versions as registry data, never permanent defaults.
- Do not download or load models, install runtimes, create provider accounts, buy credit, obtain keys, or change provider configuration unless the user separately requests that action.
- AgentFlow may use OpenCode for a plan-selected remote reviewer, but only for `review`/`rereview`. The reviewer must be independently selected, read-only, budget-limited, and unable to edit, use shells, browse the web, invoke skills, or start subagents.
- Apply D0-D3 to the minimal Review Packet. D1 requires plan-specific remote approval; D2 requires both plan-specific approval and a real passed redaction check; D3 never goes remote.
- OpenCode configured/discoverable status is not proof that a model is callable. A real smoke test requires its own plan, displayed hash, explicit approval, and authorization.
- Treat a recorded step-limit or incomplete invocation as a known failed call, not completion or `UNKNOWN`. Stop at its saved boundary and do not resume or retry it speculatively; preserve recorded usage and cost evidence. A local implementation/revision call that hit the step limit may only continue in the same OpenCode session as a new segment while within `implementation_max_continuations`, same model/worktree/file scope, and a valid session id; remote read-only reviewers never continue.
- Important or critical tasks require a review model from a different family. If none is available, stop at `waiting_review`; never represent the task as approved.

## Operating modes

- Managed: the approved plan may run without additional confirmations while it stays within scope.
- Supervised: confirm every implementation, review, revision, and rereview invocation.
- Adaptive: use only the plan's deterministic B/S thresholds and critical-node flags to decide confirmations. Do not silently change the strategy.

## Observation and intervention

Use local reads for observation; status watching must not invoke a model:

```text
agentflow --project <root> status <run-id> --watch
agentflow --project <root> logs <run-id> --follow
agentflow --project <root> cost <run-id>
```

Use safe pause by default. Use immediate freeze only when continuing the active process is riskier than losing its in-flight generation:

```text
agentflow --project <root> pause <run-id>
agentflow --project <root> pause <run-id> --immediate
agentflow --project <root> takeover <run-id>
agentflow --project <root> handoff <run-id>
agentflow --project <root> resolve-call <run-id> <call-id>
agentflow --project <root> resume <run-id>
agentflow --project <root> cancel <run-id>
```

After human edits, let AgentFlow compare worktree content and invalidate only affected tasks and their dependents. An `UNKNOWN` model call must be reconciled before resume and must not be retried speculatively.

## Completion

Report completion only when deterministic tests pass, the independent read-only review returned one protocol-valid JSON object with no P0/P1 findings, the recorded worktree changes stay within allowed files, and AgentFlow reports the task approved. Non-JSON review prose, fenced JSON, or an incomplete call cannot approve a task. Clearly label fake-adapter evidence and never present it as a real model review.
