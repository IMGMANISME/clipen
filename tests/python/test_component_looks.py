"""Graphics of one kind share a look: its typefaces are enforced, everything else stays free."""
import os
import tempfile
import unittest
from unittest.mock import patch

import test_skill_production as fixtures
from test_custom_components import CHROME
from clipen.editing.component_looks import normalize
from clipen.projects import ProjectError

PAGE = """<style>.t { position: absolute; left: 20px; top: 20px; padding: 8px; background: var(--look-accent); color: #fff;
font: 700 40px var(--look-font-main); }</style><div class="t">SHIBUYA</div>"""
LOOK = {'look_id': 'place', 'name': '地點', 'fonts': [{'key': 'main', 'family': 'PingFang TC'}], 'colors': [{'key': 'accent', 'value': '#ff2e63'}]}


class NormalizeTests(unittest.TestCase):
    def test_a_look_names_known_typefaces_and_real_colours(self):
        self.assertEqual(normalize(LOOK)['fonts'], [{'key': 'main', 'label': 'main', 'family': 'PingFang TC'}])
        for bad in ({**LOOK, 'fonts': [{'key': 'main', 'family': 'Comic Sans of Doom'}]}, {**LOOK, 'fonts': []},
                    {**LOOK, 'colors': [{'key': 'a', 'value': 'red'}]}, {**LOOK, 'look_id': 'a b'}):
            with self.assertRaises(ProjectError):
                normalize(bad)


@unittest.skipUnless(CHROME, 'needs Chrome and Playwright')
class ComponentLookTests(unittest.TestCase):
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

    def add(self, html=PAGE, **extra):
        return self.f.call('add_custom_component', name='澀谷', element_id='free', item_id='agent-video-0001', start_offset_frames=6,
                           duration_frames=30, html=html, summary='設計', **extra)

    def refused(self, call):
        with self.assertRaises(ProjectError) as caught:
            call()
        return caught.exception.code

    def profile(self):
        from clipen.editing.production_media import load_text_styles
        item = next(i for t in self.f.tools._timeline().tracks for i in t.items if i.item_id == 'free')
        return load_text_styles(self.f.projects, self.f.project.project_id)[item.style_id]

    def test_a_graphic_uses_only_its_looks_typefaces(self):
        self.assertEqual(self.refused(lambda: self.add(look_id='place')), 'component_look_not_found')
        self.f.call('set_component_look', **LOOK, summary='地點外觀')
        self.assertEqual(self.refused(lambda: self.add(PAGE.replace('var(--look-font-main)', 'Georgia'), look_id='place')),
                         'custom_component_font_outside_look')
        self.add(look_id='place')
        self.assertEqual(self.profile()['look']['fonts'][0]['family'], 'PingFang TC')
        listed = self.f.call('list_program_components').payload
        self.assertEqual((listed['looks'][0]['look_id'], listed['looks'][0]['used_by'], listed['existing_graphics'][0]['look_id']), ('place', 1, 'place'))

    def test_changing_a_look_redraws_its_graphics_and_keeps_them_consistent(self):
        from clipen.editing.history_changes import describe
        from clipen.editing.production_media import load_text_styles
        self.f.call('set_component_look', **LOOK, summary='地點外觀')
        self.add(look_id='place')
        before = self.f.tools._timeline()
        self.f.call('set_component_look', **{**LOOK, 'fonts': [{'key': 'main', 'family': 'Songti TC'}]}, summary='換字')
        self.assertEqual(self.profile()['look']['fonts'][0]['family'], 'Songti TC')
        styles = load_text_styles(self.f.projects, self.f.project.project_id)
        self.assertEqual(describe(before.to_payload(), self.f.tools._timeline().to_payload(), styles), ['換了「地點」外觀的字型'])
        # A look cannot drop a typeface its graphics still use.
        self.assertEqual(self.refused(lambda: self.f.call('set_component_look', **{**LOOK, 'fonts': [{'key': 'title', 'family': 'Futura'}]}, summary='換鍵')),
                         'custom_component_font_outside_look')

    def test_people_change_a_look_or_move_a_graphic_to_another(self):
        from clipen.editing.workbench import edit
        self.f.call('set_component_look', **LOOK, summary='地點外觀')
        self.f.call('set_component_look', look_id='chapter', name='章節', fonts=[{'key': 'main', 'family': 'Clipen Iansui'}], summary='章節外觀')
        self.f.call('set_component_look', look_id='odd', name='其他', fonts=[{'key': 'body', 'family': 'Futura'}], summary='其他')
        self.add(look_id='place')
        self.f.repository.cancel(self.f.project.project_id, self.f.run.run_id)
        self.f.tools._user_edit = True

        def person(tool, **arguments):
            revision = self.f.repository.current_revision(self.f.project.project_id, self.f.run.run_id).revision_id
            edit(self.f.repository, self.f.project.project_id, self.f.run.run_id, tool, {'summary': '使用者', **arguments}, revision, f'm-{tool}-{arguments}')
            self.f.tools._expected_revision_id = None
        person('edit_component_look', look_id='place', colors={'accent': '#2255ff'})
        self.assertEqual(self.profile()['look']['colors'][0]['value'], '#2255ff')
        self.assertEqual(self.refused(lambda: person('edit_component_look', look_id='place', fonts={'nope': 'Futura'})), 'component_look_invalid')
        person('edit_component', element_id='free', look_id='chapter')
        self.assertEqual(self.profile()['look']['look_id'], 'chapter')
        # A look without the typeface the design asks for cannot take it.
        self.assertEqual(self.refused(lambda: person('edit_component', element_id='free', look_id='odd')), 'custom_component_font_outside_look')


if __name__ == '__main__':
    unittest.main()
