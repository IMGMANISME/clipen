"""Selectable templates freeze real renderer settings and original motion samples."""

import asyncio
import base64
import hashlib
import json
import subprocess
import unittest
from importlib.resources import files
from unittest.mock import patch

import test_skill_production as fixtures

from clipen.editing import EditAgentTools
from clipen.projects import ProjectError
from clipen.skills import get_skill, list_skills
from clipen.skills.visual_styles import font_presets, style_presets


class TemplateLibraryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SkillProductionTests()
        self.f.setUp()
        self.repo = self.f.repository
        self.project = self.f.project.project_id
        self.repo.cancel(self.project, self.f.run.run_id)

    def tearDown(self):
        self.f.tearDown()

    def test_all_templates_have_unique_rendered_previews_and_playable_silent_samples(self):
        skill = get_skill()
        templates = skill["templates"]
        self.assertGreaterEqual(len(templates), 9)
        self.assertEqual(len({t["template_id"] for t in templates}), len(templates))
        previews = set()
        palettes = set()
        for template in templates:
            with self.subTest(template=template["template_id"]):
                _, presets = style_presets(skill, template["visual_style_id"])
                _, presets = font_presets(skill, presets, template["font_style_id"])
                palettes.add(json.dumps(presets["palette"], sort_keys=True))
                asset = skill["assets"][template["preview_resource"]]
                encoded = base64.b64decode(asset["data"])
                self.assertTrue(encoded.startswith(b"\x89PNG"))
                self.assertEqual(hashlib.sha256(encoded).hexdigest(), asset["sha256"])
                previews.add(asset["sha256"])
                path = files("clipen.skills").joinpath("builtin", template["sample_resource"])
                result = json.loads(
                    subprocess.check_output(
                        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
                    )
                )
                self.assertEqual([s["codec_type"] for s in result["streams"]], ["video"])
                self.assertEqual(result["streams"][0]["codec_name"], "h264")
                self.assertAlmostEqual(float(result["format"]["duration"]), 8, delta=0.05)
                self.assertEqual(template["sample_duration_seconds"], 8)
                self.assertTrue(template["references"])
                self.assertEqual(presets["appearance"]["identity"], template["template_id"])
                self.assertEqual(
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    skill["samples"][template["sample_resource"]]["sha256"],
                )
        self.assertEqual(len(previews), len(templates))
        self.assertEqual(len(palettes), len(templates))
        self.assertEqual(len(list_skills()[0]["templates"]), len(templates))

    def test_template_selection_freezes_font_style_direction_and_is_idempotent(self):
        options = {"skill_id": "travel-program", "intent": "以真實素材製作旅誌", "template_id": "food-review"}
        run, created = self.repo.create_run(self.project, "food-review-template", production=options)
        self.assertTrue(created)
        session = self.repo.production_session(self.project, run.run_id)
        self.assertEqual(session["intent"]["template"]["template_id"], "food-review")
        self.assertEqual(session["intent"]["visual_style_id"], "tpl-food-review")
        self.assertEqual(session["intent"]["font_style_id"], "rounded-tw")
        self.assertEqual(session["skill"]["selected_template_id"], "food-review")
        self.assertIn("第一口", session["intent"]["template"]["editorial_direction"])
        replay, created = self.repo.create_run(self.project, "food-review-template", production=options)
        self.assertFalse(created)
        self.assertEqual(replay.run_id, run.run_id)
        tools = EditAgentTools(self.repo, self.project, run.run_id)
        tools.activate()
        try:
            self.assertEqual(
                tools.call("get_edit_status", {}).payload["intent"]["template"], session["intent"]["template"]
            )
            tools.call(
                "set_canvas",
                {
                    "width": 640,
                    "height": 360,
                    "summary": "畫布",
                    "expected_revision_id": run.current_revision_id,
                    "mutation_id": "template-canvas",
                },
            )
        finally:
            tools.close_production()
        self.repo.cancel(self.project, run.run_id)
        current = self.repo.current_revision(self.project, run.run_id)
        self.repo.request_refinement(
            self.project, run.run_id, "keep-template", current.revision_id, "保留風格，只修改標題"
        )
        self.assertEqual(
            self.repo.production_session(self.project, run.run_id)["intent"]["template"], session["intent"]["template"]
        )

    def test_unknown_or_conflicting_templates_are_rejected(self):
        for values in ({"template_id": "unknown"}, {"template_id": "travel-vlog", "visual_style_id": "cinema"}):
            with self.assertRaises(ProjectError):
                self.repo.create_run(
                    self.project,
                    "invalid-template",
                    production={"skill_id": "travel-program", "intent": "測試模板", **values},
                )
        run, _ = self.repo.create_run(
            self.project,
            "font-override",
            production={
                "skill_id": "travel-program",
                "intent": "模板換字體",
                "template_id": "travel-vlog",
                "font_style_id": "calligraphy",
            },
        )
        self.assertEqual(
            self.repo.production_session(self.project, run.run_id)["intent"]["font_style_id"], "calligraphy"
        )

    def test_custom_style_selection_also_binds_the_matching_template_skill(self):
        options = {
            "skill_id": "travel-program",
            "intent": "用夜色的節目風格",
            "visual_style_id": "tpl-daily-vlog",
            "font_style_id": "handwriting",
        }
        run, _ = self.repo.create_run(self.project, "custom-style-skill", production=options)
        session = self.repo.production_session(self.project, run.run_id)
        self.assertEqual(session["skill"]["selected_template_id"], "daily-vlog")
        self.assertEqual(session["skill"]["skill_id"], "clipen-daily-vlog")
        self.assertEqual([e["skill_id"] for e in session["skill"]["skill_chain"]], ["clipen-daily-vlog"])
        self.assertEqual(session["intent"]["font_style_id"], "handwriting")
        self.assertEqual(session["skill"]["selected_font_style_id"], "handwriting")

    def test_new_style_is_available_to_native_mcp_and_old_frozen_sessions_reject_it(self):
        # The existing session already freezes the newly installed template collection.
        frozen = self.repo.production_session(self.project, self.f.run.run_id)["skill"]
        self.assertIn("tpl-daily-vlog", {s["visual_style_id"] for s in frozen["visual_styles"]})
        skill = get_skill()
        old = {**skill, "visual_styles": [s for s in skill["visual_styles"] if s["visual_style_id"] == "variety"]}
        with self.assertRaises(ProjectError):
            style_presets(old, "tpl-daily-vlog")

    def test_sample_endpoint_requires_auth_and_supports_range_requests(self):
        from test_project_api import request_app

        from clipen.controller import ControllerConfig, LocalSessionAuthority, ProviderState, ProviderStatus, create_app
        from clipen.editing import EditAgentCoordinator

        static = self.f.root / "static"
        static.mkdir()
        (static / "index.html").write_text("Clipen")
        coordinator = EditAgentCoordinator(self.repo, self.f.root)
        app = create_app(
            ControllerConfig(static),
            ProviderStatus("Codex", ProviderState.READY, "ready"),
            LocalSessionAuthority(),
            edit_agents=coordinator,
        )

        def request(method, path, headers=None):
            return asyncio.run(request_app(app, method, path, headers=headers))

        try:
            url = "/api/production/skills/travel-program/templates/travel-vlog/sample"
            self.assertEqual(request("GET", url)[0], 401)
            _, headers, _ = request(
                "POST", "/api/session/connect", {"origin": "http://localhost:4200", "sec-fetch-site": "same-origin"}
            )
            auth = {"cookie": headers["set-cookie"].split(";", 1)[0]}
            code, headers, body = request("GET", url, {**auth, "range": "bytes=0-63"})
            self.assertEqual(code, 206)
            self.assertEqual(headers["content-type"], "video/mp4")
            self.assertEqual(len(body), 64)
            self.assertEqual(request("GET", url.replace("travel-vlog", "unknown"), auth)[0], 404)
        finally:
            coordinator.close()

    def test_each_template_loads_its_own_skill_through_the_default_mcp_read(self):
        from clipen.skills.catalog import builtin_skills

        skill = get_skill()
        core, builtin = builtin_skills()
        instructions = set()
        for template in skill["templates"]:
            with self.subTest(template=template["template_id"]):
                run, _ = self.repo.create_run(
                    self.project,
                    "own-skill-" + template["template_id"],
                    production={
                        "skill_id": "travel-program",
                        "intent": "照選定模板製作，保留使用者指定內容",
                        "template_id": template["template_id"],
                    },
                )
                session = self.repo.production_session(self.project, run.run_id)
                chain = session["skill"]["skill_chain"]
                selected = chain[-1]
                # Each template is its own skill directly over the production core.
                self.assertEqual(len(chain), 1)
                self.assertEqual(selected["resource"], template["skill_resource"])
                self.assertEqual(selected["content_hash"], session["intent"]["template"]["skill"]["content_hash"])
                self.assertEqual(template["visual_style_id"], session["intent"]["visual_style_id"])
                self.assertEqual(template["font_style_id"], session["intent"]["font_style_id"])
                tools = EditAgentTools(self.repo, self.project, run.run_id)
                tools.activate()
                try:
                    result = tools.call("read_production_skill", {}).payload
                    own = tools.call("read_production_skill", {"resource": selected["resource"]}).payload["content"]
                    self.assertIn(own, result["content"])
                    self.assertTrue(result["content"].startswith(core["resources"]["SKILL.md"]))
                    self.assertEqual(result["applied_template_skills"], chain)
                    self.assertEqual(result["skill"]["skill_chain"], chain)
                    instructions.add(hashlib.sha256(own.encode()).hexdigest())
                    for other in skill["templates"]:
                        if other["template_id"] != template["template_id"]:
                            other_text = builtin[other["skill"]["skill_id"]]["resources"][other["skill_resource"]]
                            self.assertNotIn(other_text, result["content"])
                finally:
                    tools.close_production()
                self.repo.cancel(self.project, run.run_id)
        self.assertEqual(len(instructions), len(skill["templates"]))

    def test_refinement_and_replay_keep_the_frozen_template_skill_after_catalog_changes(self):
        options = {"skill_id": "travel-program", "intent": "做一集日常研究", "template_id": "food-review"}
        run, _ = self.repo.create_run(self.project, "frozen-template-skill", production=options)
        frozen = self.repo.production_session(self.project, run.run_id)["skill"]
        selected = frozen["skill_chain"][-1]

        def updated(skill_id="travel-program"):
            # The catalog now ships a newer food-review Skill than the frozen one.
            from clipen.skills.catalog import get_skill as live

            value = live(skill_id)
            if skill_id == "clipen-food-review":
                value["resources"][selected["resource"]] = "updated instructions must not replace the frozen session"
                value["version"] = "2.0.0"
            return value

        self.repo.cancel(self.project, run.run_id)
        revision = self.repo.current_revision(self.project, run.run_id)
        with patch("clipen.skills.get_skill", side_effect=updated):
            replay, created = self.repo.create_run(self.project, "frozen-template-skill", production=options)
            self.assertFalse(created)
            self.assertEqual(replay.run_id, run.run_id)
            self.repo.request_refinement(
                self.project, run.run_id, "polish-frozen-skill", revision.revision_id, "保留風格，修改措辭"
            )
            tools = EditAgentTools(self.repo, self.project, run.run_id)
            tools.activate()
            try:
                result = tools.call("read_production_skill", {}).payload
                self.assertIn(frozen["resources"][selected["resource"]], result["content"])
                self.assertNotIn("updated instructions must not replace", result["content"])
                self.assertEqual(result["applied_template_skills"][0], selected)
            finally:
                tools.close_production()

    def test_legacy_frozen_sessions_can_still_read_their_original_skill(self):
        tools = EditAgentTools(self.repo, self.project, self.f.run.run_id)
        # Sessions frozen before the core/skill split have no skill_chain.
        old = get_skill()
        old.pop("skill_chain")
        old.pop("selected_template_skill", None)
        old["resources"] = {
            k: v for k, v in old["resources"].items() if not k.startswith(("template-skills/", "skills/"))
        }
        with patch.object(tools, "_skill_value", return_value=old):
            result = tools._production_read_production_skill({}).payload
        self.assertEqual(result["content"], old["resources"]["SKILL.md"])
        self.assertEqual(result["applied_template_skills"], [])

    def test_skill_instruction_changes_update_hashes_and_invalid_bindings_are_rejected(self):
        import shutil
        import tempfile
        from pathlib import Path

        original = get_skill()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(str(files("clipen.skills").joinpath("builtin")), root / "builtin")
            document = root / "builtin" / original["templates"][0]["skill_resource"]
            document.write_text(document.read_text() + "\n本次增加新的故事驗收條件。\n")
            with patch("clipen.skills.catalog.files", return_value=root):
                first, other = get_skill("clipen-travel-vlog"), get_skill("clipen-product-demo")
            self.assertNotEqual(get_skill("clipen-travel-vlog")["content_hash"], first["content_hash"])
            self.assertEqual(get_skill("clipen-product-demo")["content_hash"], other["content_hash"])
            manifest = document.parent / "manifest.json"
            value = json.loads(manifest.read_text())
            value["visual_style_id"] = "tpl-product-demo"
            manifest.write_text(json.dumps(value))
            with patch("clipen.skills.catalog.files", return_value=root), self.assertRaises(ProjectError) as error:
                get_skill()
            self.assertEqual(error.exception.code, "template_skill_invalid")


