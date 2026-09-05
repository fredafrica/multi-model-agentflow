from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from agentflow.adapters import InvocationIncompleteError
from agentflow.opencode_adapter import OpenCodeAdapter, parse_opencode_json


class OpenCodeAdapterTests(unittest.TestCase):
    def test_discovery_only_returns_loaded_llms(self) -> None:
        payload = [
            {
                "type": "llm",
                "modelKey": "qwen/model",
                "identifier": "qwen/model",
                "selectedVariant": "qwen/model@8bit",
                "status": "idle",
                "contextLength": 4096,
                "trainedForToolUse": True,
            },
            {"type": "embedding", "modelKey": "embed"},
        ]
        completed = subprocess.CompletedProcess(
            ("lms",), 0, stdout=json.dumps(payload), stderr=""
        )
        with mock.patch("subprocess.run", return_value=completed):
            records = OpenCodeAdapter(lms_command="lms").discover()
        self.assertEqual(1, len(records))
        self.assertEqual("lmstudio", records[0].ref.provider)
        self.assertEqual("qwen", records[0].ref.family)
        self.assertEqual(0, records[0].input_cost_per_million)

    def test_parser_collects_text_tokens_and_session(self) -> None:
        events = "\n".join(
            (
                json.dumps(
                    {
                        "type": "text",
                        "sessionID": "session-1",
                        "part": {"type": "text", "text": "hello"},
                    }
                ),
                json.dumps(
                    {
                        "type": "step_finish",
                        "part": {
                            "tokens": {"input": 10, "output": 4},
                            "cost": 0,
                        },
                    }
                ),
            )
        )
        result = parse_opencode_json(events, duration_ms=50)
        self.assertEqual("hello", result.output)
        self.assertEqual("session-1", result.provider_request_id)
        self.assertEqual(10, result.input_tokens)
        self.assertEqual(4, result.output_tokens)
        self.assertEqual(0, result.remote_cost)
        self.assertEqual(
            0,
            parse_opencode_json(events, duration_ms=50, is_local=True).remote_cost,
        )

    def test_parser_rejects_realistic_zero_exit_step_limit_stream(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "opencode_max_steps.jsonl"
        with self.assertRaises(InvocationIncompleteError) as raised:
            parse_opencode_json(
                fixture.read_text(encoding="utf-8"),
                duration_ms=50,
                is_local=False,
            )
        result = raised.exception.result
        self.assertEqual("step_limit_reached", raised.exception.failure_kind)
        self.assertEqual("session-step-limit", result.provider_request_id)
        self.assertEqual((17, 9), (result.input_tokens, result.output_tokens))
        self.assertEqual(0.125, result.remote_cost)
        self.assertEqual("final_text", result.raw_metadata["termination_source"])
        self.assertIn("responding with text only", result.output)
        self.assertIn("## Summary of work accomplished so far", result.output)

    def test_parser_prefers_structured_step_limit_signal(self) -> None:
        event = json.dumps(
            {
                "type": "error",
                "error": {"code": "max_steps_reached"},
                "tokens": {"input": 3, "output": 1},
            }
        )
        with self.assertRaises(InvocationIncompleteError) as raised:
            parse_opencode_json(event, duration_ms=1, is_local=True)
        self.assertEqual(
            "structured_event",
            raised.exception.result.raw_metadata["termination_source"],
        )

    def test_reviewer_json_may_quote_step_limit_in_explanation(self) -> None:
        review = json.dumps(
            {
                "approved": False,
                "findings": [
                    {
                        "severity": "P1",
                        "title": "Worker stopped early",
                        "explanation": (
                            "The maximum number of steps for this agent has been "
                            "reached."
                        ),
                    }
                ],
            }
        )
        event = json.dumps(
            {"type": "text", "part": {"type": "text", "text": review}}
        )
        result = parse_opencode_json(event, duration_ms=1, is_local=False)
        self.assertEqual(review, result.output)

    def test_reviewer_json_may_quote_step_limit_in_title_or_remediation(self) -> None:
        marker = "The maximum number of steps for this agent has been reached."
        for field in ("title", "remediation"):
            with self.subTest(field=field):
                finding = {
                    "severity": "P2",
                    "title": "Quoted diagnostic",
                    "explanation": "The review documents a prior failure.",
                    "remediation": "Keep the diagnostic as evidence.",
                }
                finding[field] = marker
                review = json.dumps({"approved": True, "findings": [finding]})
                event = json.dumps(
                    {"type": "text", "part": {"type": "text", "text": review}}
                )
                self.assertEqual(
                    review,
                    parse_opencode_json(
                        event, duration_ms=1, is_local=False
                    ).output,
                )

    def test_implementation_text_may_quote_step_limit_without_terminating(self) -> None:
        output = (
            "The test log said: The maximum number of steps for this agent has "
            "been reached. The implementation handled that error correctly."
        )
        event = json.dumps(
            {"type": "text", "part": {"type": "text", "text": output}}
        )
        self.assertEqual(
            output,
            parse_opencode_json(event, duration_ms=1, is_local=True).output,
        )

    def test_structured_step_limit_overrides_valid_reviewer_json(self) -> None:
        review = json.dumps({"approved": True, "findings": []})
        for reason in (
            "max_steps",
            "maximum_steps",
            "step_limit_reached",
            "steps_exhausted",
        ):
            with self.subTest(reason=reason):
                events = "\n".join(
                    (
                        json.dumps(
                            {
                                "type": "text",
                                "part": {"type": "text", "text": review},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "step_finish",
                                "part": {"finishReason": reason},
                            }
                        ),
                    )
                )
                with self.assertRaises(InvocationIncompleteError) as raised:
                    parse_opencode_json(events, duration_ms=1, is_local=False)
                self.assertEqual(review, raised.exception.result.output)
                self.assertEqual(
                    "structured_event",
                    raised.exception.result.raw_metadata["termination_source"],
                )

    def test_parser_recognizes_documented_step_limit_text_variants(self) -> None:
        variants = (
            "CRITICAL — MAXIMUM STEPS REACHED",
            "critical - maximum steps reached",
            "The maximum number of steps for this agent has been reached.",
            (
                "CRITICAL — MAXIMUM STEPS REACHED\n\n"
                "The maximum number of steps for this agent has been reached.\n"
                "Tools are disabled until the next user input."
            ),
        )
        for marker in variants:
            with self.subTest(marker=marker):
                event = json.dumps(
                    {"type": "text", "part": {"type": "text", "text": marker}}
                )
                with self.assertRaises(InvocationIncompleteError):
                    parse_opencode_json(event, duration_ms=1, is_local=True)

    def test_step_limit_reference_in_code_quote_or_diff_is_not_a_termination(self) -> None:
        marker = "The maximum number of steps for this agent has been reached."
        outputs = (
            f"```text\nCRITICAL — MAXIMUM STEPS REACHED\n{marker}\n```",
            f"> {marker}",
            f"+CRITICAL — MAXIMUM STEPS REACHED\n+{marker}",
        )
        for output in outputs:
            with self.subTest(output=output):
                event = json.dumps(
                    {"type": "text", "part": {"type": "text", "text": output}}
                )
                self.assertEqual(
                    output,
                    parse_opencode_json(
                        event, duration_ms=1, is_local=True
                    ).output,
                )

    def test_read_only_permissions_deny_edits_and_external_access(self) -> None:
        permission = OpenCodeAdapter._permission_config(read_only=True)["permission"]
        self.assertEqual("deny", permission["edit"])
        self.assertEqual("deny", permission["bash"])
        self.assertEqual("deny", permission["external_directory"])
        self.assertEqual(["lmstudio"], OpenCodeAdapter._permission_config(read_only=True)["enabled_providers"])
        self.assertEqual(
            permission,
            OpenCodeAdapter._permission_config(read_only=True)["agent"]["agentflow-sandbox"]["permission"],
        )

    def test_cancel_stops_local_process_group(self) -> None:
        process = subprocess.Popen(
            (sys.executable, "-c", "import time; time.sleep(60)"),
            start_new_session=True,
        )
        try:
            self.assertTrue(
                OpenCodeAdapter().cancel(f"local-process-group:{process.pid}")
            )
            process.wait(timeout=2)
            self.assertNotEqual(0, process.returncode)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == "__main__":
    unittest.main()
