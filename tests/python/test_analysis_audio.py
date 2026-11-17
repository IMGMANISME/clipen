import hashlib
import os
import stat
import tempfile
import threading
import unittest
import wave
from pathlib import Path

from clipen.analysis import (
    AudioPreparationError,
    AudioPreparationInput,
    FFmpegAudioAdapter,
    PCMChunker,
    PreparedAudio,
    Rational,
    SourceSpan,
)
from clipen.media import InventoryRoot, bounded_file_fingerprint
from clipen.projects.models import DirectoryRole


class AnalysisAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.source = self.media / "clip.mov"
        self.source.write_bytes(b"source remains unchanged")
        root_stat = self.media.stat()
        self.inventory_root = InventoryRoot(
            DirectoryRole.SOURCE,
            str(self.media),
            root_stat.st_dev,
            root_stat.st_ino,
            "2026-09-02T00:00:00+00:00",
        )
        source_stat = self.source.stat()
        descriptor = os.open(self.source, os.O_RDONLY)
        try:
            fingerprint = bounded_file_fingerprint(descriptor, source_stat.st_size)
        finally:
            os.close(descriptor)
        self.span = SourceSpan(
            "av_" + "c" * 32,
            1,
            "48000",
            "336000",
            Rational(1, 48000),
        )
        self.input = AudioPreparationInput(
            self.inventory_root,
            "clip.mov",
            self.span.version_id,
            1,
            source_stat.st_size,
            source_stat.st_mtime_ns,
            fingerprint,
            self.span,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def fake_ffmpeg(self, *, seconds: int = 6, sleep: bool = False) -> Path:
        script = self.root / f"fake-ffmpeg-{seconds}-{int(sleep)}"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, time, wave\n"
            "if '-version' in sys.argv:\n"
            "    print('ffmpeg version test')\n"
            "    raise SystemExit(0)\n"
            + ("time.sleep(10)\n" if sleep else "")
            + "with wave.open(sys.argv[-1], 'wb') as output:\n"
            "    output.setnchannels(1)\n"
            "    output.setsampwidth(2)\n"
            "    output.setframerate(16000)\n"
            f"    output.writeframes(b'\\x00\\x00' * (16000 * {seconds}))\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script

    def test_ffmpeg_extracts_validated_audio_without_modifying_source(self) -> None:
        original = hashlib.sha256(self.source.read_bytes()).hexdigest()
        output = self.root / "normalized.wav"
        output.touch(mode=0o600)
        adapter = FFmpegAudioAdapter(str(self.fake_ffmpeg()), timeout_seconds=20)

        self.assertEqual(adapter.version(), "ffmpeg version test")
        prepared = adapter.extract(self.input, output)

        self.assertEqual(prepared.frame_count, 96_000)
        self.assertEqual(prepared.duration_seconds, 6)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original)

    def test_extract_rejects_changed_input_and_nonempty_output(self) -> None:
        adapter = FFmpegAudioAdapter(str(self.fake_ffmpeg()), timeout_seconds=20)
        self.source.write_bytes(b"changed")
        output = self.root / "normalized.wav"
        output.touch(mode=0o600)
        with self.assertRaises(AudioPreparationError) as changed:
            adapter.extract(self.input, output)
        self.assertEqual(changed.exception.code, "source_changed")

        output.write_bytes(b"occupied")
        with self.assertRaises(AudioPreparationError) as unsafe:
            adapter.extract(self.input, output)
        self.assertEqual(unsafe.exception.code, "audio_output_unsafe")

    def test_extract_cancels_the_process_and_removes_partial_output(self) -> None:
        output = self.root / "normalized.wav"
        output.touch(mode=0o600)
        cancel = threading.Event()
        cancel.set()
        adapter = FFmpegAudioAdapter(str(self.fake_ffmpeg(sleep=True)), timeout_seconds=20)
        with self.assertRaises(AudioPreparationError) as context:
            adapter.extract(self.input, output, cancel)
        self.assertEqual(context.exception.code, "cancelled")
        self.assertFalse(output.exists())

    def test_chunker_preserves_overlap_and_projects_source_pts(self) -> None:
        normalized = self.root / "normalized.wav"
        with wave.open(str(normalized), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x00\x00" * 96_000)
        prepared = PreparedAudio(normalized, 16000, 1, 2, 96_000)
        outputs: list[Path] = []

        def output_factory() -> Path:
            path = self.root / f"chunk-{len(outputs)}.wav"
            path.touch(mode=0o600)
            outputs.append(path)
            return path

        chunks = PCMChunker(chunk_seconds=10, overlap_seconds=1).split(
            prepared,
            self.span,
            output_factory,
        )

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].source_span, self.span)
        with wave.open(str(chunks[0].path), "rb") as chunk:
            self.assertEqual(chunk.getnframes(), 96_000)

    def test_chunker_splits_long_audio_with_exact_source_mapping(self) -> None:
        normalized = self.root / "long.wav"
        frames = 16_000 * 21
        with wave.open(str(normalized), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x00\x00" * frames)
        prepared = PreparedAudio(normalized, 16000, 1, 2, frames)
        long_span = SourceSpan(
            self.span.version_id,
            1,
            "48000",
            str(48000 + 21 * 48000),
            Rational(1, 48000),
        )
        index = 0

        def output_factory() -> Path:
            nonlocal index
            path = self.root / f"long-chunk-{index}.wav"
            index += 1
            path.touch(mode=0o600)
            return path

        chunks = PCMChunker(chunk_seconds=10, overlap_seconds=2).split(
            prepared,
            long_span,
            output_factory,
        )

        self.assertEqual(
            [(item.start_sample, item.end_sample) for item in chunks],
            [
                (0, 160_000),
                (128_000, 288_000),
                (256_000, 336_000),
            ],
        )
        self.assertEqual(
            [(item.source_span.start_pts, item.source_span.end_pts) for item in chunks],
            [("48000", "528000"), ("432000", "912000"), ("816000", "1056000")],
        )


if __name__ == "__main__":
    unittest.main()
