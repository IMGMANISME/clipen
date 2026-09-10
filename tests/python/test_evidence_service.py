import asyncio
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
import zlib

import test_analysis_storage as preparation_fixtures
from clipen.analysis.evidence_service import EvidenceService
from clipen.media import bounded_file_fingerprint
from clipen.projects import ProjectError


def png_chunk(kind, data):
    return (
        struct.pack("!I", len(data))
        + kind
        + data
        + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


PNG = (
    b"\x89PNG\r\n\x1a\n"
    + png_chunk(b"IHDR", struct.pack("!2I5B", 32, 32, 8, 2, 0, 0, 0))
    + png_chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\0\0" * 32) * 32))
    + png_chunk(b"IEND", b"")
)


class FakeExtractor:
    def version(self):
        return "ffmpeg test"

    def frame(self, source, offset, output, cancel):
        output.write_bytes(PNG)
        span = source.source_span
        return int(span.start_pts) + int(
            offset * span.time_base.denominator / span.time_base.numerator
        )

    def scenes(self, source, cancel):
        return {
            "candidate_pts": [],
            "maximum_window_seconds": 120,
            "candidate_limit_reached": False,
        }

    def audio_features(self, source, output, cancel):
        return {
            "sample_rate": 16000,
            "windows": [
                {
                    "start_sample": 0,
                    "end_sample": 16000,
                    "rms_dbfs": -120,
                    "peak": 0,
                    "low_energy": True,
                }
            ],
        }


class EvidenceServiceTests(unittest.TestCase):
    def setUp(self):
        preparation_fixtures.PreparationStorageTests.setUp(self)
        with self.projects.connect(self.project.project_id) as database:
            rows = database.execute(
                "SELECT a.relative_path,v.version_id FROM assets a "
                "JOIN asset_versions v ON a.asset_id=v.asset_id"
            ).fetchall()
            for row in rows:
                path = self.source / row["relative_path"]
                descriptor = os.open(path, os.O_RDONLY)
                try:
                    fingerprint = bounded_file_fingerprint(
                        descriptor, path.stat().st_size
                    )
                finally:
                    os.close(descriptor)
                database.execute(
                    "UPDATE asset_versions SET fingerprint=? WHERE version_id=?",
                    (fingerprint, row["version_id"]),
                )
        self.service = EvidenceService(
            self.projects, extractor_factory=FakeExtractor
        )
        self.project_id = self.project.project_id

    def tearDown(self):
        self.service.close()
        self.temporary.cleanup()

    def start(self, request_id="evidence-request-1"):
        scope = self.service.scope(self.project_id)
        return self.service.start(
            self.project_id,
            request_id,
            scope["project_revision"],
            scope["scope_id"],
        )

    def wait(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = self.service.status(self.project_id)
            latest = status["latest"]
            if latest and latest["state"] != "running":
                return status
            time.sleep(0.01)
        self.fail("evidence did not settle")

    def test_local_results_are_durable_source_bound_and_private(self):
        original = hashlib.sha256(self.clip.read_bytes()).hexdigest()
        self.start()
        status = self.wait()
        run = status["latest"]
        self.assertEqual(run["state"], "succeeded")
        self.assertEqual(len(run["results"]), 3)
        video = next(result for result in run["results"] if result["kind"] == "video")
        self.assertEqual(len(video["frames"]), 8)
        self.assertFalse(video["coverage"]["complete"])
        audio = next(result for result in run["results"] if result["kind"] == "audio")
        self.assertEqual(
            audio["coverage"]["inspected"],
            [{"start_pts": "48000", "end_pts": "96000"}],
        )
        self.assertNotIn(str(self.source), json.dumps(status))
        self.assertEqual(hashlib.sha256(self.clip.read_bytes()).hexdigest(), original)
        image = self.service.image(
            self.project_id, run["run_id"], video["frames"][0]["artifact_id"]
        )
        self.assertEqual(image.read_bytes(), PNG)

    def test_recovery_and_resume_preserve_completed_results(self):
        self.start()
        run = self.wait()["latest"]
        with self.projects.connect(self.project_id) as database:
            database.execute(
                "UPDATE evidence_runs SET state='running' WHERE run_id=?",
                (run["run_id"],),
            )
        self.service.recover()
        recovered = self.service.status(self.project_id)["latest"]
        self.assertEqual(recovered["state"], "interrupted")
        self.service.resume(self.project_id, run["run_id"])
        self.assertEqual(self.wait()["latest"]["results"], run["results"])

    def test_scope_change_marks_old_results_stale(self):
        self.start()
        self.wait()
        with self.projects.connect(self.project_id) as database:
            database.execute("UPDATE project SET revision=revision+1")
        self.assertTrue(self.service.status(self.project_id)["latest"]["stale"])

    def test_unchanged_results_reuse_verified_artifacts(self):
        self.start()
        first = self.wait()["latest"]

        class NeverExtract(FakeExtractor):
            def frame(self, *args):
                raise AssertionError("cache should avoid extraction")

            def audio_features(self, *args):
                raise AssertionError("cache should avoid extraction")

        self.service.extractor_factory = NeverExtract
        self.start("evidence-request-2")
        self.assertEqual(self.wait()["latest"]["results"], first["results"])

    def test_start_is_idempotent_and_command_ids_are_validated(self):
        initial = self.start()["latest"]["run_id"]
        self.wait()
        self.assertEqual(self.start()["latest"]["run_id"], initial)
        for invalid in (None, {}, "../private", "ev_wrong"):
            with self.subTest(invalid=invalid), self.assertRaises(ProjectError):
                self.service.cancel(self.project_id, invalid)

    def test_scope_excludes_attached_cover_images(self):
        with self.projects.connect(self.project_id) as database:
            row = database.execute(
                "SELECT version_id,details_json FROM media_streams "
                "WHERE kind='video' LIMIT 1"
            ).fetchone()
            details = json.loads(row["details_json"])
            details["attached_pic"] = True
            database.execute(
                "INSERT INTO media_streams VALUES(?,5,'video','mjpeg',?)",
                (row["version_id"], json.dumps(details)),
            )
        self.assertEqual(len(self.service.scope(self.project_id)["items"]), 3)

    def test_cancel_and_resume_keep_checkpointed_work(self):
        entered = threading.Event()

        class BlockExtractor(FakeExtractor):
            def frame(self, source, offset, output, cancel):
                entered.set()
                cancel.wait(3)
                from clipen.analysis.audio import AudioPreparationError
                raise AudioPreparationError("cancelled")

        self.service.extractor_factory = BlockExtractor
        run_id = self.start()["latest"]["run_id"]
        self.assertTrue(entered.wait(2))
        self.service.cancel(self.project_id, run_id)
        self.assertEqual(self.wait()["latest"]["state"], "cancelled")
        self.service.extractor_factory = FakeExtractor
        self.service.resume(self.project_id, run_id)
        self.assertEqual(self.wait()["latest"]["state"], "succeeded")

    def test_api_protects_images_and_mutation_routes(self):
        from clipen.controller import (
            ControllerConfig,
            LocalSessionAuthority,
            ProviderState,
            ProviderStatus,
            create_app,
        )
        from test_project_api import request_app

        static_root = self.root / "web"
        static_root.mkdir()
        (static_root / "index.html").write_text("<html>test</html>")
        authority = LocalSessionAuthority()
        credentials = authority.connect_local_browser()
        app = create_app(
            ControllerConfig(static_root),
            ProviderStatus("Codex", ProviderState.READY, "test"),
            authority,
            evidence_service=self.service,
        )
        base = f"/api/projects/{self.project_id}/evidence"
        headers = {"cookie": "clipen_session=" + credentials.session_token}
        self.assertEqual(asyncio.run(request_app(app, "GET", base))[0], 401)
        self.assertEqual(
            asyncio.run(request_app(app, "GET", base, headers=headers))[0], 200
        )
        forbidden = asyncio.run(
            request_app(app, "POST", base + "/commands/start", headers=headers, body=b"{}")
        )
        self.assertEqual(forbidden[0], 403)
        self.start()
        run = self.wait()["latest"]
        frame = next(
            result for result in run["results"] if result["kind"] == "video"
        )["frames"][0]["artifact_id"]
        image = asyncio.run(
            request_app(
                app,
                "GET",
                base + "/" + run["run_id"] + "/images/" + frame,
                headers=headers,
            )
        )
        self.assertEqual(image[0], 200)
        self.assertEqual(image[2], PNG)
        self.assertIn("no-store", image[1]["cache-control"])


if __name__ == "__main__":
    unittest.main()
