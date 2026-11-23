"""Cold-start production, source timing and actual graphics/audio output; no paid AI."""

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from clipen.analysis.transcription import TranscriptionResult, TranscriptSegment
from clipen.domain import EditTimeline, TextClip, TrackKind
from clipen.editing import EditAgentRepository, EditAgentTools
from clipen.editing.alignment import validate_alignment
from clipen.media import (
    AssetCandidate,
    AssetKind,
    InventoryRepository,
    InventoryState,
    ItemStatus,
    MediaPreview,
    MediaStream,
    ProbeStatus,
)
from clipen.projects import DirectoryReference, DirectoryRole, ProjectError, ProjectRepository
from clipen.rendering import FfmpegPreviewRenderer
from clipen.skills import get_skill


class SkillProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.projects = ProjectRepository(self.root / "projects")
        self.project = self.projects.create_project("fresh-project", "全新節目")
        source = self.root / "source"
        source.mkdir()
        info = source.stat()
        self.project = self.projects.set_directory(
            self.project.project_id,
            "grant-source",
            "a" * 64,
            self.project.revision,
            DirectoryReference(
                DirectoryRole.SOURCE,
                str(source),
                str(source),
                "原片",
                info.st_dev,
                info.st_ino,
                "2026-10-20T00:00:00+00:00",
            ),
        )
        inventory = InventoryRepository(self.projects)
        run = inventory.create_run(self.project.project_id, "source-inventory", self.project.revision)
        inventory.transition(self.project.project_id, run.run_id, InventoryState.RUNNING, phase="probing")
        inventory.stage_items(
            self.project.project_id,
            run.run_id,
            (
                AssetCandidate(
                    DirectoryRole.SOURCE,
                    "source.mp4",
                    AssetKind.VIDEO,
                    ItemStatus.USABLE,
                    None,
                    1024,
                    12345,
                    "b" * 64,
                    ProbeStatus.READY,
                    {"duration_seconds": "12"},
                    (
                        MediaStream(
                            0,
                            "video",
                            "h264",
                            {
                                "start_pts": "90000",
                                "duration_ts": "1080000",
                                "time_base": "1/90000",
                                "width": 640,
                                "height": 360,
                            },
                        ),
                        MediaStream(
                            1, "audio", "aac", {"start_pts": "48000", "duration_ts": "576000", "time_base": "1/48000"}
                        ),
                    ),
                ),
            ),
        )
        inventory.publish(self.project.project_id, run.run_id)
        self.repository = EditAgentRepository(self.projects)
        self.run, _ = self.repository.create_run(
            self.project.project_id,
            "create-production",
            production={"skill_id": "travel-program", "intent": "用真實對話製作節目，配合字幕、音樂與資訊標註。"},
            max_tool_calls=150,
        )
        self.tools = EditAgentTools(self.repository, self.project.project_id, self.run.run_id)
        self.tools.activate()
        source = self.call("list_source_media").payload["sources"][0]
        self.candidate = self.call(
            "register_source_range",
            version_id=source["version_id"],
            stream_index=0,
            start_offset_seconds=0,
            duration_seconds=8,
            description="待檢視的事件範圍",
        ).payload["candidate"]["candidate_id"]

    def tearDown(self):
        self.tools.close_production()
        self.temp.cleanup()

    def call(self, tool, **args):
        from clipen.editing.tool_receipts import TIMELINE_MUTATIONS

        if tool in TIMELINE_MUTATIONS:
            args.setdefault(
                "expected_revision_id",
                self.repository.current_revision(self.project.project_id, self.run.run_id).revision_id,
            )
            args.setdefault("mutation_id", "test-" + str(time.monotonic_ns()))
        return self.tools.call(tool, args)

    def cut(self):
        self.call("set_canvas", width=640, height=360, summary="測試畫布")
        return self.call(
            "replace_sequence",
            clips=[
                {"candidate_id": self.candidate, "start_offset_seconds": 0, "duration_seconds": 4},
                {"candidate_id": self.candidate, "start_offset_seconds": 4, "duration_seconds": 4},
            ],
            summary="兩個來源區間",
        )

    def component(self, **overrides):
        args = {
            "element_id": "dish-label",
            "item_id": "agent-video-0001",
            "component_id": "fact",
            "start_offset_frames": 6,
            "duration_frames": 45,
            "text": "招牌餐點",
            "detail": "菜單標示 980 円",
            "evidence": "本測試來源資訊標註",
            "summary": "介紹餐點",
        }
        args.update(overrides)
        if args["component_id"] != "fact" and "detail" not in overrides:
            args.pop("detail")
        # Production now requires an explicit placement; tests pick each component's designed spot.
        spot = next(
            (c["default_position"] for c in get_skill()["components"] if c["component_id"] == args["component_id"]),
            {"x": 0.06, "y": 0.12},
        )
        for axis in ("x", "y"):
            args.setdefault(axis, spot[axis])
        from legacy_components import place

        return place(self.tools, **args)

    def real_media(self):
        source = self.root / "source.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x203040:size=640x360:rate=30:duration=12",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=220:sample_rate=48000:duration=12",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(source),
            ],
            check=True,
        )
        projects = self.projects

        class Media:
            def __init__(self):
                self.projects = projects

            def open(self, *args):
                return MediaPreview(os.open(source, os.O_RDONLY), source.stat().st_size, "video/mp4", (1,), (0,))

        self.tools.media = Media()
        self.tools.preview_renderer = FfmpegPreviewRenderer(self.tools.media)
        return source

    def test_starts_without_preparation_plan_or_timeline_and_freezes_skill(self):
        with self.projects.connect(self.project.project_id) as db:
            for table in ("preparation_runs", "edit_timelines"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='plan_revisions'").fetchone())
        status = self.call("get_edit_status").payload
        self.assertEqual(status["mode"], "skill_production")
        self.assertIsNone(status["base_timeline_id"])
        timeline = self.tools._timeline()
        self.assertEqual(timeline.duration_frames, 0)
        self.assertEqual(EditTimeline.from_payload(timeline.to_payload()), timeline)
        self.assertNotIn("plan_id", timeline.to_payload())
        self.assertEqual(
            self.call("read_production_skill").payload["skill"]["content_hash"], get_skill()["content_hash"]
        )
        frozen = self.repository.production_session(self.project.project_id, self.run.run_id)["skill"]
        with patch("clipen.skills.catalog.get_skill", side_effect=AssertionError("must use frozen package")):
            self.assertEqual(
                self.call("read_production_skill", resource="references/graphics.md").payload["content"],
                frozen["resources"]["references/graphics.md"],
            )
        with self.assertRaises(ProjectError):
            self.call("read_production_skill", resource="../../project.sqlite3")
        self.assertFalse(self.call("validate_sequence").payload["valid"])
        with self.assertRaises(ProjectError):
            self.call("render_preview")

    def test_status_reports_bounded_real_activity_without_invented_progress(self):
        from clipen.editing import EditAgentCoordinator

        self.cut()
        self.component()
        for _ in range(15):
            self.call("list_program_components")
        coordinator = EditAgentCoordinator(self.repository, self.root)
        status = coordinator.status(self.project.project_id)
        progress = status["production"]["progress"]
        # Repeated calls of one kind read as a single step with its latest time.
        self.assertLessEqual(len(progress["activity"]), 10)
        self.assertEqual(progress["activity"][-1]["tool"], "list_program_components")
        self.assertEqual(progress["activity"][-1]["label"], "理解製作需求與影片風格")
        self.assertGreaterEqual(progress["activity"][-1]["count"], 15)
        self.assertTrue(all(a["label"] != b["label"] for a, b in zip(progress["activity"], progress["activity"][1:])))
        self.assertEqual(progress["updated_at"], progress["activity"][-1]["created_at"])
        self.assertNotIn("percent", progress)
        self.assertIsNone(progress["ai_message"])
        # Without a rendered film there is neither a finished film nor a draft to label.
        self.assertIsNone(status["production"]["finished_preview"])
        self.assertIsNone(status["production"]["draft_preview"])
        # While the AI is still at work there is no version of its to mark as unchecked.
        self.assertIsNone(status["production"]["ai_review"])
        # Stages only move forward to the furthest real step; no percentage is invented.
        from clipen.editing.production_progress import STAGES

        self.assertEqual(list(progress["stages"]), list(STAGES))
        self.assertEqual(STAGES[progress["reached"]], "edit")
        self.call("validate_sequence")
        checked = coordinator.status(self.project.project_id)["production"]["progress"]
        self.assertEqual(STAGES[checked["reached"]], "edit")
        self.assertIsNotNone(checked["started_at"])
        progress = checked
        self.assertEqual(
            [e["sequence"] for e in progress["activity"]], sorted(e["sequence"] for e in progress["activity"])
        )
        self.repository.record_provider_outcome(
            self.project.project_id,
            self.run.run_id,
            "failed-test",
            usage=None,
            error_code="provider_stopped_early",
            message="  需要你提供音樂檔。\n" + "說明" * 1200,
        )
        failed = coordinator.status(self.project.project_id)
        self.assertEqual(failed["latest_run"]["error_code"], "provider_stopped_early")
        self.assertEqual(failed["production"]["draft"]["ordinal"], status["production"]["draft"]["ordinal"])
        last = failed["production"]["progress"]["activity"][-1]
        self.assertEqual((last["kind"], last["label"]), ("provider_failed", "AI 先停下來了，請看 AI 的說明"))
        self.assertEqual(failed["production"]["progress"]["activity"][-2], progress["activity"][-1])
        # The AI's own closing words explain the stop, kept short for the panel.
        message = failed["production"]["progress"]["ai_message"]
        self.assertTrue(message.startswith("需要你提供音樂檔。"))
        self.assertLessEqual(len(message), 2000)
        self.assertTrue(message.endswith("…"))

    def test_quantized_zero_width_alignment_is_grouped_without_invented_character_times(self):
        words = validate_alignment(
            [
                {"text": "點", "start": 2.24, "end": 2.56},
                {"text": "一", "start": 2.56, "end": 2.56},
                {"text": "個", "start": 2.64, "end": 3.04},
            ],
            "點一個",
            4,
        )
        self.assertEqual(
            words[0], {"text": "點一", "char_start": 0, "char_end": 2, "start_seconds": 2.24, "end_seconds": 2.56}
        )
        self.assertEqual(words[1]["start_seconds"], 2.64)
        leading = validate_alignment(
            [{"text": "一", "start": 1, "end": 1}, {"text": "個", "start": 1, "end": 1.3}], "一個", 2
        )
        self.assertEqual(leading[0]["text"], "一個")
        self.assertEqual(leading[0]["start_seconds"], 1)
        with self.assertRaises(ProjectError):
            validate_alignment(
                [{"text": "一", "start": 0, "end": 0}, {"text": "個", "start": 1, "end": 1.3}], "一個", 2
            )

    def test_real_aligner_output_with_grid_step_and_isolated_instants_is_kept_without_invented_time(self):
        # Recorded from the pinned aligner on real footage: 這 sits one 80 ms step before 是.
        hotel = validate_alignment(
            [
                {"text": "這", "start": 7.6, "end": 7.6},
                {"text": "是", "start": 7.68, "end": 7.76},
                {"text": "我", "start": 7.76, "end": 7.76},
                {"text": "們", "start": 7.84, "end": 8.0},
                {"text": "的", "start": 8.0, "end": 8.08},
                {"text": "飯", "start": 8.16, "end": 8.32},
                {"text": "店", "start": 8.48, "end": 8.96},
            ],
            "這是我們的飯店。",
            9.16,
        )
        self.assertEqual((hotel[0]["text"], hotel[0]["start_seconds"], hotel[0]["end_seconds"]), ("這是我", 7.6, 7.76))
        # A character far from its neighbours keeps only its observed instant.
        words = validate_alignment(
            [
                {"text": "衛", "start": 0.72, "end": 0.8},
                {"text": "星", "start": 0.8, "end": 0.96},
                {"text": "在", "start": 0.96, "end": 1.12},
                {"text": "錄", "start": 2.56, "end": 2.64},
                {"text": "了", "start": 3.52, "end": 3.52},
            ],
            "衛星在錄了",
            4,
        )
        self.assertEqual((words[-1]["start_seconds"], words[-1]["end_seconds"]), (3.52, 3.52))
        # When the speech collapses onto one instant the model lost it; keep it for repair.
        with self.assertRaises(ProjectError) as error:
            validate_alignment(
                [{"text": "吃", "start": 8.08, "end": 8.32}]
                + [{"text": c, "start": 8.32 + n, "end": 8.32 + n} for n, c in enumerate("好吃就會說")],
                "吃好吃就會說",
                20,
            )
        self.assertEqual(error.exception.code, "alignment_invalid")
        with self.assertRaises(ProjectError):
            validate_alignment([{"text": c, "start": 8.32, "end": 8.32} for c in "好吃"], "好吃", 10)

    def test_pacing_groups_split_continuous_source_instead_of_counting_fake_variety(self):
        self.cut()
        report = self.call("inspect_pacing").payload
        self.assertEqual(len(report["source_runs"]), 1)
        self.assertEqual(report["source_runs"][0]["duration_seconds"], 8)
        self.assertEqual(len(report["source_runs"][0]["item_ids"]), 2)
        self.call("reorder_clips", item_ids=["agent-video-0002", "agent-video-0001"], summary="來源區間不連續")
        self.assertEqual(len(self.call("inspect_pacing").payload["source_runs"]), 2)

    def test_caption_omission_cannot_pass_by_retaining_one_aligned_word(self):
        from dataclasses import replace

        from clipen.domain import Track
        from clipen.editing.agent_models import ToolResult
        from clipen.editing.production_media import atomic_json, text_style_path
        from clipen.editing.programme_review import caption_coverage

        self.cut()
        cue = {
            "cue_id": "first",
            "item_id": "agent-video-0001",
            "text": "咖啡。",
            "start_frame": 0,
            "duration_frames": 30,
        }
        second = {**cue, "cue_id": "second", "item_id": "agent-video-0002", "text": "餐點上桌。", "start_frame": 120}
        styles = {
            "test.caption": {
                "kind": "program_subtitle",
                "anchor_item_id": cue["item_id"],
                "cue_id": cue["cue_id"],
                "timing_basis": "forced_alignment",
            }
        }
        atomic_json(text_style_path(self.projects, self.project.project_id), styles)
        timeline = replace(
            self.tools._timeline(),
            tracks=self.tools._timeline().tracks
            + (Track("agent-subtitles", TrackKind.TITLE, (TextClip("one-word", 0, 30, "咖啡。", "test.caption"),)),),
        )
        self.repository.save_revision(self.project.project_id, self.run.run_id, timeline, "只保留一小句")
        speech = {"cues": [cue, second], "limitations": []}
        result = caption_coverage(self.tools, speech)
        self.assertFalse(result["complete"])
        self.assertEqual([i["cue_id"] for i in result["missing_cues"]], ["second"])
        with patch.object(self.tools, "_production_list_transcripts", return_value=ToolResult(speech)):
            self.assertIn(
                "subtitle_coverage_incomplete",
                {i["code"] for i in self.call("validate_sequence").payload["delivery_issues"]},
            )
        self.assertTrue(caption_coverage(self.tools, {"cues": [cue], "limitations": []})["complete"])
        unresolved = caption_coverage(
            self.tools, {"cues": [], "limitations": [{"item_id": "agent-video-0002", "reason": "partial_utterance"}]}
        )
        self.assertEqual(unresolved["unresolved_item_ids"], ["agent-video-0002"])

    def test_visual_style_selection_is_frozen_and_request_replay_is_consistent(self):
        self.repository.cancel(self.project.project_id, self.run.run_id)
        run, _ = self.repository.create_run(
            self.project.project_id,
            "cinema-style-request",
            production={
                "skill_id": "travel-program",
                "intent": "同一套剪輯選擇不同元件風格",
                "visual_style_id": "cinema",
            },
        )
        frozen = self.repository.production_session(self.project.project_id, run.run_id)["skill"]
        self.assertEqual(frozen["selected_visual_style_id"], "cinema")
        self.assertTrue({"variety", "editorial", "cinema"} <= {s["visual_style_id"] for s in frozen["visual_styles"]})
        self.assertIn("assets/style-cinema.png", frozen["assets"])
        repeated, created = self.repository.create_run(
            self.project.project_id,
            "cinema-style-request",
            production={
                "skill_id": "travel-program",
                "intent": "同一套剪輯選擇不同元件風格",
                "visual_style_id": "cinema",
            },
        )
        self.assertFalse(created)
        self.assertEqual(repeated.run_id, run.run_id)
        with self.assertRaises(ProjectError):
            self.repository.create_run(
                self.project.project_id,
                "cinema-style-request",
                production={
                    "skill_id": "travel-program",
                    "intent": "同一套剪輯選擇不同元件風格",
                    "visual_style_id": "editorial",
                },
            )
        with self.assertRaises(ProjectError):
            self.repository.create_run(
                self.project.project_id,
                "unknown-style-request",
                production={"skill_id": "travel-program", "intent": "元件風格", "visual_style_id": "unknown"},
            )

    def test_all_component_styles_draw_distinct_frames_with_complete_traditional_glyphs(self):
        from PIL import Image, ImageChops, ImageDraw

        from clipen.rendering.program_components import draw_program_component
        from clipen.rendering.program_styles import load_style_font
        from clipen.rendering.titles import TitleCardRenderer
        from clipen.skills.visual_styles import style_presets

        self.cut()
        skill = get_skill()
        font = TitleCardRenderer._font_path()
        for component in skill["components"]:
            frames = []
            for style in ("variety", "editorial", "cinema"):
                _, presets = style_presets(skill, style)
                profile = {
                    "kind": "program_component",
                    "component_id": component["component_id"],
                    "presets": presets,
                    **component["default_position"],
                }
                if component["requires_target"]:
                    profile["target"] = {"x": 0.77, "y": 0.56, "radius": 0.05}
                image = Image.new("RGBA", (640, 360))
                item = TextClip("style-test", 0, 90, "城市・慢遊", "test")
                draw_program_component(ImageDraw.Draw(image), font, self.tools._timeline(), item, profile, 35)
                self.assertIsNotNone(image.getbbox())
                frames.append(image)
                selected = load_style_font(font, 32, profile)
                for char in "遊發題":
                    self.assertIsNotNone(selected.getmask(char).getbbox())
            self.assertIsNotNone(ImageChops.difference(frames[0], frames[1]).getbbox())
            self.assertIsNotNone(ImageChops.difference(frames[1], frames[2]).getbbox())
        frozen = self.tools._skill_value()
        self.tools._session_cache = {
            **self.tools._session_value(),
            "skill": {k: v for k, v in frozen.items() if k not in {"visual_styles", "selected_visual_style_id"}},
        }
        with self.assertRaises(ProjectError):
            self.component(visual_style_id="cinema")

    def test_editorial_composition_motion_remains_inside_measured_footprint(self):
        from dataclasses import replace

        from PIL import Image, ImageDraw

        from clipen.rendering.program_components import component_bounds, draw_program_component
        from clipen.rendering.titles import TitleCardRenderer
        from clipen.skills.visual_styles import style_presets

        self.cut()
        _, presets = style_presets(get_skill(), "editorial")
        for width, height in ((640, 360), (1920, 1080)):
            timeline = replace(self.tools._timeline(), width=width, height=height)
            for kind in (
                "program_title",
                "headline",
                "impact",
                "chapter",
                "burst",
                "reaction",
                "fact",
                "person",
                "location",
            ):
                profile = {"kind": "program_component", "component_id": kind, "presets": presets, "x": 0.08, "y": 0.12}
                item = TextClip("motion", 0, 90, "咖啡・時光\n街角小日常", "motion")
                canvas = Image.new("RGBA", (width, height))
                bounds, _, _ = component_bounds(
                    ImageDraw.Draw(canvas), TitleCardRenderer._font_path(), timeline, item, profile
                )
                frames = []
                for frame in (0, 8, 25, 59, 89):
                    image = canvas.copy()
                    draw_program_component(
                        ImageDraw.Draw(image), TitleCardRenderer._font_path(), timeline, item, profile, frame
                    )
                    actual = image.getbbox()
                    self.assertIsNotNone(actual)
                    self.assertGreaterEqual(actual[0], bounds[0])
                    self.assertGreaterEqual(actual[1], bounds[1])
                    self.assertLessEqual(actual[2], bounds[2] + 1)
                    self.assertLessEqual(actual[3], bounds[3] + 1)
                    frames.append(image.tobytes())
                self.assertNotEqual(frames[0], frames[2])
                self.assertNotEqual(frames[3], frames[4])

    def test_font_selection_freezes_with_request_and_replay(self):
        self.repository.cancel(self.project.project_id, self.run.run_id)
        request = {
            "skill_id": "travel-program",
            "intent": "保留版型，更換字體",
            "visual_style_id": "editorial",
            "font_style_id": "street",
        }
        run, _ = self.repository.create_run(self.project.project_id, "font-request", production=request)
        snapshot = self.repository.production_session(self.project.project_id, run.run_id)
        self.assertEqual(snapshot["skill"]["selected_font_style_id"], "street")
        self.assertEqual(snapshot["intent"]["font_style_id"], "street")
        self.assertIn("font_styles.json", snapshot["skill"]["resources"])
        repeated, created = self.repository.create_run(self.project.project_id, "font-request", production=request)
        self.assertFalse(created)
        self.assertEqual(repeated.run_id, run.run_id)
        with self.assertRaises(ProjectError):
            self.repository.create_run(
                self.project.project_id, "font-request", production={**request, "font_style_id": "travel"}
            )
        with self.assertRaises(ProjectError):
            self.repository.create_run(
                self.project.project_id, "invalid-font", production={**request, "font_style_id": "unknown"}
            )

    def test_font_pairings_measure_and_draw_every_component_and_preserve_legacy(self):
        from PIL import Image, ImageDraw

        from clipen.rendering.program_components import draw_program_component
        from clipen.rendering.titles import TitleCardRenderer
        from clipen.skills.visual_styles import font_presets, style_presets

        self.cut()
        skill = get_skill()
        from clipen.rendering.program_styles import load_style_font

        signatures = []
        available_faces = []
        for identifier in ("japanese", "street", "travel"):
            for component in skill["components"]:
                _, presets = font_presets(skill, style_presets(skill, "editorial")[1], identifier)
                profile = {
                    "kind": "program_component",
                    "component_id": component["component_id"],
                    "presets": presets,
                    **component["default_position"],
                }
                if component["requires_target"]:
                    profile["target"] = {"x": 0.77, "y": 0.56, "radius": 0.05}
                canvas = Image.new("RGBA", (640, 360))
                draw_program_component(
                    ImageDraw.Draw(canvas),
                    TitleCardRenderer._font_path(),
                    self.tools._timeline(),
                    TextClip("font", 0, 90, "城市・慢遊\n街角小日常", "font"),
                    profile,
                    35,
                )
                self.assertIsNotNone(canvas.getbbox())
                if component["component_id"] == "program_title":
                    signatures.append(canvas.tobytes())
                    selected = load_style_font(TitleCardRenderer._font_path(), 36, profile)
                    available_faces.append((str(selected.path), selected.index))
        # A host without the preferred families can legitimately substitute one
        # installed face. Distinct available faces must still render distinctly.
        self.assertGreaterEqual(len(set(signatures)), len(set(available_faces)))
        legacy = {k: v for k, v in skill.items() if k not in {"font_styles", "selected_font_style_id"}}
        self.assertEqual(font_presets(legacy, skill["presets"])[1], skill["presets"])
        with self.assertRaises(ProjectError):
            font_presets(legacy, skill["presets"], "street")

    def test_bundled_handwriting_and_kai_are_verified_and_render_all_styles(self):
        from PIL import Image, ImageDraw

        from clipen.rendering.program_components import draw_program_component
        from clipen.rendering.program_styles import _has_glyph, bundled_font, load_style_font
        from clipen.rendering.titles import TitleCardRenderer, TitleRenderError
        from clipen.skills.visual_styles import font_presets, style_presets

        self.cut()
        skill = get_skill()
        faces = []
        for identifier in ("handwriting", "calligraphy"):
            for visual in ("variety", "editorial", "cinema"):
                for component in skill["components"]:
                    _, presets = font_presets(skill, style_presets(skill, visual)[1], identifier)
                    profile = {
                        "kind": "program_component",
                        "component_id": component["component_id"],
                        "presets": presets,
                        **component["default_position"],
                    }
                    if component["requires_target"]:
                        profile["target"] = {"x": 0.77, "y": 0.56, "radius": 0.05}
                    canvas = Image.new("RGBA", (640, 360))
                    draw_program_component(
                        ImageDraw.Draw(canvas),
                        TitleCardRenderer._font_path(),
                        self.tools._timeline(),
                        TextClip("handwritten", 0, 90, "咖啡・時光\n旅行日記", "test"),
                        profile,
                        35,
                    )
                    self.assertIsNotNone(canvas.getbbox())
            selected = load_style_font(
                TitleCardRenderer._font_path(), 48, {"presets": presets, "component_id": "program_title"}
            )
            faces.append(selected.getname())
            for char in "咖啡時光城市慢遊發現體驗臺灣":
                self.assertTrue(_has_glyph(str(selected.path), 48, selected.index, char))
            specification = presets["font_roles"]["title"]
            with self.assertRaises(TitleRenderError):
                bundled_font({**specification, "sha256": "0" * 64})
        self.assertNotEqual(faces[0], faces[1])
        with self.assertRaises(TitleRenderError):
            bundled_font({"bundled_font": "../../secret", "sha256": "0" * 64})
        self.assertIn("references/font-chenyu-OFL.txt", skill["resources"])
        self.assertIn("references/font-iansui-OFL.txt", skill["resources"])

    def test_authenticated_resume_dispatches_existing_cut_without_resetting_budget(self):
        import asyncio

        from test_project_api import request_app

        from clipen.controller import ControllerConfig, LocalSessionAuthority, ProviderState, ProviderStatus, create_app
        from clipen.editing import EditAgentCoordinator
        from clipen.providers import ProviderCancelled

        self.cut()
        before = self.repository.get(self.project.project_id, self.run.run_id)
        self.repository.record_provider_outcome(
            self.project.project_id, self.run.run_id, "timed-out", usage=None, error_code="provider_timed_out"
        )
        started = threading.Event()
        seen = []

        class LocalOnlyAgent:
            def __init__(self, projects_root, attempt_root, repository):
                self.repository = repository

            def execute(self, project_id, run_id, *, cancel):
                seen.append(self.repository.activate(project_id, run_id))
                started.set()
                cancel.wait(3)
                raise ProviderCancelled("test stop")

        static = self.root / "resume-dist"
        static.mkdir()
        (static / "index.html").write_text("Clipen")
        coordinator = EditAgentCoordinator(self.repository, self.root, adapter_factory=LocalOnlyAgent)
        app = create_app(
            ControllerConfig(static),
            ProviderStatus("Codex", ProviderState.READY, "ready"),
            LocalSessionAuthority(),
            edit_agents=coordinator,
        )

        def request(method, path, payload=None, headers=None):
            return asyncio.run(
                request_app(
                    app, method, path, headers=headers, body=b"" if payload is None else json.dumps(payload).encode()
                )
            )

        try:
            _, headers, _ = request(
                "POST",
                "/api/session/connect",
                headers={"origin": "http://localhost:4200", "sec-fetch-site": "same-origin"},
            )
            cookie = headers["set-cookie"].split(";", 1)[0]
            _, _, body = request("GET", "/api/status", headers={"cookie": cookie})
            csrf = json.loads(body)["session"]["csrf_token"]
            mutation = {
                "origin": "http://localhost:4200",
                "sec-fetch-site": "same-origin",
                "content-type": "application/json",
                "cookie": cookie,
                "x-clipen-csrf": csrf,
            }
            route = f"/api/projects/{self.project.project_id}/production/commands/resume/{self.run.run_id}"
            self.assertEqual(request("POST", route, {"consent": True})[0], 401)
            self.assertEqual(request("POST", route, {"consent": True}, {"cookie": cookie})[0], 403)
            self.assertEqual(request("POST", route, {"consent": False}, mutation)[0], 422)
            code, _, body = request("POST", route, {"consent": True}, mutation)
            self.assertEqual(code, 202, body)
            self.assertTrue(started.wait(2))
            self.assertEqual(seen[0].current_revision_id, before.current_revision_id)
            self.assertEqual(seen[0].tool_calls, before.tool_calls)
            self.assertEqual(request("POST", route, {"consent": True}, mutation)[0], 409)
        finally:
            coordinator.close()

    def test_timeout_resume_keeps_draft_skill_and_consumed_budget(self):
        self.cut()
        before = self.repository.get(self.project.project_id, self.run.run_id)
        frozen = self.repository.production_session(self.project.project_id, self.run.run_id)
        self.repository.record_provider_outcome(
            self.project.project_id, self.run.run_id, "timeout-attempt", usage=None, error_code="provider_timed_out"
        )
        resumed = self.repository.resume_stopped(self.project.project_id, self.run.run_id)
        self.assertEqual(resumed.state, "queued")
        self.assertIsNone(resumed.error_code)
        self.assertEqual(resumed.current_revision_id, before.current_revision_id)
        self.assertEqual(resumed.tool_calls, before.tool_calls)
        self.assertEqual(self.repository.production_session(self.project.project_id, self.run.run_id), frozen)
        with self.assertRaises(ProjectError):
            self.repository.resume_stopped(self.project.project_id, self.run.run_id)
        self.repository.activate(self.project.project_id, self.run.run_id)
        self.repository.record_provider_outcome(
            self.project.project_id, self.run.run_id, "other-failure", usage=None, error_code="provider_network_error"
        )
        with self.assertRaises(ProjectError):
            self.repository.resume_stopped(self.project.project_id, self.run.run_id)

    def test_stages_count_review_only_after_a_render_and_fill_up_when_finished(self):
        from clipen.editing.production_progress import STAGES, _stages

        reached = lambda labels, finished=False: _stages(labels, finished)["reached"]
        self.assertEqual(reached([]), -1)
        self.assertEqual(STAGES[reached(["理解製作需求與影片風格", "檢查畫面、字幕與聲音"])], "edit")
        self.assertEqual(STAGES[reached(["安排故事與剪輯", "輸出樣片預覽", "檢查畫面、字幕與聲音"])], "review")
        # Going back to earlier work never moves the bar backwards.
        self.assertEqual(STAGES[reached(["尋找與安排配樂、音效", "安排故事與剪輯"])], "music")
        self.assertEqual(reached(["理解製作需求與影片風格"], True), len(STAGES))

    def test_a_revision_hands_the_draft_to_the_ai_clipen_runs_with_but_resuming_never_does(self):
        from clipen.editing import EditAgentCoordinator

        self.cut()
        before = self.repository.get(self.project.project_id, self.run.run_id)
        self.assertEqual(before.provider, "codex")
        self.repository.cancel(self.project.project_id, self.run.run_id)
        revision = self.repository.current_revision(self.project.project_id, self.run.run_id)
        switched, _ = self.repository.request_refinement(
            self.project.project_id,
            self.run.run_id,
            "claude-polish",
            revision.revision_id,
            "精修字幕",
            provider="claude",
        )
        self.assertEqual(
            (switched.provider, switched.state, switched.current_revision_id),
            ("claude", "queued", before.current_revision_id),
        )
        progress = EditAgentCoordinator(self.repository, self.root).status(self.project.project_id)["production"][
            "progress"
        ]
        self.assertEqual([e["label"] for e in progress["activity"]], ["收到你的修改要求", "改由 Claude 接手修改"])
        # The same request id with another AI is a different command.
        with self.assertRaises(ProjectError):
            self.repository.request_refinement(
                self.project.project_id,
                self.run.run_id,
                "claude-polish",
                revision.revision_id,
                "精修字幕",
                provider="codex",
            )
        # An interrupted attempt continues with the AI that was running it.
        self.repository.activate(self.project.project_id, self.run.run_id)
        self.repository.record_provider_outcome(
            self.project.project_id, self.run.run_id, "quota", usage=None, error_code="provider_usage_limit"
        )
        self.assertEqual(self.repository.resume_stopped(self.project.project_id, self.run.run_id).provider, "claude")

    def test_a_revision_request_starts_a_fresh_activity_list_with_the_request(self):
        from clipen.editing import EditAgentCoordinator

        self.cut()
        self.call("list_program_components")
        self.repository.cancel(self.project.project_id, self.run.run_id)
        revision = self.repository.current_revision(self.project.project_id, self.run.run_id)
        self.repository.request_refinement(
            self.project.project_id, self.run.run_id, "swap-music", revision.revision_id, "配樂換成我指定的歌"
        )
        progress = EditAgentCoordinator(self.repository, self.root).status(self.project.project_id)["production"][
            "progress"
        ]
        self.assertEqual(progress["request"], "配樂換成我指定的歌")
        self.assertEqual([e["label"] for e in progress["activity"]], ["收到你的修改要求"])

    def test_delivery_blockers_follow_the_current_revision_and_choose_how_to_recover(self):
        self.assertIsNone(self.repository.delivery_blockers(self.project.project_id, self.run.run_id))
        self.cut()
        validation = self.call("validate_sequence").payload
        self.assertFalse(validation["valid"])
        blockers = self.repository.delivery_blockers(self.project.project_id, self.run.run_id)
        self.assertTrue(blockers)
        self.assertEqual(set(blockers), {i["code"] for i in validation["issues"] + validation["delivery_issues"]})
        # A draft held back by checks needs a revision request, not the same instructions again.
        self.repository.record_provider_outcome(
            self.project.project_id, self.run.run_id, "blocked", usage=None, error_code="delivery_blocked"
        )
        with self.assertRaises(ProjectError):
            self.repository.resume_stopped(self.project.project_id, self.run.run_id)

    def test_an_expired_login_can_continue_the_same_draft_after_signing_in(self):
        self.cut()
        before = self.repository.get(self.project.project_id, self.run.run_id)
        self.repository.record_provider_outcome(
            self.project.project_id, self.run.run_id, "login", usage=None, error_code="provider_authentication_required"
        )
        resumed = self.repository.resume_stopped(self.project.project_id, self.run.run_id)
        self.assertEqual((resumed.state, resumed.current_revision_id), ("queued", before.current_revision_id))

    def test_sleep_during_a_run_shows_in_the_status(self):
        from clipen.editing import EditAgentCoordinator

        self.cut()
        self.repository.record_machine_sleep(self.project.project_id, self.run.run_id, [(1792800000.0, 1792807200.0)])
        progress = EditAgentCoordinator(self.repository, self.root).status(self.project.project_id)["production"][
            "progress"
        ]
        self.assertEqual(progress["slept_seconds"], 7200)
        self.assertEqual(progress["activity"][-1]["label"], "Mac 睡眠約 2 小時 0 分鐘，製作暫停")

    def test_an_early_stop_can_continue_the_same_draft(self):
        self.cut()
        before = self.repository.get(self.project.project_id, self.run.run_id)
        self.repository.record_provider_outcome(
            self.project.project_id, self.run.run_id, "early", usage=None, error_code="provider_stopped_early"
        )
        resumed = self.repository.resume_stopped(self.project.project_id, self.run.run_id)
        self.assertEqual((resumed.state, resumed.current_revision_id), ("queued", before.current_revision_id))

    def test_usage_limit_resume_continues_the_draft_and_keeps_spent_usage(self):
        self.cut()
        before = self.repository.get(self.project.project_id, self.run.run_id)
        spent = {"input_tokens": 14758358, "cached_input_tokens": 14392319, "output_tokens": 125750}
        self.repository.record_provider_outcome(
            self.project.project_id, self.run.run_id, "quota-attempt", usage=spent, error_code="provider_usage_limit"
        )
        resumed = self.repository.resume_stopped(self.project.project_id, self.run.run_id)
        self.assertEqual((resumed.state, resumed.error_code), ("queued", None))
        self.assertEqual(resumed.current_revision_id, before.current_revision_id)
        self.assertEqual(resumed.tool_calls, before.tool_calls)
        with self.projects.connect(self.project.project_id) as db:
            row = db.execute(
                "SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='run_resumed'",
                (self.run.run_id,),
            ).fetchone()
        event = json.loads(row["payload_json"])
        self.assertEqual((event["previous_error_code"], event["previous_usage"]), ("provider_usage_limit", spent))
        # The account may still be limited; stopping again keeps the draft resumable.
        self.repository.activate(self.project.project_id, self.run.run_id)
        self.repository.record_provider_outcome(
            self.project.project_id, self.run.run_id, "quota-again", usage=None, error_code="provider_usage_limit"
        )
        self.assertEqual(
            self.repository.resume_stopped(self.project.project_id, self.run.run_id).current_revision_id,
            before.current_revision_id,
        )

    def test_duration_revision_reconciles_music_length_without_changing_the_film(self):
        from clipen.editing.production_limits import limits_for, production_limit_issues

        self.repository.cancel(self.project.project_id, self.run.run_id)
        run, _ = self.repository.create_run(
            self.project.project_id,
            "music-length",
            production={"skill_id": "travel-program", "intent": "五分鐘旅遊精華", "target_duration_seconds": 300},
        )
        tools = EditAgentTools(self.repository, self.project.project_id, run.run_id)
        tools.activate()
        try:
            source = tools.call("list_source_media", {}).payload["sources"][0]
            candidate = tools.call(
                "register_source_range",
                {
                    "version_id": source["version_id"],
                    "stream_index": 0,
                    "start_offset_seconds": 0,
                    "duration_seconds": 8,
                    "description": "合成來源，測試交付片長政策",
                },
            ).payload["candidate"]["candidate_id"]
            clips = [{"candidate_id": candidate, "start_offset_seconds": 0, "duration_seconds": 2}] * 99
            clips += [{"candidate_id": candidate, "start_offset_seconds": 0, "duration_seconds": 1}] * 16
            revision = self.repository.current_revision(self.project.project_id, run.run_id).revision_id
            tools.call(
                "replace_sequence",
                {
                    "clips": clips,
                    "reprise_reason": "合成來源的政策測試，不代表剪輯建議",
                    "summary": "重現214秒115鏡",
                    "expected_revision_id": revision,
                    "mutation_id": "duration-cut",
                },
            )
            film = tools._timeline()
            revision = self.repository.current_revision(self.project.project_id, run.run_id).revision_id
            self.assertEqual(film.duration_frames, 6420)
            self.assertEqual(len(tools._video().items), 115)
            self.assertFalse(production_limit_issues(tools, film))
            self.assertTrue(production_limit_issues(tools, film, complete=True))
            validation = tools.call("validate_sequence", {}).payload
            issue = next(i for i in validation["delivery_issues"] if i["code"] == "production_duration_below_target")
            self.assertEqual((issue["actual"], issue["minimum"]), (214, 270))
            self.assertNotIn(issue, validation["render_issues"])
            self.assertIn(
                "production_duration_below_target",
                self.repository.delivery_blockers(self.project.project_id, run.run_id),
            )
            args = {
                "target_duration_seconds": 214,
                "request_excerpt": "跟我的音樂差不多的時間",
                "summary": "依使用者新要求配合歌曲量測214秒",
                "expected_revision_id": revision,
                "mutation_id": "duration-update",
            }
            with self.assertRaises(ProjectError) as error:
                tools.call("set_target_duration", args)
            self.assertEqual(error.exception.code, "duration_request_required")
            self.repository.cancel(self.project.project_id, run.run_id)
            self.repository.request_refinement(
                self.project.project_id,
                run.run_id,
                "shorten-to-music",
                revision,
                "幫我把整部片壓在跟我的音樂差不多的時間",
            )
            tools.close_production()
            tools = EditAgentTools(self.repository, self.project.project_id, run.run_id)
            tools.activate()
            args["mutation_id"] = "duration-update-authorized"
            updated = tools.call("set_target_duration", args).payload
            self.assertEqual(updated["previous_target_duration_seconds"], 300)
            self.assertEqual(updated["limits"]["delivery_min_seconds"], 193)
            self.assertEqual(updated["limits"]["clips"], 180)
            self.assertEqual(tools.call("get_edit_status", {}).payload["intent"]["target_duration_seconds"], 214)
            self.assertFalse(production_limit_issues(tools, film, complete=True))
            self.assertFalse(
                any(
                    i["code"] == "production_duration_below_target"
                    for i in tools.call("validate_sequence", {}).payload["issues"]
                )
            )
            self.assertEqual(tools._timeline(), film)
            self.assertEqual(
                self.repository.current_revision(self.project.project_id, run.run_id).revision_id, revision
            )
            replay = tools.call("set_target_duration", args).payload
            self.assertTrue(replay["replayed"])
            reloaded = EditAgentTools(self.repository, self.project.project_id, run.run_id)
            self.assertEqual(limits_for(reloaded), limits_for(tools))
            with self.projects.connect(self.project.project_id) as db:
                events = db.execute(
                    "SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='production_duration_updated'",
                    (run.run_id,),
                ).fetchall()
            self.assertEqual(len(events), 1)
            self.assertEqual(json.loads(events[0][0])["request_excerpt"], args["request_excerpt"])
            self.assertEqual(json.loads(events[0][0])["authority"], "model")
            for target in (True, 0, 1201, 214.1):
                with self.assertRaises(ProjectError):
                    self.repository.set_target_duration(
                        self.project.project_id,
                        run.run_id,
                        revision,
                        target_duration_seconds=target,
                        request_excerpt=args["request_excerpt"],
                        summary="錯誤片長",
                    )
            with self.assertRaises(ProjectError) as error:
                self.repository.set_target_duration(
                    self.project.project_id,
                    run.run_id,
                    "stale",
                    target_duration_seconds=214,
                    request_excerpt=args["request_excerpt"],
                    summary="過期版本",
                )
            self.assertEqual(error.exception.code, "revision_conflict")
            with self.assertRaises(ProjectError) as error:
                self.repository.set_target_duration(
                    self.project.project_id,
                    run.run_id,
                    revision,
                    target_duration_seconds=214,
                    request_excerpt="使用者沒有這樣說",
                    summary="不可捏造要求",
                )
            self.assertEqual(error.exception.code, "duration_request_required")
        finally:
            tools.close_production()

    def test_explicit_long_form_session_accepts_ten_minutes_without_relaxing_default(self):
        from clipen.editing.production_limits import limits_for

        self.cut()
        candidate = self.call("list_source_media", limit=50).payload["sources"]
        candidate = next(r for r in candidate if r["kind"] == "video")
        self.repository.cancel(self.project.project_id, self.run.run_id)
        run, _ = self.repository.create_run(
            self.project.project_id,
            "long-form",
            production={"skill_id": "travel-program", "intent": "十分鐘完整影片", "target_duration_seconds": 600},
        )
        long = EditAgentTools(self.repository, self.project.project_id, run.run_id)
        long.activate()
        try:
            self.assertEqual(limits_for(long).duration_seconds, 660)
            self.assertEqual(limits_for(long).clips, 180)
            candidate = long.call(
                "register_source_range",
                {
                    "version_id": candidate["version_id"],
                    "stream_index": 0,
                    "start_offset_seconds": 0,
                    "duration_seconds": 12,
                    "description": "合成素材邊界測試",
                },
            ).payload["candidate"]["candidate_id"]
            revision = self.repository.current_revision(self.project.project_id, run.run_id).revision_id
            clips = [{"candidate_id": candidate, "start_offset_seconds": 0, "duration_seconds": 12}] * 50
            long.call(
                "replace_sequence",
                {
                    "clips": clips,
                    "reprise_reason": "合成來源的長片政策邊界測試，非成片剪輯建議",
                    "summary": "驗證十分鐘可建立",
                    "expected_revision_id": revision,
                    "mutation_id": "long-boundary",
                },
            )
            timeline = long._timeline()
            self.assertEqual(timeline.duration_frames, 18000)
            self.assertEqual(len(long._video().items), 50)
            from dataclasses import replace

            from clipen.editing.production_limits import production_limit_issues

            short = replace(
                timeline,
                duration_frames=1080,
                tracks=(replace(timeline.tracks[0], items=timeline.tracks[0].items[:3]),),
            )
            self.assertFalse(production_limit_issues(long, short))
            self.assertTrue(production_limit_issues(long, short, complete=True))
            revision = self.repository.current_revision(self.project.project_id, run.run_id).revision_id
            with self.assertRaises(ProjectError):
                long.call(
                    "replace_sequence",
                    {
                        "clips": clips + clips[:6],
                        "reprise_reason": "測試超出本次長片上限",
                        "summary": "必須拒絕",
                        "expected_revision_id": revision,
                        "mutation_id": "too-long",
                    },
                )
            self.assertEqual(long._timeline(), timeline)

            self.assertEqual(
                self.repository.production_session(self.project.project_id, run.run_id)["intent"][
                    "target_duration_seconds"
                ],
                600,
            )
            repeated, created = self.repository.create_run(
                self.project.project_id,
                "long-form",
                production={"skill_id": "travel-program", "intent": "十分鐘完整影片", "target_duration_seconds": 600},
            )
            self.assertFalse(created)
            self.assertEqual(repeated.run_id, run.run_id)
            with self.assertRaises(ProjectError):
                self.repository.create_run(
                    self.project.project_id,
                    "long-form",
                    production={
                        "skill_id": "travel-program",
                        "intent": "十分鐘完整影片",
                        "target_duration_seconds": 60,
                    },
                )
        finally:
            long.close_production()
        # Prior frozen sessions remain short even after the API gains long form.
        self.assertEqual(limits_for(self.tools).duration_seconds, 75)
        for value in (True, 0, 1201, 600.5):
            with self.assertRaises(ProjectError):
                self.repository.create_run(
                    self.project.project_id,
                    "invalid-duration-" + str(value),
                    production={"skill_id": "travel-program", "intent": "片長檢查", "target_duration_seconds": value},
                )

    def test_overlapping_speech_windows_choose_one_complete_evidence_without_duplicate_captions(self):
        from clipen.projects.storage import utc_now

        self.cut()
        clip = self.tools._video().items[0]

        def add(identifier, text, aligned, correction=False):
            segment = {"text": text, "start_seconds": 0.2 if identifier != "old" else 0, "end_seconds": 3.8}
            if aligned:
                segment["words"] = [
                    {"text": text, "char_start": 0, "char_end": len(text), "start_seconds": 1, "end_seconds": 2}
                ]
            if correction:
                segment["wording_authority"] = "model_proposed_correction"
            value = {"source": clip.source.span.to_storage(), "source_start_seconds": 1, "segments": [segment]}
            with self.projects.connect(self.project.project_id) as db:
                db.execute(
                    "INSERT INTO production_evidence_jobs VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        self.run.run_id,
                        identifier,
                        "align",
                        "succeeded",
                        "{}",
                        json.dumps(value),
                        None,
                        utc_now(),
                    ),
                )

        add("old", "舊字幕", True)
        add("failed-alignment", "未對齊的新視窗", False)
        self.assertEqual(
            [c["text"] for c in self.call("list_transcripts", include_drafts=True).payload["cues"]], ["舊字幕"]
        )
        add("aligned-new-window", "新字幕", True)
        self.assertEqual(
            [c["text"] for c in self.call("list_transcripts", include_drafts=True).payload["cues"]], ["新字幕"]
        )
        add("explicit-wording-correction", "更正原話", False, True)
        self.assertEqual(
            [c["text"] for c in self.call("list_transcripts", include_drafts=True).payload["cues"]], ["更正原話"]
        )

    def test_caption_fragments_join_without_moving_word_boundaries(self):
        from clipen.editing.alignment import significant
        from clipen.editing.program_subtitles import aligned_pages

        text = "買什麼？我們剛剛買到安全帽了。"
        words = []
        for n, index in enumerate(i for i, c in enumerate(text) if significant(c)):
            start = n * 2 + (30 if n >= 4 else 0)
            words.append(
                {
                    "text": text[index],
                    "char_start": index,
                    "char_end": index + 1,
                    "start_frame": start,
                    "end_frame": start + 2,
                }
            )
        pages = aligned_pages({"text": text, "words": words}, text, None, [(0, len(text))], 30)
        self.assertEqual(pages, [(0, len(text), words[0]["start_frame"], words[-1]["end_frame"])])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required")
    def test_local_wording_correction_does_not_realign_unrelated_failed_segment(self):
        self.real_media()
        self.cut()
        original = "大家河濱公園。"
        corrected = "台場海濱公園。"
        noise = "遠方雜音。"

        class Transcriber:
            def transcribe(self, audio, output, **kwargs):
                return TranscriptionResult(
                    "zh",
                    original + noise,
                    (
                        TranscriptSegment(0, 64000, original, None, ()),
                        TranscriptSegment(64000, 128000, noise, None, ()),
                    ),
                )

            def close(self):
                pass

        self.tools.transcriber_binding = lambda: (Transcriber, {"id": "test-local", "revision": "fixed"})

        def initial(_root, _audio, text, *args):
            if text == noise:
                raise ProjectError("alignment_invalid", "未使用的背景片段")
            return [{"text": text, "start": 0.2, "end": 1.4}]

        with patch("clipen.editing.alignment.run_alignment", side_effect=initial):
            job = self.call("transcribe_source_range", candidate_id=self.candidate).payload["job"]
            before = self.call("read_evidence_job", job_id=job["job_id"], wait_seconds=10).payload["job"]
        self.assertEqual(before["state"], "succeeded")

        def only_changed(_root, _audio, text, *args):
            self.assertEqual(text, corrected)
            return [{"text": text, "start": 0.2, "end": 1.4}]

        with patch("clipen.editing.alignment.run_alignment", side_effect=only_changed):
            changed = self.call(
                "revise_source_speech",
                transcript_job_id=job["job_id"],
                segment_ordinal=0,
                text=corrected,
                evidence="來源畫面的地名指示牌",
            ).payload["job"]
            after = self.call("read_evidence_job", job_id=changed["job_id"], wait_seconds=10).payload["job"]
        self.assertEqual(after["state"], "succeeded", after)
        self.assertEqual(after["result"]["segments"][0]["source_asr_text"], original)
        self.assertEqual(after["result"]["segments"][1], before["result"]["segments"][1])
        self.assertEqual(
            self.call("read_evidence_job", job_id=job["job_id"]).payload["job"]["result"]["segments"][0]["text"],
            original,
        )

    def test_start_receipt_and_revision_guard_protect_existing_work(self):
        repeated, created = self.repository.create_run(
            self.project.project_id,
            "create-production",
            production={"skill_id": "travel-program", "intent": "用真實對話製作節目，配合字幕、音樂與資訊標註。"},
            max_tool_calls=150,
        )
        self.assertFalse(created)
        self.assertEqual(repeated.run_id, self.run.run_id)
        previous = self.cut().payload["revision"]["revision_id"]
        self.call(
            "set_color_grade", item_id="agent-video-0001", contrast=1.1, saturation=1, brightness=0, summary="改版"
        )
        with self.assertRaises(ProjectError) as error:
            self.call(
                "remove_program_component", element_id="dish-label", summary="舊版本", expected_revision_id=previous
            )
        self.assertEqual(error.exception.code, "revision_conflict")
        self.assertFalse(any(t.track_id == "program-components" for t in self.tools._timeline().tracks))

    def test_mutation_retry_replays_without_overwriting_newer_revision(self):
        self.cut()
        before = self.repository.current_revision(self.project.project_id, self.run.run_id).revision_id
        args = {
            "item_id": "agent-video-0001",
            "contrast": 1.1,
            "saturation": 1,
            "brightness": 0,
            "summary": "同一個改動",
            "expected_revision_id": before,
            "mutation_id": "stable-operation",
        }
        first = self.call("set_color_grade", **args)
        self.component()
        latest = self.repository.current_revision(self.project.project_id, self.run.run_id).revision_id
        used = self.repository.get(self.project.project_id, self.run.run_id).tool_calls
        repeated = self.call("set_color_grade", **args)
        self.assertTrue(repeated.payload["replayed"])
        self.assertEqual(repeated.payload["revision"], first.payload["revision"])
        self.assertEqual(self.repository.current_revision(self.project.project_id, self.run.run_id).revision_id, latest)
        self.assertEqual(self.repository.get(self.project.project_id, self.run.run_id).tool_calls, used)
        with self.assertRaises(ProjectError):
            self.call("set_color_grade", **{**args, "contrast": 1.2})
        with self.assertRaises(ProjectError) as error:
            self.tools.call("remove_program_component", {"element_id": "dish-label", "summary": "缺少版本與操作識別"})
        self.assertEqual(error.exception.code, "mutation_guard_required")

    def test_every_mcp_tool_schema_is_valid_json_schema_2020_12(self):
        # Claude's API rejects the whole request for one invalid tool schema.
        from jsonschema import Draft202012Validator

        from clipen.mcp_editing import EditingMcpServer

        listed = EditingMcpServer(self.tools).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        for tool in listed:
            with self.subTest(tool=tool["name"]):
                Draft202012Validator.check_schema(tool["inputSchema"])
                required = tool["inputSchema"].get("required", [])
                self.assertEqual(len(required), len(set(required)))

    def test_mcp_publishes_required_mutation_guards_and_frozen_skill_resources(self):
        from clipen.mcp_editing import EditingMcpServer

        server = EditingMcpServer(self.tools)
        listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        component = next(t for t in listed if t["name"] == "add_custom_component")
        self.assertTrue({"mutation_id", "expected_revision_id"} <= set(component["inputSchema"]["required"]))
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "read_production_skill", "arguments": {"resource": "SKILL.md"}},
            }
        )
        self.assertIn("travel-program", response["result"]["content"][0]["text"])
        preview = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "read_production_skill", "arguments": {"resource": "assets/style-cinema.png"}},
            }
        )
        self.assertEqual([item["type"] for item in preview["result"]["content"]], ["text", "image"])
        self.assertIn("style_example_not_source_evidence", preview["result"]["content"][0]["text"])

    def test_v17_migration_preserves_real_legacy_revision_and_foreign_keys(self):
        # Runs made before AI production started from a compiled base timeline; old databases still hold them.
        import hashlib

        from clipen.domain import ClipSource, MediaClip, Rational, SourceSpan, Track
        from clipen.editing.agent_models import encoded, make_id
        from clipen.projects.agent_schema import EDIT_AGENT_SCHEMA

        pid = self.project.project_id
        span = SourceSpan("av_" + "a" * 32, 0, "0", "900000", Rational(1, 90000))
        timeline = EditTimeline(
            "timeline-" + "b" * 32,
            pid,
            "plan-" + "c" * 32,
            1,
            "d" * 64,
            Rational(30, 1),
            1920,
            1080,
            300,
            (Track("video-main", TrackKind.VIDEO, (MediaClip("clip-0001", 0, 300, ClipSource(span)),)),),
        )
        payload = timeline.to_payload()
        digest = hashlib.sha256(encoded(payload).encode("utf-8")).hexdigest()
        with self.projects.connect(pid) as db:
            db.execute(
                "INSERT INTO edit_timelines (timeline_id,project_id,plan_id,plan_revision,plan_content_hash,output_preset,frame_rate,"
                "payload_digest,payload_json,created_at) VALUES(?,?,?,1,?,'landscape_1080p',30,?,?,'2026-01-01')",
                (timeline.timeline_id, pid, timeline.plan_id, timeline.plan_content_hash, digest, encoded(payload)),
            )
        run_id = make_id("editrun_", [pid, "legacy-run"])
        revision_id = make_id("editrev_", [run_id, 0, digest])
        self.tools.close_production()
        with self.projects.connect(pid) as db:
            db.execute(
                "INSERT INTO edit_agent_runs (run_id,project_id,request_id,base_timeline_id,state,phase,max_tool_calls,"
                "max_preview_renders,current_revision_id,provider,created_at) VALUES(?,?,'legacy-run',?,'succeeded','finished',20,2,?,'codex','2026-01-01')",
                (run_id, pid, timeline.timeline_id, revision_id),
            )
            db.execute(
                "INSERT INTO edit_agent_revisions (revision_id,run_id,ordinal,parent_revision_id,payload_digest,payload_json,summary,created_at) "
                "VALUES(?,?,0,NULL,?,?,'原始剪輯版本','2026-01-01')",
                (revision_id, run_id, digest, encoded(payload)),
            )
            db.execute("DELETE FROM edit_agent_events WHERE run_id=?", (self.run.run_id,))
            db.execute("DELETE FROM edit_agent_revisions WHERE run_id=?", (self.run.run_id,))
            db.execute("DELETE FROM edit_agent_command_receipts")
            db.execute("DELETE FROM production_sessions")
            db.execute("DELETE FROM edit_agent_runs WHERE run_id=?", (self.run.run_id,))
        original = self.repository.current_revision(pid, run_id)
        self.assertEqual(original.timeline, timeline)
        db = sqlite3.connect(self.projects.root / pid / "project.sqlite3")
        create = EDIT_AGENT_SCHEMA.split("CREATE TABLE edit_agent_revisions", 1)[0].replace(
            "CREATE TABLE edit_agent_runs", "CREATE TABLE runs_v17", 1
        )
        create = create.replace("base_timeline_id TEXT REFERENCES", "base_timeline_id TEXT NOT NULL REFERENCES")
        db.executescript(
            "PRAGMA foreign_keys=OFF; BEGIN IMMEDIATE;"
            + create
            + "INSERT INTO runs_v17 SELECT * FROM edit_agent_runs; DROP TABLE edit_agent_runs; ALTER TABLE runs_v17 RENAME TO edit_agent_runs;"
            "CREATE INDEX edit_agent_runs_project_created ON edit_agent_runs(project_id,created_at DESC);"
            "DROP TABLE production_tool_receipts; DROP TABLE production_evidence_jobs; DROP TABLE production_sessions; PRAGMA user_version=17; COMMIT;"
        )
        db.close()
        self.assertEqual(self.repository.current_revision(pid, run_id), original)
        with self.projects.connect(pid) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 19)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_authenticated_production_entry_uses_inventory_without_old_plan(self):
        import asyncio

        from test_project_api import request_app

        from clipen.controller import ControllerConfig, LocalSessionAuthority, ProviderState, ProviderStatus, create_app
        from clipen.editing import EditAgentCoordinator
        from clipen.providers import ProviderCancelled

        self.repository.cancel(self.project.project_id, self.run.run_id)

        class LocalOnlyAgent:
            def __init__(self, projects_root, attempt_root, repository):
                self.repository = repository

            def execute(self, project_id, run_id, *, cancel):
                self.repository.activate(project_id, run_id)
                cancel.wait(3)
                raise ProviderCancelled("test stop")

        static = self.root / "dist"
        static.mkdir()
        (static / "index.html").write_text("Clipen")
        coordinator = EditAgentCoordinator(self.repository, self.root, adapter_factory=LocalOnlyAgent)
        app = create_app(
            ControllerConfig(static),
            ProviderStatus("Codex", ProviderState.READY, "ready"),
            LocalSessionAuthority(),
            edit_agents=coordinator,
        )

        def request(method, path, payload=None, headers=None):
            return asyncio.run(
                request_app(
                    app, method, path, headers=headers, body=b"" if payload is None else json.dumps(payload).encode()
                )
            )

        try:
            _, headers, _ = request(
                "POST",
                "/api/session/connect",
                headers={"origin": "http://localhost:4200", "sec-fetch-site": "same-origin"},
            )
            cookie = headers["set-cookie"].split(";", 1)[0]
            _, _, body = request("GET", "/api/status", headers={"cookie": cookie})
            csrf = json.loads(body)["session"]["csrf_token"]
            mutation = {
                "origin": "http://localhost:4200",
                "sec-fetch-site": "same-origin",
                "content-type": "application/json",
                "cookie": cookie,
                "x-clipen-csrf": csrf,
            }
            payload = {
                "request_id": "api-production",
                "consent": False,
                "skill_id": "travel-program",
                "intent": "從來源直接製作節目",
                "visual_style_id": "editorial",
                "font_style_id": "street",
            }
            route = f"/api/projects/{self.project.project_id}/production/commands/start"
            self.assertEqual(request("POST", route, payload, mutation)[0], 422)
            code, _, body = request("POST", route, {**payload, "consent": True}, mutation)
            self.assertEqual(code, 202, body)
            code, _, body = request(
                "GET", f"/api/projects/{self.project.project_id}/edit-agent", headers={"cookie": cookie}
            )
            self.assertEqual(code, 200, body)
            self.assertEqual(json.loads(body)["production"]["skill"]["skill_id"], "travel-program")
            self.assertEqual(json.loads(body)["production"]["skill"]["selected_visual_style_id"], "editorial")
            self.assertEqual(json.loads(body)["production"]["skill"]["selected_font_style_id"], "street")
            image_code, image_headers, image_body = request(
                "GET", "/api/production/skills/travel-program/styles/editorial/preview", headers={"cookie": cookie}
            )
            self.assertEqual(image_code, 200)
            self.assertTrue(image_body.startswith(b"\x89PNG"))
            self.assertEqual(
                request(
                    "GET", "/api/production/skills/travel-program/styles/unknown/preview", headers={"cookie": cookie}
                )[0],
                404,
            )
        finally:
            coordinator.close()

    def test_graphics_follow_reorder_trim_and_undo_without_clearing_other_treatments(self):
        self.cut()
        self.component()
        self.call(
            "set_color_grade", item_id="agent-video-0001", contrast=1.1, saturation=1, brightness=0, summary="曝光"
        )
        revision = self.repository.current_revision(self.project.project_id, self.run.run_id)
        result = self.call("reorder_clips", item_ids=["agent-video-0002", "agent-video-0001"], summary="重排")
        self.assertEqual(result.payload["invalidated"], [])
        label = next(i for t in self.tools._timeline().tracks if t.track_id == "program-components" for i in t.items)
        self.assertEqual(label.start_frame, 126)
        self.assertEqual(self.tools._video().items[1].effects[0].effect_id, "video.grade")
        trimmed = self.call(
            "trim_clip", item_id="agent-video-0002", start_offset_frames=30, duration_frames=90, summary="縮短別鏡"
        )
        self.assertEqual(trimmed.payload["invalidated"], [])
        label = next(i for t in self.tools._timeline().tracks if t.track_id == "program-components" for i in t.items)
        self.assertEqual(label.start_frame, 96)
        self.call("restore_revision", revision_id=revision.revision_id, summary="還原")
        self.assertEqual(self.tools._timeline(), revision.timeline)

    def add_subtitle(self, text, start_frame, duration_frames):
        from dataclasses import replace

        from clipen.domain import Track

        timeline = self.tools._timeline()
        caption = TextClip(f"subtitle-{start_frame}", start_frame, duration_frames, text, "title.caption")
        existing = tuple(i for t in timeline.tracks if t.track_id == "agent-subtitles" for i in t.items)
        tracks = tuple(t for t in timeline.tracks if t.track_id != "agent-subtitles") + (
            Track("agent-subtitles", TrackKind.TITLE, existing + (caption,)),
        )
        self.tools._expected_revision_id = None
        self.tools._save(replace(timeline, tracks=tracks), "測試字幕")

    def test_components_must_not_restate_concurrent_subtitles(self):
        from clipen.editing.component_review import restated_ratio

        self.assertEqual(restated_ratio("還有三顆蛋", ["而且我還有三顆蛋哦。"]), 1)
        self.assertLess(restated_ratio("兩個人，三碗麵？", ["我們手滑點到了三碗拉麵"]), 0.5)
        self.cut()
        self.add_subtitle("這是我們看的第一個不用錢的夜景。", 0, 60)
        self.component(element_id="copy", component_id="headline", text="不用錢的夜景", x=0.08, y=0.25)
        self.component(
            element_id="later",
            component_id="headline",
            text="第一次的夜景",
            start_offset_frames=70,
            duration_frames=40,
            x=0.08,
            y=0.25,
        )
        self.add_subtitle("第一次的夜景真的很漂亮", 60, 60)
        validation = self.call("validate_sequence").payload
        # Echoing a subtitle is advice for the Agent, never a reason the film cannot be delivered.
        self.assertIn("component_restates_subtitle", [i["code"] for i in validation["advisories"]])
        self.assertNotIn("component_restates_subtitle", [i["code"] for i in validation["delivery_issues"]])

    def test_design_notes_flag_template_rhythm_without_blocking(self):
        from clipen.domain import Rational, Track
        from clipen.editing.component_review import design_notes

        timeline = EditTimeline(
            "t",
            "fresh-project",
            None,
            None,
            None,
            Rational(30, 1),
            640,
            360,
            0,
            (Track("video-main", TrackKind.VIDEO, ()),),
            production_session_id="s",
        )
        items = [
            TextClip(f"c{n}", n * 300, 120, text, f"s{n}")
            for n, text in enumerate(["熱熱上桌", "選擇，從細節開始", "親手試一試", "接下來，交給店員"])
        ]
        styles = {f"s{n}": {"component_id": "fact", "x": 0.07, "y": 0.1 + n * 0.005} for n in range(4)}
        codes = [n["code"] for n in design_notes(timeline, items, styles)]
        self.assertIn("component_position_repeated", codes)
        self.assertIn("component_duration_uniform", codes)
        self.assertEqual(codes.count("component_parallel_phrasing"), 2)
        varied = [TextClip(f"c{n}", n * 300, d, "拉麵 1200 円", f"s{n}") for n, d in enumerate((30, 45, 90, 150))]
        spread = {
            f"s{n}": {"component_id": "fact", "x": x, "y": y}
            for n, (x, y) in enumerate(((0.07, 0.1), (0.5, 0.4), (0.2, 0.6), (0.6, 0.1)))
        }
        self.assertEqual(design_notes(timeline, varied, spread), [])
        self.cut()
        self.component()
        validation = self.call("validate_sequence").payload
        self.assertIn("design_notes", validation)
        self.assertFalse(
            any(
                i["code"].startswith("component_") and i["code"] != "component_restates_subtitle"
                for i in validation["issues"]
            )
        )

    def test_older_graphics_are_listed_as_not_designed_freely(self):
        self.cut()
        self.component(component_id="pointer", target={"x": 0.7, "y": 0.45, "radius": 0.04})
        listed = self.call("list_program_components").payload
        self.assertEqual((listed["how"], listed["tool"]), ("free_design_only", "add_custom_component"))
        self.assertTrue(listed["existing_graphics"])
        self.assertFalse(any(g["designed_freely"] for g in listed["existing_graphics"]))

    def test_simultaneous_graphics_keep_separate_lanes_through_recut_and_remove(self):
        self.cut()
        self.component(element_id="identity", component_id="program_title", text="節目主題", y=0.10)
        self.component(element_id="speaker", component_id="person", text="現場人物", y=0.58)
        self.assertEqual(self.tools._component_issues(), [])
        self.call("reorder_clips", item_ids=["agent-video-0002", "agent-video-0001"], summary="交換鏡頭")
        elements = [
            i for t in self.tools._timeline().tracks if t.track_id.startswith("program-components") for i in t.items
        ]
        self.assertEqual({i.item_id for i in elements}, {"identity", "speaker"})
        self.assertTrue(all(i.start_frame == 126 for i in elements))
        self.assertEqual(self.tools._component_issues(), [])
        self.call("remove_program_component", element_id="identity", summary="移除主標保留人物資訊")
        validation = self.call("validate_sequence").payload
        self.assertFalse(
            any(i["code"] in {"program_graphics_missing", "title_cards_disabled"} for i in validation["issues"])
        )
        self.assertEqual(self.tools._component_issues(), [])

    def test_motion_families_animate_and_stay_inside_their_layout(self):
        from PIL import Image, ImageChops, ImageDraw

        from clipen.editing.production_media import load_text_styles
        from clipen.rendering.program_components import animation_boundaries, component_bounds, draw_program_component
        from clipen.rendering.program_motion import FAMILIES
        from clipen.rendering.titles import TitleCardRenderer

        self.cut()
        for kind in FAMILIES:
            with self.subTest(kind=kind):
                self.component(component_id=kind, element_id=kind, text="節目主題", detail="設計預覽")
                element = next(i for t in self.tools._timeline().tracks for i in t.items if i.item_id == kind)
                profile = load_text_styles(self.projects, self.project.project_id)[element.style_id]
                images = []
                for frame in (0, 8, 28):
                    image = Image.new("RGBA", (640, 360))
                    draw_program_component(
                        ImageDraw.Draw(image),
                        TitleCardRenderer._font_path(),
                        self.tools._timeline(),
                        element,
                        profile,
                        frame,
                    )
                    images.append(image)
                self.assertIsNotNone(ImageChops.difference(images[0], images[1]).getbbox())
                self.assertIsNotNone(ImageChops.difference(images[1], images[2]).getbbox())
                self.assertIn(element.start_frame + 16, animation_boundaries(element, profile))
                bounds, _, _ = component_bounds(
                    ImageDraw.Draw(images[0]), TitleCardRenderer._font_path(), self.tools._timeline(), element, profile
                )
                self.assertLessEqual(bounds[2], 640 * 0.945)
                self.assertLessEqual(bounds[3], 360 * 0.77)
        self.assertTrue(any(i["code"] == "program_component_collision" for i in self.tools._component_issues()))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_all_new_families_animate_in_actual_video_and_exit(self):
        from PIL import ImageChops

        self.real_media()
        self.cut()
        for kind, shot, offset, duration in [
            ("program_title", 1, 0, 55),
            ("headline", 1, 60, 55),
            ("impact", 2, 0, 35),
            ("chapter", 2, 40, 35),
            ("burst", 2, 80, 35),
        ]:
            self.component(
                component_id=kind,
                element_id=kind,
                item_id=f"agent-video-{shot:04d}",
                start_offset_frames=offset,
                duration_frames=duration,
                text="節目主題",
            )
        self.assertEqual(self.tools._component_issues(), [])
        render = self.call("render_preview").payload["render"]
        offsets = [0.03, 0.90, 1.94, 2.9, 4.65, 5.98, 7.30, 7.90]
        inspected = self.call(
            "inspect_preview", render_id=render["render_id"], offsets_seconds=offsets, wait_seconds=15
        )
        self.assertEqual(inspected.payload["render"]["state"], "succeeded", inspected.payload)
        from PIL import Image

        frames = [Image.open(p).convert("RGB") for p in inspected.images]
        for n in (0, 1, 3, 4, 5, 6):
            self.assertIsNotNone(ImageChops.difference(frames[n], frames[2]).crop((30, 20, 610, 270)).getbbox())
        self.assertGreater(sum(ImageChops.difference(frames[0], frames[1]).resize((1, 1)).getpixel((0, 0))), 1)
        self.assertLess(sum(ImageChops.difference(frames[2], frames[-1]).resize((1, 1)).getpixel((0, 0))), 8)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_actual_graphics_render_and_partial_draft_preview(self):
        self.real_media()
        self.cut()
        self.component()
        validation = self.call("validate_sequence").payload
        self.assertTrue(validation["delivery_issues"])
        self.assertEqual(validation["render_issues"], [])
        render = self.call("render_preview").payload["render"]
        inspected = self.call(
            "inspect_preview", render_id=render["render_id"], offsets_seconds=[0.1, 0.6, 2.0], wait_seconds=15
        )
        self.assertEqual(inspected.payload["render"]["state"], "succeeded", inspected.payload)
        from PIL import Image, ImageChops

        before, during, after = [Image.open(p).convert("RGB") for p in inspected.images]
        self.assertIsNotNone(ImageChops.difference(before, during).crop((30, 20, 300, 100)).getbbox())
        # A label no longer appears after its active interval.
        self.assertLess(sum(ImageChops.difference(before, after).resize((1, 1)).getpixel((0, 0))), 8)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required")
    def test_on_demand_speech_alignment_preserves_pauses_and_excerpt_timing(self):
        self.real_media()
        self.cut()
        text = "先等一下。真的好吃！"

        class Transcriber:
            def transcribe(self, audio, output, **kwargs):
                if not output.is_file() or output.stat().st_size != 0:
                    raise AssertionError("ASR adapter requires a reserved empty output file")
                return TranscriptionResult("zh", text, (TranscriptSegment(0, 64000, text, None, ()),))

            def close(self):
                pass

        self.tools.transcriber_binding = lambda: (Transcriber, {"id": "test-local", "revision": "fixed"})
        words = [
            {
                "text": c,
                "start": (0.4 + 0.15 * n if n < 4 else 2.4 + 0.15 * (n - 4)),
                "end": (0.55 + 0.15 * n if n < 4 else 2.55 + 0.15 * (n - 4)),
            }
            for n, c in enumerate("先等一下真的好吃")
        ]
        with patch("clipen.editing.alignment.run_alignment", return_value=words):
            job = self.call("transcribe_source_range", candidate_id=self.candidate).payload["job"]
            completed = self.call("read_evidence_job", job_id=job["job_id"], wait_seconds=10).payload["job"]
        self.assertEqual(completed["state"], "succeeded", completed)
        self.assertEqual(completed["result"]["timing_basis"], "forced_alignment")
        reused = self.call("transcribe_source_range", candidate_id=self.candidate).payload
        self.assertTrue(reused["reused"])
        cues = self.call("list_transcripts", include_drafts=True).payload["cues"]
        self.assertTrue(cues)
        self.call(
            "replace_subtitles",
            captions=[{"cue_id": cues[0]["cue_id"], "text": text}],
            allow_asr_draft=True,
            summary="依字詞時間建立字幕",
        )
        captions = [i for t in self.tools._timeline().tracks if t.track_id == "agent-subtitles" for i in t.items]
        self.assertEqual(len(captions), 2)
        self.assertEqual(captions[0].start_frame, 12)
        self.assertEqual(captions[1].start_frame, 72)
        self.assertLess(captions[0].start_frame + captions[0].duration_frames, captions[1].start_frame)
        self.call(
            "replace_subtitles",
            captions=[{"cue_id": cues[0]["cue_id"], "text": "真的好吃", "source_excerpt": {"start": 5, "end": 9}}],
            allow_asr_draft=True,
            summary="摘錄完整字詞",
        )
        caption = next(i for t in self.tools._timeline().tracks if t.track_id == "agent-subtitles" for i in t.items)
        self.assertEqual(caption.start_frame, 72)
        corrected = "先等一下。真是好吃！"
        corrected_words = [{**w, "text": c} for w, c in zip(words, "先等一下真是好吃")]
        with patch("clipen.editing.alignment.run_alignment", return_value=corrected_words):
            correction = self.call(
                "revise_source_speech",
                transcript_job_id=job["job_id"],
                segment_ordinal=0,
                text=corrected,
                evidence="測試更正提案，尚未核對原音。",
            ).payload["job"]
            correction = self.call("read_evidence_job", job_id=correction["job_id"], wait_seconds=10).payload["job"]
        self.assertEqual(correction["state"], "succeeded", correction)
        self.assertEqual(correction["result"]["segments"][0]["source_asr_text"], text)
        issues = self.call("validate_sequence").payload["issues"]
        self.assertIn("subtitle_source_changed", {issue["code"] for issue in issues})
        latest = self.call("list_transcripts", include_drafts=True).payload["cues"]
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["text"], corrected)
        self.assertEqual(latest[0]["wording_authority"], "model_proposed_correction")
        # A new session reuses source/model evidence without requiring the old
        # analysis stage or invoking the local model a second time.
        self.repository.cancel(self.project.project_id, self.run.run_id)
        next_run, _ = self.repository.create_run(
            self.project.project_id,
            "reuse-source-evidence",
            production={"skill_id": "travel-program", "intent": "另一個節目製作工作"},
        )
        following = EditAgentTools(self.repository, self.project.project_id, next_run.run_id)
        following.activate()

        def must_not_run():
            raise AssertionError("cached evidence must not rerun inference")

        following.transcriber_binding = lambda: (must_not_run, {"id": "test-local", "revision": "fixed"})
        try:
            source = following.call("list_source_media", {}).payload["sources"][0]
            candidate = following.call(
                "register_source_range",
                {
                    "version_id": source["version_id"],
                    "stream_index": 0,
                    "start_offset_seconds": 0,
                    "duration_seconds": 8,
                    "description": "相同來源",
                },
            ).payload["candidate"]["candidate_id"]
            cached = following.call("transcribe_source_range", {"candidate_id": candidate}).payload
            self.assertTrue(cached["reused"])
            self.assertEqual(cached["job"]["state"], "succeeded")
            self.assertEqual(cached["job"]["result"]["segments"][0]["text"], text)
        finally:
            following.close_production()

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required")
    def test_split_remaps_subtitles_and_can_read_partial_aligned_utterances(self):
        self.cut()
        from clipen.projects.storage import utc_now

        clip = self.tools._video().items[0]
        result = {
            "source": clip.source.span.to_storage(),
            "source_start_seconds": 1,
            "segments": [
                {
                    "text": "先等。好吃！",
                    "start_seconds": 0,
                    "end_seconds": 4,
                    "words": [
                        {"text": "先等", "char_start": 0, "char_end": 2, "start_seconds": 0.4, "end_seconds": 0.9},
                        {"text": "好吃", "char_start": 3, "char_end": 5, "start_seconds": 2.4, "end_seconds": 2.9},
                    ],
                }
            ],
        }
        with self.projects.connect(self.project.project_id) as db:
            db.execute(
                "INSERT INTO production_evidence_jobs VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "speech-split",
                    self.run.run_id,
                    "split-key",
                    "align",
                    "succeeded",
                    "{}",
                    json.dumps(result),
                    None,
                    utc_now(),
                ),
            )
        cue = self.call("list_transcripts", include_drafts=True).payload["cues"][0]
        self.call(
            "replace_subtitles",
            captions=[{"cue_id": cue["cue_id"], "text": cue["text"]}],
            allow_asr_draft=True,
            summary="來源字幕",
        )
        self.call("split_clip", item_id=clip.item_id, offset_frames=45, summary="在停頓處分鏡")
        fresh = self.call("list_transcripts", include_drafts=True).payload["cues"]
        self.assertEqual([c["text"] for c in fresh], ["先等。", "好吃！"])
        ids = [i.item_id for i in self.tools._video().items]
        self.call("reorder_clips", item_ids=[ids[1], ids[0], ids[2]], summary="檢查分割後再次修改")
        captions = [i for t in self.tools._timeline().tracks if t.track_id == "agent-subtitles" for i in t.items]
        self.assertEqual(sorted(i.start_frame for i in captions), [27, 87])

    def test_alignment_rejects_mismatch_missing_words_and_invalid_times(self):
        for words in (
            [{"text": "別", "start": 0, "end": 1}],
            [{"text": "好", "start": 1, "end": 0}],
            [{"text": "好", "start": float("nan"), "end": 1}],
            [],
        ):
            with self.assertRaises(ProjectError):
                validate_alignment(words, "好", 2)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_actual_music_stem_ducks_only_during_aligned_speech_and_reaches_final_mix(self):
        self.real_media()
        self.cut()
        self.component()
        from clipen.editing.production_media import AgentAudioStore
        from clipen.finishing.audio import inspect_library
        from clipen.projects.storage import utc_now

        store = AgentAudioStore(self.projects, self.project.project_id)
        directory = store.directory / "test-music"
        directory.mkdir()
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:sample_rate=48000:duration=8",
                str(directory / "music.wav"),
            ],
            check=True,
        )
        info = directory.stat()
        reference = DirectoryReference(
            DirectoryRole.SOURCE, str(directory), directory.name, directory.name, info.st_dev, info.st_ino, utc_now()
        ).to_storage()
        assets, _ = inspect_library("music", reference, threading.Event(), lambda _: None)
        store.add(assets, reference)
        self.call(
            "place_audio",
            item_id="agent-video-0001",
            layer_id="music",
            asset_id=assets[0]["asset_id"],
            start_frame=0,
            duration_frames=240,
            source_offset_seconds=0,
            gain_db=-8,
            duck=True,
            fade_seconds=0,
            summary="配樂測試",
        )
        span = self.tools._video().items[0].source.span.to_storage()
        speech = {
            "source": span,
            "source_start_seconds": 1,
            "segments": [
                {
                    "text": "好",
                    "start_seconds": 1.0,
                    "end_seconds": 2.0,
                    "words": [{"text": "好", "char_start": 0, "char_end": 1, "start_seconds": 1.0, "end_seconds": 2.0}],
                }
            ],
        }
        with self.projects.connect(self.project.project_id) as db:
            db.execute(
                "INSERT INTO production_evidence_jobs VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "speech-test",
                    self.run.run_id,
                    "test-aligned",
                    "align",
                    "succeeded",
                    "{}",
                    json.dumps(speech),
                    None,
                    utc_now(),
                ),
            )
        render = self.call("render_preview").payload["render"]
        done = self.call("inspect_preview", render_id=render["render_id"], offsets_seconds=[0.6], wait_seconds=15)
        self.assertEqual(done.payload["render"]["state"], "succeeded", done.payload)
        mixed = self.call("inspect_mix", render_id=render["render_id"]).payload
        self.assertEqual(mixed["duck_basis"], "aligned_source_words")
        self.assertEqual(mixed["issues"], [])
        windows = mixed["measurements"]["music"]["window_rms_dbfs"]
        self.assertAlmostEqual(windows[3] - windows[0], -7, delta=0.7)
        self.assertAlmostEqual(windows[8], windows[0], delta=0.5)
        self.assertFalse(mixed["audio_listened"])
        # Check the actual encoded final output, not just music metadata/stem.
        import math
        from array import array

        from clipen.rendering import PreviewRenderRepository

        output = PreviewRenderRepository(self.projects).output_path(
            self.project.project_id, render["render_id"], prepare=False
        )
        raw = subprocess.check_output(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                "0.5",
                "-i",
                str(output),
                "-t",
                "0.25",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-f",
                "s16le",
                "pipe:1",
            ]
        )
        samples = array("h")
        samples.frombytes(raw)

        def amplitude(frequency):
            return (
                abs(
                    sum(
                        v
                        * complex(
                            math.cos(2 * math.pi * frequency * n / 8000), math.sin(2 * math.pi * frequency * n / 8000)
                        )
                        for n, v in enumerate(samples)
                    )
                )
                * 2
                / len(samples)
            )

        self.assertGreater(amplitude(880), 300)
        self.assertGreater(amplitude(220), 300)


if __name__ == "__main__":
    unittest.main()
