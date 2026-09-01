"""Fail-closed verification for Clipen's bundled system Skills."""

from __future__ import annotations

from base64 import b64decode
from binascii import Error as Base64Error
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


_MANIFEST_NAME = "release.json"
_SIGNATURE_NAME = "release.sig"
_KEY_INFO_NAME = "release-key.json"
_CONTROL_FILES = frozenset({_MANIFEST_NAME, _SIGNATURE_NAME, _KEY_INFO_NAME})
_SCHEMA = 1
_ALGORITHM = "ed25519"
_KEY_ID = "clipen-system-skills-2026-01"
# This is the trust anchor. release-key.json is informational and must match it.
_PUBLIC_KEY_BASE64 = "s+1kIAGNqe5b/28s4K7rMp6uyf+X/pvaXiMuuOvYHJU="
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SKILL_ID = re.compile(r"^clipen-[a-z0-9]+(?:-[a-z0-9]+)*$")


class SystemSkillVerificationError(RuntimeError):
    """The bundled release cannot be trusted and must not be used."""


@dataclass(frozen=True)
class SystemSkillSummary:
    """Separately authored, browser-safe product metadata."""

    skill_id: str
    name: str
    purpose: str
    adjustable_parameters: tuple[str, ...]

    def as_public_dict(self) -> dict[str, object]:
        return {
            "id": self.skill_id,
            "name": self.name,
            "purpose": self.purpose,
            "adjustable_parameters": list(self.adjustable_parameters),
        }


@dataclass(frozen=True)
class VerifiedSystemSkillRelease:
    release_id: str
    root: Path
    summaries: tuple[SystemSkillSummary, ...]
    _skill_contents: tuple[tuple[str, str], ...] = field(repr=False)

    def _content(self, skill_id: str) -> str:
        for candidate_id, content in self._skill_contents:
            if candidate_id == skill_id:
                return content
        raise KeyError(skill_id)

    def read_skill(self, skill_id: str) -> str:
        """Return a verified in-memory snapshot, never bytes reread after verification."""
        current = verify_system_skill_release(self.root)
        if current.release_id != self.release_id:
            raise SystemSkillVerificationError("system Skill release changed")
        return current._content(skill_id)

    def attempt_snapshot(self, editing_skill_id: str) -> tuple[str, str]:
        """Reverify immediately before an Attempt and return core plus one preset."""
        selectable = {summary.skill_id for summary in self.summaries}
        if editing_skill_id not in selectable:
            raise KeyError(editing_skill_id)
        current = verify_system_skill_release(self.root)
        if current.release_id != self.release_id:
            raise SystemSkillVerificationError("system Skill release changed")
        return current._content("clipen-core"), current._content(editing_skill_id)


def _fail(reason: str) -> SystemSkillVerificationError:
    return SystemSkillVerificationError(f"system Skill release verification failed: {reason}")


