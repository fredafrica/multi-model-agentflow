"""SQLite persistence with transactional event/state updates."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .contracts import (
    AuthorizationSnapshot,
    InvocationRequest,
    InvocationResult,
    PlanContract,
)
from .schema import DDL, SCHEMA_VERSION
from .serialization import authorization_from_mapping, canonical_json, plan_hash
from .states import (
    ControlState,
    InvocationState,
    RunState,
    TaskState,
    require_transition,
)


class DuplicateInvocationError(RuntimeError):
    pass


class UnknownInvocationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        self.connection.executescript(DDL)
        columns = {
            row["name"]: row
            for row in self.connection.execute("PRAGMA table_info(model_calls)")
        }
        if columns["remote_cost"]["notnull"]:
            with self.transaction() as connection:
                connection.execute(
                    "ALTER TABLE model_calls RENAME COLUMN remote_cost TO remote_cost_legacy"
                )
                connection.execute("ALTER TABLE model_calls ADD COLUMN remote_cost REAL")
                connection.execute(
                    "UPDATE model_calls SET remote_cost = remote_cost_legacy"
                )
                connection.execute(
                    "ALTER TABLE model_calls DROP COLUMN remote_cost_legacy"
                )
            columns = {
                row["name"]: row
                for row in self.connection.execute("PRAGMA table_info(model_calls)")
            }
        additions = {
            "model_family": "TEXT",
            "is_local": "INTEGER NOT NULL DEFAULT 0 CHECK (is_local IN (0, 1))",
            "cost_unavailable": (
                "INTEGER NOT NULL DEFAULT 0 CHECK (cost_unavailable IN (0, 1))"
            ),
            "test_double": "INTEGER NOT NULL DEFAULT 0 CHECK (test_double IN (0, 1))",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE model_calls ADD COLUMN {name} {definition}"
                )
        self.connection.execute(
            "INSERT OR IGNORE INTO schema_meta(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utc_now()),
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _event(
        self,
        connection: sqlite3.Connection,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: Any,
    ) -> None:
        connection.execute(
            """
            INSERT INTO events(
                event_id, aggregate_type, aggregate_id, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(uuid4()), aggregate_type, aggregate_id, event_type, canonical_json(payload), utc_now()),
        )

    def save_plan(self, plan: PlanContract) -> str:
        content_hash = plan_hash(plan)
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO plans(
                    plan_id, version, schema_version, canonical_json, content_hash, run_mode, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.version,
                    plan.schema_version,
                    canonical_json(plan),
                    content_hash,
                    plan.run_mode.value,
                    utc_now(),
                ),
            )
            if cursor.rowcount:
                self._event(connection, "plan", plan.plan_id, "plan.saved", {"hash": content_hash})
            else:
                row = connection.execute(
                    "SELECT content_hash FROM plans WHERE plan_id = ? AND version = ?",
                    (plan.plan_id, plan.version),
                ).fetchone()
                if row["content_hash"] != content_hash:
                    raise ValueError("plan ID/version already exists with different content")
        return content_hash

    def save_authorization(self, authorization: AuthorizationSnapshot) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO authorizations(
                    authorization_id, plan_id, plan_version, plan_hash, snapshot_json,
                    authorized_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    authorization.authorization_id,
                    authorization.plan_id,
                    authorization.plan_version,
                    authorization.plan_hash,
                    canonical_json(authorization),
                    authorization.authorized_at.isoformat(),
                    authorization.expires_at.isoformat(),
                ),
            )
            if cursor.rowcount:
                self._event(
                    connection,
                    "authorization",
                    authorization.authorization_id,
                    "authorization.saved",
                    {"plan_hash": authorization.plan_hash},
                )

    def create_run(self, run_id: str, plan: PlanContract, authorization: AuthorizationSnapshot) -> None:
        with self.transaction() as connection:
            active = connection.execute(
                """
                SELECT run_id FROM runs
                WHERE run_state IN (?, ?) LIMIT 1
                """,
                (RunState.RUNNING.value, RunState.PAUSED.value),
            ).fetchone()
            if active is not None:
                raise ValueError(f"project already has an active run: {active['run_id']}")
            used = connection.execute(
                "SELECT run_id FROM runs WHERE authorization_id = ?",
                (authorization.authorization_id,),
            ).fetchone()
            if used is not None:
                raise ValueError("authorization has already been used by a run")
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, plan_id, plan_version, authorization_id, run_state,
                    control_state, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    plan.plan_id,
                    plan.version,
                    authorization.authorization_id,
                    RunState.RUNNING.value,
                    ControlState.RUNNING.value,
                    utc_now(),
                ),
            )
            for position, task in enumerate(plan.tasks):
                connection.execute(
                    """
                    INSERT INTO tasks(
                        run_id, task_id, position, contract_json, state, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        task.task_id,
                        position,
                        canonical_json(task),
                        TaskState.DRAFT.value,
                        utc_now(),
                    ),
                )
            self._event(connection, "run", run_id, "run.created", {"plan_id": plan.plan_id})

    def set_run_state(self, run_id: str, state: RunState) -> None:
        with self.transaction() as connection:
            finished_at = utc_now() if state in (
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLED,
            ) else None
            cursor = connection.execute(
                "UPDATE runs SET run_state = ?, finished_at = ? WHERE run_id = ?",
                (state.value, finished_at, run_id),
            )
            if not cursor.rowcount:
                raise KeyError(f"unknown run: {run_id}")
            self._event(connection, "run", run_id, "run.state_changed", {"state": state.value})

    def transition_task(self, run_id: str, task_id: str, target: TaskState) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM tasks WHERE run_id = ? AND task_id = ?", (run_id, task_id)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown task: {run_id}/{task_id}")
            current = TaskState(row["state"])
            require_transition(current, target)
            connection.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE run_id = ? AND task_id = ?",
                (target.value, utc_now(), run_id, task_id),
            )
            self._event(
                connection,
                "task",
                f"{run_id}:{task_id}",
                "task.transitioned",
                {"from": current.value, "to": target.value},
            )

    def transition_control(self, run_id: str, target: ControlState) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT control_state FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown run: {run_id}")
            current = ControlState(row["control_state"])
            require_transition(current, target)
            connection.execute(
                "UPDATE runs SET control_state = ? WHERE run_id = ?", (target.value, run_id)
            )
            self._event(
                connection,
                "run",
                run_id,
                "run.control_transitioned",
                {"from": current.value, "to": target.value},
            )

    def force_pause(self, run_id: str) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE runs SET control_state = ?, run_state = ?
                WHERE run_id = ? AND run_state = ?
                """,
                (
                    ControlState.PAUSED.value,
                    RunState.PAUSED.value,
                    run_id,
                    RunState.RUNNING.value,
                ),
            )
            if not cursor.rowcount:
                raise ValueError("only a running run can be frozen")
            self._event(connection, "run", run_id, "run.immediately_frozen", {})

    def save_checkpoint(self, run_id: str, checkpoint: Any) -> None:
        with self.transaction() as connection:
            found = connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if found is None:
                raise KeyError(f"unknown run: {run_id}")
            connection.execute(
                "UPDATE runs SET checkpoint_json = ? WHERE run_id = ?",
                (canonical_json(checkpoint), run_id),
            )
            self._event(connection, "run", run_id, "run.checkpoint_saved", checkpoint)

    def set_task_worktree(self, run_id: str, task_id: str, worktree_path: str) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET worktree_path = ?, updated_at = ? WHERE run_id = ? AND task_id = ?",
                (worktree_path, utc_now(), run_id, task_id),
            )
            if not cursor.rowcount:
                raise KeyError(f"unknown task: {run_id}/{task_id}")
            self._event(
                connection,
                "task",
                f"{run_id}:{task_id}",
                "task.worktree_created",
                {"worktree_path": worktree_path},
            )

    def set_task_baseline(self, run_id: str, task_id: str, baseline: str) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET file_baseline_json = ?, updated_at = ? WHERE run_id = ? AND task_id = ?",
                (canonical_json({"snapshot": baseline}), utc_now(), run_id, task_id),
            )
            if not cursor.rowcount:
                raise KeyError(f"unknown task: {run_id}/{task_id}")
            self._event(
                connection,
                "task",
                f"{run_id}:{task_id}",
                "task.baseline_saved",
                {},
            )

    def invalidate_approved_task(
        self, run_id: str, task_id: str, target: TaskState, reason: str
    ) -> None:
        if target not in (TaskState.SELF_TESTING, TaskState.FAILED):
            raise ValueError("takeover invalidation target must be self_testing or failed")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM tasks WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown task: {run_id}/{task_id}")
            if TaskState(row["state"]) is not TaskState.APPROVED:
                raise ValueError("only approved tasks can be invalidated after takeover")
            connection.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE run_id = ? AND task_id = ?",
                (target.value, utc_now(), run_id, task_id),
            )
            self._event(
                connection,
                "task",
                f"{run_id}:{task_id}",
                "task.invalidated_after_takeover",
                {"target": target.value, "reason": reason},
            )

    def create_attempt(self, attempt_id: str, run_id: str, task_id: str, number: int) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO attempts(attempt_id, run_id, task_id, attempt_number, started_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (attempt_id, run_id, task_id, number, utc_now()),
            )
            self._event(connection, "attempt", attempt_id, "attempt.started", {"number": number})

    def latest_attempt(self, run_id: str, task_id: str) -> sqlite3.Row | None:
        return self.fetch_one(
            """
            SELECT * FROM attempts WHERE run_id = ? AND task_id = ?
            ORDER BY attempt_number DESC LIMIT 1
            """,
            (run_id, task_id),
        )

    def complete_attempt(self, attempt_id: str, outcome: str) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE attempts SET finished_at = ?, outcome = ? WHERE attempt_id = ?",
                (utc_now(), outcome, attempt_id),
            )
            if not cursor.rowcount:
                raise KeyError(f"unknown attempt: {attempt_id}")
            self._event(connection, "attempt", attempt_id, "attempt.completed", {"outcome": outcome})

    def record_test(
        self,
        result_id: str,
        run_id: str,
        task_id: str,
        attempt_id: str,
        *,
        source: str,
        passed: bool,
        duration_ms: int,
        evidence: Any,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO test_results(
                    test_result_id, run_id, task_id, attempt_id, source, passed,
                    duration_ms, evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result_id,
                    run_id,
                    task_id,
                    attempt_id,
                    source,
                    int(passed),
                    duration_ms,
                    canonical_json(evidence),
                    utc_now(),
                ),
            )
            self._event(
                connection,
                "test",
                result_id,
                "test.recorded",
                {"passed": passed, "source": source},
            )

    def record_policy_denial(
        self,
        run_id: str,
        call_id: str,
        task_id: str,
        model_key: str,
        reasons: tuple[str, ...],
    ) -> None:
        with self.transaction() as connection:
            self._event(
                connection,
                "run",
                run_id,
                "call.denied",
                {
                    "call_id": call_id,
                    "task_id": task_id,
                    "model_key": model_key,
                    "reasons": reasons,
                },
            )

    def latest_test(self, run_id: str, task_id: str) -> sqlite3.Row | None:
        return self.fetch_one(
            """
            SELECT passed, duration_ms, evidence_json FROM test_results
            WHERE run_id = ? AND task_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (run_id, task_id),
        )

    def record_model_fallback(
        self,
        run_id: str,
        task_id: str,
        role: str,
        requested_model: str,
        fallback_model: str,
    ) -> None:
        with self.transaction() as connection:
            self._event(
                connection,
                "run",
                run_id,
                "model.fallback_selected",
                {
                    "task_id": task_id,
                    "role": role,
                    "requested_model": requested_model,
                    "fallback_model": fallback_model,
                },
            )

    def record_review(
        self,
        review_id: str,
        run_id: str,
        task_id: str,
        attempt_id: str,
        *,
        provider: str,
        model_id: str,
        model_version: str,
        approved: bool,
        findings: Any,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO reviews(
                    review_id, run_id, task_id, attempt_id, provider, model_id,
                    model_version, approved, findings_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    run_id,
                    task_id,
                    attempt_id,
                    provider,
                    model_id,
                    model_version,
                    int(approved),
                    canonical_json(findings),
                    utc_now(),
                ),
            )
            self._event(
                connection,
                "review",
                review_id,
                "review.recorded",
                {"approved": approved},
            )

    def register_call(self, request: InvocationRequest, attempt_id: str) -> tuple[sqlite3.Row, bool]:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM model_calls WHERE request_key = ?", (request.request_key,)
            ).fetchone()
            if existing is not None:
                state = InvocationState(existing["state"])
                if state is InvocationState.COMPLETED:
                    return existing, False
                if state is InvocationState.UNKNOWN:
                    raise UnknownInvocationError("call outcome is unknown; reconcile before retrying")
                raise DuplicateInvocationError(f"call already exists in state {state.value}")
            connection.execute(
                """
                INSERT INTO model_calls(
                    call_id, request_key, attempt_id, provider, model_id, model_version,
                    model_family, is_local, role, state, data_sensitivity, read_only,
                    request_scope_json, remote_cost, cost_unavailable, test_double,
                    input_tokens, output_tokens, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, 0, 0, 0)
                """,
                (
                    request.call_id,
                    request.request_key,
                    attempt_id,
                    request.model.provider,
                    request.model.model_id,
                    request.model.version,
                    request.model.family,
                    int(request.model.is_local),
                    request.role,
                    InvocationState.PLANNED.value,
                    request.data_sensitivity.value,
                    int(request.read_only),
                    canonical_json(
                        {
                            "allowed_files": request.metadata.get("allowed_files", ()),
                            "test_double": bool(request.metadata.get("test_double", False)),
                            "packet_hash": request.metadata.get("packet_hash"),
                            "packet_size": request.metadata.get("packet_size"),
                            "privacy_policy_version": request.metadata.get(
                                "privacy_policy_version"
                            ),
                            "estimated_remote_cost": request.metadata.get(
                                "estimated_remote_cost", 0
                            ),
                        }
                    ),
                    int(bool(request.metadata.get("test_double", False))),
                ),
            )
            self._event(
                connection,
                "call",
                request.call_id,
                "call.planned",
                {"request_key": request.request_key},
            )
            created = connection.execute(
                "SELECT * FROM model_calls WHERE call_id = ?", (request.call_id,)
            ).fetchone()
            return created, True

    def transition_call(self, call_id: str, target: InvocationState) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM model_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown call: {call_id}")
            current = InvocationState(row["state"])
            require_transition(current, target)
            updates = "state = ?"
            values: list[Any] = [target.value]
            if target is InvocationState.STARTED:
                updates += ", started_at = ?"
                values.append(utc_now())
            if target in (
                InvocationState.FAILED,
                InvocationState.CANCELLED,
                InvocationState.UNKNOWN,
            ):
                finished_at = utc_now()
                updates += (
                    ", finished_at = ?, duration_ms = CASE "
                    "WHEN started_at IS NULL THEN 0 "
                    "ELSE MAX(0, CAST((julianday(?) - julianday(started_at)) "
                    "* 86400000 AS INTEGER)) END"
                )
                values.extend((finished_at, finished_at))
            values.append(call_id)
            connection.execute(f"UPDATE model_calls SET {updates} WHERE call_id = ?", values)
            self._event(
                connection,
                "call",
                call_id,
                "call.transitioned",
                {"from": current.value, "to": target.value},
            )

    def set_provider_request_id(self, call_id: str, provider_request_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE model_calls SET provider_request_id = ? WHERE call_id = ?",
                (provider_request_id, call_id),
            )
            self._event(
                connection,
                "call",
                call_id,
                "call.provider_request_recorded",
                {"provider_request_id": provider_request_id},
            )

    def complete_call(self, call_id: str, result: InvocationResult, run_id: str) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM model_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown call: {call_id}")
            current = InvocationState(row["state"])
            require_transition(current, InvocationState.COMPLETED)
            connection.execute(
                """
                UPDATE model_calls SET
                    state = ?, provider_request_id = COALESCE(?, provider_request_id),
                    input_tokens = ?, output_tokens = ?,
                    first_token_latency_ms = ?, duration_ms = ?, remote_cost = ?,
                    cost_unavailable = ?, output_text = ?, raw_metadata_json = ?, finished_at = ?
                WHERE call_id = ?
                """,
                (
                    InvocationState.COMPLETED.value,
                    result.provider_request_id,
                    result.input_tokens,
                    result.output_tokens,
                    result.first_token_latency_ms,
                    result.duration_ms,
                    result.remote_cost,
                    int(result.cost_unavailable),
                    result.output,
                    canonical_json(result.raw_metadata),
                    utc_now(),
                    call_id,
                ),
            )
            if result.remote_cost is not None:
                connection.execute(
                    """
                    INSERT INTO cost_entries(
                        cost_entry_id, run_id, call_id, amount, currency, recorded_at
                    ) VALUES (?, ?, ?, ?, 'USD', ?)
                    """,
                    (str(uuid4()), run_id, call_id, result.remote_cost, utc_now()),
                )
            self._event(
                connection,
                "call",
                call_id,
                "call.completed",
                {
                    "remote_cost": result.remote_cost,
                    "cost_unavailable": result.cost_unavailable,
                },
            )

    def fail_call(
        self,
        call_id: str,
        result: InvocationResult,
        run_id: str,
        *,
        failure_kind: str,
    ) -> None:
        """Persist a known failed result without discarding provider usage evidence."""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM model_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown call: {call_id}")
            current = InvocationState(row["state"])
            require_transition(current, InvocationState.FAILED)
            metadata = dict(result.raw_metadata)
            metadata["failure_kind"] = failure_kind
            connection.execute(
                """
                UPDATE model_calls SET
                    state = ?, provider_request_id = COALESCE(?, provider_request_id),
                    input_tokens = ?, output_tokens = ?,
                    first_token_latency_ms = ?, duration_ms = ?, remote_cost = ?,
                    cost_unavailable = ?, output_text = ?, raw_metadata_json = ?, finished_at = ?
                WHERE call_id = ?
                """,
                (
                    InvocationState.FAILED.value,
                    result.provider_request_id,
                    result.input_tokens,
                    result.output_tokens,
                    result.first_token_latency_ms,
                    result.duration_ms,
                    result.remote_cost,
                    int(result.cost_unavailable),
                    result.output,
                    canonical_json(metadata),
                    utc_now(),
                    call_id,
                ),
            )
            if result.remote_cost is not None:
                connection.execute(
                    """
                    INSERT INTO cost_entries(
                        cost_entry_id, run_id, call_id, amount, currency, recorded_at
                    ) VALUES (?, ?, ?, ?, 'USD', ?)
                    """,
                    (str(uuid4()), run_id, call_id, result.remote_cost, utc_now()),
                )
            self._event(
                connection,
                "call",
                call_id,
                "call.failed",
                {
                    "failure_kind": failure_kind,
                    "remote_cost": result.remote_cost,
                    "cost_unavailable": result.cost_unavailable,
                },
            )

    def fetch_one(self, query: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.connection.execute(query, parameters).fetchone()

    def remote_cost_spent(self, run_id: str) -> float:
        row = self.fetch_one(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM cost_entries WHERE run_id = ?",
            (run_id,),
        )
        return float(row["total"])

    def remote_budget_committed(self, run_id: str) -> float:
        rows = self.connection.execute(
            """
            SELECT model_calls.request_scope_json
            FROM model_calls
            JOIN attempts USING (attempt_id)
            WHERE attempts.run_id = ? AND model_calls.is_local = 0
              AND model_calls.state IN (?, ?) AND model_calls.cost_unavailable = 1
            """,
            (
                run_id,
                InvocationState.COMPLETED.value,
                InvocationState.FAILED.value,
            ),
        ).fetchall()
        reserved = sum(
            float(
                json.loads(row["request_scope_json"]).get(
                    "estimated_remote_cost", 0
                )
            )
            for row in rows
        )
        return self.remote_cost_spent(run_id) + reserved

    def cost_summary(self, run_id: str) -> dict[str, Any]:
        rows = self.connection.execute(
            """
            SELECT model_calls.provider, model_calls.model_id, model_calls.model_version,
                   COUNT(*) AS calls,
                   COALESCE(SUM(model_calls.input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(model_calls.output_tokens), 0) AS output_tokens,
                   SUM(CASE WHEN model_calls.cost_unavailable = 1 THEN 1 ELSE 0 END)
                       AS cost_unavailable_calls,
                   COALESCE(SUM(cost_entries.amount), 0) AS confirmed_cost
            FROM model_calls
            JOIN attempts USING (attempt_id)
            LEFT JOIN cost_entries USING (call_id)
            WHERE attempts.run_id = ? AND model_calls.is_local = 0
            GROUP BY model_calls.provider, model_calls.model_id, model_calls.model_version
            ORDER BY model_calls.provider, model_calls.model_id, model_calls.model_version
            """,
            (run_id,),
        ).fetchall()
        providers = [dict(row) for row in rows]
        return {
            "run_id": run_id,
            "confirmed_remote_cost_usd": self.remote_cost_spent(run_id),
            "cost_unavailable_calls": sum(
                int(row["cost_unavailable_calls"]) for row in rows
            ),
            "by_provider_model": providers,
        }

    def unresolved_unknown_calls(self, run_id: str) -> int:
        row = self.fetch_one(
            """
            SELECT COUNT(*) AS count FROM model_calls
            JOIN attempts USING (attempt_id)
            WHERE attempts.run_id = ? AND model_calls.state = ?
            """,
            (run_id, InvocationState.UNKNOWN.value),
        )
        return int(row["count"])

    def known_incomplete_calls(self, run_id: str) -> int:
        rows = self.connection.execute(
            """
            SELECT model_calls.raw_metadata_json FROM model_calls
            JOIN attempts USING (attempt_id)
            WHERE attempts.run_id = ? AND model_calls.state = ?
            """,
            (run_id, InvocationState.FAILED.value),
        ).fetchall()
        return sum(
            1
            for row in rows
            if row["raw_metadata_json"]
            and json.loads(row["raw_metadata_json"]).get("failure_kind")
            == "step_limit_reached"
        )

    def inflight_calls(self, run_id: str) -> int:
        row = self.fetch_one(
            """
            SELECT COUNT(*) AS count FROM model_calls
            JOIN attempts USING (attempt_id)
            WHERE attempts.run_id = ? AND model_calls.state = ?
            """,
            (run_id, InvocationState.STARTED.value),
        )
        return int(row["count"])

    def unknown_call(self, run_id: str, call_id: str) -> sqlite3.Row:
        row = self.fetch_one(
            """
            SELECT model_calls.* FROM model_calls
            JOIN attempts USING (attempt_id)
            WHERE attempts.run_id = ? AND model_calls.call_id = ?
            """,
            (run_id, call_id),
        )
        if row is None:
            raise KeyError(f"unknown call: {call_id}")
        if InvocationState(row["state"]) is not InvocationState.UNKNOWN:
            raise ValueError("only an UNKNOWN call can be resolved")
        return row

    def active_provider_requests(self, run_id: str) -> tuple[str, ...]:
        rows = self.connection.execute(
            """
            SELECT provider_request_id FROM model_calls
            JOIN attempts USING (attempt_id)
            WHERE attempts.run_id = ? AND model_calls.state = ?
              AND provider_request_id IS NOT NULL
            """,
            (run_id, InvocationState.STARTED.value),
        ).fetchall()
        return tuple(str(row["provider_request_id"]) for row in rows)

    def mark_active_calls_unknown(self, run_id: str) -> None:
        rows = self.connection.execute(
            """
            SELECT model_calls.call_id FROM model_calls
            JOIN attempts USING (attempt_id)
            WHERE attempts.run_id = ? AND model_calls.state = ?
            """,
            (run_id, InvocationState.STARTED.value),
        ).fetchall()
        for row in rows:
            self.transition_call(str(row["call_id"]), InvocationState.UNKNOWN)

    def latest_authorization(self, plan_id: str, plan_version: int) -> AuthorizationSnapshot | None:
        row = self.fetch_one(
            """
            SELECT snapshot_json FROM authorizations
            WHERE plan_id = ? AND plan_version = ? AND revoked_at IS NULL
            ORDER BY authorized_at DESC LIMIT 1
            """,
            (plan_id, plan_version),
        )
        if row is None:
            return None
        return authorization_from_mapping(json.loads(row["snapshot_json"]))

    def run_snapshot(self, run_id: str) -> dict[str, Any]:
        run = self.fetch_one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        tasks = self.connection.execute(
            "SELECT task_id, position, state, worktree_path FROM tasks WHERE run_id = ? ORDER BY position",
            (run_id,),
        ).fetchall()
        return {"run": dict(run), "tasks": [dict(row) for row in tasks]}

    def handoff_summary(self, run_id: str) -> dict[str, Any]:
        snapshot = self.run_snapshot(run_id)
        identity = self.fetch_one(
            """
            SELECT plans.content_hash AS plan_hash, authorizations.expires_at
            FROM runs
            JOIN plans ON plans.plan_id = runs.plan_id
              AND plans.version = runs.plan_version
            JOIN authorizations USING (authorization_id)
            WHERE runs.run_id = ?
            """,
            (run_id,),
        )
        return {
            "run_id": run_id,
            "run_state": snapshot["run"]["run_state"],
            "control_state": snapshot["run"]["control_state"],
            "plan_hash": identity["plan_hash"],
            "authorization_expires_at": identity["expires_at"],
            "checkpoint": json.loads(snapshot["run"]["checkpoint_json"])
            if snapshot["run"]["checkpoint_json"]
            else None,
            "remote_cost_usd": self.remote_cost_spent(run_id),
            "unknown_calls": self.unresolved_unknown_calls(run_id),
            "tasks": snapshot["tasks"],
        }

    def task_state(self, run_id: str, task_id: str) -> TaskState:
        row = self.fetch_one(
            "SELECT state FROM tasks WHERE run_id = ? AND task_id = ?", (run_id, task_id)
        )
        if row is None:
            raise KeyError(f"unknown task: {run_id}/{task_id}")
        return TaskState(row["state"])

    def event_rows(self, run_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT sequence, event_type, aggregate_type, aggregate_id, payload_json, created_at
            FROM events
            WHERE sequence > ? AND (
                aggregate_id = ? OR aggregate_id LIKE ? OR
                aggregate_id IN (
                    SELECT authorization_id FROM runs WHERE run_id = ?
                ) OR aggregate_id IN (
                    SELECT attempt_id FROM attempts WHERE run_id = ?
                ) OR aggregate_id IN (
                    SELECT model_calls.call_id FROM model_calls
                    JOIN attempts USING (attempt_id) WHERE attempts.run_id = ?
                ) OR aggregate_id IN (
                    SELECT review_id FROM reviews WHERE run_id = ?
                ) OR aggregate_id IN (
                    SELECT test_result_id FROM test_results WHERE run_id = ?
                )
            )
            ORDER BY sequence
            """,
            (
                after_sequence,
                run_id,
                f"{run_id}:%",
                run_id,
                run_id,
                run_id,
                run_id,
                run_id,
            ),
        ).fetchall()
        return [dict(row) for row in rows]
