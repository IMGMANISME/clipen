from dataclasses import replace
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest

from clipen.analysis.audio import AudioPreparationError, AudioPreparationInput
from clipen.analysis.models import Rational, SourceSpan
from clipen.analysis.visual import LocalEvidenceExtractor
from clipen.media import InventoryRoot, bounded_file_fingerprint
from clipen.projects.models import DirectoryRole


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires local FFmpeg')
class EvidenceExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.mov'
        subprocess.run([shutil.which('ffmpeg'), '-nostdin', '-loglevel', 'error',
                        '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=10:duration=2',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2',
                        '-c:v', 'mpeg4', '-c:a', 'pcm_s16le', '-output_ts_offset', '5', str(self.source)],
                       check=True, capture_output=True, timeout=20)
        probe = json.loads(subprocess.run([shutil.which('ffprobe'), '-v', 'error', '-show_streams', '-of', 'json', str(self.source)],
                                          check=True, capture_output=True, timeout=20).stdout)
        self.streams = probe['streams']
        identity = self.root.stat()
        self.inventory_root = InventoryRoot(DirectoryRole.SOURCE, str(self.root), identity.st_dev, identity.st_ino, 'test')
        fd = os.open(self.source, os.O_RDONLY)
        try:
            self.fingerprint = bounded_file_fingerprint(fd, self.source.stat().st_size)
        finally:
            os.close(fd)
        self.extractor = LocalEvidenceExtractor()

    def tearDown(self):
        self.temp.cleanup()

    def source_input(self, kind):
        stream = next(s for s in self.streams if s['codec_type'] == kind)
        num, den = map(int, stream['time_base'].split('/'))
        span = SourceSpan('av_' + 'a'*32, stream['index'], str(stream['start_pts']),
                          str(int(stream['start_pts'])+int(stream['duration_ts'])), Rational(num, den))
        info = self.source.stat()
        return AudioPreparationInput(self.inventory_root, self.source.name, span.version_id, span.stream_index,
                                     info.st_size, info.st_mtime_ns, self.fingerprint, span)

    def test_real_frames_preserve_nonzero_source_pts_and_original(self):
        original = hashlib.sha256(self.source.read_bytes()).hexdigest()
        source = self.source_input('video')
        pts = []
        for i in range(8):
            output = self.root / f'frame-{i}.png'
            output.touch(mode=0o600)
            pts.append(self.extractor.frame(source, Fraction(i, 4), output, threading.Event()))
            data = output.read_bytes()
            self.assertTrue(data.startswith(b'\x89PNG'))
            width, height, bit_depth, color_type = struct.unpack('!2I2B', data[16:26])
            self.assertLessEqual(max(width, height), 512)
            self.assertEqual((bit_depth, color_type), (8, 2))
            self.assertLess(len(data), 1024**2)
        self.assertEqual(pts, sorted(set(pts)))
        self.assertGreaterEqual(pts[0], int(source.source_span.start_pts))
        self.assertGreater(pts[0], 0)
        self.assertLess(pts[-1], int(source.source_span.end_pts))
        scene = self.extractor.keyframe_candidates(source, threading.Event())
        self.assertLessEqual(len(scene['candidate_pts']), 64)
        self.assertEqual(
            scene['candidate_pts'], [candidate['pts'] for candidate in scene['candidates']]
        )
        self.assertTrue(
            all(0 <= candidate['score'] <= 1 for candidate in scene['candidates'])
        )
        self.assertEqual(scene['method'], 'sparse_codec_keyframe_packet_complexity_v2')
        self.assertFalse(scene['decoded_frames'])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original)

    def test_real_audio_energy_and_coverage_window(self):
        output = self.root / 'audio.wav'
        output.touch(mode=0o600)
        features = self.extractor.audio_features(self.source_input('audio'), output, threading.Event())
        self.assertEqual(len(features['windows']), 2)
        self.assertGreater(features['windows'][0]['rms_dbfs'], -40)
        self.assertFalse(features['windows'][0]['low_energy'])

    def test_changed_source_and_unsafe_output_are_rejected(self):
        source = self.source_input('video')
        output = self.root / 'frame.png'
        output.touch()
        with self.assertRaises(AudioPreparationError):
            self.extractor.frame(replace(source, fingerprint='0'*64), Fraction(0), output, threading.Event())
        output.write_bytes(b'keep this')
        with self.assertRaises(AudioPreparationError):
            self.extractor.frame(source, Fraction(0), output, threading.Event())
        self.assertEqual(output.read_bytes(), b'keep this')

    def test_packet_candidates_use_sparse_anchors_across_the_timeline(self):
        long_clip = self.root / 'long.mov'
        subprocess.run([shutil.which('ffmpeg'), '-nostdin', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'color=red:size=64x64:rate=1:duration=121',
            '-f', 'lavfi', '-i', 'color=blue:size=64x64:rate=1:duration=2',
            '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]', '-map', '[v]',
            '-c:v', 'mpeg4', str(long_clip)], check=True, capture_output=True, timeout=20)
        self.source = long_clip
        self.streams = json.loads(subprocess.run([shutil.which('ffprobe'), '-v', 'error', '-show_streams', '-of', 'json', str(long_clip)],
            check=True, capture_output=True, timeout=20).stdout)['streams']
        fd = os.open(long_clip, os.O_RDONLY)
        try:
            self.fingerprint = bounded_file_fingerprint(fd, long_clip.stat().st_size)
        finally:
            os.close(fd)
        result = self.extractor.keyframe_candidates(self.source_input('video'), threading.Event())
        source = self.source_input('video').source_span
        cutoff = int(source.start_pts) + int(
            Fraction(120) * source.time_base.denominator / source.time_base.numerator
        )
        self.assertTrue(result['candidate_pts'])
        self.assertTrue(any(int(point) >= cutoff for point in result['candidate_pts']))
        self.assertLessEqual(len(result['candidates']), 256)
        self.assertEqual(result['probe_count'], 32)

    def test_vfr_samples_use_actual_decoded_timestamps(self):
        vfr = self.root / 'vfr.mov'
        subprocess.run([shutil.which('ffmpeg'), '-nostdin', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'testsrc2=size=96x64:rate=10:duration=3',
            '-vf', 'select=eq(mod(n\\,3)\\,0)+eq(n\\,7)', '-fps_mode', 'vfr',
            '-c:v', 'mpeg4', str(vfr)], check=True, capture_output=True, timeout=20)
        self.source = vfr
        probe = json.loads(subprocess.run([shutil.which('ffprobe'), '-v', 'error', '-show_streams', '-show_frames', '-of', 'json', str(vfr)],
            check=True, capture_output=True, timeout=20).stdout)
        self.streams = probe['streams']
        fd = os.open(vfr, os.O_RDONLY)
        try:
            self.fingerprint = bounded_file_fingerprint(fd, vfr.stat().st_size)
        finally:
            os.close(fd)
        actual = {int(frame['pts']) for frame in probe['frames']}
        source = self.source_input('video')
        output = self.root / 'vfr.png'
        output.touch()
        pts = self.extractor.frame(source, Fraction(65, 100), output, threading.Event())
        self.assertIn(pts, actual)
        self.assertEqual(Fraction(pts) * Fraction(source.source_span.time_base.numerator, source.source_span.time_base.denominator), Fraction(7, 10))



if __name__ == '__main__':
    unittest.main()
