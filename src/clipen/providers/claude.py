"""Model-free checks and process environment for the Claude Code CLI."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
from typing import Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .codex import (
    _MAX_CAPTURE_BYTES,
    _MAX_RESULT_BYTES,
    CodexAttempt,
    CodexAttemptResult,
    CodexExecAdapter,
    _decode_capture,
    _inside,
    _isolated_workspace,
    _load_output_schema,
    _provider_environment,
    _workspace_file,
)
from .recovery import ProviderUsage
from .errors import (
    ProviderAuthenticationError,
    ProviderNetworkError,
    ProviderPermissionError,
    ProviderProtocolError,
    ProviderUnavailable,
    ProviderUsageLimitError,
)


# Flags the bounded editing attempt depends on; an older CLI without them
# could load user tools or settings, so it is reported as unsupported.
REQUIRED_FLAGS = (
    "--print",
    "--input-format",
    "--output-format",
    "--json-schema",
    "--safe-mode",
    "--mcp-config",
    "--strict-mcp-config",
    "--tools",
    "--allowedTools",
    "--permission-mode",
    "--no-session-persistence",
    "--setting-sources",
)
_PROBE_TIMEOUT_SECONDS = 20
_CLAUDE_ENVIRONMENT = frozenset({"ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR"})


# Clipen excludes user settings so they cannot add tools, hooks or rules. Only
# the chosen model and effort carry over, like Codex's model and effort.
_CLAUDE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_CLAUDE_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,79}")


def claude_preferences(environment: Mapping[str, str] | None = None) -> tuple[str | None, str | None]:
    """The `model` and `effortLevel` from the user's Claude Code settings."""
    source = os.environ if environment is None else environment
    home = source.get("CLAUDE_CONFIG_DIR") or (str(Path(source["HOME"]) / ".claude") if source.get("HOME") else None)
    if not home:
        return None, None
    path = Path(home) / "settings.json"
    try:
        if path.stat().st_size > 1024 * 1024:
            return None, None
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(settings, dict):
        return None, None
    model, effort = settings.get("model"), settings.get("effortLevel")
    return (model if isinstance(model, str) and _CLAUDE_MODEL.fullmatch(model) else None,
            effort if effort in _CLAUDE_EFFORTS else None)


def model_arguments(environment: Mapping[str, str] | None = None) -> tuple[str, ...]:
    model, effort = claude_preferences(environment)
    return (*(("--model", model) if model else ()), *(("--effort", effort) if effort else ()))


def claude_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    inherited = os.environ if source is None else source
    environment = _provider_environment(inherited)
    environment.update({key: value for key, value in inherited.items() if key in _CLAUDE_ENVIRONMENT})
    environment.update({
        # Match Codex's 600-second MCP tool timeout for bounded music downloads.
        "MCP_TOOL_TIMEOUT": "600000",
        "MCP_TIMEOUT": "60000",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    })
    return environment


def raise_for_claude_failure(returncode: int, diagnostic: str, status: object = None) -> None:
    text = diagnostic.lower()
    if status == 401 or any(marker in text for marker in (
            "not logged in", "invalid api key", "/login", "authentication_error", "oauth token")):
        raise ProviderAuthenticationError("Claude login is missing or no longer valid")
    if status == 429 or any(marker in text for marker in (
            "usage limit", "rate limit", "rate_limit", "quota", "credit balance", "limit reached")):
        raise ProviderUsageLimitError("Claude account usage limit prevented this attempt")
    if any(marker in text for marker in (
            "connection error", "network is unreachable", "could not resolve host", "econnrefused", "enotfound")):
        raise ProviderNetworkError("Claude could not reach its service")
    if "permission denied" in text or "operation not permitted" in text:
        raise ProviderPermissionError("Claude permission confinement failed")
    raise ProviderProtocolError(f"Claude attempt exited with code {returncode}")


@dataclass(frozen=True, slots=True)
class ClaudeProbeResult:
    version: str
    authenticated: bool
    missing_flags: tuple[str, ...]

    @property
    def supports_bounded_attempts(self) -> bool:
        return not self.missing_flags

    @property
    def available(self) -> bool:
        return self.authenticated and self.supports_bounded_attempts


