"""Home page covers are stills cut from a project's own film or footage."""
import asyncio
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_skill_production as fixtures
from test_preview_rendering import request_app
from clipen.projects import ProjectError, ProjectRepository
from clipen.rendering import ProjectCoverRepository


def clip(path: Path, seconds: float, colour: str) -> Path:
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c={colour}:size=320x180:rate=30:duration={seconds}',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', str(path)], check=True)
    return path


class ProjectCoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.projects = ProjectRepository(self.root / 'projects')
        self.project = self.projects.create_project('cover-project', '封面測試').project_id
        self.covers = ProjectCoverRepository(self.projects)

    def tearDown(self):
        self.temp.cleanup()

    def test_cuts_a_still_once_and_reuses_it(self):
        media = clip(self.root / 'film.mp4', 5, '0x204060')
        with patch.object(ProjectCoverRepository, '_source', return_value=('render_' + '1' * 32, media)):
            first = self.covers.cover(self.project)
            self.assertEqual(first.read_bytes()[:2], b'\xff\xd8')
            with patch('clipen.rendering.cover.subprocess.run') as run:
                self.assertEqual(self.covers.cover(self.project), first)
            run.assert_not_called()

    def test_a_clip_shorter_than_the_offset_uses_its_first_frame(self):
        media = clip(self.root / 'short.mp4', 1, 'white')
        with patch.object(ProjectCoverRepository, '_source', return_value=('av_' + '2' * 32, media)):
            self.assertGreater(self.covers.cover(self.project).stat().st_size, 0)

    def test_a_newer_source_replaces_the_old_still(self):
        media = clip(self.root / 'film.mp4', 5, 'black')
        with patch.object(ProjectCoverRepository, '_source', return_value=('render_' + '1' * 32, media)):
            old = self.covers.cover(self.project)
        with patch.object(ProjectCoverRepository, '_source', return_value=('render_' + '3' * 32, media)):
            new = self.covers.cover(self.project)
        self.assertFalse(old.exists())
        self.assertEqual([path.name for path in new.parent.iterdir()], [new.name])

    def test_unreadable_footage_or_no_footage_has_no_cover(self):
        with self.assertRaises(ProjectError) as caught:
            self.covers.cover(self.project)
        self.assertEqual(caught.exception.code, 'cover_unavailable')
        broken = self.root / 'broken.mp4'
        broken.write_bytes(b'not a video')
        with patch.object(ProjectCoverRepository, '_source', return_value=('av_' + '4' * 32, broken)):
            with self.assertRaises(ProjectError):
                self.covers.cover(self.project)
        self.assertEqual(list((self.projects.root / self.project / 'covers').iterdir()), [])


class CoverSourceTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SkillProductionTests()
        self.f.setUp()
        self.project = self.f.project.project_id
        self.covers = ProjectCoverRepository(self.f.projects)

    def tearDown(self):
        self.f.tearDown()

    def test_prefers_the_latest_finished_film_over_source_footage(self):
        footage = self.f.root / 'source' / 'source.mp4'
        film = self.f.root / 'film.mp4'
        with patch('clipen.media.MediaPreviewRepository.locate', return_value=(footage, {})):
            key, path = self.covers._source(self.project)
        self.assertTrue(key.startswith('av_'))
        self.assertEqual(path, footage)
        with self.f.projects.connect(self.project) as database:
            for number, completed in (('1', '2026-11-01T00:00:00+00:00'), ('2', '2026-11-02T00:00:00+00:00')):
                database.execute(
                    "INSERT INTO preview_renders(render_id,project_id,timeline_id,request_id,state,progress,created_at,completed_at) "
                    "VALUES(?,?,?,?,'succeeded',1,?,?)",
                    ('render_' + number * 32, self.project, 'timeline', 'request-' + number, completed, completed))
        with patch('clipen.rendering.PreviewRenderRepository.media_path', return_value=film) as media_path:
            self.assertEqual(self.covers._source(self.project), ('render_' + '2' * 32, film))
        self.assertEqual(media_path.call_args.args, (self.project, 'render_' + '2' * 32))

    def test_footage_missing_from_disk_is_skipped(self):
        self.assertIsNone(self.covers._source(self.project))


class CoverApiTests(unittest.TestCase):
    def test_cover_is_a_private_jpeg(self):
        from clipen.controller import ControllerConfig, LocalSessionAuthority, ProviderState, ProviderStatus, create_app
        from clipen.projects import ProjectWorkspace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'index.html').write_text('Clipen', encoding='utf-8')
            still = root / 'cover.jpg'
            still.write_bytes(b'\xff\xd8still')
            app = create_app(ControllerConfig(root), ProviderStatus('Codex', ProviderState.READY, 'AI 已連接'),
                             LocalSessionAuthority(), ProjectWorkspace(root / 'projects'))

            def request(path, headers=None):
                return asyncio.run(request_app(app, 'POST' if path.endswith('connect') else 'GET', path, headers=headers))
            _, headers, _ = request('/api/session/connect', {'origin': 'http://localhost:4200', 'sec-fetch-site': 'same-origin'})
            cookie = headers['set-cookie'].split(';', 1)[0]
            path = f"/api/projects/{'prj_' + '1' * 32}/cover"
            self.assertEqual(request(path)[0], 401)
            with patch('clipen.rendering.ProjectCoverRepository.cover', return_value=still):
                status, headers, body = request(path, {'cookie': cookie})
            self.assertEqual((status, headers['content-type'], body), (200, 'image/jpeg', b'\xff\xd8still'))
            self.assertIn('no-store', headers['cache-control'])
            missing = ProjectError('cover_unavailable', '這個作品還沒有可用的封面。', status_code=404)
            with patch('clipen.rendering.ProjectCoverRepository.cover', side_effect=missing):
                self.assertEqual(request(path, {'cookie': cookie})[0], 404)


if __name__ == '__main__':
    unittest.main()
