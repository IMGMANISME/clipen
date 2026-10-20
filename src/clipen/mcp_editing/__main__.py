"""Run the project-scoped Clipen editing MCP server."""

from __future__ import annotations

import argparse
from pathlib import Path

from clipen.editing import EditAgentRepository, EditAgentTools
from clipen.projects import ProjectRepository

from .server import EditingMcpServer, run_stdio


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--projects-root", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--run-id", required=True)
    arguments = parser.parse_args()
    projects = ProjectRepository(Path(arguments.projects_root))
    repository = EditAgentRepository(projects)
    tools = EditAgentTools(repository, arguments.project_id, arguments.run_id)
    try:
        return run_stdio(EditingMcpServer(tools))
    finally:
        tools.close_production()


if __name__ == "__main__":
    raise SystemExit(main())
