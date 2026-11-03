"""A component splits into its rows of type and a picture of everything else, for editors that set type themselves."""
import unittest
from types import SimpleNamespace

from PIL import Image, ImageChops, ImageDraw

from clipen.domain import TextClip
from clipen.rendering import program_creator
from clipen.rendering.titles import TitleCardRenderer


def creator(component_id, looks, x=.06, y=.1):
    """A travel-template component profile: Clipen's creator skin with system fonts."""
    return {'kind': 'program_component', 'component_id': component_id, 'x': x, 'y': y,
            'presets': {'appearance': {'skin': 'creator', 'looks': looks, 'motion_style': 'fade'},
                        'font_roles': {'detail': {'family': 'modern', 'weight': 'regular'}, 'information': {'family': 'modern', 'weight': 'regular'},
                                       'title': {'family': 'serif', 'weight': 'light'}, 'impact': {'family': 'serif', 'weight': 'light'}},
                        'motion': {'entry_distance_ratio': .012, 'entry_frames': 8, 'exit_frames': 6},
                        'palette': {'accent': [240, 224, 160, 255], 'ink': [16, 16, 18, 255], 'paper': [255, 255, 255, 255],
                                    'secondary': [120, 180, 220, 255]}}}


TIMELINE = SimpleNamespace(width=1280, height=720)


class EditableComponentTests(unittest.TestCase):
    def setUp(self):
        self.font_path = TitleCardRenderer._font_path()

    def test_type_lands_where_clipen_draws_it(self):
        for profile, text in ((creator('location', {'tag': 'rules'}), '京成｜Skyliner'),
                              (creator('person', {'info': 'quiet'}, y=.5), 'Cindy\n這趟東京旅伴'),
                              (creator('program_title', {'title': 'serif'}, y=.44), '東京．六日\nTokyo · July 2026')):
            with self.subTest(text=text):
                item = TextClip('component', 0, 90, text, 'style')
                plan = program_creator.editable(self.font_path, TIMELINE, item, profile)
                self.assertEqual([row['text'] for row in plan['rows']], text.split('\n'))
                self.assertIsNotNone(plan['picture'])
                full = Image.new('RGBA', (1280, 720))
                pen = ImageDraw.Draw(full)
                pen._image = full
                program_creator.draw_graphic(pen, self.font_path, TIMELINE, item, profile)
                # The picture holds no type, and type redrawn from the rows lands inside Clipen's own drawing.
                rebuilt = plan['picture'].copy()
                for row in plan['rows']:
                    face = next(f for f in (program_creator.load_style_font(self.font_path, row['size'], profile),
                                            program_creator.load_style_font(self.font_path, row['size'], profile, role='detail'))
                                if f.getname() == (row['family'], row['style']))
                    x = row['x']
                    for character in row['text']:
                        ImageDraw.Draw(rebuilt).text((x, row['baseline']), character, font=face, fill=tuple(row['fill']), anchor='ls')
                        x += face.getlength(character) + face.size * row['track']
                stray = ImageChops.subtract(rebuilt.getchannel('A'), full.getchannel('A')).point(lambda v: 255 if v > 128 else 0)
                self.assertIsNone(stray.getbbox())
                self.assertLess(plan['picture'].getchannel('A').histogram()[255], full.getchannel('A').histogram()[255])

    def test_tilted_components_stay_pictures(self):
        item = TextClip('component', 0, 90, '好吃', 'style')
        self.assertIsNone(program_creator.editable(self.font_path, TIMELINE, item, creator('reaction', {'sticker': 'stamp'}, .4, .3)))

if __name__ == '__main__':
    unittest.main()
