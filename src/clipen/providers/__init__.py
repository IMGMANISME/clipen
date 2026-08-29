"""Bounded adapters for AI providers."""

from .codex import (
    CancellationToken,
    CodexAttempt,
    CodexAttemptResult,
    CodexCapabilities,
    CodexExecAdapter,
    CodexProbeResult,
    ProviderAuthenticationError,
    ProviderCancelled,
    ProviderNetworkError,
    ProviderPermissionError,
    ProviderProtocolError,
    ProviderTimedOut,
    ProviderUnavailable,
    ProviderUsageLimitError,
)

__all__ = [
    "CancellationToken",
    "CodexAttempt",
    "CodexAttemptResult",
    "CodexCapabilities",
    "CodexExecAdapter",
    "CodexProbeResult",
    "ProviderAuthenticationError",
    "ProviderCancelled",
    "ProviderNetworkError",
    "ProviderPermissionError",
    "ProviderProtocolError",
    "ProviderTimedOut",
    "ProviderUnavailable",
    "ProviderUsageLimitError",
]
