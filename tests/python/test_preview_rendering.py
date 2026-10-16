import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from clipen.domain import (
    ClipSource,
    EditTimeline,
    EffectRequest,
    MediaClip,
    Rational,
    SourceSpan,
    Track,
    TrackKind,
    TextClip,
)
from clipen.editing import EditAgentRepository, OutputSettings, TimelineRepository
from clipen.controller import (
    ControllerConfig,
    LocalSessionAuthority,
    ProviderState,
    ProviderStatus,
    create_app,
)
from clipen.projects import ProjectError, ProjectRepository
from clipen.rendering import (
    FfmpegPreviewRenderer,
    PreviewRenderError,
    PreviewRenderRepository,
    PreviewRenderService,
)
from clipen.rendering.titles import RenderedTitle


async def request_app(app, method, path, *, headers=None, body=b""):
    route_path, separator, query = path.partition("?")
    combined = {"host": "localhost:4200", **(headers or {})}
    if body and "content-length" not in combined:
        combined["content-length"] = str(len(body))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": route_path,
        "raw_path": route_path.encode("ascii"),
        "query_string": query.encode("ascii") if separator else b"",
        "root_path": "",
        "headers": [
            (key.lower().encode("ascii"), value.encode("latin-1"))
            for key, value in combined.items()
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 4200),
    }
    sent_request = False
    messages = []

    async def receive():
        nonlocal sent_request
        if sent_request:
            return {"type": "http.disconnect"}
        sent_request = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    response_body = b"".join(
        item.get("body", b"")
        for item in messages
        if item["type"] == "http.response.body"
    )
    response_headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in start["headers"]
    }
    return start["status"], response_headers, response_body


def timeline(project_id: str) -> EditTimeline:
    video = MediaClip(
        "video-000001",
        0,
        60,
        ClipSource(SourceSpan("av_" + "a" * 32, 2, "9000", "189000", Rational(1, 90000))),
    )
    audio = MediaClip(
        "audio-000001",
        0,
        60,
        ClipSource(SourceSpan("av_" + "b" * 32, 4, "4800", "100800", Rational(1, 48000))),
    )
    return EditTimeline(
        "tl_" + "c" * 32,
        project_id,
        "plan-" + "d" * 32,
        1,
        "e" * 64,
        Rational(30, 1),
        1920,
        1080,
        60,
        (
            Track("video-primary", TrackKind.VIDEO, (video,)),
            Track("audio-primary", TrackKind.AUDIO, (audio,)),
        ),
    )


class SuccessfulRenderer:
    def render(self, project_id, edit, output, cancel, progress) -> None:
        del project_id, edit
        if cancel.is_set():
            raise PreviewRenderError("cancelled")
        progress(0.45)
        output.write_bytes(b"local preview")
        progress(1)


class BlockingRenderer:
    def __init__(self) -> None:
        self.started = threading.Event()

    def render(self, project_id, edit, output, cancel, progress) -> None:
        del project_id, edit, output
        self.started.set()
        progress(0.2)
        if not cancel.wait(timeout=3):
            raise PreviewRenderError("test_timeout")
        raise PreviewRenderError("cancelled")


class PreviewRenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.projects = ProjectRepository(Path(self.temporary.name) / "projects")
        self.project = self.projects.create_project("request-preview-project", "初剪")
        self.timeline = timeline(self.project.project_id)
        self.timelines = TimelineRepository(self.projects)
        self.timelines.save(
            self.project.project_id,
            "request-preview-timeline",
            self.timeline.plan_id,
            self.timeline.plan_revision,
            OutputSettings("landscape_1080p", 30),
            self.timeline,
        )
        self.renders = PreviewRenderRepository(self.projects)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def wait_for_terminal(self, service: PreviewRenderService):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = service.status(self.project.project_id)
            if status["render"]["state"] not in {"queued", "running", "cancelling"}:
                return status
            time.sleep(0.01)
        self.fail("preview render did not finish")

    def test_success_is_durable_idempotent_and_published_atomically(self) -> None:
        service = PreviewRenderService(self.renders, self.timelines, SuccessfulRenderer())
        started = service.start(
            self.project.project_id, "request-preview-start", self.timeline.timeline_id
        )
        self.assertIn(started["render"]["state"], {"queued", "running", "succeeded"})
        completed = self.wait_for_terminal(service)
        self.assertEqual(completed["render"]["state"], "succeeded")

        self.assertEqual(completed["render"]["progress"], 1)
        self.assertEqual(
            service.media_path(
                self.project.project_id, completed["render"]["render_id"]
            ).read_bytes(),
            b"local preview",
        )
        self.assertEqual(
            service.media_path(
                self.project.project_id, completed["render"]["render_id"]
            ).stat().st_mode & 0o777,
            0o600,
        )
        replay = service.start(
            self.project.project_id, "request-preview-start", self.timeline.timeline_id
        )
        self.assertEqual(replay["render"]["render_id"], completed["render"]["render_id"])
        self.assertEqual(list(
            (self.projects.root / self.project.project_id / "previews").glob("*.partial.mp4")
        ), [])

    def test_cancel_marks_render_and_removes_partial_output(self) -> None:
        renderer = BlockingRenderer()
        service = PreviewRenderService(self.renders, self.timelines, renderer)
        started = service.start(
            self.project.project_id, "request-preview-cancel", self.timeline.timeline_id
        )
        self.assertTrue(renderer.started.wait(timeout=1))
        service.cancel(
            self.project.project_id, started["render"]["render_id"]
        )
        completed = self.wait_for_terminal(service)
        self.assertEqual(completed["render"]["state"], "cancelled")
        with self.assertRaises(ProjectError) as unavailable:
            service.media_path(
                self.project.project_id, completed["render"]["render_id"]
            )
        self.assertEqual(unavailable.exception.code, "preview_not_ready")

    def test_close_cancels_background_render_before_controller_exits(self) -> None:
        renderer = BlockingRenderer()
        service = PreviewRenderService(self.renders, self.timelines, renderer)
        service.start(
            self.project.project_id, "request-preview-close", self.timeline.timeline_id
        )
        self.assertTrue(renderer.started.wait(timeout=1))
        service.close()
        self.assertEqual(
            service.status(self.project.project_id)["render"]["state"], "cancelled"
        )

    def test_recovery_marks_incomplete_work_as_interrupted(self) -> None:
        queued = self.renders.create(
            self.project.project_id, "request-preview-recover", self.timeline.timeline_id
        )
        partial = self.renders.output_path(
            self.project.project_id, queued["render_id"], staging=True
        )
        partial.write_bytes(b"incomplete")
        service = PreviewRenderService(self.renders, self.timelines, SuccessfulRenderer())
        service.recover((self.project.project_id,))
        recovered = self.renders.get(self.project.project_id, queued["render_id"])
        self.assertEqual(recovered["state"], "interrupted")
        self.assertEqual(recovered["error_code"], "controller_restarted")
        self.assertFalse(partial.exists())

    def test_request_conflict_and_timeline_revision_are_rejected(self) -> None:
        self.renders.create(
            self.project.project_id, "request-preview-conflict", self.timeline.timeline_id
        )
        with self.assertRaises(ProjectError) as conflict:
            self.renders.create(
                self.project.project_id,
                "request-preview-conflict",
                "tl_" + "f" * 32,
            )
        self.assertEqual(conflict.exception.code, "request_conflict")
        service = PreviewRenderService(self.renders, self.timelines, SuccessfulRenderer())
        with self.assertRaises(ProjectError) as stale:
            service.start(
                self.project.project_id,
                "request-preview-stale",
                "tl_" + "f" * 32,
            )
        self.assertEqual(stale.exception.code, "timeline_revision_conflict")

    def test_unaccepted_validation_warnings_block_preview(self) -> None:
        class NeedsConfirmation:
            def status(self, project_id):
                del project_id
                return {"state": "needs_confirmation", "validation": None}

        service = PreviewRenderService(
            self.renders, self.timelines, SuccessfulRenderer(), NeedsConfirmation()
        )
        self.assertFalse(service.status(self.project.project_id)["can_start"])
        with self.assertRaises(ProjectError) as blocked:
            service.start(
                self.project.project_id,
                "request-preview-blocked",
                self.timeline.timeline_id,
            )
        self.assertEqual(blocked.exception.code, "preview_validation_blocked")

    def test_camera_audio_and_output_capability_issues_still_allow_preview(self) -> None:
        class PreviewOnlyIssues:
            def status(self, project_id):
                del project_id
                return {
                    "state": "blocked",
                    "validation": {
                        "report": {
                            "issues": [
                                {"rule_id": "timeline.content.audio_missing"},
                                {"rule_id": "timeline.capability.element_unverified"},
                            ]
                        }
                    },
                }

        service = PreviewRenderService(
            self.renders, self.timelines, SuccessfulRenderer(), PreviewOnlyIssues()
        )
        self.assertTrue(service.status(self.project.project_id)["can_start"])

    def test_successful_ai_revision_becomes_the_preview_target(self) -> None:
        agents = EditAgentRepository(self.projects)
        run, _ = agents.create_run(
            self.project.project_id,
            "request-preview-ai-agent",
        )
        agents.activate(self.project.project_id, run.run_id)
        proof_span = SourceSpan(
            "av_" + "d" * 32,
            0,
            "0",
            "360000",
            Rational(1, 90000),
        )
        proof = EditTimeline(
            self.timeline.timeline_id,
            self.timeline.project_id,
            self.timeline.plan_id,
            self.timeline.plan_revision,
            self.timeline.plan_content_hash,
            self.timeline.frame_rate,
            self.timeline.width,
            self.timeline.height,
            960,
            (
                Track(
                    "video-proof",
                    TrackKind.VIDEO,
                    tuple(
                        MediaClip(
                            f"proof-{index}",
                            index * 120,
                            120,
                            ClipSource(proof_span),
                            (
                                EffectRequest(
                                    "video.slow_push",
                                    {"scale_from": 1.0, "scale_to": 1.08},
                                ),
                            )
                            if index == 0
                            else (),
                        )
                        for index in range(8)
                    ),
                ),
                Track(
                    "title-proof",
                    TrackKind.TITLE,
                    (TextClip("proof-title", 0, 60, "東京", "title.default"),),
                ),
            ),
        )
        agents.save_revision(self.project.project_id, run.run_id, proof, "短樣片")
        finished = agents.finish(self.project.project_id, run.run_id, "AI 版本完成")
        service = PreviewRenderService(
            self.renders,
            self.timelines,
            SuccessfulRenderer(),
            edit_agents=agents,
        )

        status = service.status(self.project.project_id)
        self.assertEqual(status["source"], "ai")
        self.assertTrue(status["target_is_style_proof"])
        self.assertEqual(status["target_stats"]["clip_count"], 8)
        self.assertEqual(status["target_timeline_id"], finished.final_revision_id)
        started = service.start(
            self.project.project_id,
            "request-preview-ai-result",
            finished.final_revision_id,
        )
        self.assertEqual(started["render"]["timeline_id"], finished.final_revision_id)
        completed = self.wait_for_terminal(service)
        self.assertEqual(completed["render"]["state"], "succeeded")

        retry, _ = agents.create_run(
            self.project.project_id,
            "request-preview-ai-agent-retry",
        )
        agents.record_provider_outcome(
            self.project.project_id,
            retry.run_id,
            "agent-failed-retry",
            usage=None,
            error_code="provider_failed",
        )
        preserved = service.status(self.project.project_id)
        self.assertEqual(preserved["source"], "ai")
        self.assertEqual(preserved["target_timeline_id"], finished.final_revision_id)

        draft_run, _ = agents.create_run(
            self.project.project_id,
            "request-preview-incomplete-proof",
        )
        agents.activate(self.project.project_id, draft_run.run_id)
        draft_video = replace(
            proof.tracks[0],
            items=tuple(replace(item, effects=()) for item in proof.tracks[0].items),
        )
        incomplete = replace(proof, tracks=(draft_video,))
        agents.save_revision(
            self.project.project_id,
            draft_run.run_id,
            incomplete,
            "只有結構的短樣片",
        )
        incomplete_run = agents.finish(
            self.project.project_id,
            draft_run.run_id,
            "保留供本機檢查",
        )

        diagnostic = service.status(self.project.project_id)
        self.assertFalse(diagnostic["target_is_style_proof"])
        self.assertTrue(diagnostic["can_start"])
        started = service.start(
            self.project.project_id,
            "request-preview-incomplete-proof-render",
            incomplete_run.final_revision_id,
        )
        self.assertEqual(
            started["render"]["timeline_id"], incomplete_run.final_revision_id
        )
        self.assertEqual(self.wait_for_terminal(service)["render"]["state"], "succeeded")

    def test_legacy_full_ai_draft_cannot_be_rendered_as_a_style_proof(self) -> None:
        agents = EditAgentRepository(self.projects)
        run, _ = agents.create_run(
            self.project.project_id,
            "request-preview-legacy-agent",
        )
        agents.activate(self.project.project_id, run.run_id)
        finished = agents.finish(self.project.project_id, run.run_id, "舊版結果")
        service = PreviewRenderService(
            self.renders,
            self.timelines,
            SuccessfulRenderer(),
            edit_agents=agents,
        )

        status = service.status(self.project.project_id)
        self.assertEqual(status["target_timeline_id"], finished.final_revision_id)
        self.assertFalse(status["target_is_style_proof"])
        self.assertFalse(status["can_start"])
        with self.assertRaises(ProjectError) as blocked:
            service.start(
                self.project.project_id,
                "request-preview-legacy-render",
                finished.final_revision_id,
            )
        self.assertEqual(blocked.exception.code, "preview_style_proof_required")

    def test_v14_preview_rows_migrate_for_ai_revision_targets(self) -> None:
        existing = self.renders.create(
            self.project.project_id,
            "request-preview-v14-migration",
            self.timeline.timeline_id,
        )
        with self.projects.connect(self.project.project_id) as connection:
            connection.execute("PRAGMA user_version=14")

        restarted = ProjectRepository(self.projects.root)
        with restarted.connect(self.project.project_id) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 15)
            row = connection.execute(
                "SELECT render_id,timeline_id FROM preview_renders WHERE request_id=?",
                ("request-preview-v14-migration",),
            ).fetchone()
            foreign_targets = {
                item["table"]
                for item in connection.execute("PRAGMA foreign_key_list(preview_renders)")
            }
        self.assertEqual(row["render_id"], existing["render_id"])
        self.assertEqual(row["timeline_id"], self.timeline.timeline_id)
        self.assertNotIn("edit_timelines", foreign_targets)
        self.assertEqual(
            PreviewRenderRepository(restarted).current(self.project.project_id)["render_id"],
            existing["render_id"],
        )

    def test_v12_project_migrates_preview_tables_without_changing_timeline(self) -> None:
        database = self.projects.root / self.project.project_id / "project.sqlite3"
        with self.projects.connect(self.project.project_id) as connection:
            connection.execute("DROP TABLE current_preview_render")
            connection.execute("DROP TABLE preview_renders")
            connection.execute("PRAGMA user_version = 12")
        current = TimelineRepository(self.projects).current(self.project.project_id)
        self.assertEqual(current.timeline, self.timeline)
        with self.projects.connect(self.project.project_id) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 15)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("preview_renders", tables)
        self.assertTrue(database.is_file())


