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
    parser.add_argument("--data-root")
    parser.add_argument("--asr-requirements")
    arguments = parser.parse_args()
    projects = ProjectRepository(Path(arguments.projects_root))
    repository = EditAgentRepository(projects)
    tools = EditAgentTools(repository, arguments.project_id, arguments.run_id)
    setup = None
    if arguments.data_root and arguments.asr_requirements:
        from clipen.analysis.setup import ASRSetupManager
        setup = ASRSetupManager(Path(arguments.data_root), Path(arguments.asr_requirements))
        tools.local_data_root = Path(arguments.data_root)
        tools.transcriber_binding = setup.runtime_binding
        tools.asr_setup_manager = setup
    try:
        return run_stdio(EditingMcpServer(tools))
    finally:
        tools.close_production()
        if setup:setup.close()


if __name__ == "__main__":
    raise SystemExit(main())