class ClaudeCliAdapter:
    """Inspect the local Claude Code CLI without contacting a model."""

    def __init__(self, binary: str | Path | None = None, environment: Mapping[str, str] | None = None) -> None:
        selected = str(binary) if binary is not None else shutil.which("claude")
        if not selected:
            raise ProviderUnavailable("Claude Code CLI is not installed or not on PATH")
        candidate = Path(selected).resolve(strict=True)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ProviderUnavailable("Claude Code CLI is not an executable regular file")
        self.binary = candidate
        self.environment = claude_environment(environment)

    def _probe_command(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                (str(self.binary), *arguments),
                env=self.environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProviderUnavailable("Claude Code CLI probe failed") from error

    def version(self) -> str:
        completed = self._probe_command("--version")
        if completed.returncode != 0:
            raise ProviderUnavailable("Claude Code CLI version probe failed")
        version = _decode_capture(completed.stdout, "version output", 256).strip()
        if not version:
            raise ProviderProtocolError("Claude Code CLI returned an invalid version string")
        return version

    def missing_flags(self) -> tuple[str, ...]:
        completed = self._probe_command("--help")
        if completed.returncode != 0:
            raise ProviderUnavailable("Claude Code CLI capability probe failed")
        help_text = _decode_capture(completed.stdout, "help output")
        return tuple(flag for flag in REQUIRED_FLAGS if flag not in help_text)

    def authenticated(self) -> bool:
        if self.environment.get("ANTHROPIC_API_KEY"):
            return True
        completed = self._probe_command("auth", "status", "--json")
        try:
            status = json.loads(_decode_capture(completed.stdout, "auth status", 64 * 1024))
        except (ProviderProtocolError, json.JSONDecodeError):
            return False
        return isinstance(status, dict) and status.get("loggedIn") is True

    def probe(self) -> ClaudeProbeResult:
        return ClaudeProbeResult(self.version(), self.authenticated(), self.missing_flags())


def _stream_events(stdout: bytes, limit: int = _MAX_CAPTURE_BYTES) -> list[dict]:
    if len(stdout) > limit:
        raise ProviderProtocolError("Claude stream-json output exceeded the capture limit")
    events = []
    # Parse line by line from bytes so a large log is never copied into one string.
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ProviderProtocolError("Claude stream contained invalid JSON") from error
        if not isinstance(event, dict):
            raise ProviderProtocolError("Claude stream events must be objects")
        # Tool results echo every thumbnail back; nothing reads them, so keep only the type.
        events.append({"type": "user"} if event.get("type") == "user" else event)
    return events


def stream_event_types(events: list[dict]) -> tuple[str, ...]:
    types = []
    for event in events:
        kind, subtype = event.get("type"), event.get("subtype")
        if isinstance(kind, str) and kind:
            types.append(f"{kind}.{subtype}" if isinstance(subtype, str) and subtype else kind)
    return tuple(types)


def stream_usage(events: list[dict]) -> ProviderUsage | None:
    """Claude reports fresh, cache-written and cache-read input separately."""
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    reported = None if result is None else result.get("usage")
    if not isinstance(reported, dict):
        return None
    try:
        read = reported.get("cache_read_input_tokens", 0)
        return ProviderUsage(
            input_tokens=reported["input_tokens"] + reported.get("cache_creation_input_tokens", 0) + read,
            cached_input_tokens=read,
            output_tokens=reported["output_tokens"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ProviderProtocolError("Claude usage data was malformed") from error


def raise_for_failed_result(events: list[dict], diagnostic: str = "") -> None:
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if result is None or (not result.get("is_error") and result.get("subtype") == "success"):
        return
    message = " ".join(str(part) for part in (result.get("result"), result.get("errors"), diagnostic) if part)
    raise_for_claude_failure(1, message, result.get("api_error_status"))


class ClaudeExecAdapter(CodexExecAdapter):
    """Run one schema-bound Claude attempt with the same receipts as Codex.

    Workspace isolation, receipts, redaction, schema validation and reconcile
    are inherited; only the launch, stdin and output stream differ.
    """

    provider_id = "claude"
    display_name = "Claude"

    def __init__(
        self,
        attempt_root: Path,
        binary: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._attempt_root = _isolated_workspace(attempt_root)
        cli = ClaudeCliAdapter(binary, environment)
        self._cli = cli
        self._binary = cli.binary
        self._environment = cli.environment

    def version(self) -> str:
        return self._cli.version()

    def probe(self) -> ClaudeProbeResult:
        """Check the local CLI and login without contacting a model."""
        return self._cli.probe()

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
            "--print",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--json-schema",
            json.dumps(_load_output_schema(schema), ensure_ascii=True),
            # Inputs arrive only through stdin; no tools, MCP servers, user
            # settings or CLAUDE.md, like Codex's --ignore-user-config/--ignore-rules.
            "--safe-mode",
            "--tools",
            "",
            "--strict-mcp-config",
            "--permission-mode",
            "dontAsk",
            "--setting-sources",
            "project",
            "--no-session-persistence",
            "--disable-slash-commands",
            *model_arguments(self._environment),
        )

    def _stdin_payload(self, attempt: CodexAttempt) -> bytes:
        """Send the prompt and approved PNG snapshots as one user message."""
        content: list[dict[str, object]] = [{"type": "text", "text": attempt.prompt}]
        for image in attempt.images:
            path = _workspace_file(image, attempt.workspace.resolve(), "image")
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.b64encode(path.read_bytes()).decode("ascii")}})
        message = {"type": "user", "message": {"role": "user", "content": content}}
        return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")

    @staticmethod
    def _events_and_usage(stdout: bytes, *, limit: int = _MAX_CAPTURE_BYTES) -> tuple[tuple[str, ...], ProviderUsage | None]:
        events = _stream_events(stdout, limit)
        return stream_event_types(events), stream_usage(events)

    @staticmethod
    def _raise_for_failed_turn(stdout: bytes, *, limit: int = _MAX_CAPTURE_BYTES) -> None:
        try:
            events = _stream_events(stdout, limit)
        except ProviderProtocolError:
            return  # Normal result validation reports the malformed stream.
        raise_for_failed_result(events)

    @staticmethod
    def _raise_for_exit(returncode: int, stderr: bytes) -> None:
        raise_for_claude_failure(returncode, _decode_capture(stderr, "stderr"))

    def _validated_result(self, attempt: CodexAttempt, output_path: Path, stdout: bytes) -> CodexAttemptResult:
        events = _stream_events(stdout)
        result = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if result is None or result.get("is_error") or result.get("subtype") != "success":
            raise ProviderProtocolError("Claude stream did not report a successful result")
        if output_path.is_symlink():
            raise ProviderProtocolError("Claude result path is unsafe")
        if not output_path.exists():
            # Claude returns structured output in its stream; persist it once so
            # receipts and reconcile hash the same file as a Codex attempt.
            payload = result.get("structured_output")
            if payload is None and isinstance(result.get("result"), str):
                try:
                    payload = json.loads(result["result"])
                except json.JSONDecodeError:
                    payload = None
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if len(encoded) > _MAX_RESULT_BYTES:
                raise ProviderProtocolError("Claude result exceeded the size limit")
            descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
        if output_path.stat().st_size > _MAX_RESULT_BYTES:
            raise ProviderProtocolError("Claude result exceeded the size limit")
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProviderProtocolError("Claude result was not valid UTF-8 JSON") from error
        if not isinstance(payload, dict):
            raise ProviderProtocolError("Claude result must be a JSON object")
        schema = _load_output_schema(_workspace_file(attempt.output_schema, _isolated_workspace(attempt.workspace), "output_schema"))
        try:
            Draft202012Validator(schema).validate(payload)
        except ValidationError as error:
            raise ProviderProtocolError("Claude result did not match the output schema") from error
        return CodexAttemptResult(attempt.attempt_id, payload, stream_event_types(events), None, stream_usage(events))
