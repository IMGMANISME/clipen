"""Rewording or moving text reuses the cut it lies over instead of rendering the film again."""

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from clipen.domain import EditTimeline, Rational, TextClip, Track, TrackKind
from clipen.rendering.renderer import FfmpegPreviewRenderer


def timeline(text="標題", duration=90):
    return EditTimeline(
        "timeline",
        "project",
        None,
        None,
        None,
        Rational(30, 1),
        320,
        180,
        duration,
        (
            Track("video-main", TrackKind.VIDEO, ()),
            Track("titles", TrackKind.TITLE, (TextClip("t", 0, 30, text, "style"),)),
        ),
        production_session_id="session",
    )


class MediaCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.renderer = FfmpegPreviewRenderer(SimpleNamespace(projects=SimpleNamespace(root=self.root / "projects")))
        self.renders = []

        def render_media(project_id, cut, output, cancel, progress):
            self.renders.append(cut.duration_frames)
            output.write_bytes(b"film-%d" % cut.duration_frames)

        self.renderer._render_media = render_media

    def tearDown(self):
        self.temp.cleanup()

    def media(self, cut):
        work = Path(tempfile.mkdtemp(dir=self.temp.name))
        return self.renderer._media_without_titles("project", cut, work, threading.Event(), lambda _value: None)

    def test_text_edits_reuse_the_cut_and_cut_edits_do_not(self):
        first = self.media(timeline("標題"))
        reused = self.media(timeline("改過的標題"))
        self.assertEqual(self.renders, [90])
        self.assertEqual(reused.read_bytes(), first.read_bytes())
        self.media(timeline(duration=120))
        self.assertEqual(self.renders, [90, 120])

    def test_only_the_last_few_cuts_are_kept(self):
        for duration in (60, 70, 80, 90):
            self.media(timeline(duration=duration))
        kept = list((self.root / "projects" / "project" / "previews" / "media-cache").glob("*.mp4"))
        self.assertEqual(len(kept), FfmpegPreviewRenderer._MEDIA_CACHE_KEEP)
        self.media(timeline(duration=90))
        self.assertEqual(self.renders, [60, 70, 80, 90])


if __name__ == "__main__":
    unittest.main()
