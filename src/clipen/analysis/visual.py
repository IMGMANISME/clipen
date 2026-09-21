"""Bounded local evidence extraction, using decoded PTS rather than average FPS."""
from __future__ import annotations

from array import array
from contextlib import contextmanager
from fractions import Fraction
import math
import os
from pathlib import Path
import re
import sys
import threading
import wave

from clipen.media import bounded_file_fingerprint
from clipen.media.discovery import open_file_beneath
from .audio import AudioPreparationInput, AudioPreparationError, FFmpegAudioAdapter


@contextmanager
def verified_source(source: AudioPreparationInput):
    descriptor = open_file_beneath(source.root, source.relative_path)
    try:
        def identity():
            info = os.fstat(descriptor)
            return info.st_size, info.st_mtime_ns, bounded_file_fingerprint(descriptor, info.st_size)
        expected = source.size_bytes, source.mtime_ns, source.fingerprint
        if identity() != expected:
            raise AudioPreparationError("source_changed")
        before = os.fstat(descriptor)
        yield descriptor
        if identity() != expected:
            raise AudioPreparationError("source_changed")
        reopened = open_file_beneath(source.root, source.relative_path)
        try:
            current = os.fstat(reopened)
            if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                raise AudioPreparationError("source_changed")
        finally:
            os.close(reopened)
    finally:
        os.close(descriptor)


class LocalEvidenceExtractor(FFmpegAudioAdapter):
    """At most eight 512px frames and a 120-second audio/scene window per stream."""
    MAX_FRAMES = 8
    WINDOW_SECONDS = 120

    def __init__(self, executable: str | None = None):
        super().__init__(executable, timeout_seconds=120, maximum_output_bytes=8 * 1024**2)

    def frame(self, source, offset: Fraction, output: Path, cancel: threading.Event):
        output = self._validate_empty_output(output)
        span = source.source_span
        tb = span.time_base
        if tb.numerator <= 0:
            raise AudioPreparationError("video_time_metadata_missing")
        duration = Fraction(span.duration_pts) * Fraction(tb.numerator, tb.denominator)
        if not 0 <= offset < duration:
            raise AudioPreparationError("frame_time_mapping_invalid")
        absolute_time = Fraction(int(span.start_pts)) * Fraction(tb.numerator, tb.denominator) + offset
        with verified_source(source) as descriptor:
            _, stderr, code = self._run((
                self.executable, "-nostdin", "-hide_banner", "-loglevel", "info",
                "-copyts", "-noaccurate_seek", "-seek_timestamp", "1", "-ss", f"{float(absolute_time):.9f}", "-i", f"/dev/fd/{descriptor}",
                "-map", f"0:{source.stream_index}", "-an", "-frames:v", "1",
                "-vf", f"scale=512:512:force_original_aspect_ratio=decrease,format=rgb24,settb={tb.numerator}/{tb.denominator},select=gte(pts\\,{math.ceil(absolute_time / Fraction(tb.numerator, tb.denominator))}),showinfo",
                "-threads", "1", "-c:v", "png", "-f", "image2", "-y", str(output),
            ), (descriptor,), cancel, observed_output=output)
        matches = re.findall(rb"\bn:\s*0\s+pts:\s*(-?\d+)", stderr)
        if code or len(matches) != 1 or not output.stat().st_size:
            raise AudioPreparationError("frame_decode_failed")
        pts = int(matches[0])
        if not int(span.start_pts) <= pts < int(span.end_pts):
            raise AudioPreparationError("frame_time_mapping_invalid")
        return pts

    def scenes(self, source, cancel: threading.Event):
        span = source.source_span
        tb = span.time_base
        end_pts = min(int(span.end_pts), int(span.start_pts) + math.floor(Fraction(self.WINDOW_SECONDS) / Fraction(tb.numerator, tb.denominator)))
        with verified_source(source) as descriptor:
            _, stderr, code = self._run((
                self.executable, "-nostdin", "-hide_banner", "-loglevel", "info",
                "-copyts", "-t", str(self.WINDOW_SECONDS), "-i", f"/dev/fd/{descriptor}", "-map", f"0:{source.stream_index}",
                "-an", "-vf",
                f"scale=160:-2,settb={tb.numerator}/{tb.denominator},trim=start_pts={span.start_pts}:end_pts={end_pts},select=gt(scene\\,0.35),showinfo",
                "-frames:v", "64", "-f", "null", "-",
            ), (descriptor,), cancel)
        if code:
            raise AudioPreparationError("scene_decode_failed")
        points = sorted(set(int(p) for p in re.findall(rb"\bn:\s*\d+\s+pts:\s*(-?\d+)", stderr)))
        if any(not int(span.start_pts) <= p < int(span.end_pts) for p in points):
            raise AudioPreparationError("frame_time_mapping_invalid")
        return {"candidate_pts": [str(p) for p in points], "threshold": 0.35,
                "maximum_window_seconds": self.WINDOW_SECONDS, "candidate_limit_reached": len(points) >= 64,
                "method": "local_pixel_change_not_semantic_scene"}

    def audio_features(self, source, output: Path, cancel: threading.Event):
        output = self._validate_empty_output(output)
        with verified_source(source) as descriptor:
            _, _, code = self._run((
                self.executable, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-i", f"/dev/fd/{descriptor}", "-map", f"0:{source.stream_index}",
                "-t", str(self.WINDOW_SECONDS), "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", "-f", "wav", "-y", str(output),
            ), (descriptor,), cancel, observed_output=output)
        if code:
            raise AudioPreparationError("audio_decode_failed")
        windows = []
        with wave.open(str(output), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
                raise AudioPreparationError("normalized_audio_invalid")
            start = 0
            while data := audio.readframes(16000):
                if cancel.is_set():
                    raise AudioPreparationError("cancelled")
                values = array("h", data)
                if sys.byteorder != "little":
                    values.byteswap()
                rms = math.sqrt(sum(v*v for v in values) / len(values)) / 32768
                windows.append({"start_sample": start, "end_sample": start + len(values),
                                "rms_dbfs": round(20 * math.log10(max(rms, 1e-6)), 2),
                                "peak": round(max(abs(v) for v in values) / 32768, 6),
                                "low_energy": rms < 0.01})
                start += len(values)
        if not windows:
            raise AudioPreparationError("normalized_audio_invalid")
        return {"sample_rate": 16000, "windows": windows,
                "method": "signal_energy_not_music_or_speech_classification"}
