"""Regression tests for abnormal cost values in OpenCode usage parsing (P1-C)."""

from __future__ import annotations

import json
import unittest

from agentflow.opencode_adapter import (
    parse_opencode_failed_usage,
    parse_opencode_json,
)


def _line(payload: dict) -> bytes:
    return (json.dumps(payload) + "\n").encode("utf-8")


class FailedUsageCostValidationTests(unittest.TestCase):
    def _failed(self, data: bytes):
        return parse_opencode_failed_usage(
            data, duration_ms=1, is_local=False, termination_reason="nonzero_exit"
        )

    def test_negative_cost_marks_unavailable_and_preserves_tokens(self) -> None:
        data = _line(
            {"type": "step_finish", "part": {"tokens": {"input": 17}, "cost": -1}}
        )
        result = self._failed(data)
        self.assertEqual(17, result.input_tokens)
        self.assertIsNone(result.remote_cost)
        self.assertTrue(result.cost_unavailable)
        self.assertTrue(result.raw_metadata["cost_invalid"])

    def test_nan_cost_marks_unavailable(self) -> None:
        data = _line(
            {
                "type": "step_finish",
                "part": {"tokens": {"input": 17}, "cost": float("nan")},
            }
        )
        result = self._failed(data)
        self.assertEqual(17, result.input_tokens)
        self.assertIsNone(result.remote_cost)
        self.assertTrue(result.cost_unavailable)

    def test_infinity_cost_marks_unavailable(self) -> None:
        data = _line(
            {
                "type": "step_finish",
                "part": {"tokens": {"input": 17}, "cost": float("inf")},
            }
        )
        result = self._failed(data)
        self.assertEqual(17, result.input_tokens)
        self.assertIsNone(result.remote_cost)
        self.assertTrue(result.cost_unavailable)

    def test_mixed_valid_and_invalid_cost_marks_unavailable(self) -> None:
        data = (
            json.dumps(
                {"type": "step_finish", "part": {"tokens": {"input": 5}, "cost": 0.5}}
            )
            + "\n"
            + json.dumps(
                {"type": "step_finish", "part": {"tokens": {"input": 12}, "cost": -1}}
            )
            + "\n"
        ).encode("utf-8")
        result = self._failed(data)
        self.assertEqual(17, result.input_tokens)
        self.assertIsNone(result.remote_cost)
        self.assertTrue(result.cost_unavailable)

    def test_overflow_accumulation_marks_unavailable(self) -> None:
        data = (
            json.dumps(
                {"type": "step_finish", "part": {"tokens": {"input": 1}, "cost": 1e308}}
            )
            + "\n"
            + json.dumps(
                {"type": "step_finish", "part": {"tokens": {"input": 2}, "cost": 1e308}}
            )
            + "\n"
        ).encode("utf-8")
        result = self._failed(data)
        self.assertEqual(3, result.input_tokens)
        self.assertIsNone(result.remote_cost)
        self.assertTrue(result.cost_unavailable)

    def test_string_and_bool_cost_are_rejected(self) -> None:
        for bad in ("0.5", True, False):
            data = _line(
                {"type": "step_finish", "part": {"tokens": {"input": 3}, "cost": bad}}
            )
            result = self._failed(data)
            self.assertEqual(3, result.input_tokens)
            self.assertIsNone(result.remote_cost)
            self.assertTrue(result.cost_unavailable)

    def test_valid_cost_still_confirmed(self) -> None:
        data = _line(
            {"type": "step_finish", "part": {"tokens": {"input": 17}, "cost": 0.125}}
        )
        result = self._failed(data)
        self.assertEqual(17, result.input_tokens)
        self.assertEqual(0.125, result.remote_cost)
        self.assertFalse(result.cost_unavailable)

    def test_zero_cost_still_confirmed(self) -> None:
        data = _line(
            {"type": "step_finish", "part": {"tokens": {"input": 17}, "cost": 0}}
        )
        result = self._failed(data)
        self.assertEqual(17, result.input_tokens)
        self.assertEqual(0.0, result.remote_cost)
        self.assertFalse(result.cost_unavailable)


class JsonSuccessCostValidationTests(unittest.TestCase):
    def test_success_path_with_invalid_cost_marks_unavailable(self) -> None:
        events = "\n".join(
            (
                json.dumps({"type": "text", "part": {"type": "text", "text": "done"}}),
                json.dumps(
                    {"type": "step_finish", "part": {"tokens": {"input": 17}, "cost": -1}}
                ),
            )
        )
        result = parse_opencode_json(events, duration_ms=1, is_local=False)
        self.assertEqual("done", result.output)
        self.assertEqual(17, result.input_tokens)
        self.assertIsNone(result.remote_cost)
        self.assertTrue(result.cost_unavailable)

    def test_success_path_with_valid_cost_still_confirmed(self) -> None:
        events = "\n".join(
            (
                json.dumps({"type": "text", "part": {"type": "text", "text": "done"}}),
                json.dumps(
                    {
                        "type": "step_finish",
                        "part": {"tokens": {"input": 17}, "cost": 0.25},
                    }
                ),
            )
        )
        result = parse_opencode_json(events, duration_ms=1, is_local=False)
        self.assertEqual("done", result.output)
        self.assertEqual(0.25, result.remote_cost)
        self.assertFalse(result.cost_unavailable)


if __name__ == "__main__":
    unittest.main()
