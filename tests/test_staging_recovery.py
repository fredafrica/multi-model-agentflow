"""Crash-recovery of remote staging sandbox outputs regression tests."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agentflow.adapters import AdapterRouter, ModelUnavailableError
from agentflow.authorization import issue_authorization
from agentflow.contracts import InvocationRequest, InvocationResult, ModelRef
from agentflow.database import Database
from agentflow.fake_adapter import FakeAdapter
from agentflow.runner import Runner
from agentflow.states import RunState, TaskState
from agentflow.workspace import GitWorkspace

from test_remote_worker import WORKER_PROVIDER, WORKER_MODEL, worker_plan, worker_task


def git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True)


class StagingRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.email", "tests@example.invalid")
        git(self.root, "config", "user.name", "AgentFlow Tests")
        (self.root / "seed.txt").write_text("seed\n", encoding="utf-8")
        git(self.root, "add", "seed.txt")
        git(self.root, "commit", "-m", "seed")
        self.runs = self.root / ".agentflow" / "runs"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _implement(self, request: InvocationRequest) -> InvocationResult:
        path = Path(request.metadata["worktree"]) / request.metadata["allowed_files"][0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("worker output\n", encoding="utf-8")
        return InvocationResult(
            f"fake:{request.request_key}", "implementation complete", 0, 0, 0, 0, 0,
            {"test_double": True},
        )

    def _approve(self, request: InvocationRequest) -> InvocationResult:
        return InvocationResult(
            f"fake:{request.request_key}",
            json.dumps({"approved": True, "findings": []}),
            0, 0, 0, 0, 0,
            {"test_double": True},
        )

    def _worker_router(self, worker: FakeAdapter) -> AdapterRouter:
        return AdapterRouter(
            {
                "fake": FakeAdapter(responder=self._approve),
                WORKER_PROVIDER: worker,
            }
        )

    def test_recovery_preserves_completed_output_without_reinvoke(self) -> None:
        database = Database(self.runs / "agentflow.db")
        database.initialize()
        workspace = GitWorkspace(self.root, self.runs)
        task = worker_task()
        plan = worker_plan(task)
        auth = issue_authorization(plan)
        worker = FakeAdapter(responder=self._implement)
        runner = Runner(database, self._worker_router(worker), workspace)
        with mock.patch.object(
            workspace, "verify_staging_inputs", side_effect=KeyboardInterrupt("crash")
        ):
            with self.assertRaises(KeyboardInterrupt):
                runner.start(plan, auth, run_id="recover")
        sandbox = Path(worker.invocations[0].metadata["worktree"])
        output_rel = task.allowed_files[0]
        self.assertTrue((sandbox / output_rel).is_file())
        self.assertEqual(1, len(worker.invocations))
        database.close()

        reopened = Database(self.runs / "agentflow.db")
        reopened.initialize()
        workspace2 = GitWorkspace(self.root, self.runs)
        worker2 = FakeAdapter(responder=self._implement)
        runner2 = Runner(reopened, self._worker_router(worker2), workspace2)
        result = runner2.resume("recover", plan, auth)
        self.assertEqual(RunState.COMPLETED, result.state)
        self.assertEqual(0, len(worker2.invocations))
        attempt = reopened.latest_attempt("recover", task.task_id)
        self.assertEqual("synced", reopened.staging_sync_state(attempt["attempt_id"])["state"])
        worktree = Path(reopened.run_snapshot("recover")["tasks"][0]["worktree_path"])
        self.assertEqual("worker output\n", (worktree / output_rel).read_text())
        self.assertEqual(
            TaskState.APPROVED, reopened.task_state("recover", task.task_id)
        )
        reopened.close()

    def test_local_to_remote_fallback_completed_before_sync_recovers(self) -> None:
        database = Database(self.runs / "agentflow.db")
        database.initialize()
        workspace = GitWorkspace(self.root, self.runs)
        task = replace(
            worker_task(),
            implementation_model=ModelRef(
                "local", "coder", "1", "local-family", True
            ),
            fallback_model=ModelRef(
                WORKER_PROVIDER, WORKER_MODEL, "2026-09", "worker-family", False
            ),
        )
        plan = worker_plan(
            task, allowed_provider_ids=("fake", WORKER_PROVIDER, "local")
        )
        auth = issue_authorization(plan)

        def local_unavailable(_request: InvocationRequest) -> InvocationResult:
            raise ModelUnavailableError("local preflight unavailable")

        local = FakeAdapter(responder=local_unavailable)
        remote = FakeAdapter(responder=self._implement)
        router = AdapterRouter(
            {
                "fake": FakeAdapter(responder=self._approve),
                "local": local,
                WORKER_PROVIDER: remote,
            }
        )
        runner = Runner(database, router, workspace)
        with mock.patch.object(
            workspace, "verify_staging_inputs", side_effect=KeyboardInterrupt("crash")
        ):
            with self.assertRaises(KeyboardInterrupt):
                runner.start(plan, auth, run_id="fallback-recover")
        output_rel = task.allowed_files[0]
        sandbox = Path(remote.invocations[0].metadata["worktree"])
        self.assertTrue((sandbox / output_rel).is_file())
        self.assertEqual(1, len(remote.invocations))
        self.assertEqual(1, len(local.invocations))
        database.close()

        reopened = Database(self.runs / "agentflow.db")
        reopened.initialize()
        workspace2 = GitWorkspace(self.root, self.runs)
        remote2 = FakeAdapter(responder=self._implement)
        local2 = FakeAdapter(responder=local_unavailable)
        router2 = AdapterRouter(
            {
                "fake": FakeAdapter(responder=self._approve),
                "local": local2,
                WORKER_PROVIDER: remote2,
            }
        )
        runner2 = Runner(reopened, router2, workspace2)
        result = runner2.resume("fallback-recover", plan, auth)
        self.assertEqual(RunState.COMPLETED, result.state)
        self.assertEqual(0, len(remote2.invocations))
        self.assertEqual(0, len(local2.invocations))
        attempt = reopened.latest_attempt("fallback-recover", task.task_id)
        self.assertEqual(
            "synced", reopened.staging_sync_state(attempt["attempt_id"])["state"]
        )
        worktree = Path(
            reopened.run_snapshot("fallback-recover")["tasks"][0]["worktree_path"]
        )
        self.assertEqual("worker output\n", (worktree / output_rel).read_text())
        self.assertEqual(
            TaskState.APPROVED, reopened.task_state("fallback-recover", task.task_id)
        )
        reopened.close()

    def test_recovery_missing_sandbox_pauses_diagnosably(self) -> None:
        database = Database(self.runs / "agentflow.db")
        database.initialize()
        workspace = GitWorkspace(self.root, self.runs)
        task = worker_task()
        plan = worker_plan(task)
        auth = issue_authorization(plan)
        worker = FakeAdapter(responder=self._implement)
        runner = Runner(database, self._worker_router(worker), workspace)
        with mock.patch.object(
            workspace, "verify_staging_inputs", side_effect=KeyboardInterrupt("crash")
        ):
            with self.assertRaises(KeyboardInterrupt):
                runner.start(plan, auth, run_id="recover-missing")
        attempt = database.latest_attempt("recover-missing", task.task_id)
        sandbox = workspace.staging_sandbox_path(
            "recover-missing", task.task_id, attempt["attempt_id"]
        )
        import shutil

        shutil.rmtree(sandbox)
        database.close()

        reopened = Database(self.runs / "agentflow.db")
        reopened.initialize()
        workspace2 = GitWorkspace(self.root, self.runs)
        runner2 = Runner(reopened, self._worker_router(FakeAdapter(responder=self._implement)), workspace2)
        result = runner2.resume("recover-missing", plan, auth)
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertTrue(
            reopened.pending_supervisor_checkpoints("recover-missing")
        )
        reopened.close()

    def _crash_start(self, run_id: str):
        database = Database(self.runs / "agentflow.db")
        database.initialize()
        workspace = GitWorkspace(self.root, self.runs)
        task = worker_task()
        plan = worker_plan(task)
        auth = issue_authorization(plan)
        worker = FakeAdapter(responder=self._implement)
        runner = Runner(database, self._worker_router(worker), workspace)
        with mock.patch.object(
            workspace, "verify_staging_inputs", side_effect=KeyboardInterrupt("crash")
        ):
            with self.assertRaises(KeyboardInterrupt):
                runner.start(plan, auth, run_id=run_id)
        return database, task, plan, auth

    def _write_owner_content(self, database, run_id: str, task) -> str:
        output_rel = task.allowed_files[0]
        worktree = Path(database.run_snapshot(run_id)["tasks"][0]["worktree_path"])
        (worktree / output_rel).parent.mkdir(parents=True, exist_ok=True)
        (worktree / output_rel).write_text("OWNER CONTENT\n", encoding="utf-8")
        return output_rel

    def _resume_and_assert_pause(
        self, run_id, database, task, plan, auth, output_rel
    ) -> None:
        database.close()
        reopened = Database(self.runs / "agentflow.db")
        reopened.initialize()
        worker2 = FakeAdapter(responder=self._implement)
        runner2 = Runner(
            reopened, self._worker_router(worker2), GitWorkspace(self.root, self.runs)
        )
        result = runner2.resume(run_id, plan, auth)
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(0, len(worker2.invocations))
        worktree = Path(reopened.run_snapshot(run_id)["tasks"][0]["worktree_path"])
        self.assertEqual("OWNER CONTENT\n", (worktree / output_rel).read_text())
        self.assertTrue(reopened.pending_supervisor_checkpoints(run_id))
        reopened.close()

    def test_recovery_null_baseline_pauses_without_overwriting_owner(self) -> None:
        database, task, plan, auth = self._crash_start("p1a-null")
        output_rel = self._write_owner_content(database, "p1a-null", task)
        attempt = database.latest_attempt("p1a-null", task.task_id)
        database.connection.execute(
            "UPDATE staging_syncs SET baseline_json = NULL WHERE attempt_id = ?",
            (attempt["attempt_id"],),
        )
        self._resume_and_assert_pause("p1a-null", database, task, plan, auth, output_rel)

    def test_recovery_invalid_json_baseline_pauses(self) -> None:
        database, task, plan, auth = self._crash_start("p1a-invalid-json")
        output_rel = self._write_owner_content(database, "p1a-invalid-json", task)
        attempt = database.latest_attempt("p1a-invalid-json", task.task_id)
        database.connection.execute(
            "UPDATE staging_syncs SET baseline_json = ? WHERE attempt_id = ?",
            ("{ not json", attempt["attempt_id"]),
        )
        self._resume_and_assert_pause(
            "p1a-invalid-json", database, task, plan, auth, output_rel
        )

    def test_recovery_empty_dict_baseline_pauses(self) -> None:
        database, task, plan, auth = self._crash_start("p1a-empty-dict")
        output_rel = self._write_owner_content(database, "p1a-empty-dict", task)
        attempt = database.latest_attempt("p1a-empty-dict", task.task_id)
        database.connection.execute(
            "UPDATE staging_syncs SET baseline_json = ? WHERE attempt_id = ?",
            ("{}", attempt["attempt_id"]),
        )
        self._resume_and_assert_pause(
            "p1a-empty-dict", database, task, plan, auth, output_rel
        )

    def test_recovery_missing_path_baseline_pauses(self) -> None:
        database, task, plan, auth = self._crash_start("p1a-missing-path")
        output_rel = self._write_owner_content(database, "p1a-missing-path", task)
        attempt = database.latest_attempt("p1a-missing-path", task.task_id)
        database.connection.execute(
            "UPDATE staging_syncs SET baseline_json = ? WHERE attempt_id = ?",
            (json.dumps({"unrelated.txt": {"exists": False}}), attempt["attempt_id"]),
        )
        self._resume_and_assert_pause(
            "p1a-missing-path", database, task, plan, auth, output_rel
        )

    def test_recovery_wrong_entry_type_baseline_pauses(self) -> None:
        database, task, plan, auth = self._crash_start("p1a-wrong-type")
        output_rel = self._write_owner_content(database, "p1a-wrong-type", task)
        attempt = database.latest_attempt("p1a-wrong-type", task.task_id)
        database.connection.execute(
            "UPDATE staging_syncs SET baseline_json = ? WHERE attempt_id = ?",
            (json.dumps({output_rel: "not-a-mapping"}), attempt["attempt_id"]),
        )
        self._resume_and_assert_pause(
            "p1a-wrong-type", database, task, plan, auth, output_rel
        )

    def test_recovery_missing_staging_sync_row_pauses(self) -> None:
        database, task, plan, auth = self._crash_start("p1a-missing-row")
        output_rel = self._write_owner_content(database, "p1a-missing-row", task)
        attempt = database.latest_attempt("p1a-missing-row", task.task_id)
        database.connection.execute(
            "DELETE FROM staging_syncs WHERE attempt_id = ?", (attempt["attempt_id"],)
        )
        self._resume_and_assert_pause(
            "p1a-missing-row", database, task, plan, auth, output_rel
        )

    def _crash_after_sync_into(self, db_path: Path, run_id: str):
        database = Database(db_path)
        database.initialize()
        workspace = GitWorkspace(self.root, self.runs)
        task = worker_task()
        plan = worker_plan(task)
        auth = issue_authorization(plan)
        worker = FakeAdapter(responder=self._implement)
        runner = Runner(database, self._worker_router(worker), workspace)
        real_done = database.record_staging_sync_done

        def crashing_done(*args, **kwargs):
            real_done(*args, **kwargs)
            raise KeyboardInterrupt("crash after sync")

        with mock.patch.object(
            database, "record_staging_sync_done", side_effect=crashing_done
        ):
            with self.assertRaises(KeyboardInterrupt):
                runner.start(plan, auth, run_id=run_id)
        return database, task, plan, auth

    def _crash_after_sync_done(self, run_id: str):
        return self._crash_after_sync_into(self.runs / "agentflow.db", run_id)

    def _synced_edge(
        self,
        run_id: str,
        corrupt=None,
        *,
        modify_output: bool = False,
    ):
        """Crash after a real sync, optionally corrupt the persisted records, resume.

        ``corrupt(real_manifest, real_synced_files)`` may return a
        ``(manifest_json, synced_files_json)`` pair (either value ``None`` leaves
        that record as written by the real sync). Each case uses its own database
        file so multiple runs never collide. Returns ``(result, reopened_db, task, worker2)``
        where ``worker2`` is the resume-time worker (re-invocation detector).
        """
        db_path = self.runs / f"{run_id}.db"
        database, task, plan, auth = self._crash_after_sync_into(db_path, run_id)
        attempt = database.latest_attempt(run_id, task.task_id)
        state = database.staging_sync_state(attempt["attempt_id"])
        new_manifest_json, new_synced_json = None, None
        if corrupt is not None:
            new_manifest_json, new_synced_json = corrupt(
                json.loads(state["manifest_json"]),
                json.loads(state["synced_files_json"]),
            )
        if modify_output:
            self._write_owner_content(database, run_id, task)
        sets, values = [], []
        if new_manifest_json is not None:
            sets.append("manifest_json = ?")
            values.append(new_manifest_json)
        if new_synced_json is not None:
            sets.append("synced_files_json = ?")
            values.append(new_synced_json)
        if sets:
            # SET placeholders bind the corrupted values first, then attempt_id last.
            database.connection.execute(
                f"UPDATE staging_syncs SET {', '.join(sets)} WHERE attempt_id = ?",
                values + [attempt["attempt_id"]],
            )
        database.close()

        reopened = Database(db_path)
        reopened.initialize()
        worker2 = FakeAdapter(responder=self._implement)
        runner2 = Runner(
            reopened, self._worker_router(worker2), GitWorkspace(self.root, self.runs)
        )
        result = runner2.resume(run_id, plan, auth)
        return result, reopened, task, worker2

    def _assert_safe_pause(self, run_id: str, result, reopened_db, worker2) -> None:
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(0, len(worker2.invocations))
        self.assertTrue(reopened_db.pending_supervisor_checkpoints(run_id))
        reopened_db.close()

    def test_synced_then_modified_worktree_pauses(self) -> None:
        database, task, plan, auth = self._crash_after_sync_done("p1b-modified")
        output_rel = task.allowed_files[0]
        worktree = Path(database.run_snapshot("p1b-modified")["tasks"][0]["worktree_path"])
        self.assertEqual("worker output\n", (worktree / output_rel).read_text())
        (worktree / output_rel).write_text("OWNER CONTENT\n", encoding="utf-8")
        database.close()

        reopened = Database(self.runs / "agentflow.db")
        reopened.initialize()
        worker2 = FakeAdapter(responder=self._implement)
        runner2 = Runner(
            reopened, self._worker_router(worker2), GitWorkspace(self.root, self.runs)
        )
        result = runner2.resume("p1b-modified", plan, auth)
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(0, len(worker2.invocations))
        worktree2 = Path(
            reopened.run_snapshot("p1b-modified")["tasks"][0]["worktree_path"]
        )
        self.assertEqual("OWNER CONTENT\n", (worktree2 / output_rel).read_text())
        self.assertTrue(reopened.pending_supervisor_checkpoints("p1b-modified"))
        reopened.close()

    def test_synced_unchanged_worktree_recovers_cleanly(self) -> None:
        database, task, plan, auth = self._crash_after_sync_done("p1b-ok")
        database.close()

        reopened = Database(self.runs / "agentflow.db")
        reopened.initialize()
        worker2 = FakeAdapter(responder=self._implement)
        runner2 = Runner(
            reopened, self._worker_router(worker2), GitWorkspace(self.root, self.runs)
        )
        result = runner2.resume("p1b-ok", plan, auth)
        self.assertEqual(RunState.COMPLETED, result.state)
        self.assertEqual(0, len(worker2.invocations))
        reopened.close()

    def test_synced_empty_manifest_pauses(self) -> None:
        database, task, plan, auth = self._crash_after_sync_done("p1c-empty-manifest")
        output_rel = task.allowed_files[0]
        attempt = database.latest_attempt("p1c-empty-manifest", task.task_id)
        state = database.staging_sync_state(attempt["attempt_id"])
        # sanity: the crash-after-sync state recorded a real synced path; we keep it.
        self.assertTrue(state["synced_files_json"])
        # Only the manifest for this attempt is blanked; synced_files_json stays intact.
        database.connection.execute(
            "UPDATE staging_syncs SET manifest_json = ? WHERE attempt_id = ?",
            ("{}", attempt["attempt_id"]),
        )
        self._write_owner_content(database, "p1c-empty-manifest", task)
        self._resume_and_assert_pause(
            "p1c-empty-manifest", database, task, plan, auth, output_rel
        )

    def test_synced_null_manifest_entry_pauses(self) -> None:
        database, task, plan, auth = self._crash_after_sync_done("p1c-null-entry")
        output_rel = task.allowed_files[0]
        attempt = database.latest_attempt("p1c-null-entry", task.task_id)
        self._write_owner_content(database, "p1c-null-entry", task)
        database.connection.execute(
            "UPDATE staging_syncs SET manifest_json = ? WHERE attempt_id = ?",
            (json.dumps({output_rel: None}), attempt["attempt_id"]),
        )
        self._resume_and_assert_pause("p1c-null-entry", database, task, plan, auth, output_rel)

    def _entry(self, real_manifest, rel: str, **overrides):
        entry = dict(next(iter(real_manifest.values())))
        entry.update(overrides)
        return {rel: entry}

    def test_synced_manifest_missing_or_extra_path_pauses(self) -> None:
        rel = "outputs/remote-impl.txt"

        def case(name, corrupt, run_id):
            with self.subTest(name=name):
                result, db, task, worker2 = self._synced_edge(run_id, corrupt)
                self._assert_safe_pause(run_id, result, db, worker2)

        # manifest has no entry for a recorded synced path (the one allowed file is dropped)
        case("missing-synced-path", lambda m, s: (json.dumps({}), None), "p1d-missing")
        # manifest carries a path that was never synced (key-set larger than synced)
        case(
            "extra-not-synced-path",
            lambda m, s: (json.dumps({**m, "outputs/ghost.txt": dict(next(iter(m.values())))}), None),
            "p1d-extra-notsynced",
        )
        # manifest and synced both name a path outside the allowed write scope
        case(
            "disallowed-path",
            lambda m, s: (json.dumps({**m, "outputs/nope.txt": dict(next(iter(m.values())))}),
                          json.dumps(list(s) + ["outputs/nope.txt"])),
            "p1d-disallowed",
        )

    def test_synced_manifest_bad_entry_type_pauses(self) -> None:
        rel = "outputs/remote-impl.txt"
        cases = {
            "entry-null": lambda m, s: (json.dumps({rel: None}), None),
            "entry-list": lambda m, s: (json.dumps({rel: [1, 2]}), None),
            "entry-string": lambda m, s: (json.dumps({rel: "not-a-mapping"}), None),
        }
        for name, corrupt in cases.items():
            run_id = f"p1d-{name}"
            with self.subTest(name=name):
                result, db, task, worker2 = self._synced_edge(run_id, corrupt)
                self._assert_safe_pause(run_id, result, db, worker2)

    def test_synced_manifest_bad_sha256_pauses(self) -> None:
        rel = "outputs/remote-impl.txt"

        def case(name, corrupt):
            run_id = f"p1d-{name}"
            with self.subTest(name=name):
                result, db, task, worker2 = self._synced_edge(run_id, corrupt)
                self._assert_safe_pause(run_id, result, db, worker2)

        case("sha-missing", lambda m, s: (json.dumps({rel: {k: v for k, v in next(iter(m.values())).items() if k != "sha256"}}), None))
        case("sha-wrong-type", lambda m, s: (json.dumps(self._entry(m, rel, sha256=12345)), None))
        case(
            "sha-short",
            lambda m, s: (json.dumps({rel: dict(list(m.values())[0], sha256=list(m.values())[0]["sha256"][:10])}), None),
        )

    def test_synced_manifest_bad_mode_pauses(self) -> None:
        rel = "outputs/remote-impl.txt"
        cases = {
            "mode-bool": lambda m, s: (json.dumps(self._entry(m, rel, mode=True)), None),
            "mode-string": lambda m, s: (json.dumps(self._entry(m, rel, mode="0o644")), None),
        }
        for name, corrupt in cases.items():
            run_id = f"p1d-{name}"
            with self.subTest(name=name):
                result, db, task, worker2 = self._synced_edge(run_id, corrupt)
                self._assert_safe_pause(run_id, result, db, worker2)

    def test_synced_bad_synced_files_pauses(self) -> None:
        rel = "outputs/remote-impl.txt"
        cases = {
            # invalid JSON: rejected while parsing the persisted list
            "bad-json": lambda m, s: (None, "{ not valid json"),
            # parses to a non-list mapping: rejected as the wrong type
            "not-a-list": lambda m, s: (None, json.dumps({"path": rel})),
            # duplicate synced path: rejected as an inconsistent list
            "duplicate-path": lambda m, s: (None, json.dumps([rel, rel])),
        }
        for name, corrupt in cases.items():
            run_id = f"p1d-{name}"
            with self.subTest(name=name):
                result, db, task, worker2 = self._synced_edge(run_id, corrupt)
                self._assert_safe_pause(run_id, result, db, worker2)

    def test_synced_legal_manifest_outcomes(self) -> None:
        # A structurally valid manifest with an unchanged output recovers and completes;
        # the same manifest over a modified owner file pauses without re-invoking.
        result, db, task, worker2 = self._synced_edge("p1d-legal-ok")
        self.assertEqual(RunState.COMPLETED, result.state)
        self.assertEqual(0, len(worker2.invocations))
        db.close()

        rel = task.allowed_files[0]
        result2, db2, _task, worker3 = self._synced_edge("p1d-legal-modified", modify_output=True)
        self.assertEqual(RunState.PAUSED, result2.state)
        self.assertEqual(0, len(worker3.invocations))
        worktree = Path(db2.run_snapshot("p1d-legal-modified")["tasks"][0]["worktree_path"])
        self.assertEqual("OWNER CONTENT\n", (worktree / rel).read_text())
        db2.close()

    def test_synced_symlinked_parent_dir_pauses_without_external_read(self) -> None:
        """A synced output whose parent dir becomes an external symlink must pause.

        Recovery must reject the unsafe ancestor *before* reading the target, so it
        never follows a parent symlink to read an out-of-worktree file. The external
        copy keeps the original bytes and mode so this is purely a path-boundary
        test (not a hash/mode drift).
        """
        run_id = "p1e-symlink-parent"
        database, task, plan, auth = self._crash_after_sync_done(run_id)
        output_rel = task.allowed_files[0]  # e.g. "outputs/remote-impl.txt"
        worktree = Path(database.run_snapshot(run_id)["tasks"][0]["worktree_path"])
        original_file = worktree / output_rel
        original_bytes = original_file.read_bytes()
        original_mode = stat.S_IMODE(original_file.lstat().st_mode)
        parent_dir = worktree / output_rel.split("/", 1)[0]  # the output's parent dir

        # External store OUTSIDE the worktree holding a byte/mode-identical copy.
        external_dir = self.root / f"external-store-{run_id}"
        external_dir.mkdir(parents=True, exist_ok=True)
        external_file = external_dir / output_rel.rsplit("/", 1)[-1]
        external_file.write_bytes(original_bytes)
        os.chmod(external_file, original_mode)

        # Replace the output's parent directory with a symlink to that external store.
        self.assertTrue(parent_dir.is_dir() and not parent_dir.is_symlink())
        shutil.rmtree(parent_dir)
        os.symlink(external_dir, parent_dir)
        self.assertTrue(parent_dir.is_symlink())

        database.close()

        reopened = Database(self.runs / "agentflow.db")
        reopened.initialize()
        worker2 = FakeAdapter(responder=self._implement)
        runner2 = Runner(
            reopened, self._worker_router(worker2), GitWorkspace(self.root, self.runs)
        )

        # Targeted: _output_target_baseline must never read the target through the
        # symlinked ancestor. Spy records every attempted read and delegates to the
        # real implementation so pre-fix behaviour is still observable.
        original_baseline = GitWorkspace._output_target_baseline
        baseline_reads: list[Path] = []

        def spy(source):
            baseline_reads.append(Path(source))
            return original_baseline(source)

        with mock.patch.object(
            GitWorkspace, "_output_target_baseline", new=staticmethod(spy)
        ):
            result = runner2.resume(run_id, plan, auth)

        self.assertEqual(RunState.PAUSED, result.state)
        # No Worker re-invocation and no advance past recovery (no test/reviewer run).
        self.assertEqual(0, len(worker2.invocations))
        self.assertIsNone(reopened.latest_test(run_id, task.task_id))
        self.assertEqual(TaskState.RUNNING, reopened.task_state(run_id, task.task_id))
        # A staging_sync_conflict pause checkpoint is recorded.
        self.assertTrue(reopened.pending_supervisor_checkpoints(run_id))
        checkpoint_raw = reopened.run_snapshot(run_id)["run"]["checkpoint_json"]
        self.assertIn("staging_sync_conflict", checkpoint_raw)
        # The unsafe target was never read via _output_target_baseline.
        self.assertEqual(0, len(baseline_reads),
                         "_output_target_baseline must not read a symlinked-ancestor target")
        # The external file is untouched (same bytes and permission bits).
        self.assertEqual(original_bytes, external_file.read_bytes())
        self.assertEqual(
            original_mode, stat.S_IMODE(external_file.lstat().st_mode)
        )
        reopened.close()


if __name__ == "__main__":
    unittest.main()
