"""One-shot Claude Code agent runner bound to a single Clipen MCP edit session."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .claude import (
    _stream_events,
    claude_environment,
    raise_for_claude_failure,
    raise_for_failed_result,
    stream_event_types,
    stream_usage,
)
from .codex import _decode_capture
from .editing_agent import _MAX_CAPTURE_BYTES, _MCP_TOOLS, BoundedEditingAgent, mcp_server_arguments
from .errors import ProviderProtocolError
from .recovery import ProviderUsage


MCP_SERVER = "clipen"


def allowed_tools() -> str:
    return ",".join(f"mcp__{MCP_SERVER}__{name}" for name in _MCP_TOOLS)


class ClaudeEditingAgent(BoundedEditingAgent):
    """Launch `claude -p` with only the Clipen MCP tools and no user settings."""

    provider_id = "claude"
    display_name = "Claude"
    executable = "claude"

    def provider_environment(self, source: Mapping[str, str] | None) -> dict[str, str]:
        return claude_environment(source)

    def command(
        self,
        project_id: str,
        run_id: str,
        workspace: Path,
        output_path: Path,
    ) -> tuple[str, ...]:
        server = {"type": "stdio", "command": str(self.python),
                  "args": mcp_server_arguments(self.projects_root, project_id, run_id, self.local_runtime)}
        return (
            str(self.binary),
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--mcp-config",
            json.dumps({"mcpServers": {MCP_SERVER: server}}, ensure_ascii=True),
            # No built-in tools: no shell, file access or web fetches. The model
            # acts only through the allowlisted Clipen MCP tools.
            "--tools",
            "",
            "--allowedTools",
            allowed_tools(),
            "--permission-mode",
            "dontAsk",
            # Excludes user settings, hooks, plugins and CLAUDE.md (verified with a
            # canary CLAUDE.md in the attempt directory). --safe-mode would do the
            # same but also disables the --mcp-config server this agent needs.
            "--setting-sources",
            "project",
            "--disable-slash-commands",
        )

    def events_and_usage(self, stdout: bytes) -> tuple[tuple[str, ...], ProviderUsage | None]:
        events = _stream_events(stdout, _MAX_CAPTURE_BYTES)
        return stream_event_types(events), stream_usage(events)

    def record_account(self, stdout: bytes) -> None:
        # Claude Code has no quota query; keep what this attempt reported.
        from .account import record_claude_account
        try:
            record_claude_account(self.attempt_root, _stream_events(stdout, _MAX_CAPTURE_BYTES))
        except (OSError, ProviderProtocolError):
            pass

    def closing_message(self, stdout: bytes, final_message: Path) -> str | None:
        # Claude reports its last message in the stream's result event.
        result = next((e for e in reversed(_stream_events(stdout, _MAX_CAPTURE_BYTES)) if e.get("type") == "result"), None)
        value = None if result is None else result.get("result")
        return value if isinstance(value, str) else None

    def raise_for_failure(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        events = _stream_events(stdout, _MAX_CAPTURE_BYTES)
        diagnostic = _decode_capture(stderr, "stderr", _MAX_CAPTURE_BYTES)
        init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None)
        if not any(e.get("type") == "result" for e in events):
            raise_for_claude_failure(returncode, diagnostic)
        if init is not None:
            servers = init.get("mcp_servers")
            connected = isinstance(servers, list) and any(
                isinstance(s, dict) and s.get("name") == MCP_SERVER and s.get("status") == "connected" for s in servers)
            if not connected:
                raise ProviderProtocolError("Claude could not connect to the Clipen MCP server")
        raise_for_failed_result(events, diagnostic)
        if returncode != 0:
            raise_for_claude_failure(returncode, diagnostic)
