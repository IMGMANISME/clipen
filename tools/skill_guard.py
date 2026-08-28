"""Verified system Skills and a constrained user Skill store.

Requires Python 3.10+, OpenSSL with Ed25519, and POSIX no-follow/dir-fd APIs.
The verifier, trust anchor, OS, and executing account are trusted. This does not
defend against an administrator replacing the program or controlling the AI.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_SKILL_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class SkillError(Exception):
    """A request that must not proceed."""


@dataclass(frozen=True)
class Skill:
    id: str
    role: str
    editable: bool
    content: str


def _name(value: str) -> str:
    if not isinstance(value, str) or len(value) > 64 or not NAME.fullmatch(value):
        raise SkillError("Invalid Skill name; use lowercase letters, digits and hyphens.")
    return value


def _identifier(value: str) -> tuple[str, str]:
    origin, sep, name = value.partition(":")
    if not sep or origin not in {"system", "user"}:
        raise SkillError("Use an explicit system:name or user:name identifier.")
    return origin, _name(name)


def _require_platform() -> None:
    if (os.name != "posix"
        or not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"))
        or os.open not in os.supports_dir_fd or os.listdir not in os.supports_fd):
        raise SkillError("Required POSIX no-follow filesystem operations are unavailable.")


@contextmanager
def _root(root: Path):
    _require_platform()
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _directory(parent: int, name: str, *, create: bool = False):
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def _read(parent: int, name: str, limit: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SkillError("Only regular, non-hard-linked Skill files are accepted.")
        data = source.read(limit + 1)
        if len(data) > limit:
            raise SkillError("Skill or metadata exceeds its size limit.")
        return data


def _existing_regular(parent: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SkillError("Refusing to change a symlink, hard link or non-regular file.")


def _atomic_write(parent: int, name: str, data: bytes) -> None:
    _existing_regular(parent, name)
    temporary = ".write-" + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        # Replacement changes the directory entry, never follows its target.
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def _openssl(*args: str) -> bytes:
    try:
        result = subprocess.run(["openssl", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SkillError("OpenSSL is unavailable or timed out; refusing to proceed.") from error
    if result.returncode:
        # Do not surface arbitrary subprocess output or private-key diagnostics.
        raise SkillError("OpenSSL operation failed; signature/key may be invalid or unsupported.")
    return result.stdout


def _verify_signature(data: bytes, signature: bytes, public_key: bytes) -> None:
    if len(signature) != 64:
        raise SkillError("Invalid Ed25519 signature length.")
    with tempfile.TemporaryDirectory(prefix="clipen-verify-") as temporary:
        folder = Path(temporary)
        (folder / "public.pem").write_bytes(public_key)
        (folder / "manifest").write_bytes(data)
        (folder / "signature").write_bytes(signature)
        _openssl("pkeyutl", "-verify", "-pubin", "-inkey", str(folder / "public.pem"),
                 "-rawin", "-in", str(folder / "manifest"), "-sigfile", str(folder / "signature"))


def _inventory(root: Path) -> dict[str, bytes]:
    contents = {}
    with _root(root) as repo, _directory(repo, "skills") as skills, _directory(skills, "system") as system:
        names = os.listdir(system)
        if not names or len(names) > 128:
            raise SkillError("Invalid system Skill inventory.")
        for name in sorted(names):
            _name(name)
            with _directory(system, name) as folder:
                if set(os.listdir(folder)) != {"SKILL.md"}:
                    raise SkillError("Unexpected files in a system Skill; review and release explicitly.")
                contents[name] = _read(folder, "SKILL.md", MAX_SKILL_BYTES)
    return contents


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SkillError("Duplicate key in system manifest.")
        result[key] = value
    return result


class SkillStore:
    def __init__(self, root: Path = PROJECT_ROOT):
        # The repository root is supplied by the host, never by a Skill ID.
        self.root = Path(root).absolute()

    def verify_system(self) -> dict[str, Skill]:
        from tools.skill_trust import MANIFEST_SHA256, PUBLIC_KEY_PEM

        with _root(self.root) as repo, _directory(repo, "skills") as folder:
            data = _read(folder, "system-manifest.json", MAX_MANIFEST_BYTES)
            encoded_signature = _read(folder, "system-manifest.sig", 1024)
        try:
            signature = base64.b64decode(encoded_signature.strip(), validate=True)
        except ValueError as error:
            raise SkillError("Malformed system signature.") from error
        _verify_signature(data, signature, PUBLIC_KEY_PEM)
        if hashlib.sha256(data).hexdigest() != MANIFEST_SHA256:
            raise SkillError("System manifest is not the version pinned by this release.")
        try:
            manifest = json.loads(data, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError) as error:
            raise SkillError("Invalid system manifest.") from error
        if (not isinstance(manifest, dict) or set(manifest) != {"format", "skills"}
            or type(manifest["format"]) is not int or manifest["format"] != 1):
            raise SkillError("Unsupported system manifest format.")
        entries = manifest["skills"]
        if not isinstance(entries, dict) or not entries or len(entries) > 128:
            raise SkillError("Invalid signed Skill entries.")
        contents = _inventory(self.root)
        if set(entries) != set(contents):
            raise SkillError("Missing or unexpected system Skill; refusing the entire bundle.")
        result = {}
        for name, entry in entries.items():
            _name(name)
            if not isinstance(entry, dict) or set(entry) != {"role", "sha256", "size"}:
                raise SkillError("Invalid signed Skill entry.")
            expected_role = "core" if name == "clipen-core" else "editing"
            if entry["role"] != expected_role or type(entry["size"]) is not int:
                raise SkillError("Invalid signed Skill role or size.")
            content = contents[name]
            if len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise SkillError(f"System Skill was changed: {name}")
            try:
                text = content.decode("utf-8")
            except UnicodeError as error:
                raise SkillError("System Skill is not UTF-8.") from error
            result[name] = Skill(f"system:{name}", entry["role"], False, text)
        if "clipen-core" not in result:
            raise SkillError("The system core Skill is missing.")
        # Return verified content, not paths that a caller would reopen.
        return result

    @contextmanager
    def _users(self, *, create: bool = False):
        with _root(self.root) as repo, _directory(repo, ".clipen", create=create) as local:
            info = os.fstat(local)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise SkillError("Local data directory must be owned by this user and not writable by others.")
            with _directory(local, "user-skills", create=create) as users:
                info = os.fstat(users)
                if info.st_uid != os.getuid() or info.st_mode & 0o022:
                    raise SkillError("User Skill directory must be owned by this user and not writable by others.")
                yield users

    def read(self, identifier: str) -> Skill:
        origin, name = _identifier(identifier)
        if origin == "system":
            bundle = self.verify_system()
            if name not in bundle:
                raise SkillError("Unknown system Skill.")
            return bundle[name]
        with self._users() as folder:
            content = _read(folder, name + ".md", MAX_SKILL_BYTES).decode("utf-8")
        # A user file cannot grant itself a system origin or core role.
        return Skill(f"user:{name}", "editing", True, content)

    def save(self, identifier: str, content: str) -> Skill:
        origin, name = _identifier(identifier)
        if origin != "user":
            raise SkillError("System Skills are read-only; create a user Skill instead.")
        data = content.encode("utf-8")
        if not content.strip() or len(data) > MAX_SKILL_BYTES:
            raise SkillError("User Skill must be nonempty and within the size limit.")
        with self._users(create=True) as folder:
            _atomic_write(folder, name + ".md", data)
        return Skill(f"user:{name}", "editing", True, content)

    def delete(self, identifier: str) -> None:
        origin, name = _identifier(identifier)
        if origin != "user":
            raise SkillError("System Skills cannot be deleted through the Skill store.")
        with self._users() as folder:
            _existing_regular(folder, name + ".md")
            os.unlink(name + ".md", dir_fd=folder)

    def list(self) -> list[Skill]:
        result = list(self.verify_system().values())
        with ExitStack() as stack:
            try:
                folder = stack.enter_context(self._users())
            except FileNotFoundError:
                return result
            for filename in sorted(os.listdir(folder)):
                if filename.startswith(".write-"):
                    continue
                if not filename.endswith(".md"):
                    raise SkillError("Unexpected entry in user Skill storage.")
                name = _name(filename[:-3])
                content = _read(folder, filename, MAX_SKILL_BYTES).decode("utf-8")
                result.append(Skill(f"user:{name}", "editing", True, content))
        return result

    def lock_system(self) -> int:
        bundle = self.verify_system()
        with _root(self.root) as repo, _directory(repo, "skills") as skills:
            with _directory(skills, "system") as system:
                for name in bundle:
                    with _directory(system, name) as folder:
                        fd = os.open("SKILL.md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
                        try:
                            info = os.fstat(fd)
                            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                                raise SkillError("Cannot lock a non-regular system Skill.")
                            os.fchmod(fd, 0o444)
                        finally:
                            os.close(fd)
                        os.fchmod(folder, 0o555)
                os.fchmod(system, 0o555)
        self.verify_system()
        return len(bundle)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("verify", help="Verify the signed system bundle.")
    commands.add_parser("list", help="List authoritative identities and editability.")
    commands.add_parser("lock-system", help="Verify, then set local read-only permissions.")
    commands.add_parser("read", help="Return verified content and identity.").add_argument("id")
    save = commands.add_parser("save", help="Save only to user Skill storage.")
    save.add_argument("id")
    save.add_argument("--file", type=Path, required=True)
    commands.add_parser("delete", help="Delete only a user Skill.").add_argument("id")
    args = parser.parse_args(argv)
    store = SkillStore()
    try:
        if args.command == "verify":
            print(f"Verified {len(store.verify_system())} signed system Skills.")
        elif args.command == "list":
            items = [{k: v for k, v in asdict(s).items() if k != "content"} for s in store.list()]
            print(json.dumps(items, ensure_ascii=False, indent=2))
        elif args.command == "read":
            print(json.dumps(asdict(store.read(args.id)), ensure_ascii=False, indent=2))
        elif args.command == "save":
            origin, _ = _identifier(args.id)
            if origin != "user":
                raise SkillError("System Skills are read-only.")
            with _root(args.file.absolute().parent) as parent:
                data = _read(parent, args.file.name, MAX_SKILL_BYTES)
            saved = store.save(args.id, data.decode("utf-8"))
            print(f"Saved {saved.id}")
        elif args.command == "delete":
            store.delete(args.id)
            print(f"Deleted {args.id}")
        elif args.command == "lock-system":
            print(f"Applied local read-only permissions to {store.lock_system()} system Skills.")
        return 0
    except (SkillError, OSError, UnicodeError, ImportError) as error:
        print(f"Skill operation refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
