"""People move, lengthen and stack graphics on the timeline."""
import os
import tempfile
import unittest
from unittest.mock import patch

import test_skill_production as fixtures
from test_custom_components import CHROME
from clipen.domain import TextClip
from clipen.editing.recut import relane
from clipen.projects import ProjectError

PAGE = '<div style="position:absolute;left:20px;top:20px;width:80px;height:40px;background:red"></div>'


@unittest.skipUnless(CHROME, 'needs Chrome and Playwright')
class ComponentTimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {'CLIPEN_CUSTOM_COMPONENT_CACHE': self.temp.name})
        self.environment.start()
        self.f = fixtures.SkillProductionTests()
        self.f.setUp()
        self.f.cut()
        self.f.call('set_component_look', look_id='base', name='基本', fonts=[{'key': 'main', 'family': 'PingFang TC'}],
                    colors=[{'key': 'accent', 'value': '#ff2e63'}], summary='外觀')
        for key, start in (('a', 6), ('b', 20)):
            self.f.call('add_custom_component', look_id='base', name=key, element_id=key, item_id='agent-video-0001', start_offset_frames=start,
                        duration_frames=30, html=PAGE, summary='設計')
        self.f.repository.cancel(self.f.project.project_id, self.f.run.run_id)

    def tearDown(self):
        self.f.tearDown()
        self.environment.stop()
        self.temp.cleanup()

    def edit(self, **arguments):
        from clipen.editing.workbench import edit
        revision = self.f.repository.current_revision(self.f.project.project_id, self.f.run.run_id).revision_id
        edit(self.f.repository, self.f.project.project_id, self.f.run.run_id, 'edit_component', {'summary': '拖曳', **arguments}, revision, f'm-{arguments}')
        self.f.tools._expected_revision_id = None

    def lanes(self):
        tracks = [t for t in self.f.tools._timeline().tracks if t.track_id.startswith('program-components')]
        return [[(i.item_id, i.start_frame, i.duration_frames) for i in t.items] for t in tracks]

    def test_overlapping_graphics_get_their_own_lanes_and_keep_them(self):
        self.assertEqual(self.lanes(), [[('a', 6, 30)], [('b', 20, 30)]])
        shots = self.f.tools._video().items
        # Dragged onto the second shot: it now belongs to that footage.
        self.edit(element_id='a', item_id=shots[1].item_id, start_offset_frames=10)
        self.assertEqual(self.lanes(), [[('a', shots[1].start_frame + 10, 30)], [('b', 20, 30)]])
        # The lower lane is free now, so b can be dropped there; graphics on higher lanes draw on top.
        self.edit(element_id='b', lane=0)
        self.assertEqual(self.lanes(), [[('b', 20, 30), ('a', shots[1].start_frame + 10, 30)]])

    def test_a_graphic_may_run_past_the_cut_but_not_past_the_film(self):
        shots = self.f.tools._video().items
        self.edit(element_id='b', duration_frames=shots[0].duration_frames)
        self.assertEqual(self.lanes()[1], [('b', 20, shots[0].duration_frames)])
        with self.assertRaises(ProjectError) as caught:
            self.edit(element_id='b', duration_frames=self.f.tools._timeline().duration_frames)
        self.assertEqual(caught.exception.code, 'component_time_out_of_bounds')
        with self.assertRaises(ProjectError) as caught:
            self.edit(element_id='a', lane=1)
        self.assertEqual(caught.exception.code, 'component_lane_busy')


class RelaneTests(unittest.TestCase):
    def test_a_graphic_that_now_meets_another_moves_up(self):
        clip = lambda key, start, length: TextClip(key, start, length, key, 'style')
        # After a recut b starts earlier and meets a; it moves to the next free lane.
        lanes = [[clip('a', 0, 50), clip('b', 30, 30)], [clip('c', 40, 30)]]
        result = [[i.item_id for i in t.items] for t in relane(lanes)]
        self.assertEqual(result, [['a'], ['c'], ['b']])


if __name__ == '__main__':
    unittest.main()
