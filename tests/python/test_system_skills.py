from pathlib import Path
import json
import shutil
import tempfile
import unittest

from clipen.system_skills import (
    SystemSkillVerificationError,
    verify_system_skill_release,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_BUNDLE = REPOSITORY_ROOT / "resources" / "system-skills"


class SystemSkillReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.bundle = Path(self.temporary.name) / "system-skills"
        shutil.copytree(SOURCE_BUNDLE, self.bundle)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_rejected(self) -> None:
        with self.assertRaises(SystemSkillVerificationError):
            verify_system_skill_release(self.bundle)

    def test_bundled_release_verifies_and_only_exposes_safe_presets(self) -> None:
        release = verify_system_skill_release(self.bundle)

        self.assertRegex(release.release_id, r"^[0-9a-f]{64}$")
        self.assertEqual(
            [summary.skill_id for summary in release.summaries],
            [
                "clipen-clean-cut",
                "clipen-talking-head",
                "clipen-story-vlog",
                "clipen-highlight",
            ],
        )
        public = [summary.as_public_dict() for summary in release.summaries]
        self.assertNotIn("content", public[0])
        self.assertNotIn("path", public[0])
        self.assertNotIn("clipen-core", repr(public))
        self.assertIn("# Clipen 核心流程", release.read_skill("clipen-core"))
        core, preset = release.attempt_snapshot("clipen-highlight")
        self.assertIn("# Clipen 核心流程", core)
        self.assertIn("# 精華短片", preset)
        with self.assertRaises(KeyError):
            release.attempt_snapshot("clipen-core")

    def test_modified_skill_is_rejected(self) -> None:
        skill = self.bundle / "system" / "clipen-highlight" / "SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
        self.assert_rejected()

    def test_missing_or_extra_file_is_rejected(self) -> None:
        missing = self.bundle / "system" / "clipen-highlight" / "SKILL.md"
        missing.unlink()
        self.assert_rejected()

        shutil.rmtree(self.bundle)
        shutil.copytree(SOURCE_BUNDLE, self.bundle)
        (self.bundle / "system" / "notes.txt").write_text("not declared", encoding="utf-8")
        self.assert_rejected()

    def test_symbolic_link_is_rejected_even_when_target_bytes_match(self) -> None:
        skill = self.bundle / "system" / "clipen-highlight" / "SKILL.md"
        copied = Path(self.temporary.name) / "matching-skill.md"
        shutil.copyfile(skill, copied)
        skill.unlink()
        skill.symlink_to(copied)
        self.assert_rejected()

    def test_replaced_key_or_signature_is_rejected(self) -> None:
        key = self.bundle / "release-key.json"
        key_info = json.loads(key.read_text(encoding="utf-8"))
        key_info["public_key"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        key.write_text(
            json.dumps(key_info, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        self.assert_rejected()

        shutil.rmtree(self.bundle)
        shutil.copytree(SOURCE_BUNDLE, self.bundle)
        signature = self.bundle / "release.sig"
        raw = bytearray(signature.read_bytes())
        raw[0] = ord("A") if raw[0] != ord("A") else ord("B")
        signature.write_bytes(raw)
        self.assert_rejected()

    def test_previously_verified_release_is_rechecked_before_read(self) -> None:
        release = verify_system_skill_release(self.bundle)
        skill = self.bundle / "system" / "clipen-core" / "SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")

        with self.assertRaises(SystemSkillVerificationError):
            release.read_skill("clipen-core")


if __name__ == "__main__":
    unittest.main()