def _read_regular(path: Path, *, maximum: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise _fail(f"missing or unsafe file: {path.name}")
    try:
        size = path.stat().st_size
        if size < 1 or size > maximum:
            raise _fail(f"invalid file size: {path.name}")
        return path.read_bytes()
    except OSError as error:
        raise _fail(f"cannot read file: {path.name}") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _fail(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _fail(f"invalid {label}") from error
    if not isinstance(value, dict):
        raise _fail(f"invalid {label}")
    return value


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _strict_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise _fail(f"invalid {label} fields")


def _decode_base64(value: object, *, length: int, label: str) -> bytes:
    if not isinstance(value, str):
        raise _fail(f"invalid {label}")
    try:
        decoded = b64decode(value, validate=True)
    except (Base64Error, ValueError) as error:
        raise _fail(f"invalid {label}") from error
    if len(decoded) != length:
        raise _fail(f"invalid {label}")
    return decoded


def _release_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": manifest["schema"],
        "files": manifest["files"],
        "public_catalog": manifest["public_catalog"],
    }


def _validate_frontmatter(raw: bytes, skill_id: str, role: str) -> None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _fail(f"Skill is not UTF-8: {skill_id}") from error
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise _fail(f"invalid Skill frontmatter: {skill_id}")
    frontmatter = text[4 : text.index("\n---\n", 4)]
    required = {
        "name": skill_id,
        "origin": "system",
        "role": role,
        "editable": "false",
        "status": "release",
    }
    observed: dict[str, str] = {}
    for line in frontmatter.splitlines():
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        cleaned = value.strip().strip('"').strip("'")
        if key in required:
            if key in observed:
                raise _fail(f"duplicate Skill metadata: {skill_id}")
            observed[key] = cleaned
    has_description = any(
        line.startswith("description:") for line in frontmatter.splitlines()
    )
    if observed != required or not has_description:
        raise _fail(f"invalid Skill metadata: {skill_id}")


def verify_system_skill_release(root: Path) -> VerifiedSystemSkillRelease:
    """Verify signature, exact inventory, metadata, sizes, and hashes."""
    if root.is_symlink() or not root.is_dir():
        raise _fail("bundle root is missing or unsafe")
    resolved_root = root.resolve(strict=True)

    manifest_raw = _read_regular(resolved_root / _MANIFEST_NAME, maximum=256_000)
    signature_file_raw = _read_regular(resolved_root / _SIGNATURE_NAME, maximum=1_000)
    key_raw = _read_regular(resolved_root / _KEY_INFO_NAME, maximum=4_000)
    manifest = _json_object(manifest_raw, label="release manifest")
    key_info = _json_object(key_raw, label="release key")
    _strict_keys(key_info, {"algorithm", "key_id", "public_key"}, "release key")
    if key_info["algorithm"] != _ALGORITHM or key_info["key_id"] != _KEY_ID:
        raise _fail("untrusted release key")
    if key_info["public_key"] != _PUBLIC_KEY_BASE64:
        raise _fail("release key does not match trust anchor")
    if key_raw != _canonical(key_info):
        raise _fail("non-canonical release key")

    public_key = _decode_base64(_PUBLIC_KEY_BASE64, length=32, label="public key")
    try:
        signature_text = signature_file_raw.decode("ascii", "strict")
        if not signature_text.endswith("\n") or "\n" in signature_text[:-1]:
            raise _fail("non-canonical signature")
        signature = _decode_base64(signature_text[:-1], length=64, label="signature")
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, manifest_raw)
    except UnicodeDecodeError as error:
        raise _fail("invalid signature") from error
    except (InvalidSignature, ValueError) as error:
        raise _fail("invalid signature") from error

    _strict_keys(
        manifest,
        {"algorithm", "files", "key_id", "public_catalog", "release_id", "schema"},
        "release manifest",
    )
    if manifest["schema"] != _SCHEMA or manifest["algorithm"] != _ALGORITHM:
        raise _fail("unsupported release manifest")
    if manifest["key_id"] != _KEY_ID or manifest_raw != _canonical(manifest):
        raise _fail("non-canonical or untrusted release manifest")
    expected_release_id = hashlib.sha256(_canonical(_release_payload(manifest))).hexdigest()
    if manifest["release_id"] != expected_release_id:
        raise _fail("release identifier mismatch")

    files = manifest["files"]
    catalog = manifest["public_catalog"]
    if not isinstance(files, list) or not files or not isinstance(catalog, list) or not catalog:
        raise _fail("empty or invalid release inventory")

    declared_paths: set[str] = set()
    skill_ids: set[str] = set()
    roles: dict[str, str] = {}
    verified_contents: list[tuple[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            raise _fail("invalid file entry")
        _strict_keys(item, {"path", "role", "sha256", "size", "skill_id"}, "file entry")
        relative = item["path"]
        skill_id = item["skill_id"]
        role = item["role"]
        if not isinstance(relative, str) or not isinstance(skill_id, str):
            raise _fail("invalid file identity")
        path_parts = PurePosixPath(relative)
        expected_path = PurePosixPath("system", skill_id, "SKILL.md")
        if path_parts != expected_path or path_parts.is_absolute() or ".." in path_parts.parts:
            raise _fail("unsafe or unexpected Skill path")
        if (
            not _SKILL_ID.fullmatch(skill_id)
            or skill_id in skill_ids
            or relative in declared_paths
        ):
            raise _fail("duplicate or invalid Skill identity")
        if role not in {"core", "editing"}:
            raise _fail("invalid Skill role")
        size = item["size"]
        digest = item["sha256"]
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 1
            or size > 1_000_000
        ):
            raise _fail("invalid Skill size")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise _fail("invalid Skill digest")
        skill_path = resolved_root.joinpath(*path_parts.parts)
        raw = _read_regular(skill_path, maximum=1_000_000)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise _fail(f"Skill content mismatch: {skill_id}")
        _validate_frontmatter(raw, skill_id, role)
        declared_paths.add(relative)
        skill_ids.add(skill_id)
        roles[skill_id] = role
        verified_contents.append((skill_id, raw.decode("utf-8")))

    summaries: list[SystemSkillSummary] = []
    catalog_ids: set[str] = set()
    for item in catalog:
        if not isinstance(item, dict):
            raise _fail("invalid public catalog entry")
        _strict_keys(
            item,
            {"adjustable_parameters", "id", "name", "purpose"},
            "public catalog entry",
        )
        skill_id = item["id"]
        name = item["name"]
        purpose = item["purpose"]
        parameters = item["adjustable_parameters"]
        if (
            not isinstance(skill_id, str)
            or skill_id not in skill_ids
            or skill_id in catalog_ids
            or not isinstance(name, str)
            or not 1 <= len(name) <= 40
            or not isinstance(purpose, str)
            or not 1 <= len(purpose) <= 160
            or not isinstance(parameters, list)
            or len(parameters) > 12
            or any(
                not isinstance(value, str) or not 1 <= len(value) <= 40
                for value in parameters
            )
            or len(set(parameters)) != len(parameters)
        ):
            raise _fail("invalid public catalog metadata")
        catalog_ids.add(skill_id)
        summaries.append(SystemSkillSummary(skill_id, name, purpose, tuple(parameters)))
    selectable_ids = {skill_id for skill_id, role in roles.items() if role == "editing"}
    if catalog_ids != selectable_ids or sum(role == "core" for role in roles.values()) != 1:
        raise _fail("catalog and Skill inventory differ")

    actual_paths: set[str] = set()
    try:
        for path in resolved_root.rglob("*"):
            if path.is_symlink():
                raise _fail("symbolic links are not allowed in the release")
            if path.is_file():
                actual_paths.add(path.relative_to(resolved_root).as_posix())
    except OSError as error:
        raise _fail("cannot inspect release inventory") from error
    if actual_paths != declared_paths | _CONTROL_FILES:
        raise _fail("release contains missing or undeclared files")

    return VerifiedSystemSkillRelease(
        release_id=expected_release_id,
        root=resolved_root,
        summaries=tuple(summaries),
        _skill_contents=tuple(verified_contents),
    )
