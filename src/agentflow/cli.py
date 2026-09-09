"""Terminal interface for the local AgentFlow control plane."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .adapters import AdapterRouter, ModelAdapter, UnsupportedProviderError
from .authorization import issue_authorization, validate_authorization
from .config import AgentFlowPaths, resolve_paths
from .contracts import (
    AuthorizationSnapshot,
    InvocationRequest,
    InvocationResult,
    ModelRef,
    PlanContract,
)
from .database import Database
from .fake_adapter import FakeAdapter
from .opencode_adapter import (
    LocalOllamaReviewerAdapter,
    OpenCodeAdapter,
    RemoteOpenCodeReviewerAdapter,
    RemoteOpenCodeWorkerAdapter,
)
from .runner import Runner
from .serialization import canonical_json, load_plan_json, plan_hash
from .resource_budgets import plan_budget_summary
from .states import ControlState, RunState
from .workspace import GitWorkspace, GitWorkspaceError

_MAX_SUPERVISOR_WAIT_SECONDS = 60
_SUPERVISOR_POLL_SECONDS = 0.5


def _plan_path(paths: AgentFlowPaths, value: str | None) -> Path:
    return Path(value).resolve() if value else paths.project_root / ".agentflow" / "plan.json"


def _load_plan(paths: AgentFlowPaths, value: str | None) -> PlanContract:
    path = _plan_path(paths, value)
    return load_plan_json(path.read_text(encoding="utf-8"))


def _open_database(paths: AgentFlowPaths, *, readonly: bool = False) -> Database:
    path = paths.project_runs / "agentflow.db"
    if readonly:
        return Database.open_readonly(path)
    database = Database(path)
    database.initialize()
    return database


def _fake_response(request: InvocationRequest) -> InvocationResult:
    output = (
        json.dumps({"approved": True, "findings": []})
        if request.role in ("review", "rereview")
        else "fake implementation completed without file writes"
    )
    return InvocationResult(
        provider_request_id=f"fake:{request.request_key}",
        output=output,
        input_tokens=0,
        output_tokens=0,
        first_token_latency_ms=0,
        duration_ms=0,
        remote_cost=0,
        raw_metadata={"test_double": True},
    )


def _confirmation(request: InvocationRequest) -> bool:
    try:
        answer = input(
            f"Confirm {request.role} model {request.model.registry_key} "
            f"for task {request.task_id}? [y/N] "
        )
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _runner(
    paths: AgentFlowPaths,
    database: Database,
    plan: PlanContract,
    authorization: AuthorizationSnapshot,
) -> Runner:
    models = tuple(
        model
        for task in plan.tasks
        for model in (
            task.implementation_model,
            task.review_model,
            task.fallback_model,
        )
        if model is not None
    )
    providers = {model.provider for model in models}
    unauthorized = providers - set(authorization.authorized_provider_ids)
    if unauthorized:
        raise ValueError(
            f"provider is outside the authorization: {sorted(unauthorized)}"
        )
    worker_providers: set[str] = set()
    reviewer_providers: set[str] = set()
    for task in plan.tasks:
        implementation = task.implementation_model
        if implementation.provider == "ollama":
            raise UnsupportedProviderError(
                "local Ollama models are review-only and cannot be the "
                "implementation model"
            )
        if not implementation.is_local and implementation.provider != "fake":
            if not task.allow_remote_implementation:
                raise ValueError(
                    "remote role denied: remote implementation is not authorized "
                    "for this task"
                )
            worker_providers.add(implementation.provider)
        for model in (task.review_model, task.fallback_model):
            if model is not None and not model.is_local and model.provider != "fake":
                reviewer_providers.add(model.provider)

    worker_roles = {"implementation", "revision"}
    reviewer_roles = {"review", "rereview"}
    worker_models: dict[str, list[ModelRef]] = {}
    reviewer_models: dict[str, list[ModelRef]] = {}

    def _add(model: ModelRef | None, roles: set[str]) -> None:
        if model is None:
            return
        for role in roles:
            bucket = worker_models if role in worker_roles else reviewer_models
            bucket.setdefault(model.provider, []).append(model)

    for task in plan.tasks:
        _add(task.implementation_model, {"implementation", "revision"})
        _add(task.review_model, {"review", "rereview"})
        _add(task.fallback_model, worker_roles | reviewer_roles)

    def _dedupe(models: list[ModelRef]) -> tuple[ModelRef, ...]:
        seen: set[str] = set()
        result: list[ModelRef] = []
        for model in models:
            if model.registry_key not in seen:
                seen.add(model.registry_key)
                result.append(model)
        return tuple(result)

    adapters: dict[str, ModelAdapter] = {}
    role_adapters: dict[tuple[str, str], ModelAdapter] = {}
    for provider in providers:
        if provider == "fake":
            adapters[provider] = FakeAdapter(responder=_fake_response)
            continue
        if provider == "lmstudio":
            adapters[provider] = OpenCodeAdapter()
            continue
        if provider == "ollama":
            ollama_reviewer_planned = _dedupe(reviewer_models.get(provider, []))
            if not ollama_reviewer_planned:
                raise UnsupportedProviderError(
                    "unsupported provider: ollama has no planned review model"
                )
            for model in ollama_reviewer_planned:
                if not model.is_local or model.provider != "ollama":
                    raise UnsupportedProviderError(
                        "planned Ollama review models must be local ollama models"
                    )
            # Deliberately not added to the default ``adapters`` map: only the
            # review/rereview roles resolve, so implementation/revision never route here.
            local_ollama_reviewer = LocalOllamaReviewerAdapter(
                planned_models=ollama_reviewer_planned
            )
            for model in ollama_reviewer_planned:
                local_ollama_reviewer.require_model(model)
            for role in reviewer_roles:
                role_adapters[(provider, role)] = local_ollama_reviewer
            continue
        provider_models = tuple(model for model in models if model.provider == provider)
        if any(model.is_local for model in provider_models):
            raise UnsupportedProviderError(
                f"unsupported provider: local provider {provider} has no adapter"
            )
        worker_planned = _dedupe(worker_models.get(provider, []))
        reviewer_planned = _dedupe(reviewer_models.get(provider, []))
        if worker_planned:
            remote_worker = RemoteOpenCodeWorkerAdapter(
                provider, planned_models=worker_planned
            )
            for model in worker_planned:
                remote_worker.require_model(model)
            for role in worker_roles:
                role_adapters[(provider, role)] = remote_worker
        if reviewer_planned:
            remote_reviewer = RemoteOpenCodeReviewerAdapter(
                provider, planned_models=reviewer_planned
            )
            for model in reviewer_planned:
                remote_reviewer.require_model(model)
            for role in reviewer_roles:
                role_adapters[(provider, role)] = remote_reviewer
        if not worker_planned and not reviewer_planned:
            raise UnsupportedProviderError(
                f"unsupported provider: {provider} has no planned model"
            )
    adapter = AdapterRouter(adapters, role_adapters=role_adapters)
    return Runner(
        database,
        adapter,
        GitWorkspace(paths.project_root, paths.project_runs),
        confirmation_callback=_confirmation,
    )


def _show_status(database: Database, run_id: str) -> RunState:
    snapshot = database.run_snapshot(run_id)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return RunState(snapshot["run"]["run_state"])


def _status_fingerprint(database: Database, run_id: str) -> tuple[object, ...]:
    snapshot = database.run_snapshot(run_id)
    return (
        snapshot["run"]["run_state"],
        snapshot["run"]["control_state"],
        tuple((task["task_id"], task["state"]) for task in snapshot["tasks"]),
        database.latest_event_sequence(run_id),
    )


def _changed_files_by_task(
    paths: AgentFlowPaths, database: Database, run_id: str
) -> dict[str, list[str]]:
    workspace = GitWorkspace(paths.project_root, paths.project_runs)
    snapshot = database.run_snapshot(run_id)
    result: dict[str, list[str]] = {}
    for task in snapshot["tasks"]:
        if not task["worktree_path"]:
            continue
        try:
            result[task["task_id"]] = list(workspace.changed_files(task["worktree_path"]))
        except GitWorkspaceError:
            result[task["task_id"]] = []
    return result


def _terminal(state: RunState) -> bool:
    return state in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentflow")
    parser.add_argument("--project", default=".", help="project root (default: current directory)")
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan")
    plan_commands = plan.add_subparsers(dest="plan_command", required=True)
    plan_show = plan_commands.add_parser("show")
    plan_show.add_argument("--file")
    plan_authorize = plan_commands.add_parser("authorize")
    plan_authorize.add_argument("--file")
    plan_authorize.add_argument("--hash", required=True, dest="approved_hash")
    plan_authorize.add_argument("--allow-d1-remote", action="store_true")
    plan_authorize.add_argument("--allow-d2-remote", action="store_true")

    start = commands.add_parser("start")
    start.add_argument("plan_id")
    start.add_argument("--file")
    start.add_argument("--run-id")

    status = commands.add_parser("status")
    status.add_argument("run_id")
    status.add_argument("--watch", action="store_true")

    pause = commands.add_parser("pause")
    pause.add_argument("run_id")
    pause.add_argument("--immediate", action="store_true")

    takeover = commands.add_parser("takeover")
    takeover.add_argument("run_id")

    handoff = commands.add_parser("handoff")
    handoff.add_argument("run_id")

    resume = commands.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--file")

    resolve = commands.add_parser("resolve-call")
    resolve.add_argument("run_id")
    resolve.add_argument("call_id")
    resolve.add_argument("--file")

    logs = commands.add_parser("logs")
    logs.add_argument("run_id")
    logs.add_argument("--follow", action="store_true")

    cost = commands.add_parser("cost")
    cost.add_argument("run_id")

    supervisor_next = commands.add_parser("supervisor-next")
    supervisor_next.add_argument("run_id")
    supervisor_next.add_argument("--after-sequence", type=int, default=0)
    supervisor_next.add_argument("--wait-seconds", type=int, default=0)

    supervisor_record = commands.add_parser("supervisor-record")
    supervisor_record.add_argument("run_id")
    supervisor_record.add_argument("checkpoint_id")
    supervisor_record.add_argument("--decision", default=None)

    cancel = commands.add_parser("cancel")
    cancel.add_argument("run_id")
    return parser


def run_command(arguments: argparse.Namespace) -> int:
    paths = resolve_paths(arguments.project)
    if arguments.command == "plan" and arguments.plan_command == "show":
        plan = _load_plan(paths, arguments.file)
        print(canonical_json({"plan": plan, "sha256": plan_hash(plan),
                              "resource_budgets": plan_budget_summary(plan)}))
        return 0

    _READ_ONLY_COMMANDS = {"status", "logs", "cost", "supervisor-next", "handoff"}
    database = _open_database(
        paths, readonly=arguments.command in _READ_ONLY_COMMANDS
    )
    try:
        if arguments.command == "plan" and arguments.plan_command == "authorize":
            plan = _load_plan(paths, arguments.file)
            actual_hash = plan_hash(plan)
            if arguments.approved_hash != actual_hash:
                raise ValueError(f"approved hash does not match plan: {actual_hash}")
            database.save_plan(plan)
            authorization = issue_authorization(
                plan,
                allow_d1_remote=arguments.allow_d1_remote,
                allow_d2_remote=arguments.allow_d2_remote,
            )
            database.save_authorization(authorization)
            print(canonical_json(authorization))
            return 0

        if arguments.command == "start":
            plan = _load_plan(paths, arguments.file)
            if plan.plan_id != arguments.plan_id:
                raise ValueError("plan ID does not match the plan file")
            authorization = database.latest_authorization(plan.plan_id, plan.version)
            if authorization is None:
                raise ValueError("plan has no authorization; run `agentflow plan authorize` first")
            validate_authorization(authorization, plan)
            result = _runner(paths, database, plan, authorization).start(
                plan, authorization, run_id=arguments.run_id
            )
            print(canonical_json(result))
            return 0 if result.state is RunState.COMPLETED else 2

        if arguments.command == "status":
            last_fingerprint: object | None = None
            while True:
                fingerprint = _status_fingerprint(database, arguments.run_id)
                if fingerprint != last_fingerprint:
                    _show_status(database, arguments.run_id)
                    last_fingerprint = fingerprint
                snapshot = database.run_snapshot(arguments.run_id)
                state = RunState(snapshot["run"]["run_state"])
                if not arguments.watch or _terminal(state):
                    return 0
                time.sleep(1)

        if arguments.command == "pause":
            if arguments.immediate:
                database.force_pause(arguments.run_id)
                snapshot = database.run_snapshot(arguments.run_id)["run"]
                plan = database.load_plan(snapshot["plan_id"], snapshot["plan_version"])
                policy = plan.supervisor_policy
                database.record_supervisor_checkpoint(
                    arguments.run_id,
                    "run_paused",
                    {"run_id": arguments.run_id, "reason": "immediate_freeze"},
                    max_chars=policy.max_checkpoint_chars,
                    max_checkpoints=policy.max_supervisor_checkpoints,
                    plan_hash_value=plan_hash(plan),
                    idempotent=True,
                )
                local = OpenCodeAdapter()
                for provider_request_id in database.active_provider_requests(arguments.run_id):
                    local.cancel(provider_request_id)
                database.mark_active_calls_unknown(arguments.run_id)
            else:
                database.transition_control(arguments.run_id, ControlState.PAUSE_REQUESTED)
            print(canonical_json(database.run_snapshot(arguments.run_id)))
            return 0

        if arguments.command == "takeover":
            database.transition_control(arguments.run_id, ControlState.USER_TAKEOVER)
            print(canonical_json(database.handoff_summary(arguments.run_id)))
            return 0

        if arguments.command == "handoff":
            print(canonical_json(database.handoff_summary(arguments.run_id)))
            return 0

        if arguments.command == "resume":
            plan = _load_plan(paths, arguments.file)
            authorization = database.latest_authorization(plan.plan_id, plan.version)
            if authorization is None:
                raise ValueError("plan authorization is missing")
            result = _runner(paths, database, plan, authorization).resume(
                arguments.run_id, plan, authorization
            )
            print(canonical_json(result))
            return 0 if result.state is RunState.COMPLETED else 2

        if arguments.command == "resolve-call":
            plan = _load_plan(paths, arguments.file)
            authorization = database.latest_authorization(plan.plan_id, plan.version)
            if authorization is None:
                raise ValueError("plan authorization is missing")
            service = _runner(paths, database, plan, authorization).invocations
            result = service.resolve_unknown(arguments.run_id, arguments.call_id)
            print(
                canonical_json(
                    {
                        "call_id": arguments.call_id,
                        "resolved": result is not None,
                        "state": "completed" if result is not None else "unknown",
                    }
                )
            )
            return 0 if result is not None else 2

        if arguments.command == "logs":
            after = 0
            while True:
                rows = database.event_rows(arguments.run_id, after)
                for row in rows:
                    print(json.dumps(row, ensure_ascii=False))
                    after = int(row["sequence"])
                state = RunState(database.run_snapshot(arguments.run_id)["run"]["run_state"])
                if not arguments.follow or _terminal(state):
                    return 0
                time.sleep(1)

        if arguments.command == "cost":
            summary = database.cost_summary(arguments.run_id)
            summary["usd"] = summary["confirmed_remote_cost_usd"]
            print(canonical_json(summary))
            return 0

        if arguments.command == "supervisor-next":
            after_sequence = arguments.after_sequence
            wait_seconds = max(0, min(arguments.wait_seconds, _MAX_SUPERVISOR_WAIT_SECONDS))
            deadline = time.monotonic() + wait_seconds
            while True:
                cursor = database.latest_event_sequence(arguments.run_id)
                if database.pending_supervisor_checkpoints(arguments.run_id):
                    digest = database.supervisor_digest(
                        arguments.run_id,
                        after_sequence=after_sequence,
                        changed_files_by_task=_changed_files_by_task(
                            paths, database, arguments.run_id
                        ),
                    )
                    print(canonical_json(digest))
                    return 0
                if time.monotonic() >= deadline:
                    print(
                        canonical_json(
                            {
                                "changed": False,
                                "wake_required": False,
                                "cursor": cursor,
                            }
                        )
                    )
                    return 0
                time.sleep(_SUPERVISOR_POLL_SECONDS)

        if arguments.command == "supervisor-record":
            decision = json.loads(arguments.decision) if arguments.decision else {}
            database.acknowledge_supervisor_checkpoint(
                arguments.run_id, arguments.checkpoint_id, decision
            )
            print(
                canonical_json(
                    {
                        "checkpoint_id": arguments.checkpoint_id,
                        "status": "acknowledged",
                    }
                )
            )
            return 0

        if arguments.command == "cancel":
            run = database.run_snapshot(arguments.run_id)["run"]
            current = RunState(run["run_state"])
            if current is RunState.CANCELLED:
                print(canonical_json(database.run_snapshot(arguments.run_id)))
                return 0
            if current in (RunState.COMPLETED, RunState.FAILED):
                raise RuntimeError(f"run is already {current.value}; it cannot be cancelled")
            plan = database.load_plan(run["plan_id"], run["plan_version"])
            database.finalize_run(
                arguments.run_id,
                RunState.CANCELLED,
                "run_cancelled",
                {"run_id": arguments.run_id, "run_state": "cancelled"},
                max_chars=plan.supervisor_policy.max_checkpoint_chars,
                max_checkpoints=plan.supervisor_policy.max_supervisor_checkpoints,
                reasoning_effort=plan.supervisor_policy.default_reasoning_effort.value,
                plan_hash_value=plan_hash(plan),
            )
            local = OpenCodeAdapter()
            for provider_request_id in database.active_provider_requests(arguments.run_id):
                local.cancel(provider_request_id)
            database.mark_active_calls_unknown(arguments.run_id)
            print(canonical_json(database.run_snapshot(arguments.run_id)))
            return 0
    finally:
        database.close()
    raise ValueError(f"unsupported command: {arguments.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        return run_command(parser.parse_args(argv))
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"agentflow: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
