import json
from pathlib import Path
import tempfile
import unittest

from clipen.providers.recovery import (
    AttemptReceiptError,
    AttemptReceiptStore,
    ProviderAttemptReceipt,
    ProviderAttemptState,
    ProviderUsage,
    sha256_bytes,
    sha256_file,
)


class ProviderRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "attempt"
        self.workspace.mkdir()
        self.prompt_hash = sha256_bytes(b"approved prompt")
        self.schema_hash = sha256_bytes(b"approved schema")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def receipt(self, **changes) -> ProviderAttemptReceipt:
        values = {
            "attempt_id": "attempt-1",
            "state": ProviderAttemptState.RUNNING,
            "prompt_sha256": self.prompt_hash,
            "schema_sha256": self.schema_hash,
        }
        values.update(changes)
        return ProviderAttemptReceipt(**values)

    def test_receipt_is_private_atomic_and_does_not_store_prompt(self) -> None:
        store = AttemptReceiptStore(self.workspace, "attempt-1")
        store.write(self.receipt(started_at="2026-09-01T00:00:00+00:00", pid=123))
        store.write(
            self.receipt(
                state=ProviderAttemptState.SUCCEEDED,
                started_at="2026-09-01T00:00:00+00:00",
                finished_at="2026-09-01T00:00:01+00:00",
                pid=123,
                return_code=0,
                output_sha256=sha256_bytes(b"output"),
                events_sha256=sha256_bytes(b"events"),
                diagnostics_sha256=sha256_bytes(b""),
                usage=ProviderUsage(100, 40, 20),
            )
        )

        loaded = store.read()
        self.assertEqual(loaded.state, ProviderAttemptState.SUCCEEDED)
        self.assertEqual(loaded.usage, ProviderUsage(100, 40, 20))
        self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("approved prompt", store.path.read_text(encoding="utf-8"))
        self.assertEqual(list(self.workspace.glob(".receipt-*")), [])

    def test_receipt_rejects_symlink_bad_permissions_and_unknown_fields(self) -> None:
        store = AttemptReceiptStore(self.workspace, "attempt-1")
        outside = self.workspace.parent / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        store.path.symlink_to(outside)
        with self.assertRaises(AttemptReceiptError):
            store.write(self.receipt())
        store.path.unlink()

        store.write(self.receipt())
        store.path.chmod(0o644)
        with self.assertRaises(AttemptReceiptError):
            store.read()

        payload = self.receipt().to_dict()
        payload["unexpected"] = True
        store.path.write_text(json.dumps(payload), encoding="utf-8")
        store.path.chmod(0o600)
        with self.assertRaises(AttemptReceiptError):
            store.read()

    def test_usage_and_hashes_are_bounded_and_validated(self) -> None:
        artifact = self.workspace / "artifact.bin"
        artifact.write_bytes(b"bounded artifact")
        self.assertEqual(sha256_file(artifact, 1024), sha256_bytes(b"bounded artifact"))
        with self.assertRaises(AttemptReceiptError):
            sha256_file(artifact, 2)
        with self.assertRaises(ValueError):
            ProviderUsage(-1, 0, 0)
        with self.assertRaises(ValueError):
            self.receipt(output_sha256="not-a-hash")


if __name__ == "__main__":
    unittest.main()
