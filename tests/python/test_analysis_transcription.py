import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
import wave

from clipen.analysis import (
    LocalModelSnapshot,
    MLXWhisperAdapter,
    PreparedAudio,
    TranscriptionError,
    hash_model_directory,
)


class TranscriptionAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.model = self.root / "model"
        self.model.mkdir()
        (self.model / "config.json").write_text("{}", encoding="utf-8")
        (self.model / "weights.npz").write_bytes(b"weights")
        self.snapshot = LocalModelSnapshot(
            "mlx-whisper-small",
            "test-revision",
            self.model,
            hash_model_directory(self.model),
        )
        self.audio_path = self.root / "audio.wav"
        with wave.open(str(self.audio_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x00\x00" * 160_000)
        self.audio = PreparedAudio(self.audio_path, 16000, 1, 2, 160_000)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def fake_runtime(self, payload: object, *, sleep: bool = False) -> Path:
        executable = self.root / f"runtime-{int(sleep)}"
        encoded = json.dumps(payload, ensure_ascii=False)
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, time\n"
            "if '--probe' in sys.argv:\n"
            "    print(json.dumps({'mlx_whisper': 'test-1.0'}))\n"
            "    raise SystemExit(0)\n"
            + ("time.sleep(10)\n" if sleep else "")
            + f"payload = {encoded!r}\n"
            "output = sys.argv[sys.argv.index('--output') + 1]\n"
            "with open(output, 'w', encoding='utf-8') as target:\n"
            "    target.write(payload)\n",
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return executable

    def valid_payload(self) -> dict[str, object]:
        return {
            "language": "zh",
            "text": " 手沖咖啡",
            "segments": [
                {
                    "start": 0.1,
                    "end": 1.5,
                    "text": " 手沖咖啡",
                    "no_speech_probability": 0.02,
                    "words": [
                        {
                            "start": 0.1,
                            "end": 0.5,
                            "text": " 手沖",
                            "probability": 0.9,
                        },
                        {
                            "start": 0.5,
                            "end": 1.5,
                            "text": "咖啡",
                            "probability": 0.8,
                        },
                    ],
                }
            ],
        }

    def test_probe_and_transcribe_validate_timestamped_output(self) -> None:
        adapter = MLXWhisperAdapter(
            self.fake_runtime(self.valid_payload()),
            self.snapshot,
            timeout_seconds=2,
        )
        output = self.root / "result.json"
        output.touch(mode=0o600)

        self.assertEqual(adapter.probe(), "test-1.0")
        result = adapter.transcribe(self.audio, output)

        self.assertEqual(result.language, "zh")
        self.assertEqual(result.segments[0].start_sample, 1600)
        self.assertEqual(result.segments[0].end_sample, 24000)
        self.assertEqual(result.segments[0].words[1].text, "咖啡")
        self.assertFalse(result.no_speech_detected)

    def test_runtime_entry_keeps_its_virtual_environment_path(self) -> None:
        target = self.fake_runtime(self.valid_payload())
        linked = self.root / "venv-python"
        linked.symlink_to(target)
        adapter = MLXWhisperAdapter(linked, self.snapshot, timeout_seconds=2)

        self.assertEqual(adapter.python_executable, linked.absolute())
        self.assertEqual(adapter.probe(), "test-1.0")

    def test_empty_transcription_is_distinct_no_speech_result(self) -> None:
        adapter = MLXWhisperAdapter(
            self.fake_runtime({"language": "zh", "text": "", "segments": []}),
            self.snapshot,
            timeout_seconds=2,
        )
        output = self.root / "result.json"
        output.touch(mode=0o600)
        self.assertTrue(adapter.transcribe(self.audio, output).no_speech_detected)

    def test_rejects_timestamp_outside_audio_and_removes_result(self) -> None:
        payload = self.valid_payload()
        payload["segments"][0]["end"] = 20
        adapter = MLXWhisperAdapter(
            self.fake_runtime(payload), self.snapshot, timeout_seconds=2
        )
        output = self.root / "result.json"
        output.touch(mode=0o600)
        with self.assertRaises(TranscriptionError) as context:
            adapter.transcribe(self.audio, output)
        self.assertEqual(context.exception.code, "asr_result_invalid")
        self.assertFalse(output.exists())

    def test_model_change_is_detected_before_runtime_executes(self) -> None:
        runtime = self.fake_runtime(self.valid_payload())
        (self.model / "weights.npz").write_bytes(b"changed")
        adapter = MLXWhisperAdapter(runtime, self.snapshot, timeout_seconds=2)
        output = self.root / "result.json"
        output.touch(mode=0o600)
        with self.assertRaises(TranscriptionError) as context:
            adapter.transcribe(self.audio, output)
        self.assertEqual(context.exception.code, "asr_model_changed")

    def test_model_change_after_probe_invalidates_cached_verification(self) -> None:
        adapter = MLXWhisperAdapter(
            self.fake_runtime(self.valid_payload()), self.snapshot, timeout_seconds=2
        )
        self.assertEqual(adapter.probe(), "test-1.0")
        (self.model / "weights.npz").write_bytes(b"changed after probe")
        output = self.root / "result.json"
        output.touch(mode=0o600)

        with self.assertRaises(TranscriptionError) as context:
            adapter.transcribe(self.audio, output)

        self.assertEqual(context.exception.code, "asr_model_changed")

    def test_cancellation_terminates_worker_and_removes_result(self) -> None:
        cancel = threading.Event()
        cancel.set()
        adapter = MLXWhisperAdapter(
            self.fake_runtime(self.valid_payload(), sleep=True),
            self.snapshot,
            timeout_seconds=20,
        )
        output = self.root / "result.json"
        output.touch(mode=0o600)
        with self.assertRaises(TranscriptionError) as context:
            adapter.transcribe(self.audio, output, cancel=cancel)
        self.assertEqual(context.exception.code, "cancelled")
        self.assertFalse(output.exists())

    def test_model_manifest_rejects_symlinked_files(self) -> None:
        target = self.root / "outside"
        target.write_bytes(b"outside")
        (self.model / "unsafe").symlink_to(target)
        with self.assertRaises(TranscriptionError) as context:
            hash_model_directory(self.model)
        self.assertEqual(context.exception.code, "asr_model_unsafe")


if __name__ == "__main__":
    unittest.main()
