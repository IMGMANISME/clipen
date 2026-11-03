import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
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
from clipen.rendering import (
    FfmpegPreviewRenderer,
    PreviewRenderError,
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

    def test_hardware_failure_retries_same_timeline_with_software(self):
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)/'preview.mp4';commands=[];media=FakeMedia()
            def popen(command,**options):
                commands.append(command);process=FakeProcess()
                if 'h264_videotoolbox' in command:
                    process.wait=lambda:1
                else:output.write_bytes(b'mp4')
                return process
            edit=timeline('prj_'+'e'*32)
            FfmpegPreviewRenderer(media,popen=popen,hardware_acceleration=True).render(edit.project_id,edit,output,threading.Event(),lambda value:None)
            self.assertEqual(len(commands),2)
            self.assertIn('videotoolbox',commands[0]);self.assertIn('h264_videotoolbox',commands[0])
            self.assertNotIn('videotoolbox',commands[1]);self.assertIn('libx264',commands[1])
            self.assertEqual(commands[0][commands[0].index('-filter_complex')+1],commands[1][commands[1].index('-filter_complex')+1])
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
        self.assertNotIn("eq=contrast=1.025:saturation=1.045", filters)
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


class FinishedFilmApiTests(unittest.TestCase):
    def test_finished_film_is_private_and_range_capable(self) -> None:
        from clipen.controller import ControllerConfig, LocalSessionAuthority, ProviderState, ProviderStatus, create_app
        from clipen.projects import ProjectWorkspace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_text("Clipen", encoding="utf-8")
            media = root / "film.mp4"
            media.write_bytes(b"0123456789")
            app = create_app(ControllerConfig(root), ProviderStatus("Codex", ProviderState.READY, "AI 已連接"),
                             LocalSessionAuthority(), ProjectWorkspace(root / "projects"))

            def request(method, path, headers=None):
                return asyncio.run(request_app(app, method, path, headers=headers))
            _, headers, _ = request("POST", "/api/session/connect", {"origin": "http://localhost:4200", "sec-fetch-site": "same-origin"})
            cookie = headers["set-cookie"].split(";", 1)[0]
            path = f"/api/projects/{'prj_' + '1' * 32}/preview/{'render_' + '2' * 32}/media"
            self.assertEqual(request("GET", path)[0], 401)
            with patch("clipen.rendering.PreviewRenderRepository.media_path", return_value=media) as media_path:
                response = request("GET", path, {"cookie": cookie, "range": "bytes=2-5"})
            self.assertEqual(media_path.call_args.args, ("prj_" + "1" * 32, "render_" + "2" * 32))
            self.assertEqual(response[0], 206)
            self.assertEqual(response[2], b"2345")
            self.assertIn("no-store", response[1]["cache-control"])


if __name__ == "__main__":
    unittest.main()
