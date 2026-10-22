"""Model-free checks and process environment for the Claude Code CLI."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Mapping

from .codex import _decode_capture, _provider_environment
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
    "--output-format",
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
