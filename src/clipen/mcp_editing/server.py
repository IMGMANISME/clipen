"""Minimal MCP stdio adapter over Clipen's allowlisted editing tools."""

from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
import sys
from typing import BinaryIO, Mapping

from clipen.editing import EditAgentTools, ToolResult
from clipen.projects import ProjectError
from clipen.editing.production_catalog import EXTRA_TOOLS


_MAX_MESSAGE_BYTES = 1024 * 1024
_MAX_IMAGE_BYTES = 96 * 1024
_MAX_IMAGE_SIZE = (640, 360)
_PROTOCOL_VERSION = "2025-06-18"


def _tool(
    name: str,
    description: str,
    properties: Mapping[str, object],
    required: tuple[str, ...] = (),
) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "object",
        "properties": dict(properties),
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return {"name": name, "description": description, "inputSchema": schema}


_TOOLS = (
    _tool(
        "get_edit_status",
        "Read the run state, bounded budget, and authoritative user-confirmed edit direction.",
        {},
    ),
    _tool(
        "search_media",
        "Search path-free candidate clips from the current approved material snapshot.",
        {
            "query": {"type": "string", "maxLength": 200},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "offset": {"type":"integer","minimum":0,"maximum":100000},
        },
    ),
    _tool(
        "inspect_clip",
        "Inspect one candidate's source-bound evidence and up to four local sample images.",
        {"candidate_id": {"type": "string"}},
        ("candidate_id",),
    ),
    _tool("inspect_sequence", "Read the current immutable timeline revision.", {}),
    _tool(
        "replace_sequence",
        "Build the short style proof from precise in/out ranges inside approved candidates.",
        {
            "clips": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "candidate_id": {"type": "string"},
                        "start_offset_seconds": {"type": "number", "minimum": 0},
                        "duration_seconds": {
                            "type": "number",
                            "minimum": 0.75,
                            "maximum": 30,
                        },
                    },
                    "required": [
                        "candidate_id",
                        "start_offset_seconds",
                        "duration_seconds",
                    ],
                    "additionalProperties": False,
                },
                "minItems": 1,
                "maxItems": 30,
            },
            "summary": {"type": "string", "maxLength": 500},
            "reprise_reason": {"type":"string","minLength":1,"maxLength":500,"description":"Explicit editorial reason for intentionally repeating a source range as a cold open/callback. Without this, duplicate source ranges are rejected."},
        },
        ("clips", "summary"),
    ),
    _tool(
        "add_title",
        "Add one editable title or caption using an allowlisted style.",
        {
            "start_frame": {"type": "integer", "minimum": 0},
            "duration_frames": {"type": "integer", "minimum": 1},
            "text": {"type": "string", "maxLength": 500},
            "style_id": {
                "type": "string",
                "enum": ["title.default", "title.chapter", "title.caption"],
            },
            "summary": {"type": "string", "maxLength": 500},
        },
        ("start_frame", "duration_frames", "text", "style_id", "summary"),
    ),
    _tool(
        "set_clip_effect",
        "Set one semantic effect on an existing media clip; exporters map it safely.",
        {
            "item_id": {"type": "string"},
            "effect_id": {
                "type": "string",
                "enum": [
                    "video.punch_in",
                    "video.slow_push",
                    "video.reframe",
                    "video.speed_ramp",
                ],
            },
            "parameters": {
                "type": "object",
                "description": (
                    "Use {} for safe defaults, or exact numeric keys: "
                    "punch_in {scale,center_x,center_y}; slow_push "
                    "{scale_from,scale_to}; reframe {x,y}; speed_ramp {rate}."
                ),
                "properties": {
                    "scale": {"type": "number", "minimum": 1.01, "maximum": 2},
                    "center_x": {"type": "number", "minimum": 0, "maximum": 1},
                    "center_y": {"type": "number", "minimum": 0, "maximum": 1},
                    "scale_from": {"type": "number", "minimum": 1, "maximum": 2},
                    "scale_to": {"type": "number", "minimum": 1, "maximum": 2},
                    "x": {"type": "number", "minimum": 0, "maximum": 1},
                    "y": {"type": "number", "minimum": 0, "maximum": 1},
                    "rate": {"type": "number", "minimum": 0.25, "maximum": 4},
                },
                "additionalProperties": False,
            },
            "summary": {"type": "string", "maxLength": 500},
        },
        ("item_id", "effect_id", "parameters", "summary"),
    ),
    _tool("undo_revision", "Select the parent of the current immutable revision.", {}),
    _tool(
        "finish_edit",
        "Finish this bounded run and freeze its selected final revision.",
        {"summary": {"type": "string", "maxLength": 1000}},
        ("summary",),
    ),
) + EXTRA_TOOLS


