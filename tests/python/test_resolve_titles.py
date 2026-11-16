"""Components reach DaVinci Resolve as Fusion titles whose words stay editable and whose look stays Clipen's."""

import json
import re
import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageChops, ImageDraw

from clipen.domain import TextClip
from clipen.editing import resolve_titles
from clipen.rendering import program_creator
from clipen.rendering.titles import TitleCardRenderer


def creator(component_id, looks, x=0.06, y=0.1):
    """A travel-template component profile: Clipen's creator skin with system fonts."""
    return {
        "kind": "program_component",
        "component_id": component_id,
        "x": x,
        "y": y,
        "presets": {
            "appearance": {"skin": "creator", "looks": looks, "motion_style": "fade"},
            "font_roles": {
                "detail": {"family": "modern", "weight": "regular"},
                "information": {"family": "modern", "weight": "regular"},
                "title": {"family": "serif", "weight": "light"},
                "impact": {"family": "serif", "weight": "light"},
            },
            "motion": {"entry_distance_ratio": 0.012, "entry_frames": 8, "exit_frames": 6},
            "palette": {
                "accent": [240, 224, 160, 255],
                "ink": [16, 16, 18, 255],
                "paper": [255, 255, 255, 255],
                "secondary": [120, 180, 220, 255],
            },
        },
    }


TIMELINE = SimpleNamespace(width=1280, height=720)


def unpack(blob: str) -> tuple[str, str]:
    """A CompositionBA back to (composition header, tool list)."""
    data = bytes.fromhex(blob)
    variant = zlib.decompress(data[4:])
    self_length = struct.unpack(">I", data[:4])[0]
    assert self_length == len(variant)
    raw = variant[variant.index("0_data".encode("utf-16-be")) + 12 + 9 :]
    header, rest = raw.split(b"\x00", 1)
    length = struct.unpack("<I", rest[:4])[0]
    tools = zlib.decompress(rest[4:])
    assert len(tools) == length
    return header.decode(), tools.decode()


class EditableComponentTests(unittest.TestCase):
    def setUp(self):
        self.font_path = TitleCardRenderer._font_path()

    def test_type_lands_where_clipen_draws_it(self):
        for profile, text in (
            (creator("location", {"tag": "rules"}), "京成｜Skyliner"),
            (creator("person", {"info": "quiet"}, y=0.5), "Cindy\n這趟東京旅伴"),
            (creator("program_title", {"title": "serif"}, y=0.44), "東京．六日\nTokyo · July 2026"),
        ):
            with self.subTest(text=text):
                item = TextClip("component", 0, 90, text, "style")
                plan = program_creator.editable(self.font_path, TIMELINE, item, profile)
                self.assertEqual([row["text"] for row in plan["rows"]], text.split("\n"))
                self.assertIsNotNone(plan["picture"])
                full = Image.new("RGBA", (1280, 720))
                pen = ImageDraw.Draw(full)
                pen._image = full
                program_creator.draw_graphic(pen, self.font_path, TIMELINE, item, profile)
                # The picture holds no type, and type redrawn from the rows lands inside Clipen's own drawing.
                rebuilt = plan["picture"].copy()
                for row in plan["rows"]:
                    face = next(
                        f
                        for f in (
                            program_creator.load_style_font(self.font_path, row["size"], profile),
                            program_creator.load_style_font(self.font_path, row["size"], profile, role="detail"),
                        )
                        if f.getname() == (row["family"], row["style"])
                    )
                    x = row["x"]
                    for character in row["text"]:
                        ImageDraw.Draw(rebuilt).text(
                            (x, row["baseline"]), character, font=face, fill=tuple(row["fill"]), anchor="ls"
                        )
                        x += face.getlength(character) + face.size * row["track"]
                stray = ImageChops.subtract(rebuilt.getchannel("A"), full.getchannel("A")).point(
                    lambda v: 255 if v > 128 else 0
                )
                self.assertIsNone(stray.getbbox())
                self.assertLess(plan["picture"].getchannel("A").histogram()[255], full.getchannel("A").histogram()[255])

    def test_tilted_components_stay_pictures(self):
        item = TextClip("component", 0, 90, "好吃", "style")
        self.assertIsNone(
            program_creator.editable(
                self.font_path, TIMELINE, item, creator("reaction", {"sticker": "stamp"}, 0.4, 0.3)
            )
        )


