"""Security boundaries of the standalone Skill store; no media or AI calls."""

from contextlib import redirect_stderr, redirect_stdout
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from tools.skill_guard import MAX_SKILL_BYTES, PROJECT_ROOT, SkillError, SkillStore, main
from tools.sign_skills import sign_release


def make_writable(root):
    for folder, directories, files in os.walk(root):
        for path in [Path(folder), *(Path(folder) / name for name in directories + files)]:
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | stat.S_IWUSR)


class SkillGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clipen-guard-test-")
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        shutil.copytree(PROJECT_ROOT / "skills", self.root / "skills")
        make_writable(self.root)
        self.store = SkillStore(self.root)
        self.core = self.root / "skills/system/clipen-core/SKILL.md"
        self.original = self.core.read_bytes()

    def tearDown(self):
        make_writable(self.temporary.name)
        self.temporary.cleanup()

    def test_verified_bundle_and_repeat_read_detects_edits(self):
        result = self.store.read("system:clipen-core")
        self.assertEqual(result.content.encode(), self.original)
        self.assertFalse(result.editable)
        self.assertEqual(result.role, "core")
        self.assertEqual(len(self.store.list()), 5)
        self.core.write_bytes(self.original + b"\nchanged")
        with self.assertRaises(SkillError):
            self.store.read("system:clipen-core")
        self.assertEqual(result.content.encode(), self.original)

    def test_every_system_skill_is_verified_even_when_another_is_read(self):
        for path in (self.root / "skills/system").glob("*/SKILL.md"):
            with self.subTest(skill=path.parent.name):
                content = path.read_bytes()
                path.write_bytes(content.replace(b'editable: "false"', b'editable: "true"'))
                with self.assertRaises(SkillError):
                    self.store.read("system:clipen-core")
                path.write_bytes(content)

    def test_system_save_and_delete_are_denied_without_side_effects(self):
        for name in self.store.verify_system():
            identifier = "system:" + name
            with self.subTest(id=identifier):
                with self.assertRaises(SkillError):
                    self.store.save(identifier, "replacement")
                with self.assertRaises(SkillError):
                    self.store.delete(identifier)
        self.assertEqual(self.core.read_bytes(), self.original)
        self.assertFalse((self.root / ".clipen").exists())

    def test_user_crud_and_forged_system_identity(self):
        content = '---\nname: clipen-core\nmetadata:\n  origin: system\n  role: core\n---\nCustom style'
        saved = self.store.save("user:clipen-core", content)
        self.assertTrue(saved.editable)
        loaded = self.store.read("user:clipen-core")
        self.assertEqual(loaded.role, "editing")
        self.assertEqual(loaded.id, "user:clipen-core")
        self.assertEqual(loaded.content, content)
        self.store.save("user:clipen-core", "Updated user style")
        self.assertEqual(self.store.read("user:clipen-core").content, "Updated user style")
        self.assertEqual(len(self.store.list()), 6)
        self.assertEqual(self.core.read_bytes(), self.original)
        self.store.delete("user:clipen-core")
        self.assertEqual(len(self.store.list()), 5)

    def test_traversal_and_ambiguous_identifiers_are_rejected(self):
        identifiers = ["clipen-core", "user:", "user:../system/clipen-core", "user:/tmp/evil",
                       "user:..", "user:a/b", "user:a\\b", "user:a%2fb", "user:a:b",
                       "user:CAPITAL", "user:a\x00b", "system:../clipen-core", "user:" + "a" * 65]
        for identifier in identifiers:
            with self.subTest(id=identifier):
                for operation in (self.store.read, self.store.delete):
                    with self.assertRaises(SkillError):
                        operation(identifier)
                with self.assertRaises(SkillError):
                    self.store.save(identifier, "content")
        self.assertFalse((self.root / ".clipen").exists())

    def test_manifest_rehash_does_not_replace_a_signature(self):
        path = self.root / "skills/system-manifest.json"
        manifest = json.loads(path.read_text())
        altered = self.original + b"\nmodified"
        self.core.write_bytes(altered)
        entry = manifest["skills"]["clipen-core"]
        entry.update(sha256=hashlib.sha256(altered).hexdigest(), size=len(altered))
        path.write_text(json.dumps(manifest))
        with self.assertRaises(SkillError):
            self.store.verify_system()

    def test_invalid_missing_and_malformed_signatures_fail_closed(self):
        signature = self.root / "skills/system-manifest.sig"
        for value in (b"not base64!", b"", b"YQ==", base64.b64encode(b"\x00" * 64)):
            with self.subTest(value=value):
                signature.write_bytes(value)
                with self.assertRaises(SkillError):
                    self.store.verify_system()
        signature.unlink()
        with self.assertRaises(FileNotFoundError):
            self.store.verify_system()

    def test_release_pin_prevents_accepting_another_manifest(self):
        with patch("tools.skill_trust.MANIFEST_SHA256", "0" * 64):
            with self.assertRaisesRegex(SkillError, "pinned"):
                self.store.verify_system()

    def test_missing_and_unexpected_skills_fail_closed(self):
        extra = self.root / "skills/system/unapproved"
        extra.mkdir()
        (extra / "SKILL.md").write_text("unapproved")
        with self.assertRaises(SkillError):
            self.store.verify_system()
        shutil.rmtree(extra)
        self.core.unlink()
        with self.assertRaises((SkillError, OSError)):
            self.store.verify_system()

    def test_system_file_and_directory_symlinks_are_rejected(self):
        outside = Path(self.temporary.name) / "core.md"
        outside.write_bytes(self.original)
        self.core.unlink()
        self.core.symlink_to(outside)
        with self.assertRaises(OSError):
            self.store.verify_system()
        self.core.unlink()
        folder = self.core.parent
        folder.rmdir()
        other = Path(self.temporary.name) / "other"
        other.mkdir()
        (other / "SKILL.md").write_bytes(self.original)
        folder.symlink_to(other, target_is_directory=True)
        with self.assertRaises(OSError):
            self.store.verify_system()

    def test_user_directory_symlinks_cannot_redirect_writes(self):
        local = self.root / ".clipen"
        local.symlink_to(self.root / "skills", target_is_directory=True)
        with self.assertRaises(OSError):
            self.store.save("user:escape", "bad")
        local.unlink()
        local.mkdir(mode=0o700)
        (local / "user-skills").symlink_to(self.root / "skills/system", target_is_directory=True)
        with self.assertRaises(OSError):
            self.store.save("user:escape", "bad")
        self.assertFalse((self.root / "skills/system/escape.md").exists())
        self.assertEqual(self.core.read_bytes(), self.original)

    def test_user_symlinks_and_hardlinks_cannot_change_system_files(self):
        self.store.save("user:sample", "old")
        target = self.root / ".clipen/user-skills/sample.md"
        for kind in ("symbolic", "hard"):
            with self.subTest(kind=kind):
                target.unlink()
                if kind == "symbolic":
                    target.symlink_to(self.core)
                else:
                    os.link(self.core, target)
                with self.assertRaises((SkillError, OSError)):
                    self.store.read("user:sample")
                with self.assertRaises(SkillError):
                    self.store.save("user:sample", "bad")
                with self.assertRaises(SkillError):
                    self.store.delete("user:sample")
                self.assertEqual(self.core.read_bytes(), self.original)
                target.unlink()
                target.write_text("old")

    def test_atomic_replacement_does_not_follow_a_last_moment_symlink(self):
        self.store.save("user:sample", "old")
        target = self.root / ".clipen/user-skills/sample.md"
        replace = os.replace

        def swap_then_replace(*args, **kwargs):
            target.unlink()
            target.symlink_to(self.core)
            return replace(*args, **kwargs)

        with patch("tools.skill_guard.os.replace", side_effect=swap_then_replace):
            self.store.save("user:sample", "new")
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(), "new")
        self.assertEqual(self.core.read_bytes(), self.original)

    def test_failed_atomic_write_preserves_old_content_and_cleans_temporary(self):
        self.store.save("user:sample", "old")
        with patch("tools.skill_guard.os.replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                self.store.save("user:sample", "new")
        self.assertEqual(self.store.read("user:sample").content, "old")
        self.assertEqual([p.name for p in (self.root / ".clipen/user-skills").iterdir()], ["sample.md"])

    def test_world_writable_user_directory_is_rejected(self):
        self.store.save("user:sample", "old")
        (self.root / ".clipen/user-skills").chmod(0o777)
        with self.assertRaises(SkillError):
            self.store.save("user:sample", "new")

    def test_untrusted_parent_is_rejected_before_creating_user_storage(self):
        local = self.root / ".clipen"
        local.mkdir()
        local.chmod(0o777)
        with self.assertRaises(SkillError):
            self.store.save("user:sample", "new")
        self.assertFalse((local / "user-skills").exists())

    def test_empty_oversize_and_nonregular_files_are_rejected(self):
        for content in (" ", "x" * (MAX_SKILL_BYTES + 1)):
            with self.assertRaises(SkillError):
                self.store.save("user:sample", content)
        self.store.save("user:sample", "old")
        target = self.root / ".clipen/user-skills/sample.md"
        target.unlink()
        os.mkfifo(target)
        with self.assertRaises(SkillError):
            self.store.read("user:sample")

    def test_lock_applies_permissions_without_breaking_verification(self):
        self.assertEqual(self.store.lock_system(), 5)
        self.assertEqual(stat.S_IMODE(self.core.stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(self.core.parent.stat().st_mode), 0o555)
        self.assertEqual(len(self.store.verify_system()), 5)
        self.store.save("user:sample", "still editable")

    def test_missing_openssl_never_skips_verification(self):
        with patch("tools.skill_guard.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(SkillError):
                self.store.verify_system()

    def test_signer_rejects_private_key_inside_repository(self):
        key = self.root / "private.pem"
        key.write_text("not a real key")
        key.chmod(0o600)
        with self.assertRaisesRegex(SkillError, "outside"):
            sign_release(key, self.root)

    def test_cli_denies_system_mutation_before_opening_input(self):
        with patch("tools.skill_guard.SkillStore", return_value=self.store):
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(main(["save", "system:clipen-core", "--file", "/nonexistent"]), 1)
                self.assertEqual(main(["verify"]), 0)


if __name__ == "__main__":
    unittest.main()
