"""A project with one ten-second source and AI production runs over it; no paid AI.

Every edit run is a production session, so tools are created the way the
workbench creates them and timeline mutations carry the revision guard.
"""
from pathlib import Path
import tempfile
import time

from clipen.editing import EditAgentRepository, EditAgentTools
from clipen.media import (AssetCandidate, AssetKind, InventoryRepository, InventoryState,
                          ItemStatus, MediaStream, ProbeStatus)
from clipen.projects import DirectoryReference, DirectoryRole, ProjectRepository

PRODUCTION = {'skill_id': 'travel-program', 'intent': '剪成旅行影片'}


class ProductionFixture:
    """One source video of 10 s at 1/90000 (start 0) with a 48 kHz audio stream."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.projects = ProjectRepository(root / 'projects')
        self.project = self.projects.create_project('request-project', 'AI 剪輯')
        source = root / 'source'; source.mkdir(); info = source.stat()
        self.project = self.projects.set_directory(
            self.project.project_id, 'grant-source', 'a' * 64, self.project.revision,
            DirectoryReference(DirectoryRole.SOURCE, str(source), str(source), '原片',
                               info.st_dev, info.st_ino, '2026-10-20T00:00:00+00:00'))
        inventory = InventoryRepository(self.projects)
        run = inventory.create_run(self.project.project_id, 'source-inventory', self.project.revision)
        inventory.transition(self.project.project_id, run.run_id, InventoryState.RUNNING, phase='probing')
        inventory.stage_items(self.project.project_id, run.run_id, (
            AssetCandidate(DirectoryRole.SOURCE, 'street.mp4', AssetKind.VIDEO, ItemStatus.USABLE, None, 1024, 12345,
                           'b' * 64, ProbeStatus.READY, {'duration_seconds': '10'},
                           (MediaStream(0, 'video', 'h264', {'start_pts': '0', 'duration_ts': '900000',
                                                             'time_base': '1/90000', 'width': 320, 'height': 180}),
                            MediaStream(1, 'audio', 'aac', {'start_pts': '0', 'duration_ts': '480000',
                                                            'time_base': '1/48000'}))),))
        inventory.publish(self.project.project_id, run.run_id)
        self.repository = EditAgentRepository(self.projects)

    def tearDown(self):
        self.temporary.cleanup()

    def create_tools(self, *, calls=20, request_id='request-edit-run', production=None):
        """Start a production run and register the whole source video as one candidate."""
        run, created = self.repository.create_run(
            self.project.project_id, request_id, production=production or PRODUCTION,
            max_tool_calls=calls, max_preview_renders=2)
        assert created
        tools = EditAgentTools(self.repository, self.project.project_id, run.run_id)
        tools.activate()
        self.candidate = self.register(tools)
        return run, tools

    def register(self, tools, *, start=0, duration=10):
        source = next(s for s in tools._source_rows() if s['kind'] == 'video')
        self.version_id = source['version_id']
        return tools._production_register_source_range({
            'version_id': source['version_id'], 'stream_index': 0, 'start_offset_seconds': start,
            'duration_seconds': duration, 'description': '早上的東京街景'}).payload['candidate']['candidate_id']

    def call(self, tools, name, arguments):
        """Call a tool as the production provider does, guarding timeline mutations."""
        from clipen.editing.tool_receipts import TIMELINE_MUTATIONS
        arguments = dict(arguments)
        if name in TIMELINE_MUTATIONS:
            arguments.setdefault('expected_revision_id',
                                 self.repository.current_revision(tools.project_id, tools.run_id).revision_id)
            arguments.setdefault('mutation_id', 'test-' + str(time.monotonic_ns()))
        return tools.call(name, arguments)