class FusionTitleTests(unittest.TestCase):
    rows = [
        {
            "text": '京成｜"Skyliner"',
            "family": "PingFang TC",
            "style": "Regular",
            "size": 26,
            "track": 0.24,
            "fill": [255, 255, 255, 255],
            "x": 109,
            "baseline": 100,
            "shadow": {"colour": [16, 16, 18, 230], "offset": 1, "blur": 3},
        }
    ]

    def test_clip_fields_match_what_resolve_writes(self):
        # A Text+ title Resolve 21 exported at frame 545.
        self.assertEqual(
            resolve_titles._fields(545),
            "000000020000002b800a160a122205546578742b4a05546578742ba801a104200112100000000000000006ffffffffffffffff",
        )

    def test_composition_publishes_each_row_over_the_picture(self):
        header, tools = unpack(
            resolve_titles.composition(
                resolve_titles.tools(self.rows, Path("/a/底圖.png"), 1280, 720, 90), 1280, 720, 90
            )
        )
        self.assertIn('TEMPLATE_ID = "Text+"', header)
        self.assertIn("RenderRange = { 0, 89 }", header)
        self.assertTrue(tools.startswith("{ Template = GroupOperator"))
        self.assertIn('Row1Text = InstanceInput { SourceOp = "Row1", Source = "StyledText", Name = "文字", }', tools)
        self.assertIn('StyledText = Input { Value = "京成｜\\"Skyliner\\"", }', tools)
        self.assertIn('Filename = "/a/底圖.png"', tools)
        self.assertIn(f"Size = Input {{ Value = {26 * 1.76 / 1280:.6g}, }}", tools)
        self.assertIn(f"CharacterSpacing = Input {{ Value = {1 + 0.24 / 1.76:.6g}, }}", tools)
        self.assertIn(f"Center = Input {{ Value = {{ {109 / 1280:.6g}, {1 - 100 / 720:.6g} }}, }}", tools)
        self.assertIn('Input = Input { SourceOp = "Merge1", Source = "Output", }', tools)

    def test_convert_swaps_component_pngs_for_titles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            components = root / "Q & A" / "元件"
            components.mkdir(parents=True)
            (components / resolve_titles.MANIFEST).write_text(
                json.dumps(
                    {
                        "002 京成.png": {
                            "rows": self.rows,
                            "picture": "002 京成（底圖）.png",
                            "width": 1280,
                            "height": 720,
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            def clip(path, start):
                escaped = str(path).replace("&", "&amp;")
                return (
                    f'<Sm2TiVideoClip DbId="x{start}">\n <Name>{path.name}</Name>\n <Start>{start}</Start>\n <Duration>75</Duration>\n'
                    f" <MediaFilePath>{escaped}</MediaFilePath>\n <MediaFrameRate>0000000000003e400000000000000000</MediaFrameRate>\n</Sm2TiVideoClip>"
                )

            sequence = (
                "<Sm2SequenceContainer>\n <Items>\n  <Element>\n   "
                + clip(components / "002 京成.png", 120)
                + "\n  </Element>\n  <Element>\n   "
                + clip(root / "shot.mp4", 0)
                + "\n  </Element>\n </Items>\n</Sm2SequenceContainer>"
            )
            source = root / "film.drt"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("project.xml", "<SM_Project/>")
                archive.writestr("SeqContainer/a.xml", sequence)
            self.assertTrue(resolve_titles.uses(source, components))
            self.assertFalse(resolve_titles.uses(source, root / "other"))
            target = root / "out.drt"
            self.assertEqual(resolve_titles.convert(source, target), 1)
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(archive.read("project.xml"), b"<SM_Project/>")
                out = archive.read("SeqContainer/a.xml").decode()
            title = re.search(r'<Sm2TiVideoClip DbId="[0-9a-f-]{36}">.*?</Sm2TiVideoClip>', out, re.S).group(0)
            self.assertIn("<PrettyType>Fusion Title</PrettyType>", title)
            self.assertIn("<Start>120</Start>", title)
            self.assertIn("<Duration>75</Duration>", title)
            self.assertIn(resolve_titles._fields(120), title)
            _, tools = unpack(re.search(r"<CompositionBA>([0-9a-f]+)<", title).group(1))
            self.assertIn(f'Filename = "{components / "002 京成（底圖）.png"}"', tools)
            self.assertIn("GlobalOut = Input { Value = 74, }", tools)
            # Footage and anything Clipen did not make pass through untouched.
            self.assertIn("<Name>shot.mp4</Name>", out)
            self.assertNotIn("002 京成.png</MediaFilePath>", out)


if __name__ == "__main__":
    unittest.main()
