"""Read-only local music inspection and bounded subprocess execution."""

from __future__ import annotations

from array import array
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from clipen.media.discovery import MediaDiscovery, open_file_beneath
from clipen.media.models import AssetKind, InventoryRoot
from clipen.media.probe import FFprobeAdapter, bounded_file_fingerprint
from clipen.projects import DirectoryReference, ProjectError
from clipen.projects.picker import refresh_availability

from .models import digest


def run_process(command, cancel, *, pass_fds=(), timeout=300, max_bytes=8*1024*1024):
    """Drain through bounded files, poll cancellation even if FFmpeg is silent."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=errors, pass_fds=pass_fds)
        except OSError as error:
            raise ProjectError("finishing_runtime_missing", "找不到可用的 FFmpeg／ffprobe。", status_code=409) from error
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancel.is_set():
                    raise ProjectError("user_cancelled", "已取消製作。", status_code=409)
                if time.monotonic() >= deadline:
                    raise ProjectError("finishing_timeout", "本機製作超過時間上限。", status_code=409)
                if os.fstat(out.fileno()).st_size > max_bytes or os.fstat(errors.fileno()).st_size > max_bytes:
                    raise ProjectError("finishing_output_limit", "本機工具輸出超過上限。", status_code=409)
                cancel.wait(0.05)
            if cancel.is_set():
                raise ProjectError("user_cancelled", "已取消製作。", status_code=409)
            if process.returncode:
                raise ProjectError("finishing_render_failed", "本機製作失敗，請檢查素材格式與磁碟。", status_code=409)
            out.seek(0)
            result = out.read(max_bytes + 1)
            if len(result) > max_bytes:
                raise ProjectError("finishing_output_limit", "本機工具輸出超過上限。", status_code=409)
            return result
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def inventory_root(reference):
    ref = DirectoryReference.from_storage(reference)
    if refresh_availability(ref).availability.value != "available":
        raise ProjectError("finishing_library_offline", "音樂或音效資料夾已變更或離線，請重新選擇。", status_code=409)
    return InventoryRoot(ref.role, ref.canonical_path, ref.device, ref.inode, ref.saved_at)


def inspect_library(role, reference, cancel, progress):
    root = inventory_root(reference)
    probe = FFprobeAdapter()
    assets, issues = [], []
    for entry in MediaDiscovery(maximum_files=1000, maximum_seconds=60).scan(root, cancel):
        if entry.kind != AssetKind.AUDIO or not entry.should_probe:
            continue
        if len(assets) >= 24:
            issues.append({"name": "", "code": "library_limit_24"})
            break
        try:
            result = probe.probe(root, entry, cancel)
            stream = next(s for s in result.streams if s.kind == "audio")
            duration = float(result.summary.get("duration_seconds", 0))
            if not math.isfinite(duration) or not 0.1 <= duration <= 3600:
                raise ValueError("duration unsupported")
            fd = open_file_beneath(root, entry.relative_path)
            try:
                # Inspect at most 90 seconds: enough for a one-minute proof.
                pcm = run_process([
                    "ffmpeg", "-v", "error", "-nostdin", "-i", f"/dev/fd/{fd}",
                    "-map", f"0:{stream.stream_index}", "-t", "90", "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1",
                ], cancel, pass_fds=(fd,), timeout=45, max_bytes=2*1024*1024)
            finally:
                os.close(fd)
            samples = array('h')
            samples.frombytes(pcm)
            if sys.byteorder != "little": samples.byteswap()
            rms = math.sqrt(sum(v*v for v in samples) / max(1, len(samples))) / 32768
            if rms < 0.0001:
                raise ValueError("silent audio")
            beats, bpm = detect_beats(samples)
            asset = {
                "asset_id": "fa_" + digest([role, reference, entry.relative_path, result.fingerprint])[:32],
                "role": role, "name": Path(entry.relative_path).name,
                "relative_path": entry.relative_path, "size_bytes": result.size_bytes,
                "mtime_ns": result.mtime_ns, "fingerprint": result.fingerprint,
                "stream_index": stream.stream_index, "duration_seconds": duration,
                "rms_db": round(20*math.log10(rms), 2), "peak_db": round(20*math.log10(max(abs(v) for v in samples)/32768), 2),
                "beats_seconds": beats, "bpm": bpm, "analyzed_seconds": len(samples)/8000,
            }
            assets.append(asset)
            progress(min(0.95, len(assets)/24))
        except Exception as error:
            if cancel.is_set(): raise
            issues.append({"name": Path(entry.relative_path).name, "code": getattr(error, "code", "audio_unusable")})
    return assets, issues


def detect_beats(samples):
    """Energy-onset autocorrelation, with no fabricated beats for flat audio."""
    hop = 160  # 20 ms, 50 observations per second
    energy = [math.sqrt(sum(v*v for v in samples[i:i+hop])/hop) for i in range(0, len(samples)-hop, hop)]
    onsets = [max(0., energy[i]-energy[i-1]) for i in range(1, len(energy))]
    if not onsets or max(onsets) < 30:
        return [], None
    scores = [(sum(onsets[i]*onsets[i-lag] for i in range(lag, len(onsets))), lag) for lag in range(17, 51)]
    score, lag = max(scores)
    if score <= 0: return [], None
    # Return observed onset peaks near the periodic pulse; do not invent timestamps.
    phase = max(range(lag), key=lambda p: sum(onsets[p::lag]))
    beats = []
    for index in range(phase, len(onsets), lag):
        low, high = max(0,index-3), min(len(onsets),index+4)
        peak = max(range(low,high), key=lambda i:onsets[i])
        if onsets[peak] > max(onsets)*0.08:
            value = round((peak+1)*hop/8000, 3)
            if not beats or value-beats[-1] > .20: beats.append(value)
    return beats[:300], round(3000/lag, 1)


def open_asset(context, asset_id):
    asset = next((a for a in context["assets"] if a["asset_id"] == asset_id), None)
    if asset is None:
        raise ProjectError("finishing_asset_missing", "找不到指定的音樂或音效。", status_code=409)
    fd = open_file_beneath(inventory_root(context["directories"][asset["role"]]), asset["relative_path"])
    try:
        info = os.fstat(fd)
        if info.st_size != asset["size_bytes"] or info.st_mtime_ns != asset["mtime_ns"] or bounded_file_fingerprint(fd, info.st_size) != asset["fingerprint"]:
            raise ProjectError("media_changed", "音樂或音效已變更，請重新檢查。", status_code=409)
        return fd, asset
    except Exception:
        os.close(fd)
        raise
