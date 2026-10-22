"""One-shot Claude Code agent runner bound to a single Clipen MCP edit session."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .claude import claude_environment, raise_for_claude_failure
from .codex import _decode_capture
from .editing_agent import _MAX_CAPTURE_BYTES, _MCP_TOOLS, BoundedEditingAgent, mcp_server_arguments
from .errors import ProviderProtocolError
from .recovery import ProviderUsage


MCP_SERVER = "clipen"


def allowed_tools() -> str:
    return ",".join(f"mcp__{MCP_SERVER}__{name}" for name in _MCP_TOOLS)


def _events(stdout: bytes) -> list[dict]:
    events = []
    for line in _decode_capture(stdout, "stream-json output", _MAX_CAPTURE_BYTES).splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise ProviderProtocolError("Claude stream contained invalid JSON") from error
        if not isinstance(event, dict):
            raise ProviderProtocolError("Claude stream events must be objects")
        events.append(event)
    return events


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
            # The attempt workspace is empty, so this excludes user settings,
            # hooks and plugins without loading anything else.
            "--setting-sources",
            "project",
            "--disable-slash-commands",
        )

    def events_and_usage(self, stdout: bytes) -> tuple[tuple[str, ...], ProviderUsage | None]:
        event_types: list[str] = []
        usage: ProviderUsage | None = None
        for event in _events(stdout):
            kind = event.get("type")
            if not isinstance(kind, str) or not kind:
                continue
            subtype = event.get("subtype")
            event_types.append(f"{kind}.{subtype}" if isinstance(subtype, str) and subtype else kind)
            reported = event.get("usage")
            if kind == "result" and isinstance(reported, dict):
                try:
                    read = reported.get("cache_read_input_tokens", 0)
                    usage = ProviderUsage(
                        input_tokens=reported["input_tokens"] + reported.get("cache_creation_input_tokens", 0) + read,
                        cached_input_tokens=read,
                        output_tokens=reported["output_tokens"],
                    )
                except (KeyError, TypeError, ValueError) as error:
                    raise ProviderProtocolError("Claude usage data was malformed") from error
        return tuple(event_types), usage

    def raise_for_failure(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        events = _events(stdout)
        diagnostic = _decode_capture(stderr, "stderr", _MAX_CAPTURE_BYTES)
        init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None)
        result = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if result is None:
            raise_for_claude_failure(returncode, diagnostic)
        if init is not None:
            servers = init.get("mcp_servers")
            connected = isinstance(servers, list) and any(
                isinstance(s, dict) and s.get("name") == MCP_SERVER and s.get("status") == "connected" for s in servers)
            if not connected:
                raise ProviderProtocolError("Claude could not connect to the Clipen MCP server")
        if result.get("is_error") or result.get("subtype") != "success":
            detail = result.get("result")
            errors = result.get("errors")
            message = " ".join(str(part) for part in (detail, errors, diagnostic) if part)
            raise_for_claude_failure(returncode or 1, message, result.get("api_error_status"))
        if returncode != 0:
            raise_for_claude_failure(returncode, diagnostic)
