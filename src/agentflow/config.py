"""Cross-platform, environment-overridable local paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentFlowPaths:
    project_root: Path
    project_policy: Path
    project_runs: Path
    user_config: Path
    user_data: Path


def resolve_paths(project_root: str | Path) -> AgentFlowPaths:
    root = Path(project_root).resolve()
    config_home = Path(
        os.environ.get(
            "AGENTFLOW_CONFIG_HOME",
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"),
        )
    )
    data_home = Path(
        os.environ.get(
            "AGENTFLOW_DATA_HOME",
            os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"),
        )
    )
    return AgentFlowPaths(
        project_root=root,
        project_policy=root / ".agentflow" / "project.toml",
        project_runs=root / ".agentflow" / "runs",
        user_config=config_home / "agentflow",
        user_data=data_home / "agentflow",
    )
