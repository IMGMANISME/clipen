"""The user's global library of imported skill packages.

Layout under the Clipen data root::

    skills/<skill_id>/<content-hash-16>/...   immutable imported copy
    skills/<skill_id>/current.json            the version offered for new work
    skills/.trash/<uuid>/                     removed skills, never followed

Importing validates the source, copies only accepted files into staging,
validates the copy again and publishes it atomically. Production sessions
freeze the package content, so removing or replacing a skill never changes
existing work.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import threading
import uuid

from clipen.projects import ProjectError

from .package import SKILL_ID, SkillPackage, load_package


_POINTER = "current.json"
_STAGING = ".staging"
_TRASH = ".trash"


class SkillLibrary:
    def __init__(self, root: Path, design: dict[str, object], reserved: frozenset[str]) -> None:
        self.root = Path(root)
        self.design = design
        self.reserved = reserved
        self._lock = threading.Lock()

    def _ensure_root(self) -> Path:
        if self.root.is_symlink():
            raise ProjectError("storage_unsafe", "Skill 庫路徑不安全。", status_code=409)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self.root.resolve(strict=True)

    def _skill_dir(self, skill_id: str) -> Path:
        if not isinstance(skill_id, str) or not SKILL_ID.fullmatch(skill_id):
            raise ProjectError("skill_not_found", "找不到這套影片製作 Skill。", status_code=404)
        return self._ensure_root() / skill_id

    def _current(self, skill_id: str) -> Path | None:
        directory = self._skill_dir(skill_id)
        pointer = directory / _POINTER
        if directory.is_symlink() or pointer.is_symlink() or not pointer.is_file():
            return None
        try:
            version_dir = json.loads(pointer.read_text(encoding="utf-8"))["version_dir"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        candidate = directory / str(version_dir)
        if "/" in str(version_dir) or candidate.is_symlink() or not candidate.is_dir():
            return None
        return candidate

    def load(self, skill_id: str) -> SkillPackage:
        version = self._current(skill_id)
        if version is None:
            raise ProjectError("skill_not_found", "找不到這套影片製作 Skill。", status_code=404)
        package = load_package(version, self.design, self.reserved)
        if package.skill_id != skill_id or not version.name == package.content_hash[:16]:
            raise ProjectError("skill_package_changed", "Skill 庫裡的這套 Skill 內容已被修改，請重新匯入。", status_code=409)
        return package

    def list(self) -> list[dict[str, object]]:
        """Every importable skill, reporting damaged ones instead of hiding them."""
        root = self._ensure_root()
        entries = []
        for directory in sorted(root.iterdir()):
            if directory.name.startswith(".") or directory.is_symlink() or not directory.is_dir():
                continue
            if not SKILL_ID.fullmatch(directory.name) or self._current(directory.name) is None:
                continue
            try:
                entries.append({"state": "ready", **self.load(directory.name).summary()})
            except ProjectError as error:
                entries.append({"state": "invalid", "skill_id": directory.name, "error": error.message})
        return entries

    def import_folder(self, source: Path) -> SkillPackage:
        package = load_package(source, self.design, self.reserved)
        with self._lock:
            root = self._ensure_root()
            staging = root / _STAGING / uuid.uuid4().hex
            staging.mkdir(mode=0o700, parents=True)
            try:
                for relative, data in package.files().items():
                    target = staging / relative
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(data)
                # Re-validate what was written; the source may change during the copy.
                copied = load_package(staging, self.design, self.reserved)
                if copied.content_hash != package.content_hash or copied.skill_id != package.skill_id:
                    raise ProjectError("skill_package_changed", "匯入期間 Skill 內容有變動，請重新匯入。", status_code=409)
                directory = self._skill_dir(package.skill_id)
                directory.mkdir(mode=0o700, exist_ok=True)
                version_dir = directory / package.content_hash[:16]
                if not version_dir.exists():
                    os.replace(staging, version_dir)
                pointer = directory / f".{_POINTER}.{uuid.uuid4().hex}"
                pointer.write_text(json.dumps({"version_dir": version_dir.name, "version": package.version},
                                              ensure_ascii=False), encoding="utf-8")
                os.replace(pointer, directory / _POINTER)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        # The copy holds only accepted files; report what the source had skipped.
        return replace(copied, ignored=package.ignored)

    def remove(self, skill_id: str) -> None:
        """Hide a skill from new work; frozen sessions keep their own copy."""
        with self._lock:
            directory = self._skill_dir(skill_id)
            if directory.is_symlink() or not directory.is_dir():
                raise ProjectError("skill_not_found", "找不到這套影片製作 Skill。", status_code=404)
            trash = self._ensure_root() / _TRASH
            trash.mkdir(mode=0o700, exist_ok=True)
            # Only managed paths under the library root are ever moved.
            os.replace(directory, trash / f"{skill_id}-{uuid.uuid4().hex}")
