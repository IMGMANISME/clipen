"""Free designs can mark words and colours that people then change without asking the AI."""

import os
import tempfile
import unittest
from unittest.mock import patch

import test_skill_production as fixtures
from test_custom_components import CHROME

from clipen.projects import ProjectError

PAGE = """<style>:root { --clipen-主色: #ff2e63; --clipen-字色: #ffffff; --clipen-size: 40px; }
.t { position: absolute; left: 20px; top: 20px; padding: 10px; background: var(--clipen-主色); color: var(--clipen-字色); font: 700 40px var(--look-font-main); }</style>
<div class="t"><span data-clipen-text="title" data-clipen-label="主標">SHIBUYA</span></div>"""


@unittest.skipUnless(CHROME, "needs Chrome and Playwright")
class CustomFieldTests(unittest.TestCase):
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

    def profile(self):
        from clipen.editing.production_media import load_text_styles

        item = next(i for t in self.f.tools._timeline().tracks for i in t.items if i.item_id == "free")
        return load_text_styles(self.f.projects, self.f.project.project_id)[item.style_id]

    def test_marked_words_and_colours_are_editable_and_survive_a_redesign(self):
        result = self.f.call(
            "add_custom_component",
            look_id="base",
            **{"name": "澀谷"},
            element_id="free",
            item_id="agent-video-0001",
            start_offset_frames=6,
            duration_frames=30,
            html=PAGE,
            summary="設計",
        ).payload
        self.assertEqual(result["editable_by_people"], {"texts": ["主標"], "colors": ["主色", "字色"]})
        self.assertEqual(self.profile()["fields"]["texts"], [{"key": "title", "label": "主標", "value": "SHIBUYA"}])
        self.f.repository.cancel(self.f.project.project_id, self.f.run.run_id)
        self.f.tools._user_edit = True
        draft, changed, _ = self.f.tools._component_draft(
            {"element_id": "free", "texts": {"title": "TOKYO"}, "colors": {"主色": "#2255FF", "字色": "#ffffff"}}
        )
        # Only what differs from the design is kept.
        self.assertEqual(changed["edits"], {"texts": {"title": "TOKYO"}, "colors": {"主色": "#2255ff"}})
        for bad in ({"texts": {"nope": "x"}}, {"texts": {"title": "  "}}, {"colors": {"主色": "blue"}}):
            with self.assertRaises(ProjectError) as caught:
                self.f.tools._component_draft({"element_id": "free", **bad})
            self.assertEqual(caught.exception.code, "component_field_invalid")
        from clipen.editing.workbench import edit

        edit(
            self.f.repository,
            self.f.project.project_id,
            self.f.run.run_id,
            "edit_component",
            {"element_id": "free", "texts": {"title": "TOKYO"}, "colors": {"主色": "#2255ff"}, "summary": "改字"},
            self.f.repository.current_revision(self.f.project.project_id, self.f.run.run_id).revision_id,
            "person-edit",
        )
        self.assertEqual(self.profile()["edits"], {"texts": {"title": "TOKYO"}, "colors": {"主色": "#2255ff"}})
        # The page is redesigned; the person's word stays because the new page keeps that field.
        self.f.tools._expected_revision_id = None
        self.f.tools.production_call(
            "update_custom_component",
            {"element_id": "free", "html": PAGE.replace("left: 20px", "left: 60px"), "summary": "重排"},
        )
        self.assertEqual(self.profile()["edits"], {"texts": {"title": "TOKYO"}, "colors": {"主色": "#2255ff"}})

    def test_a_design_with_nothing_marked_is_told_so(self):
        result = self.f.call(
            "add_custom_component",
            look_id="base",
            **{"name": "方塊"},
            element_id="free",
            item_id="agent-video-0001",
            start_offset_frames=6,
            duration_frames=30,
            html='<div style="width:40px;height:40px;background:red"></div>',
            summary="設計",
        ).payload
        self.assertEqual(result["editable_by_people"], {"texts": [], "colors": []})
        self.assertTrue(any("data-clipen-text" in note for note in result["notes"]))


if __name__ == "__main__":
    unittest.main()