class NativeIdentityTests(unittest.TestCase):
    def test_all_channel_packages_are_distinct_with_identical_type_and_palette(self):
        from PIL import Image, ImageDraw

        from clipen.domain import EditTimeline, Rational, TextClip, Track, TrackKind
        from clipen.rendering.program_components import component_bounds, draw_program_component
        from clipen.rendering.titles import TitleCardRenderer

        skill = get_skill()
        masks = set()
        for width, height in ((640, 360), (1920, 1080)):
            timeline = EditTimeline(
                "identity",
                "identity",
                None,
                None,
                None,
                Rational(30, 1),
                width,
                height,
                90,
                (Track("video", TrackKind.VIDEO, ()),),
                production_session_id="identity",
            )
            for template in skill["templates"]:
                _, presets = style_presets(skill, template["visual_style_id"])
                # Structural variety must survive removing colour and font differences.
                presets["palette"] = skill["presets"]["palette"]
                for component in skill["components"]:
                    kind = component["component_id"]
                    with self.subTest(width=width, template=template["template_id"], kind=kind):
                        item = TextClip("identity", 0, 90, "現場故事\n當下的觀察", "identity")
                        profile = {
                            "kind": "program_component",
                            "component_id": kind,
                            "presets": presets,
                            "x": 0.07,
                            "y": 0.12,
                        }
                        if component["requires_target"]:
                            profile["target"] = {"x": 0.78, "y": 0.48, "radius": 0.04}
                        bounds, _, _ = component_bounds(None, TitleCardRenderer._font_path(), timeline, item, profile)
                        rendered = []
                        for frame in (0, 35, 89):
                            image = Image.new("RGBA", (width, height))
                            draw_program_component(
                                ImageDraw.Draw(image), TitleCardRenderer._font_path(), timeline, item, profile, frame
                            )
                            alpha = image.getchannel("A")
                            box = alpha.getbbox()
                            self.assertIsNotNone(box)
                            if not component["requires_target"]:
                                self.assertGreaterEqual(box[0], bounds[0] - 1)
                                self.assertGreaterEqual(box[1], bounds[1] - 1)
                                self.assertLessEqual(box[2], bounds[2] + 2)
                                self.assertLessEqual(box[3], bounds[3] + 2)
                            rendered.append(hashlib.sha256(alpha.tobytes()).hexdigest())
                        self.assertNotEqual(rendered[0], rendered[1])
                        self.assertNotEqual(rendered[1], rendered[2])
                        if width == 640 and kind == "program_title":
                            masks.add(rendered[1])
        self.assertEqual(len(masks), len(skill["templates"]))

    def test_retired_template_identities_still_render_for_frozen_sessions(self):
        from PIL import Image, ImageDraw

        from clipen.domain import EditTimeline, Rational, TextClip, Track, TrackKind
        from clipen.rendering.program_components import draw_program_component
        from clipen.rendering.program_identities import IDENTITIES
        from clipen.rendering.titles import TitleCardRenderer

        skill = get_skill()
        self.assertFalse(
            {"tpl-" + name for name in IDENTITIES} & {s["visual_style_id"] for s in skill["visual_styles"]}
        )
        timeline = EditTimeline(
            "legacy",
            "legacy",
            None,
            None,
            None,
            Rational(30, 1),
            640,
            360,
            90,
            (Track("video", TrackKind.VIDEO, ()),),
            production_session_id="legacy",
        )
        for name in sorted(IDENTITIES):
            with self.subTest(identity=name):
                _, presets = style_presets(skill, "variety")
                presets["appearance"] = {**presets["appearance"], "identity": name}
                image = Image.new("RGBA", (640, 360))
                draw_program_component(
                    ImageDraw.Draw(image),
                    TitleCardRenderer._font_path(),
                    timeline,
                    TextClip("legacy", 0, 90, "現場故事\n當下的觀察", "legacy"),
                    {
                        "kind": "program_component",
                        "component_id": "program_title",
                        "presets": presets,
                        "x": 0.07,
                        "y": 0.12,
                    },
                    35,
                )
                self.assertIsNotNone(image.getchannel("A").getbbox())

    def test_review_verdicts_mark_strengths_and_drawbacks_differently(self):
        from PIL import Image, ImageDraw

        from clipen.domain import EditTimeline, Rational, TextClip, Track, TrackKind
        from clipen.rendering.program_components import draw_program_component
        from clipen.rendering.titles import TitleCardRenderer

        skill = get_skill()
        _, presets = style_presets(skill, "tpl-product-review")
        timeline = EditTimeline(
            "verdict",
            "verdict",
            None,
            None,
            None,
            Rational(30, 1),
            960,
            540,
            90,
            (Track("video", TrackKind.VIDEO, ()),),
            production_session_id="verdict",
        )

        def colours(text):
            image = Image.new("RGBA", (960, 540))
            draw_program_component(
                ImageDraw.Draw(image),
                TitleCardRenderer._font_path(),
                timeline,
                TextClip("verdict", 0, 90, text, "verdict"),
                {"kind": "program_component", "component_id": "fact", "presets": presets, "x": 0.1, "y": 0.4},
                45,
            )
            return {pixel[:3] for pixel in image.getdata() if pixel[3] == 255}

        accent, secondary = (tuple(presets["palette"][name][:3]) for name in ("accent", "secondary"))
        strength, drawback = colours("優點\n對焦很快"), colours("缺點\n機身有點重")
        self.assertIn(accent, strength)
        self.assertNotIn(secondary, strength)
        self.assertIn(secondary, drawback)
        self.assertNotIn(accent, drawback)

    def test_template_positions_and_user_adjustments_survive_native_mcp_updates(self):
        f = fixtures.SkillProductionTests()
        f.setUp()
        try:
            from clipen.editing.production_media import load_text_styles

            f.cut()
            expected = style_presets(get_skill(), "tpl-travel-highlight")[1]["component_positions"]["program_title"]
            f.component(
                component_id="program_title", text="今天的故事", visual_style_id="tpl-travel-highlight", **expected
            )

            def profile():
                item = next(i for t in f.tools._timeline().tracks for i in t.items if i.item_id == "dish-label")
                return load_text_styles(f.projects, f.project.project_id)[item.style_id]

            self.assertEqual({axis: profile()[axis] for axis in ("x", "y")}, expected)
            self.assertEqual(profile()["presets"]["appearance"]["identity"], "travel-highlight")
            frozen = f.repository.production_session(f.project.project_id, f.run.run_id)["skill"]
            self.assertIn("references/channel-design.md", frozen["resources"])
        finally:
            f.tearDown()
