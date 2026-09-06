"""Review independence and revision-evidence regression tests."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agentflow.adapters import ModelUnavailableError
from agentflow.authorization import issue_authorization
from agentflow.contracts import (
    BusinessImportance,
    InvocationRequest,
    InvocationResult,
    ModelRef,
    ReviewAcceptancePolicy,
)
from agentflow.database import Database
from agentflow.fake_adapter import FakeAdapter
from agentflow.runner import Runner
from agentflow.states import RunState
from agentflow.workspace import GitWorkspace

from test_runner import approved_response, make_plan, make_task


def git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True)


class ReviewIndependenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.email", "tests@example.invalid")
        git(self.root, "config", "user.name", "AgentFlow Tests")
        (self.root / "seed.txt").write_text("seed\n", encoding="utf-8")
        git(self.root, "add", "seed.txt")
        git(self.root, "commit", "-m", "seed")
        runs = self.root / ".agentflow" / "runs"
        self.database = Database(runs / "agentflow.db")
        self.database.initialize()
        self.workspace = GitWorkspace(self.root, runs)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def test_fallback_model_cannot_self_review(self) -> None:
        task = replace(make_task("one"), fallback_model=make_task("one").review_model)
        plan = make_plan(tasks=(task,))

        def response(request: InvocationRequest) -> InvocationResult:
            if request.model.model_id == "coder":
                raise ModelUnavailableError("fixture preflight unavailable")
            return approved_response(request)

        adapter = FakeAdapter(responder=response)
        result = Runner(self.database, adapter, self.workspace).start(
            plan, issue_authorization(plan), run_id="fallback-self-review"
        )
        self.assertEqual(RunState.PAUSED, result.state)
        review_calls = [
            request for request in adapter.invocations if request.role == "review"
        ]
        self.assertEqual([], review_calls)

    def test_revision_prompt_carries_unresolved_findings(self) -> None:
        marker = "REVIEW_FIX_SENTINEL_42"
        task = replace(
            make_task("one"),
            review_acceptance_policy=ReviewAcceptancePolicy.ZERO_FINDINGS,
        )
        plan = make_plan(tasks=(task,))

        def response(request: InvocationRequest) -> InvocationResult:
            if request.role == "review":
                return InvocationResult(
                    "review-1",
                    json.dumps(
                        dict(
                            approved=False,
                            findings=[
                                dict(
                                    severity="P2",
                                    title=marker,
                                    explanation="fix the value",
                                    remediation="replace wrong with right",
                                )
                            ],
                        )
                    ),
                    0, 0, 0, 0, 0,
                )
            return approved_response(request)

        adapter = FakeAdapter(responder=response)
        result = Runner(self.database, adapter, self.workspace).start(
            plan, issue_authorization(plan), run_id="revision-findings"
        )
        self.assertEqual(RunState.COMPLETED, result.state)
        revision_calls = [
            request for request in adapter.invocations if request.role == "revision"
        ]
        self.assertEqual(1, len(revision_calls))
        self.assertIn(marker, revision_calls[0].prompt)

    def test_important_task_rejects_same_family_contributor(self) -> None:
        task = replace(
            make_task("one", importance=BusinessImportance.IMPORTANT),
            fallback_model=ModelRef("fake", "reviewer", "1", "coder-family", True),
        )
        plan = make_plan(tasks=(task,))

        def response(request: InvocationRequest) -> InvocationResult:
            if request.model.model_id == "coder":
                raise ModelUnavailableError("fixture preflight unavailable")
            return approved_response(request)

        adapter = FakeAdapter(responder=response)
        result = Runner(self.database, adapter, self.workspace).start(
            plan, issue_authorization(plan), run_id="family-self-review"
        )
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(
            [],
            [r for r in adapter.invocations if r.role == "review"],
        )


if __name__ == "__main__":
    unittest.main()
