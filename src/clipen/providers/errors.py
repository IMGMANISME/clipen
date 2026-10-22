"""Provider-neutral failures shared by every AI adapter."""

from __future__ import annotations

from typing import Protocol


class ProviderUnavailable(RuntimeError):
    """The selected provider cannot be launched on this machine."""


class ProviderProtocolError(RuntimeError):
    """The provider violated the expected one-shot result contract."""


class ProviderTimedOut(RuntimeError):
    """The provider exceeded the bounded attempt duration."""


class ProviderCancelled(RuntimeError):
    """The caller cancelled the provider attempt."""


class ProviderAuthenticationError(ProviderUnavailable):
    """The provider login is missing or no longer valid."""


class ProviderUsageLimitError(RuntimeError):
    """The provider rejected the attempt because of an account limit."""


class ProviderNetworkError(RuntimeError):
    """The provider could not reach its service."""


class ProviderPermissionError(RuntimeError):
    """The provider sandbox or permission contract was rejected."""


class CancellationToken(Protocol):
    """Minimal cancellation contract accepted by the adapter."""

    def is_set(self) -> bool: ...
