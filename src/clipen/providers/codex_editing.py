"""One-shot Codex agent runner bound to a single Clipen MCP edit session."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Mapping

from .codex import (
    CodexExecAdapter,
    _permission_config,
    _provider_environment,
    _toml_string,
    model_config,
)
from .editing_agent import (
    _MAX_CAPTURE_BYTES,
    _MCP_TOOLS,
    _PRODUCTION_PROMPT,
    _PROMPT,
    BoundedEditingAgent,
    EditingResult,
    mcp_server_arguments,
)
from .recovery import ProviderUsage

__all__ = ["CodexEditingAgent", "CodexEditingResult", "_MCP_TOOLS", "_PRODUCTION_PROMPT", "_PROMPT"]

CodexEditingResult = EditingResult


class CodexEditingAgent(BoundedEditingAgent):
    """Launch an ephemeral Codex turn with one exact MCP server configuration."""

    provider_id = "codex"
    display_name = "Codex"
    executable = "codex"

    def provider_environment(self, source: Mapping[str, str] | None) -> dict[str, str]:
        return _provider_environment(source)

    def command(
        self,
        project_id: str,
        run_id: str,
        workspace: Path,
        output_path: Path,
    ) -> tuple[str, ...]:
        args = mcp_server_arguments(self.projects_root, project_id, run_id, self.local_runtime)
        return (
            str(self.binary),
            "--ask-for-approval",
            "never",
            *_permission_config(workspace, Path(tempfile.gettempdir()).resolve()),
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--json",
            "--output-last-message",
            str(output_path),
            "-c",
            f"mcp_servers.clipen.command={_toml_string(str(self.python))}",
            "-c",
            f"mcp_servers.clipen.args={json.dumps(args, ensure_ascii=True)}",
            "-c",
            "mcp_servers.clipen.required=true",
            "-c",
            "mcp_servers.clipen.tool_timeout_sec=600",
            "-c",
            'mcp_servers.clipen.default_tools_approval_mode="approve"',
            "-c",
            f"mcp_servers.clipen.enabled_tools={json.dumps(_MCP_TOOLS)}",
            *model_config(self.binary, self.environment),
            "--cd",
            str(workspace),
            "-",
        )

    def events_and_usage(self, stdout: bytes) -> tuple[tuple[str, ...], ProviderUsage | None]:
        return CodexExecAdapter._events_and_usage(stdout, limit=_MAX_CAPTURE_BYTES)

    def raise_for_failure(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        CodexExecAdapter._raise_for_failed_turn(stdout, limit=_MAX_CAPTURE_BYTES)
        if returncode != 0:
            CodexExecAdapter._raise_for_exit(returncode, stderr)
