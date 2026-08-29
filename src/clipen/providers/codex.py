"""A bounded, non-interactive Codex CLI adapter.

The adapter deliberately knows nothing about projects or source-media paths. A
Controller must first materialize the exact approved inputs into an isolated
attempt workspace.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from typing import Mapping


_MAX_PROMPT_BYTES = 1_000_000
_MAX_CAPTURE_BYTES = 2_000_000
_MAX_RESULT_BYTES = 2_000_000
_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "ALL_PROXY",
        "CODEX_HOME",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "USER",
    }
)


class ProviderUnavailable(RuntimeError):
    """The selected provider cannot be launched on this machine."""


class ProviderProtocolError(RuntimeError):
    """The provider violated the expected one-shot result contract."""


class ProviderTimedOut(RuntimeError):
    """The provider exceeded the bounded attempt duration."""


def _bounded_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(f"{field} must be a non-empty bounded identifier")


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _isolated_workspace(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError("attempt workspace cannot be a symbolic link")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("attempt workspace must be a directory")
    return resolved


def _workspace_file(path: Path, workspace: Path, field: str) -> Path:
    if path.is_symlink():
        raise ValueError(f"{field} cannot be a symbolic link")
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or not _inside(resolved, workspace):
        raise ValueError(f"{field} must be a regular file inside the attempt workspace")
    return resolved


def _provider_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    inherited = os.environ if source is None else source
    environment = {key: value for key, value in inherited.items() if key in _ENVIRONMENT_ALLOWLIST}
    environment["CLICOLOR"] = "0"
    environment["NO_COLOR"] = "1"
    return environment


@dataclass(frozen=True)
class CodexAttempt:
    attempt_id: str
    workspace: Path
    output_schema: Path
    prompt: str
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        _bounded_identifier(self.attempt_id, "attempt_id")
        workspace = _isolated_workspace(self.workspace)
        schema = _workspace_file(self.output_schema, workspace, "output_schema")
        try:
            parsed_schema = json.loads(schema.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("output_schema must contain valid UTF-8 JSON") from error
        if not isinstance(parsed_schema, dict):
            raise ValueError("output_schema must contain a JSON object")
        prompt_bytes = self.prompt.encode("utf-8")
        if not self.prompt.strip() or len(prompt_bytes) > _MAX_PROMPT_BYTES:
            raise ValueError("prompt must be non-empty and at most 1,000,000 UTF-8 bytes")
        if not 0 < self.timeout_seconds <= 3600:
            raise ValueError("timeout_seconds must be between zero and 3600")


@dataclass(frozen=True)
class CodexAttemptResult:
    attempt_id: str
    payload: object
    event_types: tuple[str, ...]
    elapsed_seconds: float


class CodexExecAdapter:
    """Run one ephemeral Codex attempt without loading project configuration."""

    def __init__(
        self,
        attempt_root: Path,
        binary: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._attempt_root = _isolated_workspace(attempt_root)
        selected = str(binary) if binary is not None else shutil.which("codex")
        if not selected:
            raise ProviderUnavailable("Codex CLI is not installed or not on PATH")
        candidate = Path(selected)
        if candidate.is_symlink():
            candidate = candidate.resolve(strict=True)
        else:
            candidate = candidate.resolve(strict=True)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ProviderUnavailable("Codex CLI is not an executable regular file")
        self._binary = candidate
        self._environment = _provider_environment(environment)

    @property
    def binary(self) -> Path:
        return self._binary

    def version(self) -> str:
        completed = subprocess.run(
            [str(self._binary), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._environment,
            check=False,
            timeout=10,
        )
        if completed.returncode != 0:
            raise ProviderUnavailable("Codex CLI version probe failed")
        version = completed.stdout.decode("utf-8", errors="replace").strip()
        if not version or len(version) > 256:
            raise ProviderProtocolError("Codex CLI returned an invalid version string")
        return version

    def command(self, attempt: CodexAttempt, output_path: Path) -> tuple[str, ...]:
        workspace = _isolated_workspace(attempt.workspace)
        if workspace == self._attempt_root or not _inside(workspace, self._attempt_root):
            raise ValueError("attempt workspace must be a child of the managed attempt root")
        schema = _workspace_file(attempt.output_schema, workspace, "output_schema")
        if output_path.is_symlink():
            raise ValueError("provider output cannot be a symbolic link")
        resolved_output = output_path.resolve(strict=False)
        if not _inside(resolved_output, workspace) or resolved_output.exists():
            raise ValueError("provider output must be a new file inside the attempt workspace")
        return (
            str(self._binary),
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--json",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(resolved_output),
            "--cd",
            str(workspace),
            "-",
        )

    def execute(self, attempt: CodexAttempt) -> CodexAttemptResult:
        workspace = _isolated_workspace(attempt.workspace)
        output_path = workspace / f"provider-output-{attempt.attempt_id}.json"
        command = self.command(attempt, output_path)
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=self._environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        try:
            stdout, stderr = process.communicate(
                input=attempt.prompt.encode("utf-8"),
                timeout=attempt.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            self._terminate(process)
            process.communicate()
            raise ProviderTimedOut(f"Codex attempt {attempt.attempt_id} exceeded its timeout") from error

        if len(stdout) > _MAX_CAPTURE_BYTES or len(stderr) > _MAX_CAPTURE_BYTES:
            raise ProviderProtocolError("Codex diagnostic output exceeded the capture limit")
        if process.returncode != 0:
            raise ProviderProtocolError(f"Codex attempt exited with code {process.returncode}")
        if not output_path.is_file() or output_path.is_symlink():
            raise ProviderProtocolError("Codex did not create the expected result file")
        if output_path.stat().st_size > _MAX_RESULT_BYTES:
            raise ProviderProtocolError("Codex result exceeded the size limit")
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProviderProtocolError("Codex result was not valid UTF-8 JSON") from error

        event_types = self._event_types(stdout)
        return CodexAttemptResult(
            attempt_id=attempt.attempt_id,
            payload=payload,
            event_types=event_types,
            elapsed_seconds=time.monotonic() - started,
        )

    @staticmethod
    def _event_types(stdout: bytes) -> tuple[str, ...]:
        event_types: list[str] = []
        try:
            lines = stdout.decode("utf-8", errors="strict").splitlines()
        except UnicodeDecodeError as error:
            raise ProviderProtocolError("Codex JSONL stream was not valid UTF-8") from error
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise ProviderProtocolError("Codex JSONL stream contained invalid JSON") from error
            if not isinstance(event, dict):
                raise ProviderProtocolError("Codex JSONL events must be objects")
            event_type = event.get("type")
            if isinstance(event_type, str) and event_type:
                event_types.append(event_type)
        return tuple(event_types)

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=2)
