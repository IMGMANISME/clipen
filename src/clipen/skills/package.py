"""Read and validate an Agent Skills style folder as an inert Clipen skill package.

A package is a folder with ``SKILL.md`` (YAML-style frontmatter with ``name``
and ``description``), optional ``references/`` text, optional PNG ``assets/``
and an optional ``clipen.json`` that binds built-in Clipen design and may
``extends`` a built-in skill. Only text
and PNG files are read. Scripts and other files are reported and ignored;
nothing in a package is ever executed.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from clipen.projects import ProjectError


SKILL_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")
VERSION = re.compile(r"\d{1,4}(\.\d{1,4}){0,2}")
TEXT_SUFFIXES = frozenset({".md", ".txt", ".json"})
IMAGE_SUFFIXES = frozenset({".png"})
MAX_FILES = 200
MAX_TEXT_BYTES = 256 * 1024
MAX_TOTAL_TEXT_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_DEPTH = 4
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Finder and editor droppings that are neither content nor worth reporting.
_QUIET = frozenset({".DS_Store", "Thumbs.db"})
CLIPEN_KEYS = frozenset({"skill_id", "version", "extends", "visual_style_id", "font_style_id",
                         "components", "default_duration_seconds"})


def invalid(message: str, code: str = "skill_package_invalid") -> ProjectError:
    return ProjectError(code, message, status_code=422)


@dataclass(frozen=True)
class SkillPackage:
    skill_id: str
    name: str
    description: str
    version: str
    frontmatter: dict[str, str]
    resources: dict[str, str]
    assets: dict[str, dict[str, str]]
    binding: dict[str, object]
    ignored: tuple[str, ...] = field(default=())

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(json.dumps(
            {"resources": self.resources, "assets": {k: v["sha256"] for k, v in self.assets.items()}},
            ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def files(self) -> dict[str, bytes]:
        """Exact bytes to persist when this package is copied into a library."""
        content = {path: text.encode("utf-8") for path, text in self.resources.items()}
        content.update({path: base64.b64decode(asset["data"]) for path, asset in self.assets.items()})
        return content

    def summary(self) -> dict[str, object]:
        return {"skill_id": self.skill_id, "name": self.name, "description": self.description,
                "version": self.version, "content_hash": self.content_hash, "binding": self.binding,
                "resources": sorted(self.resources), "assets": sorted(self.assets),
                "ignored": list(self.ignored)}


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Read flat ``key: value`` frontmatter; nested YAML is kept out of scope."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        raise invalid("SKILL.md 開頭需要 frontmatter（--- 包住的 name 與 description）。")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        raise invalid("SKILL.md 的 frontmatter 沒有結束的 ---。") from None
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#") or line.startswith((" ", "\t", "-")):
            continue  # Comments, list items and nested values are not used by Clipen.
        key, separator, value = line.partition(":")
        if not separator:
            raise invalid(f"SKILL.md frontmatter 無法讀取這一行：{line[:80]}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values, "\n".join(lines[end + 1:])


def _slug(value: str) -> str | None:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:64]
    return slug if SKILL_ID.fullmatch(slug) else None


def _walk(root: Path):
    """Yield (relative path, absolute path) for regular files, refusing links."""
    stack = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if entry.is_symlink():
                raise invalid(f"Skill 不能包含捷徑連結：{relative}")
            if entry.is_dir(follow_symlinks=False):
                if depth + 1 > MAX_DEPTH:
                    raise invalid(f"Skill 的資料夾層數太深：{relative}")
                stack.append((path, depth + 1))
            elif entry.is_file(follow_symlinks=False):
                yield relative, path
            else:
                raise invalid(f"Skill 只能包含一般檔案：{relative}")


def _read_regular(path: Path, limit: int, relative: str) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise invalid(f"Skill 檔案必須是單一連結的一般檔案：{relative}")
        if info.st_size > limit:
            raise invalid(f"Skill 檔案太大：{relative}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read(limit + 1)
    finally:
        os.close(descriptor)


def load_package(root: Path, design: dict[str, object] | None = None,
                 reserved: frozenset[str] = frozenset()) -> SkillPackage:
    """Validate a folder; ``design`` lists the built-in ids clipen.json may bind."""
    if root.is_symlink() or not root.is_dir():
        raise invalid("請選擇一個 Skill 資料夾。")
    root = root.resolve(strict=True)
    resources: dict[str, str] = {}
    assets: dict[str, dict[str, str]] = {}
    ignored: list[str] = []
    text_total = total = 0
    for relative, path in _walk(root):
        name = PurePosixPath(relative).name
        suffix = PurePosixPath(relative).suffix.lower()
        if name in _QUIET or name.startswith("._"):
            continue
        if any(part.startswith(".") for part in PurePosixPath(relative).parts):
            ignored.append(relative)
            continue
        if suffix in TEXT_SUFFIXES:
            data = _read_regular(path, MAX_TEXT_BYTES, relative)
            try:
                resources[relative] = data.decode("utf-8")
            except UnicodeDecodeError:
                raise invalid(f"Skill 文字檔需要 UTF-8 編碼：{relative}") from None
            text_total += len(data)
        elif suffix in IMAGE_SUFFIXES:
            data = _read_regular(path, MAX_IMAGE_BYTES, relative)
            if not data.startswith(PNG_SIGNATURE):
                raise invalid(f"Skill 圖片必須是 PNG：{relative}")
            assets[relative] = {"mime_type": "image/png", "sha256": hashlib.sha256(data).hexdigest(),
                                "data": base64.b64encode(data).decode("ascii")}
        else:
            # Scripts, binaries and unknown files stay out; Clipen never runs them.
            ignored.append(relative)
            continue
        total += len(data)
        if len(resources) + len(assets) > MAX_FILES:
            raise invalid(f"Skill 檔案數量超過 {MAX_FILES} 個。")
        if text_total > MAX_TOTAL_TEXT_BYTES or total > MAX_TOTAL_BYTES:
            raise invalid("Skill 的內容總量太大。")
    if "SKILL.md" not in resources:
        raise invalid("Skill 資料夾最上層需要 SKILL.md。")
    frontmatter, body = parse_frontmatter(resources["SKILL.md"])
    name, description = frontmatter.get("name", "").strip(), frontmatter.get("description", "").strip()
    if not 1 <= len(name) <= 64:
        raise invalid("SKILL.md 的 name 必填，且最多 64 字。")
    if not 1 <= len(description) <= 1024:
        raise invalid("SKILL.md 的 description 必填，且最多 1024 字。")
    if not body.strip():
        raise invalid("SKILL.md 需要寫出剪輯指引內容。")
    binding = _binding(resources.get("clipen.json"), design)
    skill_id = binding.pop("skill_id", None) or _slug(name) or _slug(root.name)
    if skill_id is None:
        skill_id = "skill-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    if not SKILL_ID.fullmatch(skill_id):
        raise invalid("clipen.json 的 skill_id 只能用小寫英數與 -，長度 2–64。")
    if skill_id in reserved or skill_id.startswith("clipen-"):
        raise invalid(f"skill_id「{skill_id}」是 Clipen 內建 Skill 使用的名稱，請在 clipen.json 換一個 skill_id。",
                      "skill_id_reserved")
    version = str(binding.pop("version", frontmatter.get("version", "1.0.0")))
    if not VERSION.fullmatch(version):
        raise invalid("版本號需要像 1.0.0 這樣的格式。")
    return SkillPackage(skill_id, name, description, version, frontmatter, resources, assets,
                        binding, tuple(sorted(ignored)))


def _binding(raw: str | None, design: dict[str, object] | None) -> dict[str, object]:
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise invalid("clipen.json 不是有效的 JSON。") from None
    if not isinstance(value, dict):
        raise invalid("clipen.json 需要是一個物件。")
    unknown = set(value) - CLIPEN_KEYS
    if unknown:
        raise invalid("clipen.json 有無法辨識的欄位：" + "、".join(sorted(unknown)))
    design = design or {}
    if "extends" in value and value["extends"] not in design.get("extends", ()):
        raise invalid(f"clipen.json 的 extends「{value['extends']}」不是 Clipen 內建的 Skill。")
    for key, catalog in (("visual_style_id", "visual_styles"), ("font_style_id", "font_styles")):
        if key in value and value[key] not in design.get(catalog, ()):
            raise invalid(f"clipen.json 的 {key}「{value[key]}」不是 Clipen 內建的選項。")
    if "components" in value:
        components = value["components"]
        if (not isinstance(components, list) or not components or len(set(components)) != len(components)
                or any(c not in design.get("components", ()) for c in components)):
            raise invalid("clipen.json 的 components 必須是不重複的內建元件名稱清單。")
    if "default_duration_seconds" in value:
        duration = value["default_duration_seconds"]
        if isinstance(duration, bool) or not isinstance(duration, int) or not 30 <= duration <= 1200:
            raise invalid("clipen.json 的 default_duration_seconds 必須是 30–1200 的整數。")
    return dict(value)
