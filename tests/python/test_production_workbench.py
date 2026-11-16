"""Restoring and refinement retain the exact native film and explicit music."""

import json
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_skill_production as fixtures
from legacy_components import place

from clipen.editing.workbench import edit, import_music, state
from clipen.projects import ProjectError


class ProductionWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SkillProductionTests()
        self.f.setUp()
        self.f.cut()
        self.repository = self.f.repository
        self.project = self.f.project.project_id
        self.run = self.f.run.run_id

    def tearDown(self):
        self.f.tearDown()

    def stop(self):
        self.repository.cancel(self.project, self.run)

    def test_identity_renderer_changes_invalidate_preview_recipe(self):
        import tempfile

        from clipen.editing.production_preview import render_recipe_id
        from clipen.rendering import program_identities

        before = render_recipe_id()
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / "program_identities.py"
            changed.write_bytes(
                Path(program_identities.__file__).read_bytes() + b"\n# new identity renderer revision\n"
            )
            with patch.object(program_identities, "__file__", str(changed)):
                self.assertNotEqual(render_recipe_id(), before)
        self.assertEqual(render_recipe_id(), before)

    def test_status_keeps_successful_preview_when_new_output_is_pending_or_fails(self):
        from clipen.editing.coordinator import EditAgentCoordinator
        from clipen.editing.production_preview import _event, render_recipe_id
        from clipen.rendering import PreviewRenderRepository

        renders = PreviewRenderRepository(self.f.projects)
        revision = self.repository.current_revision(self.project, self.run)
        recipe = render_recipe_id()
        old = renders.create(self.project, "previous-good-output", revision.revision_id)
        renders.mark_running(self.project, old["render_id"])
        # Exercise durable render states; encoding is covered by native render tests.
        path = renders.output_path(self.project, old["render_id"])
        path.write_bytes(b"isolated-output")
        renders.complete(self.project, old["render_id"], path.name, path.stat().st_size)
        _event(
            self.f.tools,
            "production_preview",
            {"render_id": old["render_id"], "revision_id": revision.revision_id, "recipe_id": recipe},
        )
        self.repository.finish(self.project, self.run, "完成舊版")
        coordinator = EditAgentCoordinator(self.repository, self.f.root)
        try:
            for ending in ("failed", "cancelled"):
                pending = renders.create(self.project, "next-output-" + ending, revision.revision_id)
                _event(
                    self.f.tools,
                    "production_preview",
                    {"render_id": pending["render_id"], "revision_id": revision.revision_id, "recipe_id": recipe},
                )
                for state in ("queued", "running", ending):
                    if state == "running":
                        renders.mark_running(self.project, pending["render_id"])
                    elif state == ending:
                        renders.finish_unsuccessful(
                            self.project, pending["render_id"], ending, error_code="ffmpeg_render_failed"
                        )
                    production = coordinator.status(self.project)["production"]
                    self.assertEqual(production["preview"]["state"], state)
                    self.assertEqual(production["last_successful_preview"]["render_id"], old["render_id"])
                    self.assertEqual(production["last_successful_preview"]["state"], "succeeded")
                with patch("clipen.editing.production_preview.render_recipe_id", return_value="changed-recipe"):
                    self.assertTrue(coordinator.status(self.project)["production"]["last_successful_preview"]["stale"])
        finally:
            coordinator.close()

    def test_history_preview_uses_latest_successful_output_without_restoring(self):
        from clipen.rendering import PreviewRenderRepository

        self.stop()
        before = self.repository.get(self.project, self.run)
        revision = self.repository.current_revision(self.project, self.run)
        renders = PreviewRenderRepository(self.f.projects)
        without_output = state(self.repository, self.project, self.run)["history"][0]
        self.assertIsNone(without_output["preview_url"])
        expected_duration = (
            revision.timeline.duration_frames
            * revision.timeline.frame_rate.denominator
            / revision.timeline.frame_rate.numerator
        )
        self.assertEqual(without_output["duration_seconds"], expected_duration)
        good = renders.create(self.project, "history-success", revision.revision_id)
        renders.mark_running(self.project, good["render_id"])
        path = renders.output_path(self.project, good["render_id"])
        path.write_bytes(b"isolated-output")
        renders.complete(self.project, good["render_id"], path.name, path.stat().st_size)
        failed = renders.create(self.project, "history-failed", revision.revision_id)
        renders.finish_unsuccessful(self.project, failed["render_id"], "failed", error_code="test_failure")
        version = state(self.repository, self.project, self.run)["history"][0]
        self.assertEqual(version["preview_url"], f"/api/projects/{self.project}/preview/{good['render_id']}/media")
        self.assertEqual(self.repository.get(self.project, self.run), before)
        self.assertEqual(self.repository.current_revision(self.project, self.run), revision)

    def test_user_can_only_restore_versions_without_ai_budget_and_export(self):
        place(
            self.f.tools,
            element_id="header",
            item_id="agent-video-0001",
            component_id="headline",
            start_offset_frames=0,
            duration_frames=60,
            text="原來的標題",
            x=0.08,
            y=0.25,
            evidence="測試畫面標註",
            summary="元件",
        )
        before = self.repository.current_revision(self.project, self.run)
        self.f.call("remove_item", item_id="header", summary="AI 拿掉標題")
        changed = self.repository.current_revision(self.project, self.run)
        budget = self.repository.get(self.project, self.run).tool_calls
        restore = {"revision_id": before.revision_id, "summary": "還原標題"}
        with self.assertRaises(ProjectError):
            edit(self.repository, self.project, self.run, "restore_revision", restore, changed.revision_id, "busy")
        self.stop()
        # Other changes go through review notes; the workbench only rewords graphics directly.
        with self.assertRaises(ProjectError) as rejected:
            edit(
                self.repository,
                self.project,
                self.run,
                "edit_text",
                {
                    "item_id": "header",
                    "start_frame": 0,
                    "duration_frames": 60,
                    "text": "我修好的標題",
                    "style_id": "title.caption",
                    "summary": "修改標題",
                },
                changed.revision_id,
                "user-text",
            )
        self.assertEqual(rejected.exception.code, "tool_not_allowed")
        edit(self.repository, self.project, self.run, "restore_revision", restore, changed.revision_id, "user-restore")
        current = self.repository.current_revision(self.project, self.run)
        # Revisions are content-addressed, so restoring selects the earlier version again.
        self.assertEqual(current.revision_id, before.revision_id)
        self.assertEqual(self.repository.get(self.project, self.run).tool_calls, budget)
        # Going back is the newest step in the history, and says it went back.
        top = state(self.repository, self.project, self.run)["history"][0]
        self.assertEqual((top["kind"], top["revision_id"]), ("restore", before.revision_id))
        self.assertTrue(
            edit(
                self.repository,
                self.project,
                self.run,
                "restore_revision",
                restore,
                changed.revision_id,
                "user-restore",
            ).get("replayed")
        )
        with self.assertRaises(ProjectError):
            edit(self.repository, self.project, self.run, "restore_revision", restore, changed.revision_id, "stale")
        info = state(self.repository, self.project, self.run)
        self.assertFalse({"tools", "styles", "visual_styles"} & set(info))
        from clipen.editing.workbench import tools_for

        exported = tools_for(self.repository, self.project, self.run).production_call("export_edit", {}).payload
        path = (
            self.f.root
            / "projects"
            / self.project
            / "agent-production"
            / self.run
            / "exports"
            / (exported["revision_id"] + ".json")
        )
        self.assertEqual(json.loads(path.read_text())["timeline"], current.timeline.to_payload())

    def test_user_rewords_a_component_and_keeps_its_look(self):
        place(
            self.f.tools,
            element_id="header",
            item_id="agent-video-0001",
            component_id="headline",
            start_offset_frames=0,
            duration_frames=60,
            text="原來的標題",
            x=0.08,
            y=0.25,
            evidence="測試畫面標註",
            summary="元件",
        )
        before = self.repository.current_revision(self.project, self.run)
        budget = self.repository.get(self.project, self.run).tool_calls
        self.stop()

        def reword(arguments, mutation, expected=None):
            return edit(
                self.repository,
                self.project,
                self.run,
                "set_component_text",
                arguments,
                expected or self.repository.current_revision(self.project, self.run).revision_id,
                mutation,
            )

        for arguments, code in (
            ({"element_id": "header", "text": "  "}, "component_text_empty"),
            ({"element_id": "header", "text": "兩\n行"}, "component_text_overflow"),
            ({"element_id": "header", "text": "長" * 80}, "component_text_overflow"),
            ({"element_id": "header", "text": "原來的標題"}, "component_unchanged"),
            ({"element_id": "agent-video-0001", "text": "鏡頭"}, "component_not_found"),
        ):
            with self.assertRaises(ProjectError) as rejected:
                reword(arguments, "bad-" + code + str(len(arguments["text"])))
            self.assertEqual(rejected.exception.code, code)
        reword({"element_id": "header", "text": " 我改好的標題 ", "detail": "補充一行"}, "user-reword")
        current = self.repository.current_revision(self.project, self.run)
        item = lambda revision: next(i for t in revision.timeline.tracks for i in t.items if i.item_id == "header")
        self.assertEqual(item(current).text, "我改好的標題\n補充一行")
        self.assertEqual(
            (item(current).style_id, item(current).start_frame, item(current).duration_frames),
            (item(before).style_id, item(before).start_frame, item(before).duration_frames),
        )
        self.assertEqual(self.repository.get(self.project, self.run).tool_calls, budget)
        self.assertEqual(state(self.repository, self.project, self.run)["history"][0]["label"], "手動修改：元件文字")
        # Changing it back gives the same picture as before, yet it is a new step at the top of the history.
        reword({"element_id": "header", "text": "原來的標題", "detail": ""}, "user-reword-back")
        history = state(self.repository, self.project, self.run)["history"]
        self.assertEqual(self.repository.current_revision(self.project, self.run).revision_id, before.revision_id)
        self.assertEqual(history[0]["revision_id"], before.revision_id)
        self.assertEqual(history[0]["number"], history[1]["number"] + 1)
        # A stale version is refused rather than overwriting what changed since.
        with self.assertRaises(ProjectError):
            reword({"element_id": "header", "text": "再改"}, "stale", current.revision_id)
        # The AI never sees this tool; it revises graphics through its own component tools.
        with self.assertRaises(ProjectError) as hidden:
            self.f.call("set_component_text", element_id="header", text="AI 改的")
        self.assertEqual(hidden.exception.code, "tool_not_allowed")

    def test_user_moves_restyles_retimes_and_removes_a_component(self):
        from clipen.editing.production_media import load_text_styles

        place(
            self.f.tools,
            element_id="header",
            item_id="agent-video-0001",
            component_id="headline",
            start_offset_frames=0,
            duration_frames=60,
            text="原來的標題",
            x=0.08,
            y=0.25,
            evidence="測試畫面標註",
            summary="元件",
        )
        place(
            self.f.tools,
            element_id="later",
            item_id="agent-video-0001",
            component_id="headline",
            start_offset_frames=70,
            duration_frames=30,
            text="後面的標題",
            x=0.08,
            y=0.25,
            evidence="測試畫面標註",
            summary="元件",
        )
        self.stop()
        current = lambda: self.repository.current_revision(self.project, self.run)
        item = lambda key: next(i for t in current().timeline.tracks for i in t.items if i.item_id == key)

        def change(arguments, mutation):
            return edit(
                self.repository,
                self.project,
                self.run,
                "edit_component",
                {"element_id": "header", **arguments},
                current().revision_id,
                mutation,
            )

        editing = state(self.repository, self.project, self.run)["component_editing"]
        self.assertEqual((editing["components"]["header"]["x"], editing["components"]["header"]["y"]), (0.08, 0.25))
        self.assertTrue(editing["visual_styles"])
        self.assertTrue(editing["font_styles"])
        before = item("header")
        change({"x": 0.3, "y": 0.4}, "move")
        profile = load_text_styles(self.f.projects, self.project)[item("header").style_id]
        self.assertEqual((profile["x"], profile["y"], item("header").text), (0.3, 0.4, "原來的標題"))
        other = next(v["id"] for v in editing["visual_styles"] if v["id"] != profile["visual_style_id"])
        change({"visual_style_id": other}, "restyle")
        self.assertEqual(
            load_text_styles(self.f.projects, self.project)[item("header").style_id]["visual_style_id"], other
        )
        # Moving it later so it overlaps the next graphic puts them on separate lanes.
        change({"start_offset_frames": 50, "duration_frames": 40}, "retime")
        self.assertEqual((item("header").start_frame, item("header").duration_frames), (before.start_frame + 50, 40))
        lanes = [t for t in current().timeline.tracks if t.track_id.startswith("program-components")]
        self.assertEqual(len(lanes), 2)
        for arguments, code in (
            ({"x": 1.2}, "component_unsafe_position"),
            ({"start_offset_frames": 0, "duration_frames": 10**6}, "component_time_out_of_bounds"),
            ({"duration_frames": 2}, "component_time_out_of_bounds"),
            ({"visual_style_id": "nope"}, "visual_style_unavailable"),
            ({"font_style_id": "nope"}, "font_style_unavailable"),
            ({}, "component_unchanged"),
        ):
            with self.assertRaises(ProjectError) as rejected:
                change(arguments, "bad-" + code + str(arguments))
            self.assertEqual(rejected.exception.code, code)
        edit(
            self.repository,
            self.project,
            self.run,
            "remove_program_component",
            {"element_id": "header", "summary": "使用者刪除元件"},
            current().revision_id,
            "remove",
        )
        self.assertFalse(any(i.item_id == "header" for t in current().timeline.tracks for i in t.items))
        labels = [v["label"] for v in state(self.repository, self.project, self.run)["history"][:4]]
        self.assertEqual(
            labels, ["手動修改：移除節目元件", "手動修改：修改元件", "手動修改：修改元件", "手動修改：修改元件"]
        )

    def test_user_resizes_and_recolours_a_component(self):
        from PIL import Image, ImageDraw

        from clipen.editing.production_media import load_text_styles
        from clipen.rendering.program_components import component_bounds, draw_program_component
        from clipen.rendering.titles import TitleCardRenderer

        place(
            self.f.tools,
            element_id="header",
            item_id="agent-video-0001",
            component_id="headline",
            start_offset_frames=0,
            duration_frames=60,
            text="標題",
            x=0.08,
            y=0.2,
            evidence="測試畫面標註",
            summary="元件",
        )
        self.stop()
        current = lambda: self.repository.current_revision(self.project, self.run)
        item = lambda: next(i for t in current().timeline.tracks for i in t.items if i.item_id == "header")
        original = item().style_id

        def change(arguments, mutation):
            return edit(
                self.repository,
                self.project,
                self.run,
                "edit_component",
                {"element_id": "header", **arguments},
                current().revision_id,
                mutation,
            )

        font = TitleCardRenderer._font_path()
        timeline = current().timeline
        box = lambda profile: component_bounds(
            ImageDraw.Draw(Image.new("RGBA", (1, 1))), font, timeline, item(), profile
        )[0]
        designed = box(load_text_styles(self.f.projects, self.project)[original])
        change({"scale": 1.4, "colors": {"accent": "#FF0000"}}, "bigger-red")
        profile = load_text_styles(self.f.projects, self.project)[item().style_id]
        self.assertEqual(
            (profile["scale"], profile["colors"], profile["presets"]["palette"]["accent"][:3]),
            (1.4, {"accent": "#ff0000"}, [255, 0, 0]),
        )
        bigger = box(profile)
        # It grows from its top-left corner.
        self.assertEqual(bigger[:2], designed[:2])
        self.assertGreater(bigger[2] - bigger[0], (designed[2] - designed[0]) * 1.3)
        picture = Image.new("RGBA", (timeline.width, timeline.height), (0, 0, 0, 0))
        draw_program_component(ImageDraw.Draw(picture), font, timeline, item(), profile, 20)
        self.assertIsNotNone(picture.getbbox())
        # Back to the designed size and the style's colours gives the graphic its original identity.
        change({"scale": 1, "colors": {}}, "reset")
        self.assertEqual(item().style_id, original)
        for arguments, code in (
            ({"scale": 3}, "component_scale_invalid"),
            ({"colors": {"accent": "red"}}, "component_color_invalid"),
            ({"colors": {"shadow": "#000000"}}, "component_color_invalid"),
            ({"x": 0.6, "scale": 1.6}, "program_component_overflow"),
        ):
            with self.assertRaises(ProjectError) as rejected:
                change(arguments, "bad-" + code + str(arguments))
            self.assertEqual(rejected.exception.code, code)
        editing = state(self.repository, self.project, self.run)["component_editing"]
        self.assertEqual(set(editing["visual_styles"][0]["colors"]), {"accent", "paper", "ink"})
        self.assertEqual((editing["components"]["header"]["scale"], editing["components"]["header"]["colors"]), (1, {}))

    def test_refine_keeps_timeline_and_frozen_style_and_is_idempotent(self):
        before = self.repository.current_revision(self.project, self.run)
        frozen = self.repository.production_session(self.project, self.run)["skill"]
        self.stop()
        run, created = self.repository.request_refinement(
            self.project, self.run, "refine-one", before.revision_id, "保留鏡頭，只修改字幕。"
        )
        self.assertTrue(created)
        self.assertEqual(run.current_revision_id, before.revision_id)
        self.assertEqual(run.tool_calls, 0)
        self.assertEqual(self.repository.current_revision(self.project, self.run).timeline, before.timeline)
        session = self.repository.production_session(self.project, self.run)
        self.assertEqual(session["skill"], frozen)
        self.assertEqual(session["intent"]["latest_revision_request"]["instruction"], "保留鏡頭，只修改字幕。")
        from clipen.editing import EditAgentTools

        fresh = EditAgentTools(self.repository, self.project, self.run)
        fresh.activate()
        try:
            with self.assertRaises(ProjectError) as rejected:
                fresh.call(
                    "replace_sequence",
                    {
                        "clips": [],
                        "summary": "不應重新生成",
                        "expected_revision_id": before.revision_id,
                        "mutation_id": "blocked-rebuild",
                    },
                )
            self.assertEqual(rejected.exception.code, "incremental_revision_required")
        finally:
            fresh.close_production()

        same, created = self.repository.request_refinement(
            self.project, self.run, "refine-one", before.revision_id, "保留鏡頭，只修改字幕。"
        )
        self.assertFalse(created)
        self.assertEqual(same.run_id, run.run_id)
        with self.assertRaises(ProjectError):
            self.repository.request_refinement(self.project, self.run, "refine-two", before.revision_id, "不同修改")

    def test_local_music_is_frozen_as_user_choice_not_claimed_public_license(self):
        self.stop()
        from clipen.editing.production_media import AgentAudioStore

        directory = AgentAudioStore(self.f.projects, self.project).directory / "upload-test"
        directory.mkdir()
        path = directory / "music.wav"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-y", str(path)],
            check=True,
            capture_output=True,
        )
        asset = import_music(self.repository, self.project, path, "我的配樂.wav")
        self.assertEqual(asset["provenance"]["source"], "user_upload")
        self.assertFalse(asset["provenance"]["rights_verified"])
        before = self.repository.current_revision(self.project, self.run)
        run, _ = self.repository.request_refinement(
            self.project, self.run, "use-my-music", before.revision_id, "換成我選的音樂", asset["asset_id"]
        )
        self.assertEqual(
            self.repository.production_session(self.project, self.run)["intent"]["music_choice"]["asset_id"],
            asset["asset_id"],
        )
        self.repository.cancel(self.project, self.run)
        with self.assertRaises(ProjectError):
            self.repository.request_refinement(
                self.project, self.run, "bad-music", before.revision_id, "換音樂", "not-this-project"
            )
        initial, created = self.repository.create_run(
            self.project,
            "initial-user-music",
            production={
                "skill_id": "travel-program",
                "intent": "使用我提供的配樂製作影片",
                "target_duration_seconds": 300,
                "music_asset_id": asset["asset_id"],
            },
        )
        self.assertTrue(created)
        self.assertEqual(
            self.repository.production_session(self.project, initial.run_id)["intent"]["music_choice"]["asset_id"],
            asset["asset_id"],
        )

    def test_authenticated_upload_edit_export_and_refinement_api(self):
        import asyncio

        from test_project_api import request_app

        from clipen.controller import ControllerConfig, LocalSessionAuthority, ProviderState, ProviderStatus, create_app
        from clipen.editing import EditAgentCoordinator
        from clipen.providers import ProviderCancelled

        original = self.repository.current_revision(self.project, self.run)
        place(
            self.f.tools,
            element_id="editable-header",
            item_id="agent-video-0001",
            component_id="headline",
            start_offset_frames=0,
            duration_frames=60,
            text="原標題",
            x=0.08,
            y=0.25,
            evidence="測試標題",
            summary="標題",
        )
        self.stop()
        before = self.repository.current_revision(self.project, self.run)
        started = threading.Event()

        class LocalAgent:
            def __init__(self, projects_root, attempt_root, repository):
                self.repository = repository

            def execute(self, project_id, run_id, *, cancel):
                self.repository.activate(project_id, run_id)
                started.set()
                cancel.wait(3)
                raise ProviderCancelled("test")

        static = self.f.root / "api-dist"
        static.mkdir()
        (static / "index.html").write_text("Clipen")
        coordinator = EditAgentCoordinator(self.repository, self.f.root, adapter_factory=LocalAgent)
        app = create_app(
            ControllerConfig(static),
            ProviderStatus("Codex", ProviderState.READY, "ready"),
            LocalSessionAuthority(),
            edit_agents=coordinator,
        )

        def request(method, path, payload=None, headers=None, raw=None):
            return asyncio.run(
                request_app(
                    app,
                    method,
                    path,
                    headers=headers,
                    body=raw if raw is not None else b"" if payload is None else json.dumps(payload).encode(),
                )
            )

        try:
            base = f"/api/projects/{self.project}/production"
            work = f"{base}/{self.run}/workbench"
            self.assertEqual(request("GET", work)[0], 401)
            _, headers, _ = request(
                "POST",
                "/api/session/connect",
                headers={"origin": "http://localhost:4200", "sec-fetch-site": "same-origin"},
            )
            cookie = headers["set-cookie"].split(";", 1)[0]
            _, _, body = request("GET", "/api/status", headers={"cookie": cookie})
            csrf = json.loads(body)["session"]["csrf_token"]
            auth = {"cookie": cookie}
            mutation = {
                **auth,
                "origin": "http://localhost:4200",
                "sec-fetch-site": "same-origin",
                "content-type": "application/json",
                "x-clipen-csrf": csrf,
            }
            code, _, body = request("GET", work, headers=auth)
            self.assertEqual(code, 200, body)
            self.assertEqual(json.loads(body)["revision"]["revision_id"], before.revision_id)
            manual = {
                "tool": "edit_text",
                "arguments": {
                    "item_id": "editable-header",
                    "text": "人工修改",
                    "style_id": "title.caption",
                    "start_frame": 0,
                    "duration_frames": 60,
                    "summary": "人工修改",
                },
                "expected_revision_id": before.revision_id,
                "mutation_id": "api-user-edit",
            }
            self.assertEqual(request("POST", work + "/edit", manual, mutation)[0], 422)
            args = {
                "tool": "restore_revision",
                "arguments": {"revision_id": original.revision_id, "summary": "還原"},
                "expected_revision_id": before.revision_id,
                "mutation_id": "api-user-restore",
            }
            self.assertEqual(request("POST", work + "/edit", args, auth)[0], 403)
            code, _, body = request("POST", work + "/edit", args, mutation)
            self.assertEqual(code, 200, body)
            current = self.repository.current_revision(self.project, self.run)
            self.assertNotEqual(current.revision_id, before.revision_id)
            code, _, body = request("POST", work + "/export", {}, mutation)
            self.assertEqual(code, 200, body)
            self.assertIn("download_url", json.loads(body))
            source = self.f.root / "upload.wav"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=2", "-y", str(source)],
                check=True,
                capture_output=True,
            )
            upload_headers = {**mutation, "content-type": "application/octet-stream"}
            code, _, body = request(
                "POST", base + "/music?filename=my-song.wav", headers=upload_headers, raw=source.read_bytes()
            )
            self.assertEqual(code, 201, body)
            asset = json.loads(body)["asset"]
            self.assertEqual(asset["provenance"]["source"], "user_upload")
            self.assertEqual(
                request("POST", base + "/music?filename=bad.exe", headers=upload_headers, raw=b"bad")[0], 422
            )
            refine = {
                "request_id": "api-refine-film",
                "instruction": "保留畫面，使用我的音樂",
                "consent": True,
                "expected_revision_id": current.revision_id,
                "music_asset_id": asset["asset_id"],
            }
            code, _, body = request("POST", work + "/refine", refine, mutation)
            self.assertEqual(code, 200, body)
            self.assertTrue(started.wait(2))
            self.assertEqual(self.repository.current_revision(self.project, self.run).revision_id, current.revision_id)
            from clipen.editing import EditAgentTools

            tools = EditAgentTools(self.repository, self.project, self.run)
            with self.assertRaises(ProjectError):
                tools.production_call("search_music", {"tags": ["calm"]})
            tools.close_production()
        finally:
            coordinator.close()

    def test_second_ai_round_can_render_after_preview_budget_is_renewed(self):
        from clipen.rendering import PreviewRenderRepository

        class Renderer:
            def render(self, project, timeline, output, cancel, progress):
                output.write_bytes(b"preview")
                progress(1)

        self.f.tools.preview_renderer = Renderer()

        def render():
            result = self.f.tools.production_call("render_preview", {}).payload
            for cancel, thread in self.f.tools._preview_jobs.values():
                thread.join(3)
            self.assertEqual(
                PreviewRenderRepository(self.f.projects).get(self.project, result["render"]["render_id"])["state"],
                "succeeded",
            )
            return result["render"]["render_id"]

        with (
            patch("clipen.editing.production_preview.verify_output"),
            patch.object(
                self.f.tools,
                "_production_validate_sequence",
                return_value=type("Result", (), {"payload": {"render_issues": []}})(),
            ),
        ):
            first = render()
            self.stop()
            revision = self.repository.current_revision(self.project, self.run)
            self.repository.request_refinement(
                self.project, self.run, "second-render-round", revision.revision_id, "只降低原音"
            )
            self.repository.activate(self.project, self.run)
            self.f.call("set_source_audio", item_id="agent-video-0001", mode="mute", gain_db=0, summary="第二輪修改")
            second = render()
            self.assertNotEqual(first, second)
            self.assertEqual(self.repository.get(self.project, self.run).preview_renders, 1)

    def test_manual_preview_does_not_consume_ai_budget_or_cancel_terminal_run(self):
        self.stop()
        from clipen.editing.coordinator import EditAgentCoordinator

        before = self.repository.get(self.project, self.run)

        class Renderer:
            def render(self, project, timeline, output, cancel, progress):
                progress(0.5)
                self.cancelled = cancel.is_set()
                output.write_bytes(b"preview")
                progress(1)

        renderer = Renderer()
        coordinator = EditAgentCoordinator(self.repository, self.f.root)
        from clipen.editing.workbench import tools_for

        tools = tools_for(self.repository, self.project, self.run)
        tools.preview_renderer = renderer
        coordinator._user_previews[(self.project, self.run)] = tools
        try:
            with (
                patch("clipen.editing.production_preview.verify_output"),
                patch.object(
                    tools,
                    "_production_validate_sequence",
                    return_value=type("Result", (), {"payload": {"render_issues": []}})(),
                ),
            ):
                result = coordinator.workbench_render(self.project, self.run, before.current_revision_id)
                for cancel, thread in tools._preview_jobs.values():
                    thread.join(3)
            from clipen.rendering import PreviewRenderRepository

            self.assertEqual(
                PreviewRenderRepository(self.f.projects).get(self.project, result["render"]["render_id"])["state"],
                "succeeded",
            )
            after = self.repository.get(self.project, self.run)
            self.assertEqual(after.preview_renders, before.preview_renders)
            self.assertEqual(after.state, before.state)
            self.assertFalse(renderer.cancelled)
        finally:
            coordinator.close()
