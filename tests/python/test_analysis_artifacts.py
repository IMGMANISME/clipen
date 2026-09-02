import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from clipen.analysis import ArtifactKind, ArtifactStore, Rational, SourceSpan
from clipen.projects import ProjectError, ProjectRepository


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.projects = ProjectRepository(self.root / "projects")
        self.project = self.projects.create_project("request-artifact-project", "逐字稿")
        self.store = ArtifactStore(self.projects, max_artifact_bytes=1024)
        self.operation_id = "op_" + "a" * 32
        self.attempt_id = "att_" + "b" * 32
        self.span = SourceSpan(
            "av_" + "c" * 32,
            1,
            "48000",
            "144000",
            Rational(1, 48000),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_publish_is_private_content_addressed_and_durable(self) -> None:
        staging = self.store.create_staging_file(self.project.project_id, suffix=".wav")
        staging.write_bytes(b"bounded audio")

        record = self.store.publish(
            self.project.project_id,
            staging,
            kind=ArtifactKind.ANALYSIS_AUDIO,
            producer_operation_id=self.operation_id,
            producer_attempt_id=self.attempt_id,
            recipe_id="audio.pcm16.v1",
            lineage={"inventory_run_id": "inv_" + "d" * 32},
            source_span=self.span,
        )

        self.assertFalse(staging.exists())
        self.assertEqual(record.sha256, hashlib.sha256(b"bounded audio").hexdigest())
        self.assertEqual(record.source_span, self.span)
        published = self.store.path(record)
        self.assertEqual(published.read_bytes(), b"bounded audio")
        self.assertEqual(published.stat().st_mode & 0o777, 0o600)
        self.assertFalse(Path(record.relative_path).is_absolute())
        self.assertEqual(self.store.get(self.project.project_id, record.artifact_id), record)

    def test_identical_publication_reuses_the_blob_and_record(self) -> None:
        records = []
        for _ in range(2):
            staging = self.store.create_staging_file(self.project.project_id)
            staging.write_bytes(b"same")
            records.append(
                self.store.publish(
                    self.project.project_id,
                    staging,
                    kind=ArtifactKind.CHECKPOINT,
                    producer_operation_id=self.operation_id,
                    producer_attempt_id=self.attempt_id,
                    recipe_id="checkpoint.v1",
                    lineage={},
                )
            )

        self.assertEqual(records[0], records[1])
        blobs = list(
            (self.root / "projects" / self.project.project_id / "artifacts" / "sha256").glob("*/*")
        )
        self.assertEqual(len(blobs), 1)

    def test_publish_rejects_paths_outside_managed_staging(self) -> None:
        outside = self.root / "raw.mov"
        outside.write_bytes(b"original")
        with self.assertRaises(ProjectError) as context:
            self.store.publish(
                self.project.project_id,
                outside,
                kind=ArtifactKind.ANALYSIS_AUDIO,
                producer_operation_id=self.operation_id,
                producer_attempt_id=self.attempt_id,
                recipe_id="audio.pcm16.v1",
                lineage={},
            )
        self.assertEqual(context.exception.code, "artifact_staging_unsafe")
        self.assertEqual(outside.read_bytes(), b"original")

    def test_publish_rejects_symlink_and_oversized_staging(self) -> None:
        outside = self.root / "outside"
        outside.write_bytes(b"keep")
        staging_root = self.store.create_staging_file(self.project.project_id).parent
        linked = staging_root / "linked.wav"
        linked.symlink_to(outside)
        with self.assertRaises(ProjectError) as linked_error:
            self.store.publish(
                self.project.project_id,
                linked,
                kind=ArtifactKind.ANALYSIS_AUDIO,
                producer_operation_id=self.operation_id,
                producer_attempt_id=self.attempt_id,
                recipe_id="audio.pcm16.v1",
                lineage={},
            )
        self.assertEqual(linked_error.exception.code, "artifact_staging_unsafe")

        oversized = self.store.create_staging_file(self.project.project_id)
        oversized.write_bytes(os.urandom(1025))
        with self.assertRaises(ProjectError) as size_error:
            self.store.publish(
                self.project.project_id,
                oversized,
                kind=ArtifactKind.ANALYSIS_AUDIO,
                producer_operation_id=self.operation_id,
                producer_attempt_id=self.attempt_id,
                recipe_id="audio.pcm16.v1",
                lineage={},
            )
        self.assertEqual(size_error.exception.code, "artifact_too_large")
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_publish_rejects_symlinked_content_store_component(self) -> None:
        project_root = self.root / "projects" / self.project.project_id
        artifacts = project_root / "artifacts"
        artifacts.mkdir(mode=0o700)
        outside = self.root / "outside-store"
        outside.mkdir()
        (artifacts / "sha256").symlink_to(outside, target_is_directory=True)
        staging = self.store.create_staging_file(self.project.project_id)
        staging.write_bytes(b"safe source")

        with self.assertRaises(ProjectError) as context:
            self.store.publish(
                self.project.project_id,
                staging,
                kind=ArtifactKind.ANALYSIS_AUDIO,
                producer_operation_id=self.operation_id,
                producer_attempt_id=self.attempt_id,
                recipe_id="audio.pcm16.v1",
                lineage={},
            )

        self.assertEqual(context.exception.code, "artifact_store_unsafe")
        self.assertEqual(list(outside.iterdir()), [])

    def test_source_span_rejects_invalid_time_contracts(self) -> None:
        with self.assertRaises(ValueError):
            SourceSpan("av_" + "c" * 32, 0, "20", "20", Rational(1, 1000))
        with self.assertRaises(ValueError):
            Rational(1, 0)


if __name__ == "__main__":
    unittest.main()
