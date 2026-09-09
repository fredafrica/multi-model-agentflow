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
    SUPERVISOR_CHECKPOINT_LIMIT_SENTINEL_REASON,
    SUPERVISOR_ESCALATED_REASONS,
    AuthorizationSnapshot,
    InvocationRequest,
    InvocationResult,
    PlanContract,
)
from .schema import DDL, SCHEMA_VERSION
from .serialization import (
    authorization_from_mapping,
    canonical_json,
    digest_sha256,
    plan_from_mapping,
    plan_hash,
    task_from_mapping,
)
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
    def __init__(self, path: str | Path, *, readonly: bool = False) -> None:
        self.path = Path(path)
        self.readonly = readonly
        if not readonly and self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if readonly:
            self.connection = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro", uri=True
            )
        else:
            self.connection = sqlite3.connect(str(self.path), isolation_level=None)
            self.connection.execute("PRAGMA foreign_keys = ON")
            if self.path != Path(":memory:"):
                self.connection.execute("PRAGMA journal_mode = WAL").fetchone()
        self.connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self.connection.close()

    @classmethod
    def open_readonly(cls, path: str | Path) -> "Database":
        """Open a read-only handle and verify the schema is current.

        Observation commands must not run DDL or take write locks. This opens
        the file in read-only mode and only verifies the schema version, so a
        running Worker is never blocked by a ``status`` or ``supervisor-next``.
        An empty or outdated database raises a clear error instead.
        """
        target = Path(path)
        if not target.exists():
            raise RuntimeError(
                "database is not initialized; run a write command "
                "(e.g. `agentflow plan authorize`) first"
            )
        database = cls(target, readonly=True)
        try:
            row = database.connection.execute(
                "SELECT version FROM schema_meta ORDER BY version DESC LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is None:
            database.close()
            raise RuntimeError(
                "database is not initialized; run a write command "
                "(e.g. `agentflow plan authorize`) first"
            )
        if int(row["version"]) != SCHEMA_VERSION:
            version = int(row["version"])
            database.close()
            raise RuntimeError(
                f"database schema version {version} does not match this build "
                f"(expected {SCHEMA_VERSION}); open it with a write command to migrate"
            )
        return database

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
            "segment_index": "INTEGER NOT NULL DEFAULT 0 CHECK (segment_index >= 0)",
            "continuation_of_call_id": "TEXT",
            "continuation_session_id": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE model_calls ADD COLUMN {name} {definition}"
                )
        # Rebuild the provider-request index to include segment_index so that
        # continuation segments reusing the same OpenCode session ID do not
        # collide with their parent call. The rebuild is conditional: a database
        # already at the current schema is left untouched, so repeated opens do
        # not drop and recreate indexes (or contend for write locks).
        index_columns = [
            row["name"]
            for row in self.connection.execute(
                "PRAGMA index_info(idx_model_calls_provider_request)"
            )
        ]
        if "segment_index" not in index_columns:
            self.connection.execute(
                "DROP INDEX IF EXISTS idx_model_calls_provider_request"
            )
            self.connection.execute(
                "CREATE UNIQUE INDEX idx_model_calls_provider_request "
                "ON model_calls(provider, provider_request_id, segment_index) "
                "WHERE provider_request_id IS NOT NULL"
            )
        self.connection.execute(
            "INSERT OR IGNORE INTO schema_meta(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utc_now()),
        )
        self._migrate_supervisor_checkpoints()
        self._migrate_staging_syncs()

    def _migrate_supervisor_checkpoints(self) -> None:
        """Add later supervisor columns to databases created before them."""
        columns = {
            row["name"]: row
            for row in self.connection.execute(
                "PRAGMA table_info(supervisor_checkpoints)"
            )
        }
        additions = {
            "reasoning_effort": "TEXT NOT NULL DEFAULT 'medium'",
            "plan_hash": "TEXT",
            "event_sequence": "INTEGER NOT NULL DEFAULT 0",
            "terminal": "INTEGER NOT NULL DEFAULT 0 CHECK (terminal IN (0, 1))",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE supervisor_checkpoints ADD COLUMN {name} {definition}"
                )
        terminal = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_supervisor_checkpoints_terminal'"
        ).fetchone()
        if terminal is None:
            self.connection.execute(
                "CREATE UNIQUE INDEX idx_supervisor_checkpoints_terminal "
                "ON supervisor_checkpoints(run_id) WHERE terminal = 1"
            )

    def _migrate_staging_syncs(self) -> None:
        """Add output-target baseline and sync-manifest columns to older databases."""
        columns = {
            row["name"]: row
            for row in self.connection.execute("PRAGMA table_info(staging_syncs)")
        }
        if "baseline_json" not in columns:
            self.connection.execute(
                "ALTER TABLE staging_syncs ADD COLUMN baseline_json TEXT"
            )
        if "manifest_json" not in columns:
            self.connection.execute(
                "ALTER TABLE staging_syncs ADD COLUMN manifest_json TEXT"
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

    @contextmanager
    def read_snapshot(self) -> Iterator[sqlite3.Connection]:
        """Open a short read-only snapshot that never blocks a concurrent writer.

        WAL mode gives the reads a consistent view of the database without
        holding a lock that would prevent a Worker from committing. Any writes
        issued through the yielded connection are rolled back on exit.
        """
        self.connection.execute("BEGIN")
        try:
            yield self.connection
        finally:
            self.connection.execute("ROLLBACK")

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
            row = connection.execute(
                "SELECT run_state FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown run: {run_id}")
            current = RunState(row["run_state"])
            terminal = (
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLED,
            )
            if current in terminal and state is not current:
                raise ValueError(
                    f"terminal run state {current.value} is irreversible; "
                    f"cannot transition to {state.value}"
                )
            finished_at = utc_now() if state in terminal else None
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
                "SELECT control_state, finished_at FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown run: {run_id}")
            if row["finished_at"] is not None:
                raise ValueError(
                    f"run {run_id} is terminal; control state cannot change"
                )
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

    @staticmethod
    def _utf8_safe_prefix(text: str, max_bytes: int) -> str:
        """Return a UTF-8-safe prefix of ``text`` of at most ``max_bytes`` bytes.

        The prefix is truncated on a code-point boundary so it never splits a
        multibyte character.
        """
        if max_bytes <= 0:
            return ""
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    @staticmethod
    def _bounded_supervisor_json(payload: Any, max_chars: int) -> str:
        encoded = canonical_json(payload)
        encoded_bytes = len(encoded.encode("utf-8"))
        if encoded_bytes <= max_chars:
            return encoded
        envelope = {"truncated": True, "original_bytes": encoded_bytes, "preview": ""}
        overhead = len(canonical_json(envelope).encode("utf-8"))
        preview = Database._utf8_safe_prefix(encoded, max(0, max_chars - overhead))
        result = canonical_json(
            {"truncated": True, "original_bytes": encoded_bytes, "preview": preview}
        )
        if len(result.encode("utf-8")) > max_chars:
            result = canonical_json(
                {"truncated": True, "original_bytes": encoded_bytes, "preview": ""}
            )
        return result

    def _insert_supervisor_checkpoint_row(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        reason: str,
        content: Any,
        *,
        max_chars: int,
        reasoning_effort: str,
        plan_hash_value: str | None,
        terminal: bool,
    ) -> str:
        next_row = connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1 AS next
            FROM supervisor_checkpoints WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        event_sequence = self._latest_event_sequence(connection, run_id)
        checkpoint_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO supervisor_checkpoints(
                checkpoint_id, run_id, sequence, reason, content_json, created_at,
                reasoning_effort, plan_hash, event_sequence, terminal
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint_id,
                run_id,
                int(next_row["next"]),
                reason,
                self._bounded_supervisor_json(content, max_chars),
                utc_now(),
                reasoning_effort,
                plan_hash_value,
                event_sequence,
                1 if terminal else 0,
            ),
        )
        self._event(
            connection,
            "run",
            run_id,
            "supervisor.checkpoint_recorded",
            {
                "checkpoint_id": checkpoint_id,
                "reason": reason,
                "terminal": terminal,
            },
        )
        return checkpoint_id

    def record_supervisor_checkpoint(
        self,
        run_id: str,
        reason: str,
        content: Any,
        *,
        max_chars: int,
        max_checkpoints: int,
        reasoning_effort: str = "medium",
        plan_hash_value: str | None = None,
        terminal: bool = False,
        idempotent: bool = False,
    ) -> str:
        """Record a supervisor checkpoint, enforcing the per-run count limit.

        When the non-terminal limit is reached, a single bounded sentinel
        checkpoint (``checkpoint_limit_reached``) is recorded instead of
        silently dropping the reason. The sentinel is idempotent: while a
        pending sentinel already exists its content is refreshed with the newly
        dropped reason rather than inserting another row. Terminal checkpoints
        bypass the count limit but remain exactly-once per run via a partial
        unique index.

        When ``idempotent`` is true, an already-pending checkpoint with the same
        ``reason`` and bounded content is returned instead of inserting a
        duplicate. This lets a pause notification be replayed after a crash
        without producing a second wake event.
        """
        with self.transaction() as connection:
            found = connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if found is None:
                raise KeyError(f"unknown run: {run_id}")
            existing_terminal = connection.execute(
                "SELECT 1 FROM supervisor_checkpoints WHERE run_id = ? AND terminal = 1",
                (run_id,),
            ).fetchone()
            if terminal and existing_terminal is not None:
                raise RuntimeError(
                    f"run {run_id} already has a terminal supervisor checkpoint"
                )
            if not terminal:
                bounded_content = self._bounded_supervisor_json(content, max_chars)
                if idempotent:
                    existing_pending = connection.execute(
                        """
                        SELECT checkpoint_id FROM supervisor_checkpoints
                        WHERE run_id = ? AND reason = ? AND status = 'pending'
                          AND content_json = ?
                        ORDER BY sequence LIMIT 1
                        """,
                        (run_id, reason, bounded_content),
                    ).fetchone()
                    if existing_pending is not None:
                        return str(existing_pending["checkpoint_id"])
                total = connection.execute(
                    "SELECT COUNT(*) AS n FROM supervisor_checkpoints WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if int(total["n"]) >= max_checkpoints:
                    self._event(
                        connection,
                        "run",
                        run_id,
                        "supervisor.checkpoint_limit_reached",
                        {"reason": reason, "limit": max_checkpoints},
                    )
                    return self._record_checkpoint_limit_sentinel(
                        connection,
                        run_id,
                        reason,
                        max_chars=max_chars,
                        max_checkpoints=max_checkpoints,
                        plan_hash_value=plan_hash_value,
                    )
            return self._insert_supervisor_checkpoint_row(
                connection,
                run_id,
                reason,
                content,
                max_chars=max_chars,
                reasoning_effort=reasoning_effort,
                plan_hash_value=plan_hash_value,
                terminal=terminal,
            )

    def _record_checkpoint_limit_sentinel(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        dropped_reason: str,
        *,
        max_chars: int,
        max_checkpoints: int,
        plan_hash_value: str | None,
    ) -> str:
        """Record or refresh the single reusable limit sentinel for a run.

        There is at most one sentinel per run regardless of acknowledgement
        status: once the limit is reached, every further non-terminal checkpoint
        reuses it. Reuse re-opens the sentinel as pending, advances its
        ``event_sequence`` to the current cursor (so a decision read against an
        older version is rejected), and aggregates the bounded set of dropped
        reasons, a running count, and the event range. The previous decision, if
        any, is preserved in the audit event rather than silently discarded.
        """
        event_sequence = self._latest_event_sequence(connection, run_id)
        existing = connection.execute(
            """
            SELECT checkpoint_id, content_json, decision_json, acknowledged_at
            FROM supervisor_checkpoints
            WHERE run_id = ? AND reason = ?
            ORDER BY sequence LIMIT 1
            """,
            (run_id, SUPERVISOR_CHECKPOINT_LIMIT_SENTINEL_REASON),
        ).fetchone()
        if existing is not None:
            dropped_reasons, dropped_count, event_sequence_min = self._merge_dropped(
                existing["content_json"], dropped_reason, event_sequence
            )
            sentinel_content = {
                "dropped_reason": dropped_reason,
                "dropped_reasons": dropped_reasons,
                "dropped_count": dropped_count,
                "checkpoint_limit": max_checkpoints,
                "event_sequence_min": event_sequence_min,
                "event_sequence_max": event_sequence,
            }
            connection.execute(
                """
                UPDATE supervisor_checkpoints
                SET content_json = ?, plan_hash = ?, event_sequence = ?,
                    status = 'pending', decision_json = NULL, acknowledged_at = NULL
                WHERE checkpoint_id = ?
                """,
                (
                    self._bounded_supervisor_json(sentinel_content, max_chars),
                    plan_hash_value,
                    event_sequence,
                    existing["checkpoint_id"],
                ),
            )
            self._event(
                connection,
                "run",
                run_id,
                "supervisor.checkpoint_sentinel_refreshed",
                {
                    "checkpoint_id": existing["checkpoint_id"],
                    "dropped_reason": dropped_reason,
                    "dropped_count": dropped_count,
                    "previous_decision": (
                        json.loads(existing["decision_json"])
                        if existing["decision_json"]
                        else None
                    ),
                    "previous_acknowledged_at": existing["acknowledged_at"],
                },
            )
            return str(existing["checkpoint_id"])
        sentinel_content = {
            "dropped_reason": dropped_reason,
            "dropped_reasons": [dropped_reason],
            "dropped_count": 1,
            "checkpoint_limit": max_checkpoints,
            "event_sequence_min": event_sequence,
            "event_sequence_max": event_sequence,
        }
        return self._insert_supervisor_checkpoint_row(
            connection,
            run_id,
            SUPERVISOR_CHECKPOINT_LIMIT_SENTINEL_REASON,
            sentinel_content,
            max_chars=max_chars,
            reasoning_effort="high",
            plan_hash_value=plan_hash_value,
            terminal=False,
        )

    _SENTINEL_MAX_DROPPED_REASONS = 32

    @staticmethod
    def _merge_dropped(
        content_json: str, dropped_reason: str, event_sequence: int
    ) -> tuple[list[str], int, int]:
        """Merge a newly dropped reason into a sentinel's aggregated content.

        The reason list is kept bounded and de-duplicated while ``dropped_count``
        always advances, so a sentinel that survives many overflows reports an
        accurate total without growing without bound.
        """
        reasons: list[str] = []
        count = 0
        event_sequence_min: int | None = None
        try:
            parsed = json.loads(content_json)
        except (ValueError, TypeError):
            parsed = {}
        if isinstance(parsed, dict):
            raw = parsed.get("dropped_reasons")
            if isinstance(raw, list):
                reasons = [r for r in raw if isinstance(r, str)]
            raw_count = parsed.get("dropped_count")
            if isinstance(raw_count, int) and not isinstance(raw_count, bool):
                count = raw_count
            raw_min = parsed.get("event_sequence_min")
            if isinstance(raw_min, int) and not isinstance(raw_min, bool):
                event_sequence_min = raw_min
        if dropped_reason not in reasons:
            reasons.append(dropped_reason)
        if len(reasons) > Database._SENTINEL_MAX_DROPPED_REASONS:
            reasons = reasons[-Database._SENTINEL_MAX_DROPPED_REASONS :]
        return reasons, count + 1, (
            event_sequence if event_sequence_min is None else event_sequence_min
        )

    def finalize_run(
        self,
        run_id: str,
        state: RunState,
        reason: str,
        content: Any,
        *,
        max_chars: int,
        max_checkpoints: int,
        reasoning_effort: str = "medium",
        plan_hash_value: str | None = None,
    ) -> str:
        """Record the terminal supervisor checkpoint and the terminal run state
        transition in a single transaction.

        This prevents a crash from leaving a terminal checkpoint paired with a
        still-running run state. The terminal checkpoint is exactly-once per run.
        """
        if state not in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED):
            raise ValueError(f"finalize_run requires a terminal state, got {state.value}")
        bounded_content = self._bounded_supervisor_json(content, max_chars)
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT run_state, finished_at FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"unknown run: {run_id}")
            current_state = RunState(run["run_state"])
            existing = connection.execute(
                """
                SELECT checkpoint_id, reason, plan_hash, content_json
                FROM supervisor_checkpoints WHERE run_id = ? AND terminal = 1
                """,
                (run_id,),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["reason"] == reason
                    and existing["plan_hash"] == plan_hash_value
                    and existing["content_json"] == bounded_content
                )
                if not same:
                    raise RuntimeError(
                        f"conflicting terminal checkpoint for run {run_id}; "
                        "the run has already reached a terminal state"
                    )
                if run["finished_at"] is not None and current_state is not state:
                    raise RuntimeError(
                        f"conflicting terminal run state for run {run_id}"
                    )
                if run["finished_at"] is None:
                    connection.execute(
                        "UPDATE runs SET run_state = ?, finished_at = ? WHERE run_id = ?",
                        (state.value, utc_now(), run_id),
                    )
                    self._event(
                        connection, "run", run_id, "run.state_changed",
                        {"state": state.value},
                    )
                return existing["checkpoint_id"]
            if run["finished_at"] is not None:
                raise RuntimeError(
                    f"run {run_id} is already terminal without a terminal checkpoint"
                )
            checkpoint_id = self._insert_supervisor_checkpoint_row(
                connection,
                run_id,
                reason,
                content,
                max_chars=max_chars,
                reasoning_effort=reasoning_effort,
                plan_hash_value=plan_hash_value,
                terminal=True,
            )
            connection.execute(
                "UPDATE runs SET run_state = ?, finished_at = ? WHERE run_id = ?",
                (state.value, utc_now(), run_id),
            )
            self._event(
                connection, "run", run_id, "run.state_changed", {"state": state.value}
            )
        return checkpoint_id

    def record_terminal_supervisor_checkpoint(
        self,
        run_id: str,
        reason: str,
        content: Any,
        *,
        max_chars: int,
        max_checkpoints: int,
        reasoning_effort: str = "medium",
        plan_hash_value: str | None = None,
    ) -> str:
        """Record the terminal checkpoint exactly once, idempotently.

        If a terminal checkpoint already exists for the run this is a no-op and
        returns the existing checkpoint id.
        """
        existing = self.fetch_one(
            """
            SELECT checkpoint_id FROM supervisor_checkpoints
            WHERE run_id = ? AND terminal = 1
            """,
            (run_id,),
        )
        if existing is not None:
            return existing["checkpoint_id"]
        try:
            return self.record_supervisor_checkpoint(
                run_id,
                reason,
                content,
                max_chars=max_chars,
                max_checkpoints=max_checkpoints,
                reasoning_effort=reasoning_effort,
                plan_hash_value=plan_hash_value,
                terminal=True,
            )
        except sqlite3.IntegrityError:
            existing = self.fetch_one(
                """
                SELECT checkpoint_id FROM supervisor_checkpoints
                WHERE run_id = ? AND terminal = 1
                """,
                (run_id,),
            )
            if existing is not None:
                return existing["checkpoint_id"]
            raise

    def record_input_artifact_snapshot(
        self, run_id: str, task_id: str, snapshot: dict[str, Any]
    ) -> None:
        """Persist an authoritative input-artifact snapshot manifest.

        The manifest stores only project-relative paths and never an absolute
        path, so it can safely travel to a reviewer without leaking host layout.
        """
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO input_artifact_snapshots(
                    snapshot_id, run_id, task_id, attempt_id, path, sha256, size, snapshot_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    run_id,
                    task_id,
                    str(snapshot["attempt_id"]),
                    str(snapshot["path"]),
                    str(snapshot["sha256"]),
                    int(snapshot["size"]),
                    utc_now(),
                ),
            )
            self._event(
                connection,
                "task",
                f"{run_id}:{task_id}",
                "input_artifact.snapshotted",
                {
                    "path": snapshot["path"],
                    "sha256": snapshot["sha256"],
                    "size": int(snapshot["size"]),
                    "attempt_id": snapshot["attempt_id"],
                },
            )

    def input_artifact_manifest(
        self, run_id: str, task_id: str, attempt_id: str
    ) -> list[dict[str, Any]]:
        """Return the persisted snapshot manifest for an attempt, oldest first."""
        rows = self.connection.execute(
            """
            SELECT path, sha256, size, attempt_id, snapshot_id, snapshot_at
            FROM input_artifact_snapshots
            WHERE run_id = ? AND task_id = ? AND attempt_id = ?
            ORDER BY rowid
            """,
            (run_id, task_id, attempt_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def contributor_model_refs(self, run_id: str, task_id: str) -> list[dict[str, Any]]:
        """Return the distinct models that actually produced the current artifact.

        Only ``COMPLETED`` implementation/revision calls count: a fallback model
        that failed before producing output never contributed, while every
        successful fallback and continuation segment is included.
        """
        rows = self.connection.execute(
            """
            SELECT DISTINCT provider, model_id, model_version, model_family, is_local
            FROM model_calls
            JOIN attempts USING (attempt_id)
            WHERE attempts.run_id = ? AND attempts.task_id = ?
              AND role IN ('implementation', 'revision')
              AND state = ?
            """,
            (run_id, task_id, InvocationState.COMPLETED.value),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_staging_sync_start(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str,
        *,
        baseline_json: str | None = None,
    ) -> None:
        """Persist the minimal staging-sandbox identity before the remote call.

        The record marks that a sandbox was created for this attempt and that
        outputs have not yet been synced. ``baseline_json`` captures the
        output-target baseline (existence/content-hash/type) recorded at
        sandbox creation, which the sync uses to detect owner-side changes
        instead of silently overwriting them. It is idempotent per attempt.
        """
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO staging_syncs(
                    attempt_id, run_id, task_id, state, baseline_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'created', ?, ?, ?)
                ON CONFLICT(attempt_id) DO NOTHING
                """,
                (attempt_id, run_id, task_id, baseline_json, utc_now(), utc_now()),
            )

    def record_staging_sync_done(
        self, attempt_id: str, synced_files: tuple[str, ...], manifest: dict[str, Any]
    ) -> None:
        """Mark a staging sandbox's outputs as committed to the worktree.

        ``manifest`` is the control-plane snapshot of each synced output (path to
        content hash and permission bits). It is persisted atomically with the
        ``synced`` state so a crash after the state write can never leave a
        synced record without a verifiable manifest for recovery to check.
        """
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE staging_syncs
                SET state = 'synced', synced_files_json = ?, manifest_json = ?,
                    updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    canonical_json(list(synced_files)),
                    canonical_json(manifest),
                    utc_now(),
                    attempt_id,
                ),
            )
            if not cursor.rowcount:
                raise KeyError(f"unknown staging sync record: {attempt_id}")

    def staging_sync_state(self, attempt_id: str) -> dict[str, Any] | None:
        """Return the persisted staging-sync record for an attempt, if any."""
        row = self.fetch_one(
            "SELECT * FROM staging_syncs WHERE attempt_id = ?", (attempt_id,)
        )
        return dict(row) if row is not None else None

    def record_staging_rollback_failed(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str,
        unrecovered_paths: tuple[str, ...],
    ) -> None:
        """Record that a staging-sync rollback could not fully restore state."""
        with self.transaction() as connection:
            self._event(
                connection,
                "task",
                f"{run_id}:{task_id}",
                "staging.rollback_failed",
                {
                    "attempt_id": attempt_id,
                    "unrecovered_paths": list(unrecovered_paths),
                },
            )

    @staticmethod
    def _latest_event_sequence(connection: sqlite3.Connection, run_id: str) -> int:
        row = connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) AS seq
            FROM events
            WHERE aggregate_id = ?
               OR aggregate_id LIKE ?
               OR aggregate_id IN (SELECT authorization_id FROM runs WHERE run_id = ?)
               OR aggregate_id IN (
                    SELECT attempt_id FROM attempts WHERE run_id = ?
               )
               OR aggregate_id IN (
                    SELECT call_id FROM model_calls
                    WHERE attempt_id IN (SELECT attempt_id FROM attempts WHERE run_id = ?)
               )
               OR aggregate_id IN (SELECT review_id FROM reviews WHERE run_id = ?)
               OR aggregate_id IN (
                    SELECT test_result_id FROM test_results WHERE run_id = ?
               )
            """,
            (
                run_id,
                f"{run_id}:%",
                run_id,
                run_id,
                run_id,
                run_id,
                run_id,
            ),
        ).fetchone()
        return int(row["seq"])

    def latest_event_sequence(self, run_id: str) -> int:
        # Read-only monitoring: never acquire a write lock (no BEGIN IMMEDIATE),
        # so a supervisor polling the cursor cannot block a worker's writes.
        return self._latest_event_sequence(self.connection, run_id)

    def pending_supervisor_checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT checkpoint_id, sequence, reason, content_json, status,
                   reasoning_effort, plan_hash, event_sequence, terminal, created_at
            FROM supervisor_checkpoints WHERE run_id = ? AND status = 'pending'
            ORDER BY sequence
            """,
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def next_supervisor_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        row = self.fetch_one(
            """
            SELECT checkpoint_id, sequence, reason, content_json, status,
                   reasoning_effort, plan_hash, event_sequence, terminal, created_at
            FROM supervisor_checkpoints WHERE run_id = ? AND status = 'pending'
            ORDER BY sequence LIMIT 1
            """,
            (run_id,),
        )
        return dict(row) if row is not None else None

    def supervisor_run_plan_hash(self, run_id: str) -> str:
        row = self.fetch_one(
            """
            SELECT plans.content_hash AS plan_hash
            FROM runs JOIN plans
              ON runs.plan_id = plans.plan_id AND runs.plan_version = plans.version
            WHERE runs.run_id = ?
            """,
            (run_id,),
        )
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        return row["plan_hash"]

    def acknowledge_supervisor_checkpoint(
        self, run_id: str, checkpoint_id: str, decision: Any
    ) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT checkpoint_id, status, decision_json, event_sequence
                FROM supervisor_checkpoints WHERE checkpoint_id = ? AND run_id = ?
                """,
                (checkpoint_id, run_id),
            ).fetchone()
            if row is None:
                raise KeyError(
                    f"unknown supervisor checkpoint: {checkpoint_id} for run {run_id}"
                )
            self._validate_supervisor_decision(connection, run_id, row, decision)
            encoded = canonical_json(decision)
            if row["status"] == "acknowledged":
                if row["decision_json"] == encoded:
                    return
                raise RuntimeError(
                    f"conflicting decision for already-acknowledged checkpoint {checkpoint_id}"
                )
            cursor = connection.execute(
                """
                UPDATE supervisor_checkpoints
                SET status = 'acknowledged', decision_json = ?, acknowledged_at = ?
                WHERE checkpoint_id = ? AND run_id = ? AND status = 'pending'
                """,
                (encoded, utc_now(), checkpoint_id, run_id),
            )
            if not cursor.rowcount:
                raise RuntimeError(
                    f"failed to acknowledge supervisor checkpoint {checkpoint_id}"
                )
            self._event(
                connection,
                "run",
                run_id,
                "supervisor.checkpoint_acknowledged",
                {"checkpoint_id": checkpoint_id},
            )

    def _validate_supervisor_decision(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        checkpoint_row: sqlite3.Row,
        decision: Any,
    ) -> None:
        if not isinstance(decision, dict):
            raise ValueError("supervisor decision must be a JSON object")
        unknown = sorted(set(decision) - self._SUPERVISOR_DECISION_KEYS)
        if unknown:
            raise ValueError(
                f"supervisor decision has unknown field(s): {unknown}"
            )
        action = decision.get("action")
        if not isinstance(action, str) or action not in self._SUPERVISOR_DECISION_ACTIONS:
            raise ValueError(
                "supervisor decision action must be one of "
                f"{sorted(self._SUPERVISOR_DECISION_ACTIONS)}"
            )
        decision_plan_hash = decision.get("plan_hash")
        run_plan_hash = self.supervisor_run_plan_hash(run_id)
        if not isinstance(decision_plan_hash, str) or decision_plan_hash != run_plan_hash:
            raise ValueError("supervisor decision plan_hash does not match the run")
        cursor_value = decision.get("cursor")
        if not isinstance(cursor_value, int) or isinstance(cursor_value, bool):
            raise ValueError("supervisor decision cursor must be an integer")
        latest = self._latest_event_sequence(connection, run_id)
        if cursor_value > latest:
            raise ValueError("supervisor decision cursor is ahead of the run")
        if cursor_value < int(checkpoint_row["event_sequence"]):
            raise ValueError("supervisor decision cursor is stale")

    _SUPERVISOR_DECISION_ACTIONS: frozenset[str] = frozenset(
        {"pause", "resume", "escalate"}
    )

    _SUPERVISOR_DECISION_KEYS: frozenset[str] = frozenset(
        {"action", "plan_hash", "cursor"}
    )

    def supervisor_digest(
        self,
        run_id: str,
        after_sequence: int = 0,
        changed_files_by_task: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        # Read every digest field from a single consistent snapshot so that
        # ``pending_checkpoints``, ``reason_codes``, ``cursor`` and the run state
        # can never be torn apart by a concurrent writer. The snapshot never
        # blocks a Worker's writes (WAL read transaction, no BEGIN IMMEDIATE).
        with self.read_snapshot():
            return self._supervisor_digest_impl(
                run_id, after_sequence, changed_files_by_task
            )

    def _supervisor_digest_impl(
        self,
        run_id: str,
        after_sequence: int,
        changed_files_by_task: dict[str, list[str]] | None,
    ) -> dict[str, Any]:
        snapshot = self.run_snapshot(run_id)
        run = snapshot["run"]
        plan = self.load_plan(run["plan_id"], run["plan_version"])
        plan_hash_value = self.supervisor_run_plan_hash(run_id)
        authorization_id = run.get("authorization_id")
        pending = self.pending_supervisor_checkpoints(run_id)
        reason_codes = sorted({item["reason"] for item in pending})
        cursor = self.latest_event_sequence(run_id)
        changed = cursor > after_sequence
        wake_required = any(
            int(item["event_sequence"]) > after_sequence for item in pending
        )
        pending_checkpoints = self._pending_checkpoint_entries(pending)

        reasoning_effort = (
            plan.supervisor_policy.escalated_reasoning_effort.value
            if any(reason in SUPERVISOR_ESCALATED_REASONS for reason in reason_codes)
            else plan.supervisor_policy.default_reasoning_effort.value
        )

        tasks = []
        scope: set[str] = set()
        for task in snapshot["tasks"]:
            attempt = self.latest_attempt(run_id, task["task_id"])
            call = self._latest_call_info(run_id, task["task_id"])
            tasks.append(
                {
                    "task_id": task["task_id"],
                    "task_state": task["state"],
                    "attempt_number": attempt["attempt_number"] if attempt else 0,
                    "role": call["role"] if call else None,
                    "segment_index": call["segment_index"] if call else 0,
                }
            )
            scope.update(self.task_allowed_files(run_id, task["task_id"]))

        cost_summary = self.cost_summary(run_id)
        confirmed = cost_summary["confirmed_remote_cost_usd"]
        limit = plan.max_remote_cost
        usage_ratio = (confirmed / limit) if limit > 0 else 0.0
        cost = {
            "confirmed_remote_cost_usd": confirmed,
            "limit_usd": limit,
            "usage_ratio": usage_ratio,
            "cost_unavailable": cost_summary["cost_unavailable_calls"] > 0,
        }

        unknown_count = self.unresolved_unknown_calls(run_id)
        latest_test = self.latest_test_reference(run_id)
        latest_review = self.latest_review_reference(run_id)
        event_count = self._event_sequence_count(run_id, after_sequence, cursor)

        digest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "plan_hash": plan_hash_value,
            "authorization_id": authorization_id,
            "changed": changed,
            "wake_required": wake_required,
            "recommended_reasoning_effort": reasoning_effort,
            "reason_codes": reason_codes,
            "pending_checkpoints": pending_checkpoints,
            "run_state": run["run_state"],
            "control_state": run["control_state"],
            "cursor": cursor,
            "tasks": tasks,
            "scope": sorted(scope),
            "cost": cost,
            "unknown_call_count": unknown_count,
            "latest_test": latest_test,
            "latest_review": latest_review,
            "changed_files": changed_files_by_task or {},
            "event_sequence": {
                "after": after_sequence,
                "cursor": cursor,
                "count": event_count,
            },
        }
        max_chars = plan.supervisor_policy.max_checkpoint_chars
        bounded = self._bounded_supervisor_digest(digest, max_chars)
        bounded["digest_sha256"] = digest_sha256(bounded)
        final_bytes = len(canonical_json(bounded).encode("utf-8"))
        if final_bytes > max_chars:
            raise RuntimeError(
                f"supervisor digest exceeded the hard byte cap "
                f"({final_bytes} > {max_chars})"
            )
        return bounded

    @staticmethod
    def _pending_checkpoint_entries(
        pending: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Build the digest's acknowledgement handles from pending checkpoints.

        Each entry exposes the unique ``checkpoint_id``, its ``reason`` and the
        ``event_sequence`` the supervisor must acknowledge against. For the
        limit sentinel the aggregated dropped reasons are surfaced too so the
        supervisor can tell what exception prompted the notification without
        opening the full database.
        """
        entries: list[dict[str, Any]] = []
        for item in pending:
            entry: dict[str, Any] = {
                "checkpoint_id": item["checkpoint_id"],
                "reason": item["reason"],
                "event_sequence": int(item["event_sequence"]),
                "terminal": bool(item["terminal"]),
            }
            if item["reason"] == SUPERVISOR_CHECKPOINT_LIMIT_SENTINEL_REASON:
                try:
                    content = json.loads(item["content_json"])
                except (ValueError, TypeError):
                    content = {}
                if isinstance(content, dict):
                    for key in ("dropped_reason", "dropped_reasons", "dropped_count"):
                        if key in content:
                            entry[key] = content[key]
            entries.append(entry)
        return entries

    def task_allowed_files(self, run_id: str, task_id: str) -> list[str]:
        row = self.fetch_one(
            "SELECT contract_json FROM tasks WHERE run_id = ? AND task_id = ?",
            (run_id, task_id),
        )
        if row is None:
            raise KeyError(f"unknown task: {run_id}/{task_id}")
        contract = task_from_mapping(json.loads(row["contract_json"]))
        return list(contract.allowed_files)

    def _latest_call_info(
        self, run_id: str, task_id: str
    ) -> dict[str, Any] | None:
        row = self.fetch_one(
            """
            SELECT model_calls.role AS role, model_calls.segment_index AS segment_index
            FROM model_calls JOIN attempts USING (attempt_id)
            WHERE attempts.run_id = ? AND attempts.task_id = ?
            ORDER BY attempts.attempt_number DESC, model_calls.segment_index DESC
            LIMIT 1
            """,
            (run_id, task_id),
        )
        return dict(row) if row is not None else None

    def latest_test_reference(self, run_id: str) -> dict[str, Any] | None:
        row = self.fetch_one(
            """
            SELECT test_result_id, task_id, passed, duration_ms
            FROM test_results WHERE run_id = ?
            ORDER BY rowid DESC LIMIT 1
            """,
            (run_id,),
        )
        return dict(row) if row is not None else None

    def latest_review_reference(self, run_id: str) -> dict[str, Any] | None:
        row = self.fetch_one(
            """
            SELECT review_id, task_id, approved
            FROM reviews WHERE run_id = ?
            ORDER BY rowid DESC LIMIT 1
            """,
            (run_id,),
        )
        return dict(row) if row is not None else None

    def load_plan(self, plan_id: str, version: int) -> PlanContract:
        row = self.fetch_one(
            "SELECT canonical_json FROM plans WHERE plan_id = ? AND version = ?",
            (plan_id, version),
        )
        if row is None:
            raise KeyError(f"unknown plan: {plan_id}@{version}")
        return plan_from_mapping(json.loads(row["canonical_json"]))

    @staticmethod
    def _digest_count(value: Any) -> int:
        if isinstance(value, dict):
            return len(value)
        if isinstance(value, (list, tuple)):
            return len(value)
        return 1

    @staticmethod
    def _summarize_digest_field(value: Any) -> dict[str, Any]:
        return {"count": Database._digest_count(value), "sha256": digest_sha256(value)}

    @classmethod
    def _bounded_pending_checkpoints(
        cls, pending: list[dict[str, Any]], max_bytes: int
    ) -> list[dict[str, Any]]:
        """Bound a pending-checkpoint list to ``max_bytes`` without losing handles.

        Each entry is trimmed to its acknowledgement handle (``checkpoint_id``,
        ``reason``, ``event_sequence``) plus the sentinel's last ``dropped_reason``
        and ``dropped_count``, so truncation never removes the fields a supervisor
        needs to ack a specific notification.
        """
        if not pending or max_bytes <= 0:
            return []
        result: list[dict[str, Any]] = []
        for entry in pending:
            trimmed: dict[str, Any] = {
                "checkpoint_id": entry.get("checkpoint_id"),
                "reason": entry.get("reason"),
                "event_sequence": entry.get("event_sequence"),
                "terminal": entry.get("terminal"),
            }
            if entry.get("dropped_reason") is not None:
                trimmed["dropped_reason"] = entry["dropped_reason"]
            if entry.get("dropped_count") is not None:
                trimmed["dropped_count"] = entry["dropped_count"]
            candidate = result + [trimmed]
            if result and len(canonical_json(candidate).encode("utf-8")) > max_bytes:
                break
            result = candidate
        return result

    _DIGEST_SHA256_RESERVE_BYTES = 96
    _DIGEST_FIELD_LIMIT_BYTES = 256

    @staticmethod
    def _bounded_supervisor_digest(
        digest: dict[str, Any], max_chars: int
    ) -> dict[str, Any]:
        """Return a digest whose canonical JSON fits within ``max_chars``.

        The digest is first returned verbatim when it already fits the budget
        (which reserves room for the trailing ``digest_sha256`` field). Otherwise
        a minimal emergency envelope is guaranteed, and every optional field is
        re-added greedily only while it still fits. Any variable-length field is
        bounded individually so a pathological ``run_id``, ``authorization_id``,
        ``plan_hash``, or reason list can never blow the cap.
        """
        budget = max(1, max_chars - Database._DIGEST_SHA256_RESERVE_BYTES)
        encoded = canonical_json(digest)
        if len(encoded.encode("utf-8")) <= budget:
            return digest
        original_bytes = len(encoded.encode("utf-8"))

        def bounded(value: Any) -> Any:
            enc = canonical_json(value)
            if len(enc.encode("utf-8")) <= Database._DIGEST_FIELD_LIMIT_BYTES:
                return value
            summary = Database._summarize_digest_field(value)
            summary["original_bytes"] = len(enc.encode("utf-8"))
            return summary

        minimal = {
            "schema_version": digest["schema_version"],
            "run_id": bounded(digest["run_id"]),
            "plan_hash": bounded(digest["plan_hash"]),
            "wake_required": digest["wake_required"],
            "reason_codes": bounded(digest["reason_codes"]),
            "run_state": digest["run_state"],
            "control_state": digest["control_state"],
            "cursor": digest["cursor"],
            "truncated": True,
            "original_bytes": original_bytes,
        }
        base_bytes = len(canonical_json(minimal).encode("utf-8"))
        minimal["pending_checkpoints"] = Database._bounded_pending_checkpoints(
            digest.get("pending_checkpoints", []), budget - base_bytes
        )
        result = dict(minimal)
        for key in (
            "authorization_id",
            "changed",
            "recommended_reasoning_effort",
            "unknown_call_count",
            "cost",
            "tasks",
            "scope",
            "changed_files",
            "latest_test",
            "latest_review",
        ):
            if key not in digest:
                continue
            candidate = dict(result)
            candidate[key] = bounded(digest[key])
            if len(canonical_json(candidate).encode("utf-8")) <= budget:
                result = candidate
        return result

    def _event_sequence_count(
        self, run_id: str, after_sequence: int, cursor: int
    ) -> int:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS n FROM events
            WHERE sequence > ? AND sequence <= ? AND (
                aggregate_id = ?
                OR aggregate_id LIKE ?
                OR aggregate_id IN (SELECT authorization_id FROM runs WHERE run_id = ?)
                OR aggregate_id IN (SELECT attempt_id FROM attempts WHERE run_id = ?)
                OR aggregate_id IN (
                    SELECT call_id FROM model_calls
                    WHERE attempt_id IN (SELECT attempt_id FROM attempts WHERE run_id = ?)
                )
                OR aggregate_id IN (SELECT review_id FROM reviews WHERE run_id = ?)
                OR aggregate_id IN (SELECT test_result_id FROM test_results WHERE run_id = ?)
            )
            """,
            (
                after_sequence,
                cursor,
                run_id,
                f"{run_id}:%",
                run_id,
                run_id,
                run_id,
                run_id,
                run_id,
            ),
        ).fetchone()
        return int(row["n"])


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

    def latest_review_findings(self, run_id: str, task_id: str) -> tuple[dict[str, Any], ...]:
        """Return the most recent review's findings as primitive dicts, oldest-first.

        Findings are the unresolved ones that triggered the current revision.
        """
        row = self.fetch_one(
            """
            SELECT findings_json FROM reviews
            WHERE run_id = ? AND task_id = ?
            ORDER BY rowid DESC LIMIT 1
            """,
            (run_id, task_id),
        )
        if row is None or not row["findings_json"]:
            return ()
        return tuple(json.loads(row["findings_json"]))

    def _insert_planned_call(
        self,
        connection: sqlite3.Connection,
        request: InvocationRequest,
        attempt_id: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO model_calls(
                call_id, request_key, attempt_id, provider, model_id, model_version,
                model_family, is_local, role, state, data_sensitivity, read_only,
                request_scope_json, remote_cost, cost_unavailable, test_double,
                segment_index, continuation_of_call_id, continuation_session_id,
                input_tokens, output_tokens, duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, ?, 0, 0, 0)
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
                        "resource_budgets": request.metadata.get("resource_budgets"),
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
                int(request.metadata.get("segment_index", 0)),
                request.metadata.get("continuation_of_call_id"),
                request.metadata.get("continuation_session_id"),
            ),
        )
        self._event(
            connection,
            "call",
            request.call_id,
            "call.planned",
            {"request_key": request.request_key},
        )
        continuation_of_call_id = request.metadata.get("continuation_of_call_id")
        if continuation_of_call_id is not None:
            self._event(
                connection,
                "run",
                request.run_id,
                "continuation.scheduled",
                {
                    "task_id": request.task_id,
                    "attempt_id": attempt_id,
                    "continuation_of_call_id": continuation_of_call_id,
                    "segment_index": int(request.metadata.get("segment_index", 0)),
                    "session_id": request.metadata.get("continuation_session_id"),
                },
            )

    def register_call(
        self,
        request: InvocationRequest,
        attempt_id: str,
        *,
        reuse_planned: bool = False,
    ) -> tuple[sqlite3.Row, bool]:
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
                if state is InvocationState.PLANNED and reuse_planned:
                    return existing, False
                raise DuplicateInvocationError(f"call already exists in state {state.value}")
            self._insert_planned_call(connection, request, attempt_id)
            created = connection.execute(
                "SELECT * FROM model_calls WHERE call_id = ?", (request.call_id,)
            ).fetchone()
            return created, True

    def begin_call(
        self,
        request: InvocationRequest,
        attempt_id: str,
        *,
        reuse_planned: bool = False,
        estimated_remote_cost: float | None = None,
        usable_budget: float | None = None,
    ) -> tuple[sqlite3.Row | None, str]:
        """Atomically gate, register, and start a model call in one transaction.

        The single ``BEGIN IMMEDIATE`` transaction re-checks that the run is
        still RUNNING, registers the call (or idempotently reuses an existing
        PLANNED call), transitions the allowed call to STARTED, and audits the
        transition. This closes the pause TOCTOU between the runner's gate check
        and the adapter invocation.

        When both ``estimated_remote_cost`` and ``usable_budget`` are provided,
        the remote budget is re-checked inside the same transaction that starts
        the call, so two concurrent dispatches cannot both reserve beyond the
        usable budget.

        Returns ``(row, outcome)`` where ``outcome`` is one of:
          - ``STARTED``: the call is registered (or a PLANNED call reused) and
            is now STARTED; ``row`` is the authoritative persisted row.
          - ``REUSED_COMPLETED``: an existing COMPLETED call was returned.
          - ``PAUSE_BLOCKED``: the run was no longer RUNNING; nothing changed.
          - ``BUDGET_BLOCKED``: the remote budget would be exceeded; nothing changed.
        Raises ``UnknownInvocationError`` / ``DuplicateInvocationError`` as before.
        """
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT control_state, run_state, finished_at FROM runs WHERE run_id = ?",
                (request.run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"unknown run: {request.run_id}")
            if ControlState(run["control_state"]) is not ControlState.RUNNING:
                return None, "PAUSE_BLOCKED"
            if run["finished_at"] is not None or RunState(run["run_state"]) in (
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLED,
            ):
                return None, "PAUSE_BLOCKED"
            attempt = connection.execute(
                "SELECT run_id, task_id FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise ValueError(f"unknown attempt: {attempt_id}")
            if attempt["run_id"] != request.run_id or attempt["task_id"] != request.task_id:
                raise ValueError(
                    f"attempt {attempt_id} does not belong to run "
                    f"{request.run_id} / task {request.task_id}"
                )
            existing = connection.execute(
                "SELECT * FROM model_calls WHERE request_key = ?", (request.request_key,)
            ).fetchone()
            if existing is not None:
                state = InvocationState(existing["state"])
                if state is InvocationState.COMPLETED:
                    return existing, "REUSED_COMPLETED"
                if state is InvocationState.UNKNOWN:
                    raise UnknownInvocationError(
                        "call outcome is unknown; reconcile before retrying"
                    )
                if not (state is InvocationState.PLANNED and reuse_planned):
                    raise DuplicateInvocationError(
                        f"call already exists in state {state.value}"
                    )
                call_id = str(existing["call_id"])
            else:
                call_id = request.call_id
            if estimated_remote_cost is not None and usable_budget is not None:
                committed = self._committed_budget(connection, request.run_id)
                if committed + estimated_remote_cost > usable_budget:
                    return None, "BUDGET_BLOCKED"
            if existing is None:
                self._insert_planned_call(connection, request, attempt_id)
            connection.execute(
                "UPDATE model_calls SET state = ?, started_at = ? WHERE call_id = ?",
                (InvocationState.STARTED.value, utc_now(), call_id),
            )
            self._event(
                connection,
                "call",
                call_id,
                "call.transitioned",
                {
                    "from": InvocationState.PLANNED.value,
                    "to": InvocationState.STARTED.value,
                },
            )
            row = connection.execute(
                "SELECT * FROM model_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            return row, "STARTED"

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

    def mark_call_unknown(
        self,
        call_id: str,
        provider_request_id: str | None,
        result: InvocationResult,
    ) -> None:
        """Persist an unknown outcome while keeping confirmed usage evidence."""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM model_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown call: {call_id}")
            current = InvocationState(row["state"])
            require_transition(current, InvocationState.UNKNOWN)
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
                    InvocationState.UNKNOWN.value,
                    provider_request_id,
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
            self._event(
                connection,
                "call",
                call_id,
                "call.unknown",
                {
                    "remote_cost": result.remote_cost,
                    "cost_unavailable": result.cost_unavailable,
                },
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

    def _confirmed_remote_cost(self, connection: sqlite3.Connection, run_id: str) -> float:
        row = connection.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM cost_entries WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return float(row["total"])

    def _reserved_remote_cost(self, connection: sqlite3.Connection, run_id: str) -> float:
        rows = connection.execute(
            """
            SELECT model_calls.request_scope_json
            FROM model_calls
            JOIN attempts USING (attempt_id)
            WHERE attempts.run_id = ? AND model_calls.is_local = 0
              AND (
                model_calls.state IN (?, ?)
                OR model_calls.cost_unavailable = 1
              )
            """,
            (
                run_id,
                InvocationState.STARTED.value,
                InvocationState.UNKNOWN.value,
            ),
        ).fetchall()
        return sum(
            float(
                json.loads(row["request_scope_json"]).get(
                    "estimated_remote_cost", 0
                )
            )
            for row in rows
        )

    def _committed_budget(self, connection: sqlite3.Connection, run_id: str) -> float:
        return self._confirmed_remote_cost(connection, run_id) + self._reserved_remote_cost(
            connection, run_id
        )

    def remote_cost_spent(self, run_id: str) -> float:
        return self._confirmed_remote_cost(self.connection, run_id)

    def remote_budget_committed(self, run_id: str) -> float:
        return self._committed_budget(self.connection, run_id)

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
        confirmed = self.remote_cost_spent(run_id)
        committed = self.remote_budget_committed(run_id)
        return {
            "run_id": run_id,
            "confirmed_remote_cost_usd": confirmed,
            "reserved_remote_cost_usd": committed - confirmed,
            "committed_remote_cost_usd": committed,
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

    def unresolved_suspected_step_limit_calls(self, run_id: str) -> int:
        """Count failed calls with no resolution path other than manual review.

        A ``suspected_step_limit`` call cannot be continued, so resuming the run
        would only pause the same failure again. These calls must be inspected
        or resolved through the existing authorization flow before resume.
        """
        rows = self.connection.execute(
            """
            SELECT model_calls.raw_metadata_json
            FROM model_calls
            JOIN attempts USING (attempt_id)
            WHERE attempts.run_id = ? AND model_calls.state = ?
            """,
            (run_id, InvocationState.FAILED.value),
        ).fetchall()
        count = 0
        for row in rows:
            if not row["raw_metadata_json"]:
                continue
            metadata = json.loads(row["raw_metadata_json"])
            if metadata.get("failure_kind") == "suspected_step_limit":
                count += 1
        return count

    def step_limit_calls_without_continuation(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT model_calls.call_id, model_calls.attempt_id, attempts.task_id,
                   model_calls.role, model_calls.is_local, model_calls.segment_index,
                   model_calls.provider_request_id, model_calls.raw_metadata_json
            FROM model_calls
            JOIN attempts USING (attempt_id)
            WHERE attempts.run_id = ? AND model_calls.state = ?
              AND NOT EXISTS (
                  SELECT 1 FROM model_calls AS child
                  WHERE child.continuation_of_call_id = model_calls.call_id
                    AND child.state != ?
              )
            """,
            (
                run_id,
                InvocationState.FAILED.value,
                InvocationState.PLANNED.value,
            ),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            if not row["raw_metadata_json"]:
                continue
            metadata = json.loads(row["raw_metadata_json"])
            if metadata.get("failure_kind") != "step_limit_reached":
                continue
            result.append(dict(row))
        return result

    def nonretryable_output_failures(self, run_id: str) -> int:
        rows = self.connection.execute(
            """SELECT raw_metadata_json FROM model_calls JOIN attempts USING (attempt_id)
               WHERE attempts.run_id = ? AND model_calls.state = ?""",
            (run_id, InvocationState.FAILED.value),
        ).fetchall()
        return sum(
            json.loads(row["raw_metadata_json"] or "{}").get("failure_kind")
            in {"output_limit_reached", "protocol_error"}
            for row in rows
        )

    def latest_role_call(self, attempt_id: str, role: str) -> sqlite3.Row | None:
        return self.fetch_one(
            """
            SELECT call_id, request_key, state, segment_index, provider_request_id,
                   raw_metadata_json, role, is_local,
                   provider, model_id, model_version, model_family,
                   continuation_of_call_id, continuation_session_id
            FROM model_calls
            WHERE attempt_id = ? AND role = ?
            ORDER BY segment_index DESC, rowid DESC
            LIMIT 1
            """,
            (attempt_id, role),
        )

    def unfinished_calls(self, run_id: str) -> int:
        row = self.fetch_one(
            """
            SELECT COUNT(*) AS count FROM model_calls
            JOIN attempts USING (attempt_id)
            WHERE attempts.run_id = ? AND model_calls.state IN (?, ?, ?)
            """,
            (
                run_id,
                InvocationState.PLANNED.value,
                InvocationState.STARTED.value,
                InvocationState.UNKNOWN.value,
            ),
        )
        return int(row["count"])

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
