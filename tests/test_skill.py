from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "multi-model-agentflow" / "SKILL.md"
OPENAI_YAML = SKILL.parent / "agents" / "openai.yaml"


class SkillTests(unittest.TestCase):
    def test_implicit_use_cannot_start_a_model(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("It is never authorization to start a model", text)
        self.assertLess(text.index("plan authorize"), text.index("start <plan-id>"))
        self.assertIn("configured/discoverable status is not proof", text)
        self.assertIn("only for `review`/`rereview`", text)
        self.assertIn("real smoke test requires its own plan", text)
        self.assertIn("known failed call", text)
        self.assertIn("protocol-valid JSON object", text)
        self.assertIn("allow_implicit_invocation: true", OPENAI_YAML.read_text())

    def test_skill_has_no_permanent_model_default(self) -> None:
        text = SKILL.read_text(encoding="utf-8").lower()
        for fixed_name in ("qwen", "deepseek", "gpt-", "claude"):
            self.assertNotIn(fixed_name, text)


if __name__ == "__main__":
    unittest.main()
