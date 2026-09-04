"""Git worktree isolation and post-execution file boundary checks."""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


class GitWorkspaceError(RuntimeError):
    pass


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    if not cleaned:
        raise ValueError("worktree identifier has no safe characters")
    return cleaned


@dataclass(frozen=True)
class TestCommandResult:
    passed: bool
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int


class GitWorkspace:
    def __init__(self, project_root: str | Path, runs_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.runs_root = Path(runs_root).resolve()

    def _git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ("git", *args),
            cwd=cwd or self.project_root,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise GitWorkspaceError(result.stderr.strip() or result.stdout.strip())
        return result

    def require_committed_base(self) -> None:
        self._git("rev-parse", "--verify", "HEAD")

    def create(self, run_id: str, task_id: str) -> Path:
        self.require_committed_base()
        safe_run = _safe_component(run_id)
        safe_task = _safe_component(task_id)
        path = self.runs_root / safe_run / "worktrees" / safe_task
        if (path / ".git").exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        branch = f"agentflow/{safe_run}/{safe_task}"
        self._git("worktree", "add", "-b", branch, str(path), "HEAD")
        return path

    def changed_files(self, worktree: str | Path) -> tuple[str, ...]:
        root = Path(worktree)
        tracked = self._git("diff", "HEAD", "--name-only", "-z", cwd=root).stdout
        untracked = self._git(
            "ls-files", "--others", "--exclude-standard", "-z", cwd=root
        ).stdout
        return tuple(sorted(set(filter(None, (tracked + untracked).split("\0")))))

    def status_snapshot(self, worktree: str | Path) -> str:
        root = Path(worktree)
        status = self._git("status", "--porcelain=v1", "-z", cwd=root).stdout
        hashes: list[str] = []
        for relative in self.changed_files(root):
            path = root / relative
            if path.is_file():
                hashes.append(f"{relative}:{hashlib.sha256(path.read_bytes()).hexdigest()}")
        return status + "\n" + "\n".join(hashes)

    def diff(self, worktree: str | Path) -> str:
        root = Path(worktree)
        diff = self._git("diff", "--no-ext-diff", "HEAD", cwd=root).stdout
        untracked = self._git(
            "ls-files", "--others", "--exclude-standard", "-z", cwd=root
        ).stdout.split("\0")
        additions: list[str] = []
        for relative in filter(None, untracked):
            path = root / relative
            if path.is_file():
                content = path.read_text(encoding="utf-8", errors="replace")[:100_000]
                additions.append(f"--- /dev/null\n+++ b/{relative}\n{content}")
        return diff + "\n".join(additions)

    def run_test(
        self, worktree: str | Path, command: tuple[str, ...], timeout_seconds: int = 120
    ) -> TestCommandResult:
        if not command:
            return TestCommandResult(True, 0, "No deterministic command configured", "", 0)
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                cwd=Path(worktree),
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            return TestCommandResult(
                False,
                124,
                error.stdout or "",
                error.stderr or "test command timed out",
                int((time.monotonic() - started) * 1000),
            )
        return TestCommandResult(
            result.returncode == 0,
            result.returncode,
            result.stdout,
            result.stderr,
            int((time.monotonic() - started) * 1000),
        )
