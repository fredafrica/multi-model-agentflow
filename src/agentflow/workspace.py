"""Git worktree isolation and post-execution file boundary checks."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .contracts import InputArtifact


class GitWorkspaceError(RuntimeError):
    pass


class InputArtifactError(RuntimeError):
    """An explicit input artifact is missing, inconsistent, or unsafe."""


class StagingSyncError(RuntimeError):
    """The remote worker staging sandbox cannot be safely synchronized."""

    def __init__(self, message: str, *, unrecovered_paths: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.unrecovered_paths = tuple(unrecovered_paths)


# Reserved control file written into every staging sandbox. Business files must
# never silently collide with it, so it is reserved against both input
# artifacts and allowed files at sandbox creation time.
BRIEFING_RELATIVE = ".agentflow-briefing.md"

_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    if not cleaned:
        raise ValueError("worktree identifier has no safe characters")
    if cleaned != value:
        # A sanitized identifier can collide with a different raw identifier
        # (e.g. "a b" and "a-b"). Disambiguate with a short content hash so two
        # distinct run/task identifiers never map to the same worktree path.
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        cleaned = f"{cleaned}-{digest}"
    return cleaned


def _atomic_write_bytes(destination: Path, content: bytes) -> None:
    """Atomically write ``content`` to ``destination`` without following links.

    The temporary file is created exclusively with a random name in the same
    directory (``mkstemp``), so a pre-created symlink at a predictable name
    cannot redirect the write. The existing file's permission bits are applied
    to the temporary file before the atomic ``os.replace``. On any failure the
    temporary file is removed and the destination is left untouched.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=str(destination.parent),
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            try:
                mode = stat.S_IMODE(destination.lstat().st_mode)
            except FileNotFoundError:
                mode = None
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _relative_parts(relative: str) -> tuple[str, ...]:
    """Split a project-relative POSIX path, rejecting escapes and empties."""
    if "\x00" in relative:
        raise StagingSyncError(f"unsafe path: {relative!r}")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise StagingSyncError(f"unsafe path: {relative!r}")
    parts = tuple(part for part in pure.parts if part not in ("", "."))
    if not parts:
        raise StagingSyncError(f"unsafe path: {relative!r}")
    return parts


def _canonical_relative(relative: str) -> str:
    """Return the canonical ``/``-joined form of a project-relative path."""
    return "/".join(_relative_parts(relative))


def _require_safe_ancestors(root: Path, parts: tuple[str, ...]) -> None:
    """Reject symlink, non-directory, or anomalous components along a path.

    Every existing component from ``root`` down to the leaf is inspected with
    ``lstat`` (never following symlinks). Any symlink or non-directory ancestor
    is rejected so a staging sync can neither read nor write outside the root.
    Components that do not exist yet are safe to create and end the walk.
    """
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            raise StagingSyncError(
                f"symlink component in path: {'/'.join(parts[: index + 1])}"
            )
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise StagingSyncError(
                f"non-directory component in path: {'/'.join(parts[: index + 1])}"
            )


def _require_safe_input_ancestors(root: Path, parts: tuple[str, ...]) -> None:
    """Reject any symlink or non-directory ancestor of an input artifact.

    Unlike ``_require_safe_ancestors`` this is fail-closed on a missing
    component (an input artifact must exist) and raises
    :class:`InputArtifactError` so the caller can distinguish an unsafe input
    from a staging-sync problem.
    """
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise InputArtifactError(
                f"input artifact path is missing: {'/'.join(parts[: index + 1])}"
            ) from error
        except OSError as error:
            raise InputArtifactError(
                f"input artifact path is unreadable: {'/'.join(parts[: index + 1])}"
            ) from error
        if stat.S_ISLNK(info.st_mode):
            raise InputArtifactError(
                f"input artifact path traverses a symlink: {'/'.join(parts[: index + 1])}"
            )
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise InputArtifactError(
                f"input artifact path traverses a non-directory: {'/'.join(parts[: index + 1])}"
            )


