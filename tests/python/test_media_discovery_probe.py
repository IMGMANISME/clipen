import errno
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from clipen.media import (
    AssetKind,
    DiscoveredAsset,
    DiscoveryError,
    FFprobeAdapter,
    InventoryRoot,
    MediaDiscovery,
    MediaProbeError,
)
from clipen.projects import DirectoryRole


def root_snapshot(path: Path) -> InventoryRoot:
    identity = path.stat()
    return InventoryRoot(
        DirectoryRole.SOURCE,
        str(path.resolve()),
        identity.st_dev,
        identity.st_ino,
        "2026-09-01T00:00:00+00:00",
    )


def tree_state(root: Path) -> tuple[tuple[str, int, int, bytes | None], ...]:
    result = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        value = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        result.append((relative, info.st_size, info.st_mtime_ns, value))
    return tuple(result)


class MediaDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "source"
        (self.root / "camera").mkdir(parents=True)
        (self.root / "camera" / "A001.MOV").write_bytes(b"video")
        (self.root / "voice.wav").write_bytes(b"audio")
        (self.root / "notes.txt").write_text("notes", encoding="utf-8")
        (self.root / ".DS_Store").write_bytes(b"metadata")
        (self.root / ".hidden").mkdir()
        (self.root / ".hidden" / "private.mov").write_bytes(b"hidden")
        self.outside = Path(self.temporary.name) / "outside"
        self.outside.mkdir()
        (self.outside / "escape.mp4").write_bytes(b"outside")
        (self.root / "linked").symlink_to(self.outside, target_is_directory=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_discovers_recognized_files_and_never_follows_links(self) -> None:
        before_source = tree_state(self.root)
        before_outside = tree_state(self.outside)
        items = tuple(MediaDiscovery().scan(root_snapshot(self.root)))
        by_path = {item.relative_path: item for item in items}

        self.assertEqual(by_path["camera/A001.MOV"].kind, AssetKind.VIDEO)
        self.assertEqual(by_path["voice.wav"].kind, AssetKind.AUDIO)
        self.assertEqual(by_path["notes.txt"].issue_code, "non_media")
        self.assertEqual(by_path["linked"].issue_code, "symlink_ignored")
        self.assertNotIn("linked/escape.mp4", by_path)
        self.assertNotIn(".DS_Store", by_path)
        self.assertNotIn(".hidden/private.mov", by_path)
        self.assertEqual(tree_state(self.root), before_source)
        self.assertEqual(tree_state(self.outside), before_outside)

    def test_root_identity_and_file_count_bounds_fail_closed(self) -> None:
        actual = root_snapshot(self.root)
        changed = InventoryRoot(
            actual.role,
            actual.canonical_path,
            actual.device,
            actual.inode + 1,
            actual.saved_at,
        )
        with self.assertRaises(DiscoveryError) as identity:
            tuple(MediaDiscovery().scan(changed))
        self.assertEqual(identity.exception.code, "root_changed")

        with self.assertRaises(DiscoveryError) as count:
            tuple(MediaDiscovery(maximum_files=1).scan(actual))
        self.assertEqual(count.exception.code, "too_many_files")

    def test_wide_tree_keeps_open_descriptors_bounded(self) -> None:
        wide = Path(self.temporary.name) / "wide"
        wide.mkdir()
        for index in range(80):
            child = wide / f"camera-{index:03d}"
            child.mkdir()
            (child / "clip.mov").write_bytes(b"media")

        actual_open = os.open
        actual_close = os.close
        tracked: set[int] = set()
        peak = 0

        def open_tracked(*args, **kwargs):
            nonlocal peak
            descriptor = actual_open(*args, **kwargs)
            tracked.add(descriptor)
            peak = max(peak, len(tracked))
            return descriptor

        def close_tracked(descriptor: int) -> None:
            tracked.discard(descriptor)
            actual_close(descriptor)

        with (
            mock.patch("clipen.media.discovery.os.open", side_effect=open_tracked),
            mock.patch("clipen.media.discovery.os.close", side_effect=close_tracked),
        ):
            items = tuple(MediaDiscovery().scan(root_snapshot(wide)))

        self.assertEqual(len(items), 80)
        self.assertLessEqual(peak, 2)
        self.assertEqual(tracked, set())


class FFprobeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        self.media = self.root / "clip.mov"
        self.media.write_bytes(b"synthetic media bytes")
        info = self.media.stat()
        self.asset = DiscoveredAsset(
            DirectoryRole.SOURCE,
            "clip.mov",
            AssetKind.VIDEO,
            info.st_size,
            info.st_mtime_ns,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def script(self, body: str) -> str:
        path = Path(self.temporary.name) / f"fake-ffprobe-{time.monotonic_ns()}"
        path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
        path.chmod(0o700)
        return str(path)

    def successful_script(self) -> str:
        return self.script(
            """
if [ "${1:-}" = "-version" ]; then
  echo 'ffprobe version synthetic-1'
  exit 0
fi
cat <<'JSON'
{"streams":[{"index":0,"codec_name":"h264","codec_type":"video","width":1920,"height":1080,"time_base":"1/90000","start_pts":0,"duration_ts":180000,"avg_frame_rate":"30000/1001","r_frame_rate":"30000/1001","tags":{"rotate":"-90"}},{"index":1,"codec_name":"aac","codec_type":"audio","sample_rate":"48000","channels":2,"time_base":"1/48000","start_pts":0,"duration_ts":96000,"avg_frame_rate":"0/0","r_frame_rate":"0/0"}],"format":{"filename":"must-not-leak","format_name":"mov,mp4,m4a,3gp,3g2,mj2","start_time":"0.000000","duration":"2.000000","bit_rate":"1000000","tags":{"private":"ignore"}}}
JSON
"""
        )

    def test_parses_only_bounded_safe_media_metadata(self) -> None:
        adapter = FFprobeAdapter(self.successful_script())
        self.assertEqual(adapter.version(), "ffprobe version synthetic-1")
        result = adapter.probe(root_snapshot(self.root), self.asset)

        self.assertEqual(result.kind, AssetKind.VIDEO)
        self.assertEqual(result.summary["duration_seconds"], "2")
        self.assertEqual(result.streams[0].details["rotation"], 270)
        self.assertEqual(result.streams[1].details["sample_rate"], 48000)
        self.assertNotIn("filename", repr(result.summary))
        self.assertNotIn(str(self.root), repr(result))
        self.assertRegex(result.fingerprint, r"^[0-9a-f]{64}$")

    def test_rejects_invalid_or_oversized_probe_output(self) -> None:
        invalid = FFprobeAdapter(self.script("echo 'not-json'"))
        with self.assertRaises(MediaProbeError) as invalid_error:
            invalid.probe(root_snapshot(self.root), self.asset)
        self.assertEqual(invalid_error.exception.code, "invalid_probe_output")

        oversized = FFprobeAdapter(
            self.script("dd if=/dev/zero bs=1048576 count=5 2>/dev/null"),
            timeout_seconds=5,
        )
        with self.assertRaises(MediaProbeError) as oversized_error:
            oversized.probe(root_snapshot(self.root), self.asset)
        self.assertEqual(oversized_error.exception.code, "probe_output_too_large")

        diagnostic = FFprobeAdapter(
            self.script("echo 'private source diagnostic' >&2\nexit 1")
        )
        with self.assertRaises(MediaProbeError) as diagnostic_error:
            diagnostic.probe(root_snapshot(self.root), self.asset)
        self.assertEqual(diagnostic_error.exception.code, "unreadable")
        self.assertNotIn("private source diagnostic", repr(diagnostic_error.exception))
        self.assertNotIn(str(self.root), repr(diagnostic_error.exception))

    def test_timeout_terminates_the_complete_process_group(self) -> None:
        pid_file = Path("/private/tmp") / f"clipen-ffprobe-child-{os.getpid()}-{time.monotonic_ns()}.pid"
        script = self.script(
            f"/bin/sleep 30 &\necho $! > '{pid_file}'\nwait\n"
        )
        try:
            adapter = FFprobeAdapter(script, timeout_seconds=0.5)
            with self.assertRaises(MediaProbeError) as timeout:
                adapter.probe(root_snapshot(self.root), self.asset)
            self.assertEqual(timeout.exception.code, "timeout")
            child_pid = int(pid_file.read_text(encoding="ascii"))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except OSError as error:
                    if error.errno == errno.ESRCH:
                        break
                time.sleep(0.02)
            else:
                self.fail("ffprobe child process was still alive after timeout")
        finally:
            pid_file.unlink(missing_ok=True)

    def test_cancel_terminates_the_complete_process_group(self) -> None:
        pid_file = (
            Path("/private/tmp")
            / f"clipen-ffprobe-cancel-child-{os.getpid()}-{time.monotonic_ns()}.pid"
        )
        script = self.script(f"/bin/sleep 30 &\necho $! > '{pid_file}'\nwait\n")
        cancelled = threading.Event()

        def cancel_after_start() -> None:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not pid_file.exists():
                time.sleep(0.01)
            cancelled.set()

        canceller = threading.Thread(target=cancel_after_start)
        try:
            canceller.start()
            with self.assertRaises(MediaProbeError) as cancellation:
                FFprobeAdapter(script, timeout_seconds=5).probe(
                    root_snapshot(self.root), self.asset, cancelled
                )
            self.assertEqual(cancellation.exception.code, "cancelled")
            child_pid = int(pid_file.read_text(encoding="ascii"))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except OSError as error:
                    if error.errno == errno.ESRCH:
                        break
                time.sleep(0.02)
            else:
                self.fail("ffprobe child process was still alive after cancellation")
        finally:
            canceller.join(2)
            pid_file.unlink(missing_ok=True)

    def test_rejects_symlink_swapped_after_discovery(self) -> None:
        self.media.unlink()
        self.media.symlink_to(self.outside_file())
        with self.assertRaises(MediaProbeError) as error:
            FFprobeAdapter(self.successful_script()).probe(
                root_snapshot(self.root), self.asset
            )
        self.assertIn(error.exception.code, {"entry_unreadable", "entry_not_regular"})

    def outside_file(self) -> Path:
        path = Path(self.temporary.name) / "outside.mov"
        path.write_bytes(b"outside")
        return path


if __name__ == "__main__":
    unittest.main()