class EditingMcpServer:
    """Handle MCP JSON-RPC without exposing a shell, resources, or arbitrary files."""

    def __init__(self, tools: EditAgentTools) -> None:
        self.tools = tools

    def start(self) -> None:
        self.tools.activate()

    def handle(self, message: object) -> dict[str, object] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "Invalid Request")
        identifier = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            return self._error(identifier, -32600, "Invalid Request")
        if identifier is None:
            return None
        params = message.get("params", {})
        if method == "initialize":
            return self._result(
                identifier,
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "clipen-editing", "version": "0.1.0"},
                    "instructions": (
                        "Use only these bounded tools. Start with get_edit_status and execute "
                        "its user-confirmed direction. Use evidence only for bounded corrections, "
                        "keep source media immutable, and call finish_edit once."
                    ),
                },
            )
        if method == "ping":
            return self._result(identifier, {})
        if method == "tools/list":
            return self._result(identifier, {"tools": list(_TOOLS)})
        if method == "tools/call":
            return self._call_tool(identifier, params)
        return self._error(identifier, -32601, "Method not found")

    def _call_tool(self, identifier: object, params: object) -> dict[str, object]:
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return self._error(identifier, -32602, "Invalid params")
        arguments = params.get("arguments", {})
        try:
            result = self.tools.call(params["name"], arguments)
        except ProjectError as error:
            return self._result(
                identifier,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"error": error.code, "message": error.message},
                                ensure_ascii=False,
                            ),
                        }
                    ],
                    "isError": True,
                },
            )
        except (TypeError, ValueError) as error:
            return self._result(
                identifier,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"error": "agent_input_invalid", "message": str(error)},
                                ensure_ascii=False,
                            ),
                        }
                    ],
                    "isError": True,
                },
            )
        return self._result(identifier, self._content(result))

    @staticmethod
    def _content(result: ToolResult) -> dict[str, object]:
        content: list[dict[str, object]] = [
            {"type": "text", "text": json.dumps(result.payload, ensure_ascii=False)}
        ]
        for path in result.images:
            encoded = _bounded_image(path)
            if encoded is None:
                continue
            data, mime = encoded
            content.append(
                {
                    "type": "image",
                    "data": base64.b64encode(data).decode("ascii"),
                    "mimeType": mime,
                }
            )
        return {"content": content, "isError": False}

    @staticmethod
    def _result(identifier: object, value: object) -> dict[str, object]:
        return {"jsonrpc": "2.0", "id": identifier, "result": value}

    @staticmethod
    def _error(identifier: object, code: int, message: str) -> dict[str, object]:
        return {
            "jsonrpc": "2.0",
            "id": identifier,
            "error": {"code": code, "message": message},
        }


def _bounded_image(path: Path) -> tuple[bytes, str] | None:
    """Encode a model-readable thumbnail without modifying the evidence artifact."""

    try:
        from PIL import Image

        with Image.open(path) as source:
            image = source.convert("RGB")
            for size in (_MAX_IMAGE_SIZE, (480, 270), (320, 180)):
                image.thumbnail(size, Image.Resampling.LANCZOS)
                for quality in (76, 60, 44):
                    output = BytesIO()
                    image.save(output, "JPEG", quality=quality, optimize=True)
                    data = output.getvalue()
                    if len(data) <= _MAX_IMAGE_BYTES:
                        return data, "image/jpeg"
            return None
    except (ImportError, OSError, ValueError):
        try:
            if path.stat().st_size > _MAX_IMAGE_BYTES:
                return None
            data = path.read_bytes()
        except OSError:
            return None
        suffix = path.suffix.lower()
        mime = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"
        return data, mime


def run_stdio(
    server: EditingMcpServer,
    source: BinaryIO | None = None,
    destination: BinaryIO | None = None,
) -> int:
    """Serve newline-delimited MCP messages with a strict input bound."""

    input_stream = source or sys.stdin.buffer
    output_stream = destination or sys.stdout.buffer
    server.start()
    for raw in input_stream:
        if len(raw) > _MAX_MESSAGE_BYTES:
            response = server._error(None, -32600, "Message too large")
        else:
            try:
                response = server.handle(json.loads(raw))
            except (UnicodeDecodeError, json.JSONDecodeError):
                response = server._error(None, -32700, "Parse error")
        if response is not None:
            output_stream.write(
                json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                + b"\n"
            )
            output_stream.flush()
    return 0