def _as_text(value: str | bytes | None) -> str:
    """Decode subprocess output to text, tolerating a bytes ``TimeoutExpired``."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


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
        return tuple(
            sorted(set(filter(None, tracked.split("\0"))) | set(self.untracked_files(root)))
        )

    def untracked_files(self, worktree: str | Path) -> tuple[str, ...]:
        output = self._git(
            "ls-files", "--others", "--exclude-standard", "-z", cwd=Path(worktree)
        ).stdout
        return tuple(sorted(filter(None, output.split("\0"))))

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
        additions: list[str] = []
        for relative in self.untracked_files(root):
            path = root / relative
            if path.is_file():
                content = path.read_text(encoding="utf-8", errors="replace")[:100_000]
                additions.append(f"--- /dev/null\n+++ b/{relative}\n{content}")
        return diff + "\n".join(additions)

    def untracked_whitespace_errors(self, worktree: str | Path) -> tuple[str, ...]:
        """Apply explicit default whitespace checks to untracked text files."""
        root = Path(worktree)
        errors: list[str] = []
        for relative in self.untracked_files(root):
            path = root / relative
            if not path.is_file():
                continue
            content = path.read_bytes()
            if b"\0" in content:
                continue
            lines = content.splitlines()
            for line_number, line in enumerate(lines, 1):
                if line.endswith((b" ", b"\t")):
                    errors.append(f"{relative}:{line_number}: trailing whitespace")
                if re.match(rb"^ +\t", line):
                    errors.append(f"{relative}:{line_number}: space before tab in indent")
            if lines and lines[-1].strip() == b"":
                errors.append(f"{relative}:{len(lines)}: new blank line at EOF")
        return tuple(errors)

    def input_artifact_mismatches(
        self, worktree: str | Path, artifacts: tuple[InputArtifact, ...]
    ) -> tuple[str, ...]:
        """Verify explicit input artifacts exist and match their SHA-256."""
        root = Path(worktree)
        mismatches: list[str] = []
        for artifact in artifacts:
            path = root / artifact.path
            if not path.is_file():
                mismatches.append(f"{artifact.path}: missing")
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != artifact.sha256:
                mismatches.append(f"{artifact.path}: hash mismatch")
        return tuple(mismatches)

    @staticmethod
    def _output_target_baseline(source: Path) -> dict[str, object]:
        """Capture a worktree output-target baseline without following symlinks.

        Returns ``{"exists": False}`` for a path that does not exist, and
        ``{"exists": True, "sha256": ..., "mode": ...}`` for a regular file.
        Non-regular entries are recorded by type so the sync can detect an
        owner-side type change before overwriting anything.
        """
        try:
            info = source.lstat()
        except FileNotFoundError:
            return {"exists": False}
        if stat.S_ISLNK(info.st_mode):
            return {"exists": True, "type": "symlink"}
        if not stat.S_ISREG(info.st_mode):
            return {"exists": True, "type": "other"}
        return {
            "exists": True,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "mode": stat.S_IMODE(info.st_mode),
        }

    @staticmethod
    def _output_target_conflict(
        baseline: dict[str, object],
        current: dict[str, object],
        content: bytes,
    ) -> str | None:
        """Return a conflict description if the destination drifted, else None.

        A destination is safe to write only when it still matches the recorded
        baseline, or when it already equals exactly what this sync is about to
        write (an idempotent re-sync of our own bytes). Anything else means the
        owner changed the target while the worker ran and must not be
        overwritten silently.
        """
        content_hash = hashlib.sha256(content).hexdigest()
        if baseline["exists"]:
            if current["exists"] and current.get("type", "file") == "file":
                if current["sha256"] == baseline["sha256"]:
                    return None
                if current["sha256"] == content_hash:
                    return None
                return "modified since baseline"
            if not current["exists"]:
                return "deleted since baseline"
            return "type changed since baseline"
        if not current["exists"]:
            return None
        if current.get("type", "file") == "file" and current["sha256"] == content_hash:
            return None
        return "created since baseline"

    @staticmethod
    def validate_staging_baseline(
        baseline: object, allowed_files: tuple[str, ...]
    ) -> str | None:
        """Return a reason ``baseline`` is untrustworthy, or ``None`` when valid.

        A usable baseline is a non-empty mapping covering exactly the canonical
        allowed output paths, with each entry a mapping whose ``exists`` flag is
        a boolean and, when present, whose ``sha256``/``mode`` are a 64-hex
        digest and an integer. A ``NULL``/unparseable JSON/empty dict/missing
        path/wrong entry type all yield a stable reason code so recovery can
        pause instead of silently overwriting owner content.
        """
        if not isinstance(baseline, Mapping):
            return "staging_baseline_invalid"
        allowed = {_canonical_relative(item) for item in allowed_files}
        if not allowed:
            return None if not baseline else "staging_baseline_invalid"
        if set(baseline) != allowed:
            return "staging_baseline_incomplete"
        for entry in baseline.values():
            if not isinstance(entry, Mapping):
                return "staging_baseline_invalid"
            exists = entry.get("exists")
            if exists is False:
                continue
            if exists is not True:
                return "staging_baseline_invalid"
            digest = entry.get("sha256")
            if not isinstance(digest, str) or not _SHA256_HEX_RE.match(digest):
                return "staging_baseline_invalid"
            if not isinstance(entry.get("mode"), int):
                return "staging_baseline_invalid"
        return None

    def _snapshot_input_artifact(
        self, artifact: InputArtifact, sandbox: Path, attempt_id: str
    ) -> dict[str, object]:
        """Copy one verified input artifact from the project root into a sandbox.

        The source is read from ``project_root`` (not the worktree), so untracked
        inputs are supported. Every ancestor component is inspected with
        ``lstat`` (never following symlinks), so a symlinked parent is rejected
        even when ``resolve()`` would still land inside ``project_root``. Only a
        regular, non-symlink file that resolves inside ``project_root`` and
        matches its declared SHA-256 is accepted. The copy is atomic and the
        sandbox copy is made read-only.
        """
        parts = tuple(PurePosixPath(artifact.path).parts)
        _require_safe_input_ancestors(self.project_root, parts)
        source = self.project_root / artifact.path
        try:
            info = source.lstat()
        except FileNotFoundError as error:
            raise InputArtifactError(f"input artifact is missing: {artifact.path}") from error
        except OSError as error:
            raise InputArtifactError(f"input artifact is unreadable: {artifact.path}") from error
        if stat.S_ISLNK(info.st_mode):
            raise InputArtifactError(f"input artifact is a symlink: {artifact.path}")
        if not stat.S_ISREG(info.st_mode):
            raise InputArtifactError(f"input artifact is not a regular file: {artifact.path}")
        resolved = source.resolve()
        if not resolved.is_relative_to(self.project_root):
            raise InputArtifactError(f"input artifact escapes the project root: {artifact.path}")
        try:
            content = resolved.read_bytes()
        except OSError as error:
            raise InputArtifactError(f"input artifact is unreadable: {artifact.path}") from error
        digest = hashlib.sha256(content).hexdigest()
        if digest != artifact.sha256:
            raise InputArtifactError(f"input artifact hash mismatch: {artifact.path}")
        destination = sandbox / artifact.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(destination, content)
        os.chmod(destination, 0o444)
        return {
            "path": artifact.path,
            "sha256": digest,
            "size": len(content),
            "attempt_id": attempt_id,
        }

    def create_staging_sandbox(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt_id: str,
        worktree: str | Path,
        input_artifacts: tuple[InputArtifact, ...],
        allowed_files: tuple[str, ...],
        briefing: str,
    ) -> tuple[Path, tuple[dict[str, object], ...], dict[str, dict[str, object]]]:
        """Build a minimal staging sandbox for a remote worker invocation.

        The sandbox contains only the hash-verified read-only inputs, the
        allowed files that already exist in the worktree, and a small
        AgentFlow-generated briefing. It never contains ``.git``, environment
        files, or any other repository content.

        Returns ``(sandbox, snapshots, baseline)``. ``baseline`` maps each
        canonical allowed path to its output-target baseline captured at this
        moment (existence, content hash, type/permission). The caller must
        persist it in a worker-unforgeable control-plane record so the later
        sync can detect owner-side changes instead of silently overwriting
        them.
        """
        canonical_allowed = tuple(_canonical_relative(item) for item in allowed_files)
        canonical_inputs = tuple(
            _canonical_relative(artifact.path) for artifact in input_artifacts
        )
        if BRIEFING_RELATIVE in canonical_allowed or BRIEFING_RELATIVE in canonical_inputs:
            raise StagingSyncError(
                f"reserved control path conflicts with task files: {BRIEFING_RELATIVE}"
            )
        sandbox = self.staging_sandbox_path(run_id, task_id, attempt_id)
        if sandbox.exists():
            shutil.rmtree(sandbox)
        sandbox.mkdir(parents=True, exist_ok=True)
        snapshots: list[dict[str, object]] = []
        for artifact in input_artifacts:
            snapshots.append(
                self._snapshot_input_artifact(artifact, sandbox, attempt_id)
            )
        baseline: dict[str, dict[str, object]] = {}
        for relative in canonical_allowed:
            parts = _relative_parts(relative)
            _require_safe_ancestors(Path(worktree), parts)
            source = Path(worktree).joinpath(*parts)
            try:
                info = source.lstat()
            except FileNotFoundError:
                baseline[relative] = {"exists": False}
                continue
            if stat.S_ISLNK(info.st_mode):
                raise StagingSyncError(f"allowed file is a symlink: {relative}")
            if not stat.S_ISREG(info.st_mode):
                raise StagingSyncError(f"allowed file is not a regular file: {relative}")
            baseline[relative] = self._output_target_baseline(source)
            destination = sandbox.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(destination, source.read_bytes())
        briefing_path = sandbox / BRIEFING_RELATIVE
        _atomic_write_bytes(briefing_path, briefing.encode("utf-8"))
        return sandbox, tuple(snapshots), baseline

    def staging_sandbox_path(self, run_id: str, task_id: str, attempt_id: str) -> Path:
        """Return the deterministic staging-sandbox path for an attempt.

        The sandbox identity is derived entirely from ``run_id``/``task_id``/
        ``attempt_id`` so a recovery path can locate and reuse the original
        snapshot without re-reading project inputs.
        """
        return (
            self.runs_root
            / _safe_component(run_id)
            / "staging"
            / _safe_component(task_id)
            / _safe_component(attempt_id)
        )

    def _verify_staging_inputs(self, root: Path, inputs: dict[str, str]) -> None:
        """Verify every input artifact is unchanged and reachable without
        traversing a symlinked ancestor."""
        for path, expected in inputs.items():
            parts = _relative_parts(path)
            _require_safe_ancestors(root, parts)
            candidate = root.joinpath(*parts)
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                raise StagingSyncError(f"input artifact is missing: {path}") from None
            if stat.S_ISLNK(info.st_mode):
                raise StagingSyncError(f"input artifact became a symlink: {path}")
            if not stat.S_ISREG(info.st_mode):
                raise StagingSyncError(f"input artifact is no longer a regular file: {path}")
            if hashlib.sha256(candidate.read_bytes()).hexdigest() != expected:
                raise StagingSyncError(f"input artifact was modified by the worker: {path}")

    def verify_staging_inputs(
        self, sandbox: str | Path, input_artifacts: tuple[InputArtifact, ...]
    ) -> None:
        """Re-verify a sandbox's input artifacts against their declared hashes.

        Called immediately after a worker returns (before outputs are synced) so
        a worker that tampered with a read-only input is detected as early as
        possible.
        """
        inputs = {
            _canonical_relative(artifact.path): artifact.sha256
            for artifact in input_artifacts
        }
        self._verify_staging_inputs(Path(sandbox), inputs)

    @staticmethod
    def _missing_parent_dirs(destination: Path) -> list[Path]:
        missing: list[Path] = []
        parent = destination.parent
        while not parent.exists():
            missing.append(parent)
            if parent.parent == parent:
                break
            parent = parent.parent
        return missing

    @staticmethod
    def _rollback_writes(
        written: list[tuple[Path, bool, bytes, int | None]], created_dirs: list[Path]
    ) -> tuple[str, ...]:
        """Undo committed writes, returning any paths that could not be restored."""
        unrecovered: list[str] = []
        for destination, existed, original, original_mode in reversed(written):
            try:
                if existed:
                    _atomic_write_bytes(destination, original)
                else:
                    destination.unlink()
            except OSError:
                unrecovered.append(destination.as_posix())
        for directory in sorted(
            {path for path in created_dirs}, key=lambda item: -len(item.parts)
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        return tuple(unrecovered)

    def sync_staging_outputs(
        self,
        sandbox: str | Path,
        worktree: str | Path,
        *,
        input_artifacts: tuple[InputArtifact, ...],
        allowed_files: tuple[str, ...],
        baseline: dict[str, dict[str, object]] | None = None,
    ) -> tuple[str, ...]:
        """Verify a staging sandbox and copy outputs back all-or-nothing.

        Rejects any file outside the input/output scope, any non-regular file
        (symlink, hardlink, device, socket, FIFO), any symlink or non-directory
        ancestor (source or destination), and any changed input. Outputs are
        staged in memory and committed as a batch; on any failure every already
        written destination is restored (bytes and permission bits), newly
        created files and empty parent directories are removed, and any staging
        exception is re-raised as :class:`StagingSyncError`.

        When ``baseline`` is provided, each output target is compared against
        the baseline captured at sandbox creation; a destination that was
        created, deleted, modified, or changed type since then aborts the whole
        batch with a diagnostic instead of overwriting the owner's changes. An
        output already equal to the exact bytes being written is treated as an
        idempotent re-sync, not a conflict. The baseline is required: a missing,
        empty, or structurally invalid baseline aborts the sync (fail-closed)
        rather than skipping conflict detection.

        Deletion is not performed: files a worker removes are simply not synced
        and the worktree copy is left untouched (conservative, no data loss).

        The rollback in this method is best-effort within a single process: it
        restores already-written destinations if an in-process exception
        interrupts the batch, but it is not crash-atomic across a process or
        machine failure.
        """
        root = Path(sandbox)
        target = Path(worktree)
        allowed = {_canonical_relative(item) for item in allowed_files}
        inputs = {
            _canonical_relative(artifact.path): artifact.sha256
            for artifact in input_artifacts
        }
        if baseline is None:
            raise StagingSyncError("staging sync requires a complete baseline")
        baseline_reason = self.validate_staging_baseline(baseline, allowed_files)
        if baseline_reason is not None:
            raise StagingSyncError(f"staging sync baseline is unusable: {baseline_reason}")
        try:
            self._verify_staging_inputs(root, inputs)
            # Enumerate every file in the sandbox, rejecting symlinked
            # directories and any out-of-scope or non-regular entry.
            produced: dict[str, Path] = {}
            for base, dirs, files in os.walk(root, followlinks=False):
                for name in dirs:
                    full = Path(base) / name
                    if full.is_symlink():
                        raise StagingSyncError(
                            f"worker produced a symlinked directory: "
                            f"{full.relative_to(root).as_posix()}"
                        )
                for name in files:
                    full = Path(base) / name
                    relative = full.relative_to(root).as_posix()
                    if relative in inputs or relative == BRIEFING_RELATIVE:
                        continue
                    info = full.lstat()
                    if stat.S_ISLNK(info.st_mode):
                        raise StagingSyncError(f"worker produced a symlink: {relative}")
                    if not stat.S_ISREG(info.st_mode):
                        raise StagingSyncError(f"worker produced a non-regular file: {relative}")
                    if info.st_nlink > 1:
                        raise StagingSyncError(f"worker produced a hard link: {relative}")
                    if relative not in allowed:
                        raise StagingSyncError(f"worker produced a file outside scope: {relative}")
                    produced[relative] = full
            # Stage phase: read every output and validate every destination
            # ancestor before touching the worktree.
            staged: list[tuple[str, Path, bytes]] = []
            for relative, source in produced.items():
                destination = target.joinpath(*_relative_parts(relative))
                _require_safe_ancestors(target, _relative_parts(relative))
                content = source.read_bytes()
                current = self._output_target_baseline(destination)
                if current["exists"]:
                    current_type = current.get("type", "file")
                    if current_type == "symlink":
                        raise StagingSyncError(f"output destination is a symlink: {relative}")
                    if current_type != "file":
                        raise StagingSyncError(
                            f"output destination is not a regular file: {relative}"
                        )
                entry = baseline[relative]
                conflict = self._output_target_conflict(entry, current, content)
                if conflict is not None:
                    raise StagingSyncError(
                        f"output destination changed since baseline "
                        f"({conflict}): {relative}"
                    )
                staged.append((relative, destination, content))
            # Commit phase with rollback.
            written: list[tuple[Path, bool, bytes, int | None]] = []
            created_dirs: list[Path] = []
            try:
                for _relative, destination, content in staged:
                    try:
                        info = destination.lstat()
                    except FileNotFoundError:
                        info = None
                    existed = info is not None
                    original = destination.read_bytes() if existed else b""
                    original_mode = stat.S_IMODE(info.st_mode) if existed else None
                    for directory in self._missing_parent_dirs(destination):
                        if directory not in created_dirs:
                            created_dirs.append(directory)
                    written.append((destination, existed, original, original_mode))
                    _atomic_write_bytes(destination, content)
            except Exception as error:
                unrecovered = self._rollback_writes(written, created_dirs)
                message = f"staging sync failed and could not be rolled back: {unrecovered}" \
                    if unrecovered else "staging sync failed"
                if isinstance(error, StagingSyncError) and not unrecovered:
                    raise
                raise StagingSyncError(
                    message, unrecovered_paths=unrecovered
                ) from error
            return tuple(sorted(produced))
        except StagingSyncError:
            raise
        except Exception as error:
            raise StagingSyncError("staging sync failed") from error

    def staging_output_manifest(
        self, worktree: str | Path, produced: tuple[str, ...]
    ) -> dict[str, dict[str, object]]:
        """Snapshot the control-plane view of the outputs just committed.

        Returns ``{relative: {exists, sha256, mode}}`` for each produced path as
        written to the worktree. This is the durable manifest a later recovery
        verifies against. It is derived from the worktree the control plane just
        wrote, never from the worker sandbox (which may have changed after the
        call), so a tampered sandbox cannot fabricate recovery expectations.
        """
        target = Path(worktree)
        manifest: dict[str, dict[str, object]] = {}
        for relative in produced:
            destination = target.joinpath(*_relative_parts(relative))
            entry = self._output_target_baseline(destination)
            if not entry.get("exists"):
                raise StagingSyncError(f"synced output missing after write: {relative}")
            if entry.get("type", "file") != "file":
                raise StagingSyncError(
                    f"synced output is not a regular file: {relative}"
                )
            manifest[relative] = entry
        return manifest

    @staticmethod
    def validate_synced_manifest(
        manifest: object,
        synced_files: object,
        allowed_files: tuple[str, ...],
    ) -> str | None:
        """Return a reason the synced manifest is structurally untrustworthy, or ``None``.

        A sync persists two parallel records: the exact list of paths copied back
        (``synced_files``) and a manifest mapping each of those paths to its
        post-write ``{exists, sha256, mode}`` snapshot. Recovery may trust the
        manifest only when it is consistent with both: exactly the same set of
        paths, every path inside the task's canonical allowed scope (with no
        duplicates or unsafe forms), and each entry a well-formed mapping whose
        hash/mode/existence agree with a synced regular file. ``allowed_files`` is
        the write scope, not the sync target: a subset of it may be produced, so we
        check against ``synced_files``, never require full coverage, and only call a
        blank manifest invalid when a non-empty sync was actually recorded. A sync
        with no outputs legitimately leaves an empty pair. Any mismatch (a blank
        manifest over recorded paths, a missing/extra path, a non-mapping entry, or
        a bad hash/mode) yields a stable pause reason instead of a silent success.
        """
        if not isinstance(manifest, dict):
            return "staging_manifest_invalid"
        if not isinstance(synced_files, list):
            return "staging_manifest_invalid"

        allowed: set[str] = set()
        for item in allowed_files:
            try:
                allowed.add(_canonical_relative(item))
            except StagingSyncError:
                continue

        synced_canonical: set[str] = set()
        for item in synced_files:
            if not isinstance(item, str):
                return "staging_manifest_invalid"
            try:
                canonical = _canonical_relative(item)
            except StagingSyncError:
                return "staging_manifest_invalid"
            if canonical in synced_canonical:
                return "staging_manifest_invalid"
            if canonical not in allowed:
                return "staging_manifest_invalid"
            synced_canonical.add(canonical)

        if set(manifest) != synced_canonical:
            return "staging_manifest_invalid"

        for entry in manifest.values():
            if not isinstance(entry, Mapping):
                return "staging_manifest_invalid"
            digest = entry.get("sha256")
            if not isinstance(digest, str) or not _SHA256_HEX_RE.match(digest):
                return "staging_manifest_invalid"
            mode = entry.get("mode")
            if isinstance(mode, bool) or not isinstance(mode, int):
                return "staging_manifest_invalid"
            if mode < 0:
                return "staging_manifest_invalid"
            if entry.get("exists") is not True:
                return "staging_manifest_invalid"
            if entry.get("type") in ("symlink", "other"):
                return "staging_manifest_invalid"
        return None

    def verify_synced_manifest(
        self, worktree: str | Path, manifest: dict[str, dict[str, object]]
    ) -> str | None:
        """Return a conflict reason if the worktree diverged from ``manifest``.

        A previously synced worktree must still match the persisted manifest
        (existence, regular-file type, content hash, and permission bits). Any
        divergence is reported instead of being passed through or overwritten.

        Every ancestor component is checked with ``lstat`` (never following
        symlinks) *before* the target is read, so a parent directory that has been
        replaced by an external symlink cannot be used to make this read (or any
        later check) touch a file outside the worktree. An unsafe ancestor raises
        :class:`StagingSyncError` for the caller to turn into a safe pause.
        """
        target = Path(worktree)
        for relative, expected in manifest.items():
            parts = _relative_parts(relative)
            _require_safe_ancestors(target, parts)
            destination = target.joinpath(*parts)
            current = self._output_target_baseline(destination)
            if not current["exists"]:
                return f"synced output deleted: {relative}"
            if current.get("type", "file") != "file":
                return f"synced output type changed: {relative}"
            if current["sha256"] != expected.get("sha256"):
                return f"synced output modified: {relative}"
            if current.get("mode") != expected.get("mode"):
                return f"synced output mode changed: {relative}"
        return None

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
                _as_text(error.stdout) or "",
                _as_text(error.stderr) or "test command timed out",
                int((time.monotonic() - started) * 1000),
            )
        return TestCommandResult(
            result.returncode == 0,
            result.returncode,
            _as_text(result.stdout),
            _as_text(result.stderr),
            int((time.monotonic() - started) * 1000),
        )
