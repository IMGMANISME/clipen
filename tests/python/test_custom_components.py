"""Graphics an Agent designs as a web page, drawn frame by frame in a sealed headless Chrome."""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_skill_production as fixtures
from PIL import Image, ImageDraw

from clipen.domain import TrackKind
from clipen.rendering import custom_components, program_components
from clipen.rendering.stage import current_stage, read_stage, stage_path
from clipen.rendering.titles import TitleRenderError, compose_dense_titles


def _chrome_available():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as playwright:
            try:
                playwright.chromium.launch(channel="chrome").close()
            except Exception:
                playwright.chromium.launch().close()
        return True
    except Exception:
        return False


CHROME = _chrome_available()
TIMELINE = SimpleNamespace(width=320, height=180, frame_rate=SimpleNamespace(numerator=30, denominator=1))
SLIDE = """<style>
.card { position: absolute; left: 40px; top: 30px; width: 100px; height: 40px; background: rgb(255, 0, 0);
        animation: in 1s linear both; }
@keyframes in { from { transform: translateX(120px); } to { transform: none; } }
</style><div class="card"></div>"""


def item(duration=30, start=0):
    return SimpleNamespace(start_frame=start, duration_frames=duration, text="x")


class CancelTests(unittest.TestCase):
    """Drawing every frame of many free designs takes minutes; a cancel must not wait for it."""

    def test_a_cancelled_render_stops_the_chrome_run(self):
        with tempfile.TemporaryDirectory() as folder:
            slow = Path(folder) / "slow"
            slow.write_text("#!/bin/sh\nsleep 30\n")
            slow.chmod(0o755)
            cancel = threading.Event()
            threading.Timer(0.3, cancel.set).start()
            started = time.monotonic()
            with (
                patch.object(sys, "executable", str(slow)),
                self.assertRaises(custom_components.CustomRenderError) as caught,
            ):
                custom_components._run({"frames": []}, timeout=60, cancel=cancel)
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertLess(time.monotonic() - started, 5)

    def test_drawing_free_designs_reports_progress_and_stops_when_cancelled(self):
        styles = {"look": {"kind": "program_component", "component_id": "custom", "html": "<div></div>"}}
        items = [SimpleNamespace(style_id="look", start_frame=n * 30, duration_frames=30) for n in range(4)]
        timeline = SimpleNamespace(
            tracks=[SimpleNamespace(kind=TrackKind.TITLE, items=items)],
            production_session_id="session",
            duration_frames=120,
            width=320,
            height=180,
            frame_rate=SimpleNamespace(numerator=30, denominator=1),
        )
        cancel, reports, drawn = threading.Event(), [], []

        def draw(item, profile, timeline=None, cancel_=None):
            drawn.append(current_stage())
            if len(drawn) == 2:
                cancel.set()
            return {item.start_frame}

        with tempfile.TemporaryDirectory() as folder, patch.object(program_components, "animation_boundaries", draw):
            with self.assertRaises(TitleRenderError) as caught:
                compose_dense_titles(
                    "ffmpeg",
                    Path(folder) / "in.mp4",
                    timeline,
                    Path(folder),
                    Path(folder) / "out.mp4",
                    cancel,
                    styles,
                    progress=reports.append,
                )
        self.assertEqual(caught.exception.code, "cancelled")
        # The status names the graphic being drawn, out of how many.
        self.assertEqual(drawn, ["畫自由設計元件 1/4", "畫自由設計元件 2/4"])
        self.assertEqual(reports, [0.0, 0.05])

    def test_the_step_file_sits_beside_the_film_and_reads_back(self):
        with tempfile.TemporaryDirectory() as folder:
            film = Path(folder) / "render_1.mp4"
            self.assertIsNone(read_stage(film))
            stage_path(film).write_text("畫自由設計元件 3/9", encoding="utf-8")
            self.assertEqual(stage_path(film).name, ".render_1.stage")
            self.assertEqual(read_stage(film), "畫自由設計元件 3/9")


