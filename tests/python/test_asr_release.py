import json
from pathlib import Path
import tempfile
import unittest

from clipen.asr_release import (
    MODEL_FILES,
    MODEL_ID,
    MODEL_REVISION,
    NOTICE_VERSION,
    RELEASE_ID,
    quick_release_is_ready,
    release_root,
    requirements_digest,
)


class ASRReleaseContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.requirements = self.root / "asr-runtime.txt"
        self.requirements.write_text("mlx-speech==0.5.2\n", encoding="utf-8")
        release = release_root(self.root)
        (release / "runtime" / "bin").mkdir(parents=True)
        python = release / "runtime" / "bin" / "python"
        python.write_text("#!/bin/sh\n", encoding="utf-8")
        python.chmod(0o700)
        (release / "model").mkdir()
        for filename, size, _digest in MODEL_FILES:
            with (release / "model" / filename).open("wb") as output:
                output.truncate(size)
        (release / "receipt.json").write_text(
            json.dumps(
                {
                    "schema": 3,
                    "release_id": RELEASE_ID,
                    "notice_version": NOTICE_VERSION,
                    "model_id": MODEL_ID,
                    "model_revision": MODEL_REVISION,
                    "model_manifest_sha256": "a" * 64,
                    "requirements_sha256": requirements_digest(self.requirements),
                    "runtime_python": "runtime/bin/python",
                    "model_path": "model",
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_quick_check_binds_receipt_runtime_model_sizes_and_requirements(self) -> None:
        self.assertTrue(quick_release_is_ready(self.root, self.requirements))
        self.requirements.write_text("mlx-speech==0.5.3\n", encoding="utf-8")
        self.assertFalse(quick_release_is_ready(self.root, self.requirements))

    def test_quick_check_rejects_changed_model_size(self) -> None:
        model = release_root(self.root) / "model" / MODEL_FILES[-1][0]
        with model.open("r+b") as output:
            output.truncate(MODEL_FILES[-1][1] - 1)
        self.assertFalse(quick_release_is_ready(self.root, self.requirements))

    def test_quick_check_rejects_symlinked_release_components(self) -> None:
        release = release_root(self.root)
        model = release / "model"
        outside = self.root / "outside-model"
        model.rename(outside)
        model.symlink_to(outside, target_is_directory=True)
        self.assertFalse(quick_release_is_ready(self.root, self.requirements))


if __name__ == "__main__":
    unittest.main()
