"""Where API keys actually live. Prefers the OS keyring; falls back to a
local file (mode 0600) when no keyring backend is available -- e.g. some
headless CI/dev-container setups. `_keyring_available()` is its own
function (not inlined) so tests can monkeypatch it to force the file
backend deterministically instead of depending on what the host machine
happens to have."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Protocol

from clipen.auth.providers import FIELD_API_KEY
from clipen.config import paths as clipen_paths

_SERVICE_NAME = "clipen"


class CredentialStore(Protocol):
    def get(self, provider_name: str, field: str) -> str | None: ...
    def set(self, provider_name: str, field: str, value: str) -> None: ...
    def delete(self, provider_name: str) -> None: ...


def _keyring_available() -> bool:
    try:
        import keyring

        keyring.get_keyring()
        return True
    except Exception:
        return False


class KeyringCredentialStore:
    def get(self, provider_name: str, field: str) -> str | None:
        import keyring

        return keyring.get_password(_SERVICE_NAME, f"{provider_name}:{field}")

    def set(self, provider_name: str, field: str, value: str) -> None:
        import keyring

        keyring.set_password(_SERVICE_NAME, f"{provider_name}:{field}", value)

    def delete(self, provider_name: str) -> None:
        import keyring

        try:
            keyring.delete_password(_SERVICE_NAME, f"{provider_name}:{FIELD_API_KEY}")
        except Exception:
            pass


class FileCredentialStore:
    def _path(self) -> Path:
        return clipen_paths.credentials_path()

    def _load(self) -> dict[str, dict[str, str]]:
        path = self._path()
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        path = self._path()
        path.write_text(json.dumps(data, indent=2))
        path.chmod(0o600)

    def get(self, provider_name: str, field: str) -> str | None:
        return self._load().get(provider_name, {}).get(field)

    def set(self, provider_name: str, field: str, value: str) -> None:
        data = self._load()
        data.setdefault(provider_name, {})[field] = value
        self._save(data)

    def delete(self, provider_name: str) -> None:
        data = self._load()
        data.pop(provider_name, None)
        self._save(data)


def resolve_credential_store(*, warn: Callable[[str], None] | None = None) -> CredentialStore:
    if _keyring_available():
        return KeyringCredentialStore()
    if warn is not None:
        warn(
            "No secure keyring backend found -- storing API keys in a local "
            f"file ({clipen_paths.credentials_path()}) instead."
        )
    return FileCredentialStore()