class FakeMedia:
    def __init__(self, audio_stream_indices=(4,), video_stream_indices=(2,)) -> None:
        self.next_descriptor = 40
        self.opened = []
        self.audio_stream_indices = audio_stream_indices
        self.video_stream_indices = video_stream_indices

    def open(self, project_id, version_id):
        del project_id
        descriptor = self.next_descriptor
        self.next_descriptor += 1
        record = type(
            "OpenedMedia",
            (),
            {
                "descriptor": descriptor,
                "version_id": version_id,
                "audio_stream_indices": self.audio_stream_indices,
                "video_stream_indices": self.video_stream_indices,
                "closed": False,
                "close": lambda self: setattr(self, "closed", True),
            },
        )()
        self.opened.append(record)
        return record


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = iter(
            ("out_time_ms=N/A\n", "out_time_ms=1000000\n", "progress=end\n")
        )
        self.terminated = False

    def wait(self):
        return 0

    def terminate(self):
        self.terminated = True


class FfmpegPreviewRendererTests(unittest.TestCase):
    def test_command_uses_exact_video_and_audio_streams(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "preview.mp4"
        media = FakeMedia()
        captured = {}

        def popen(command, **options):
            captured["command"] = command
            captured["options"] = options
            output.write_bytes(b"mp4")
            return FakeProcess()

        edit = timeline("prj_" + "1" * 32)
        progress = []
        FfmpegPreviewRenderer(media, popen=popen).render(
            edit.project_id, edit, output, threading.Event(), progress.append
        )
        command = captured["command"]
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("[0:2]trim=duration=2.000000", filters)
        self.assertIn("[1:4]atrim=duration=2.000000", filters)
        self.assertIn("concat=n=1:v=1:a=0[vout]", filters)
        self.assertIn("concat=n=1:v=0:a=1[asequence]", filters)
        self.assertEqual(captured["options"]["pass_fds"], (40, 41))
        self.assertEqual(progress[-1], 1)
        self.assertTrue(all(item.closed for item in media.opened))

    def test_semantic_video_effects_are_rendered_and_audio_is_normalized(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "preview.mp4"
        media = FakeMedia()
        captured = {}

        def popen(command, **options):
            del options
            captured["command"] = command
            output.write_bytes(b"mp4")
            return FakeProcess()

        original = timeline("prj_" + "e" * 32)
        source = original.tracks[0].items[0].source
        effected = MediaClip(
            "video-effected",
            0,
            60,
            source,
            (
                EffectRequest("video.punch_in", {"scale": 1.12}),
                EffectRequest("video.speed_ramp", {"rate": 1.25}),
            ),
        )
        edit = EditTimeline(
            original.timeline_id,
            original.project_id,
            original.plan_id,
            original.plan_revision,
            original.plan_content_hash,
            original.frame_rate,
            original.width,
            original.height,
            original.duration_frames,
            (
                Track("video-main", TrackKind.VIDEO, (effected,)),
                original.tracks[1],
            ),
        )
        FfmpegPreviewRenderer(media, popen=popen).render(
            edit.project_id, edit, output, threading.Event(), lambda value: None
        )
        filters = captured["command"][captured["command"].index("-filter_complex") + 1]
        self.assertIn("scale=iw*1.1200:ih*1.1200", filters)
        self.assertIn("split=3[vr0a][vr0b][vr0c]", filters)
        self.assertIn("setpts=(PTS-STARTPTS)/1.250000", filters)
        self.assertIn("eq=contrast=1.025:saturation=1.045", filters)
        self.assertIn("loudnorm=I=-16:LRA=11:TP=-1.5", filters)

    def test_title_track_gets_a_separate_bounded_compositing_pass(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        output = root / "preview.mp4"
        title_image = root / "title.png"
        title_image.write_bytes(b"png")
        commands = []

        def popen(command, **options):
            del options
            commands.append(command)
            Path(command[-1]).write_bytes(b"mp4")
            return FakeProcess()

        original = timeline("prj_" + "f" * 32)
        edit = EditTimeline(
            original.timeline_id,
            original.project_id,
            original.plan_id,
            original.plan_revision,
            original.plan_content_hash,
            original.frame_rate,
            original.width,
            original.height,
            original.duration_frames,
            original.tracks
            + (
                Track(
                    "titles-main",
                    TrackKind.TITLE,
                    (TextClip("title-one", 0, 30, "東京", "title.default"),),
                ),
            ),
        )
        rendered = (RenderedTitle(title_image, 0.0, 1.0),)
        with patch(
            "clipen.rendering.renderer.TitleCardRenderer.render",
            return_value=rendered,
        ):
            FfmpegPreviewRenderer(FakeMedia(), popen=popen).render(
                edit.project_id, edit, output, threading.Event(), lambda value: None
            )

        self.assertEqual(len(commands), 2)
        filters = commands[1][commands[1].index("-filter_complex") + 1]
        self.assertIn("fade=t=in", filters)
        self.assertIn("overlay=0:0", filters)
        self.assertEqual(commands[1][commands[1].index("-map") + 1], "[composed1]")

    def test_camera_audio_follows_video_speed_ramp_without_duration_drift(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "preview.mp4"
        captured = {}

        def popen(command, **options):
            del options
            captured["command"] = command
            output.write_bytes(b"mp4")
            return FakeProcess()

        original = timeline("prj_" + "0" * 32)
        source = original.tracks[0].items[0].source
        clip = MediaClip(
            "video-speed",
            0,
            60,
            source,
            (EffectRequest("video.speed_ramp", {"rate": 1.25}),),
        )
        edit = EditTimeline(
            original.timeline_id,
            original.project_id,
            original.plan_id,
            original.plan_revision,
            original.plan_content_hash,
            original.frame_rate,
            original.width,
            original.height,
            original.duration_frames,
            (Track("video-main", TrackKind.VIDEO, (clip,)),),
        )
        FfmpegPreviewRenderer(FakeMedia(), popen=popen).render(
            edit.project_id, edit, output, threading.Event(), lambda value: None
        )
        filters = captured["command"][captured["command"].index("-filter_complex") + 1]
        self.assertIn("asplit=3[ar0a][ar0b][ar0c]", filters)
        self.assertIn("atempo=1.250000", filters)
        self.assertIn("atempo=0.952381", filters)

    def test_video_only_timeline_uses_camera_audio_stream(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "preview.mp4"
        media = FakeMedia(audio_stream_indices=(6,))
        captured = {}

        def popen(command, **options):
            captured["command"] = command
            output.write_bytes(b"mp4")
            return FakeProcess()

        original = timeline("prj_" + "3" * 32)
        edit = EditTimeline(
            original.timeline_id,
            original.project_id,
            original.plan_id,
            original.plan_revision,
            original.plan_content_hash,
            original.frame_rate,
            original.width,
            original.height,
            original.duration_frames,
            (original.tracks[0],),
        )
        FfmpegPreviewRenderer(media, popen=popen).render(
            edit.project_id, edit, output, threading.Event(), lambda value: None
        )
        command = captured["command"]
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("[0:6]atrim=duration=2.000000", filters)
        self.assertNotIn("anullsrc", filters)
        self.assertIn("concat=n=1:v=0:a=1[asequence]", filters)

    def test_video_track_recovers_from_audio_stream_index_in_existing_timeline(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "preview.mp4"
        media = FakeMedia(audio_stream_indices=(1,), video_stream_indices=(0, 5))
        captured = {}

        def popen(command, **options):
            captured["command"] = command
            output.write_bytes(b"mp4")
            return FakeProcess()

        original = timeline("prj_" + "5" * 32)
        wrong_source = SourceSpan(
            original.tracks[0].items[0].source.span.version_id,
            1,
            "0",
            "96000",
            Rational(1, 48000),
        )
        wrong_clip = MediaClip(
            "video-000001", 0, 60, ClipSource(wrong_source)
        )
        edit = EditTimeline(
            original.timeline_id,
            original.project_id,
            original.plan_id,
            original.plan_revision,
            original.plan_content_hash,
            original.frame_rate,
            original.width,
            original.height,
            original.duration_frames,
            (Track("video-primary", TrackKind.VIDEO, (wrong_clip,)),),
        )
        FfmpegPreviewRenderer(media, popen=popen).render(
            edit.project_id, edit, output, threading.Event(), lambda value: None
        )
        command = captured["command"]
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("[0:0]trim=duration=2.000000", filters)
        self.assertIn("[0:1]atrim=duration=2.000000", filters)

    def test_large_timeline_renders_bounded_batches_before_lossless_concat(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "preview.mp4"
        media = FakeMedia(audio_stream_indices=(1,), video_stream_indices=(0,))
        commands = []

        def popen(command, **options):
            commands.append((command, options))
            Path(command[-1]).write_bytes(b"mp4")
            return FakeProcess()

        project_id = "prj_" + "6" * 32
        clips = tuple(
            MediaClip(
                f"video-{index:06d}",
                index * 60,
                60,
                ClipSource(
                    SourceSpan(
                        "av_" + "7" * 32,
                        0,
                        "0",
                        "180000",
                        Rational(1, 90000),
                    )
                ),
            )
            for index in range(9)
        )
        edit = EditTimeline(
            "tl_" + "8" * 32,
            project_id,
            "plan-" + "9" * 32,
            1,
            "a" * 64,
            Rational(30, 1),
            1920,
            1080,
            540,
            (Track("video-primary", TrackKind.VIDEO, clips),),
        )
        progress = []
        FfmpegPreviewRenderer(media, popen=popen).render(
            project_id, edit, output, threading.Event(), progress.append
        )
        self.assertEqual(len(commands), 3)
        self.assertIn("-filter_complex", commands[0][0])
        self.assertEqual(len(commands[0][1]["pass_fds"]), 8)
        self.assertEqual(len(commands[1][1]["pass_fds"]), 1)
        self.assertEqual(commands[2][0][commands[2][0].index("-f") + 1], "concat")
        self.assertNotIn("pass_fds", commands[2][1])
        self.assertEqual(progress[-1], 1)

    def test_video_without_camera_audio_gets_clip_length_silence(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "preview.mp4"
        captured = {}

        def popen(command, **options):
            captured["command"] = command
            output.write_bytes(b"mp4")
            return FakeProcess()

        original = timeline("prj_" + "4" * 32)
        edit = EditTimeline(
            original.timeline_id,
            original.project_id,
            original.plan_id,
            original.plan_revision,
            original.plan_content_hash,
            original.frame_rate,
            original.width,
            original.height,
            original.duration_frames,
            (original.tracks[0],),
        )
        FfmpegPreviewRenderer(FakeMedia(audio_stream_indices=()), popen=popen).render(
            edit.project_id, edit, output, threading.Event(), lambda value: None
        )
        command = captured["command"]
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("anullsrc=r=48000:cl=stereo,atrim=duration=2.000000[a0]", filters)

    def test_missing_ffmpeg_has_stable_error_and_closes_source(self) -> None:
        media = FakeMedia()

        def unavailable(*args, **kwargs):
            del args, kwargs
            raise FileNotFoundError

        edit = timeline("prj_" + "2" * 32)
        with self.assertRaises(PreviewRenderError) as error:
            FfmpegPreviewRenderer(media, popen=unavailable).render(
                edit.project_id,
                edit,
                Path(tempfile.gettempdir()) / "unused-preview.mp4",
                threading.Event(),
                lambda value: None,
            )
        self.assertEqual(error.exception.code, "ffmpeg_unavailable")
        self.assertTrue(all(item.closed for item in media.opened))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is unavailable")
    def test_real_ffmpeg_uses_embedded_audio_for_video_only_timeline(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "source.mp4"
        output = root / "preview.mp4"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                str(source),
            ],
            check=True,
        )

        version_id = "av_" + "9" * 32
        video = MediaClip(
            "video-000001", 0, 60,
            ClipSource(SourceSpan(version_id, 0, "0", "180000", Rational(1, 90000))),
        )
        edit = EditTimeline(
            "tl_" + "8" * 32,
            "prj_" + "7" * 32,
            "plan-" + "6" * 32,
            1,
            "5" * 64,
            Rational(30, 1),
            640,
            360,
            60,
            (Track("video-primary", TrackKind.VIDEO, (video,)),),
        )

        class LocalMedia:
            def open(self, project_id, requested_version):
                del project_id
                if requested_version != version_id:
                    raise AssertionError("unexpected source")
                descriptor = os.open(source, os.O_RDONLY)
                return type(
                    "OpenedMedia",
                    (),
                    {
                        "descriptor": descriptor,
                        "audio_stream_indices": (1,),
                        "video_stream_indices": (0,),
                        "close": lambda self: os.close(self.descriptor),
                    },
                )()

        FfmpegPreviewRenderer(LocalMedia()).render(
            edit.project_id, edit, output, threading.Event(), lambda value: None
        )
        probe = json.loads(subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                "-show_entries", "format=duration", "-of", "json", str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout)
        self.assertEqual({stream["codec_type"] for stream in probe["streams"]}, {"video", "audio"})
        self.assertAlmostEqual(float(probe["format"]["duration"]), 2, delta=0.15)


class FakePreviewService:
    def __init__(self, media_path: Path) -> None:
        self.media = media_path
        self.started = None
        self.cancelled = None

    def status(self, project_id):
        return {"render": None, "can_start": project_id.startswith("prj_")}

    def start(self, project_id, request_id, timeline_id):
        self.started = (project_id, request_id, timeline_id)
        return {
            "render": {"render_id": "render_" + "2" * 32, "state": "queued"},
            "can_start": False,
        }

    def cancel(self, project_id, render_id):
        self.cancelled = (project_id, render_id)
        return {"render": {"render_id": render_id, "state": "cancelling"}, "can_start": False}

    def media_path(self, project_id, render_id):
        del project_id, render_id
        return self.media


class PreviewRenderApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        (root / "index.html").write_text("Clipen", encoding="utf-8")
        self.media = root / "preview.mp4"
        self.media.write_bytes(b"0123456789")
        self.service = FakePreviewService(self.media)
        self.app = create_app(
            ControllerConfig(root),
            ProviderStatus("Codex", ProviderState.READY, "AI 已連接"),
            LocalSessionAuthority(),
            preview_renders=self.service,
        )
        _, headers, _ = self.request(
            "POST",
            "/api/session/connect",
            headers={"origin": "http://localhost:4200", "sec-fetch-site": "same-origin"},
        )
        self.cookie = headers["set-cookie"].split(";", 1)[0]
        _, _, body = self.request("GET", "/api/status", headers={"cookie": self.cookie})
        self.csrf = json.loads(body)["session"]["csrf_token"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, method, path, payload=None, headers=None):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        return asyncio.run(request_app(self.app, method, path, headers=headers, body=body))

    @property
    def command_headers(self):
        return {
            "origin": "http://localhost:4200",
            "sec-fetch-site": "same-origin",
            "content-type": "application/json",
            "cookie": self.cookie,
            "x-clipen-csrf": self.csrf,
        }

    def test_status_and_commands_require_local_session_and_csrf(self) -> None:
        project_id = "prj_" + "1" * 32
        base = f"/api/projects/{project_id}/preview"
        self.assertEqual(self.request("GET", base)[0], 401)
        self.assertEqual(self.request("GET", base, headers={"cookie": self.cookie})[0], 200)
        payload = {"request_id": "request-api-preview", "timeline_id": "tl_" + "3" * 32}
        self.assertEqual(self.request("POST", base + "/commands/start", payload, {"cookie": self.cookie})[0], 403)
        started = self.request("POST", base + "/commands/start", payload, self.command_headers)
        self.assertEqual(started[0], 202)
        self.assertEqual(self.service.started, (project_id, payload["request_id"], payload["timeline_id"]))
        render_id = "render_" + "2" * 32
        cancelled = self.request(
            "POST", f"{base}/{render_id}/commands/cancel", {}, self.command_headers
        )
        self.assertEqual(cancelled[0], 200)
        self.assertEqual(self.service.cancelled, (project_id, render_id))

    def test_completed_preview_is_private_and_range_capable(self) -> None:
        project_id = "prj_" + "1" * 32
        render_id = "render_" + "2" * 32
        path = f"/api/projects/{project_id}/preview/{render_id}/media"
        self.assertEqual(self.request("GET", path)[0], 401)
        response = self.request(
            "GET",
            path,
            headers={"cookie": self.cookie, "range": "bytes=2-5"},
        )
        self.assertEqual(response[0], 206)
        self.assertEqual(response[2], b"2345")
        self.assertEqual(response[1]["cache-control"], "no-store")

if __name__ == "__main__":
    unittest.main()
