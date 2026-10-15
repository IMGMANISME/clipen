"""Project-scoped MCP server for bounded AI editing."""

from .server import EditingMcpServer, run_stdio

__all__ = ["EditingMcpServer", "run_stdio"]