@unittest.skipUnless(CHROME, "needs Chrome and Playwright")
class CustomRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"CLIPEN_CUSTOM_COMPONENT_CACHE": self.temp.name})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def picture(self, html, frame, duration=30, x=0, y=0):
        image = Image.new("RGBA", (TIMELINE.width, TIMELINE.height), (0, 0, 0, 255))
        program_components.draw_program_component(
            ImageDraw.Draw(image),
            None,
            TIMELINE,
            item(duration),
            {"component_id": "custom", "html": html, "x": x, "y": y},
            frame,
        )
        return image

    def test_clipen_sets_the_time_of_css_animations(self):
        start, end = self.picture(SLIDE, 0), self.picture(SLIDE, 29)
        self.assertEqual(start.getpixel((170, 50))[:3], (255, 0, 0))
        self.assertEqual(start.getpixel((60, 50))[:3], (0, 0, 0))
        self.assertEqual(end.getpixel((60, 50))[:3], (255, 0, 0))
        # Transparent everywhere it draws nothing, so the footage shows through.
        self.assertEqual(end.getpixel((300, 170))[:3], (0, 0, 0))
        # The same frame always gives the same picture, and a person's move shifts the whole page.
        self.assertEqual(self.picture(SLIDE, 29).tobytes(), end.tobytes())
        moved = self.picture(SLIDE, 29, x=0.25)
        self.assertEqual(moved.getpixel((60 + 80, 50))[:3], (255, 0, 0))

    def test_script_driven_pages_draw_the_frame_they_are_asked_for(self):
        html = """<canvas id="c" width="320" height="180"></canvas><script>
        window.clipenFrame = (frame) => { const g = document.getElementById('c').getContext('2d');
          g.clearRect(0, 0, 320, 180); g.fillStyle = '#00f'; g.fillRect(frame * 10, 0, 10, 10); };
        </script>"""
        self.assertEqual(self.picture(html, 5).getpixel((55, 5))[:3], (0, 0, 255))
        self.assertEqual(self.picture(html, 5).getpixel((5, 5))[:3], (0, 0, 0))
        marks = program_components.animation_boundaries(
            item(12, start=100), {"component_id": "custom", "html": html}, TIMELINE
        )
        self.assertEqual(marks, set(range(100, 113)))

    def test_still_stretches_cost_one_image(self):
        marks = program_components.animation_boundaries(
            item(30, start=10),
            {"component_id": "custom", "html": '<div style="width:9px;height:9px;background:red"></div>'},
            TIMELINE,
        )
        self.assertEqual(marks, {10, 40})

    def test_the_page_has_no_network_and_no_real_clock(self):
        html = """<div id="d" style="width:50px;height:50px;background:rgb(0,255,0)"></div>
        <img src="https://example.com/a.png"><script>
        fetch('https://example.com/x').then(() => { document.getElementById('d').style.background = 'rgb(255,0,0)'; }).catch(() => {});
        if (Date.now() !== 1767225600000) document.getElementById('d').style.background = 'rgb(255,0,0)';
        </script>"""
        picture = self.picture(html, 0)
        self.assertEqual(picture.getpixel((20, 20))[:3], (0, 255, 0))
        # A picture the page could not fetch is hidden, not drawn as a broken icon.
        self.assertEqual(picture.getpixel((70, 10))[:3], (0, 0, 0))

    def test_only_technical_problems_are_refused(self):
        def code(html):
            with self.assertRaises(custom_components.CustomRenderError) as caught:
                custom_components.check(TIMELINE, item(), {"component_id": "custom", "html": html})
            return caught.exception.code

        self.assertEqual(code("<script>throw new Error('boom')</script><div>x</div>"), "custom_component_script_error")
        self.assertEqual(code("<div></div>"), "custom_component_empty")
        self.assertEqual(
            code("<b>" + "x" * custom_components.MAX_HTML_BYTES + "</b>"), "custom_component_html_too_large"
        )
        with patch.object(custom_components, "FRAME_TIMEOUT_SECONDS", 2):
            self.assertEqual(code("<div>x</div><script>while (true) {}</script>"), "custom_component_load_timeout")


@unittest.skipUnless(CHROME, "needs Chrome and Playwright")
class CustomComponentToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"CLIPEN_CUSTOM_COMPONENT_CACHE": self.temp.name})
        self.environment.start()
        self.f = fixtures.SkillProductionTests()
        self.f.setUp()
        self.f.cut()
        self.f.call(
            "set_component_look",
            look_id="base",
            name="基本",
            fonts=[{"key": "main", "family": "PingFang TC"}],
            colors=[{"key": "accent", "value": "#ff2e63"}],
            summary="外觀",
        )

    def tearDown(self):
        self.f.tearDown()
        self.environment.stop()
        self.temp.cleanup()

    def styles(self):
        from clipen.editing.production_media import load_text_styles

        return load_text_styles(self.f.projects, self.f.project.project_id)

    def element(self, element_id="free"):
        return next(i for t in self.f.tools._timeline().tracks for i in t.items if i.item_id == element_id)

    def test_agent_designs_updates_and_people_move_a_free_graphic(self):
        from clipen.projects import ProjectError

        result = self.f.call(
            "add_custom_component",
            look_id="base",
            **{"name": "手繪標題"},
            element_id="free",
            item_id="agent-video-0001",
            start_offset_frames=6,
            duration_frames=30,
            html=SLIDE,
            summary="自由設計標題",
        )
        profile = self.styles()[self.element().style_id]
        self.assertEqual((profile["component_id"], profile["html"], self.element().text), ("custom", SLIDE, "手繪標題"))
        # The Agent gets a still of its design over the actual footage to look at.
        self.assertEqual(len(result.images), 2)
        self.assertTrue(all(Path(image).is_file() for image in result.images))
        # The second image is a sheet of frames from entry to exit: the slide-in is visible as it moves.
        from PIL import Image

        with Image.open(result.images[1]) as sheet:
            self.assertGreater(sheet.width, sheet.height)
        # Updating replaces only what is given.
        self.f.call("update_custom_component", element_id="free", duration_frames=20, summary="縮短")
        self.assertEqual((self.element().duration_frames, self.styles()[self.element().style_id]["html"]), (20, SLIDE))
        with self.assertRaises(ProjectError) as caught:
            self.f.call("update_custom_component", element_id="free", html="<div></div>", summary="空白")
        self.assertEqual(caught.exception.code, "custom_component_empty")
        # A person drags it: the whole page shifts, the design itself is untouched.
        self.f.tools._user_edit = True
        draft, moved, _ = self.f.tools._component_draft({"element_id": "free", "x": 0.1, "y": -0.05})
        self.assertEqual((moved["x"], moved["y"], moved["html"]), (0.1, -0.05, SLIDE))
        editing = self.f.tools._component_editing()["components"]["free"]
        self.assertEqual((editing["custom"], editing["name"]), (True, "手繪標題"))


if __name__ == "__main__":
    unittest.main()
