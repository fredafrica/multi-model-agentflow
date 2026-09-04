"""Terminal interface for the local AgentFlow control plane."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .authorization import issue_authorization, validate_authorization
from .adapters import AdapterRouter
from .config import AgentFlowPaths, resolve_paths
from .contracts import InvocationRequest, InvocationResult, PlanContract, RunMode
from .database import Database
from .fake_adapter import FakeAdapter
from .opencode_adapter import OpenCodeAdapter
from .runner import Runner
from .serialization import canonical_json, load_plan_json, plan_hash
from .states import ControlState, RunState
from .workspace import GitWorkspace


def _plan_path(paths: AgentFlowPaths, value: str | None) -> Path:
    return Path(value).resolve() if value else paths.project_root / ".agentflow" / "plan.json"


def _load_plan(paths: AgentFlowPaths, value: str | None) -> PlanContract:
    path = _plan_path(paths, value)
    return load_plan_json(path.read_text(encoding="utf-8"))


def _open_database(paths: AgentFlowPaths) -> Database:
    database = Database(paths.project_runs / "agentflow.db")
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


def _runner(paths: AgentFlowPaths, database: Database, plan: PlanContract) -> Runner:
    providers = {
        model.provider
        for task in plan.tasks
        for model in (task.implementation_model, task.review_model)
    }
    unsupported = providers - {"fake", "lmstudio"}
    if unsupported:
        raise ValueError(f"MVP has no adapter for providers: {sorted(unsupported)}")
    available = {
        "fake": FakeAdapter(responder=_fake_response),
        "lmstudio": OpenCodeAdapter(),
    }
    adapter = AdapterRouter({provider: available[provider] for provider in providers})
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

    cancel = commands.add_parser("cancel")
    cancel.add_argument("run_id")
    return parser


def run_command(arguments: argparse.Namespace) -> int:
    paths = resolve_paths(arguments.project)
    if arguments.command == "plan" and arguments.plan_command == "show":
        plan = _load_plan(paths, arguments.file)
        print(canonical_json({"plan": plan, "sha256": plan_hash(plan)}))
        return 0

    database = _open_database(paths)
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
            result = _runner(paths, database, plan).start(
                plan, authorization, run_id=arguments.run_id
            )
            print(canonical_json(result))
            return 0 if result.state is RunState.COMPLETED else 2

        if arguments.command == "status":
            while True:
                state = _show_status(database, arguments.run_id)
                if not arguments.watch or _terminal(state):
                    return 0
                time.sleep(1)

        if arguments.command == "pause":
            if arguments.immediate:
                database.force_pause(arguments.run_id)
                local = OpenCodeAdapter()
                for provider_request_id in database.active_provider_requests(arguments.run_id):
                    local.cancel(provider_request_id)
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
            result = _runner(paths, database, plan).resume(
                arguments.run_id, plan, authorization
            )
            print(canonical_json(result))
            return 0 if result.state is RunState.COMPLETED else 2

        if arguments.command == "resolve-call":
            plan = _load_plan(paths, arguments.file)
            service = _runner(paths, database, plan).invocations
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
            print(canonical_json({"run_id": arguments.run_id, "usd": database.remote_cost_spent(arguments.run_id)}))
            return 0

        if arguments.command == "cancel":
            database.set_run_state(arguments.run_id, RunState.CANCELLED)
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
