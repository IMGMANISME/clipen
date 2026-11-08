"""Graphics an Agent designs as a web page, drawn frame by frame in a sealed headless Chrome."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import test_skill_production as fixtures
from clipen.rendering import custom_components, program_components


def _chrome_available():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as playwright:
            try:
                playwright.chromium.launch(channel='chrome').close()
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
    return SimpleNamespace(start_frame=start, duration_frames=duration, text='x')


@unittest.skipUnless(CHROME, 'needs Chrome and Playwright')
class CustomRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {'CLIPEN_CUSTOM_COMPONENT_CACHE': self.temp.name})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def picture(self, html, frame, duration=30, x=0, y=0):
        image = Image.new('RGBA', (TIMELINE.width, TIMELINE.height), (0, 0, 0, 255))
        program_components.draw_program_component(ImageDraw.Draw(image), None, TIMELINE, item(duration),
                                                  {'component_id': 'custom', 'html': html, 'x': x, 'y': y}, frame)
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
        moved = self.picture(SLIDE, 29, x=.25)
        self.assertEqual(moved.getpixel((60+80, 50))[:3], (255, 0, 0))

    def test_script_driven_pages_draw_the_frame_they_are_asked_for(self):
        html = """<canvas id="c" width="320" height="180"></canvas><script>
        window.clipenFrame = (frame) => { const g = document.getElementById('c').getContext('2d');
          g.clearRect(0, 0, 320, 180); g.fillStyle = '#00f'; g.fillRect(frame * 10, 0, 10, 10); };
        </script>"""
        self.assertEqual(self.picture(html, 5).getpixel((55, 5))[:3], (0, 0, 255))
        self.assertEqual(self.picture(html, 5).getpixel((5, 5))[:3], (0, 0, 0))
        marks = program_components.animation_boundaries(item(12, start=100), {'component_id': 'custom', 'html': html}, TIMELINE)
        self.assertEqual(marks, set(range(100, 113)))

    def test_still_stretches_cost_one_image(self):
        marks = program_components.animation_boundaries(item(30, start=10), {'component_id': 'custom', 'html': '<div style="width:9px;height:9px;background:red"></div>'}, TIMELINE)
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
                custom_components.check(TIMELINE, item(), {'component_id': 'custom', 'html': html})
            return caught.exception.code
        self.assertEqual(code("<script>throw new Error('boom')</script><div>x</div>"), 'custom_component_script_error')
        self.assertEqual(code('<div></div>'), 'custom_component_empty')
        self.assertEqual(code('<b>' + 'x' * custom_components.MAX_HTML_BYTES + '</b>'), 'custom_component_html_too_large')
        with patch.object(custom_components, 'FRAME_TIMEOUT_SECONDS', 2):
            self.assertEqual(code('<div>x</div><script>while (true) {}</script>'), 'custom_component_load_timeout')


@unittest.skipUnless(CHROME, 'needs Chrome and Playwright')
class CustomComponentToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {'CLIPEN_CUSTOM_COMPONENT_CACHE': self.temp.name})
        self.environment.start()
        self.f = fixtures.SkillProductionTests()
        self.f.setUp()
        self.f.cut()

    def tearDown(self):
        self.f.tearDown()
        self.environment.stop()
        self.temp.cleanup()

    def styles(self):
        from clipen.editing.production_media import load_text_styles
        return load_text_styles(self.f.projects, self.f.project.project_id)

    def element(self, element_id='free'):
        return next(i for t in self.f.tools._timeline().tracks for i in t.items if i.item_id == element_id)

    def test_agent_designs_updates_and_people_move_a_free_graphic(self):
        from clipen.projects import ProjectError
        result = self.f.call('add_custom_component', **{'name': '手繪標題'}, element_id='free', item_id='agent-video-0001', start_offset_frames=6,
                             duration_frames=30, html=SLIDE, summary='自由設計標題')
        profile = self.styles()[self.element().style_id]
        self.assertEqual((profile['component_id'], profile['html'], self.element().text), ('custom', SLIDE, '手繪標題'))
        # The Agent gets a still of its design over the actual footage to look at.
        self.assertEqual(len(result.images), 1)
        self.assertTrue(Path(result.images[0]).is_file())
        # Updating replaces only what is given.
        self.f.call('update_custom_component', element_id='free', duration_frames=20, summary='縮短')
        self.assertEqual((self.element().duration_frames, self.styles()[self.element().style_id]['html']), (20, SLIDE))
        with self.assertRaises(ProjectError) as caught:
            self.f.call('update_custom_component', element_id='free', html='<div></div>', summary='空白')
        self.assertEqual(caught.exception.code, 'custom_component_empty')
        with self.assertRaises(ProjectError) as caught:
            self.f.call('update_program_component', element_id='free', item_id='agent-video-0001', component_id='headline', start_offset_frames=6,
                        duration_frames=30, text='x', evidence='e', summary='s')
        self.assertEqual(caught.exception.code, 'component_identity_conflict')
        # Built-in restyling leaves free designs alone.
        self.f.component()
        self.f.call('restyle_program_components', visual_style_id='editorial', summary='換風格')
        self.assertEqual(self.styles()[self.element().style_id]['html'], SLIDE)
        # A person drags it: the whole page shifts, the design itself is untouched.
        self.f.tools._user_edit = True
        draft, moved, _ = self.f.tools._component_draft({'element_id': 'free', 'x': .1, 'y': -.05})
        self.assertEqual((moved['x'], moved['y'], moved['html']), (.1, -.05, SLIDE))
        editing = self.f.tools._component_editing()['components']['free']
        self.assertEqual((editing['custom'], editing['name']), (True, '手繪標題'))


if __name__ == '__main__':
    unittest.main()
