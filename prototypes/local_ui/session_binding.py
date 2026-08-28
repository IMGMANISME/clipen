"""Short-lived proof that an explicit provider thread reaches this MCP process.

This gate does not discover sessions, trust hook input, or start model turns.
A future native adapter must supply a trusted lifecycle identity and the owning
control transport. A UUID, matching directory, or readable history is not proof.
"""
from dataclasses import dataclass
from pathlib import Path
import secrets
import re
import time
from uuid import UUID

from .store import Rejected


FAILURE_REASONS = frozenset({
    'provider_probe_expired', 'provider_probe_mismatch', 'provider_probe_rejected',
    'provider_thread_mismatch', 'provider_control_unavailable', 'invalid_provider_response',
    'provider_probe_not_pending', 'provider_probe_already_used',
})


def canonical_uuid(value):
    if not isinstance(value, str):
        raise Rejected('invalid_provider_identity', 422)
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise Rejected('invalid_provider_identity', 422) from None
    return value


@dataclass(frozen=True)
class Candidate:
    # Session IDs identify a tree; thread IDs identify one thread in that tree.
    session_id: str
    thread_id: str
    workspace: Path

    def __post_init__(self):
        canonical_uuid(self.session_id)
        canonical_uuid(self.thread_id)
        if not isinstance(self.workspace, Path) or not self.workspace.is_absolute():
            raise Rejected('invalid_provider_identity', 422)

    def check_thread(self, response):
        thread = response.get('thread') if isinstance(response, dict) else None
        if not isinstance(thread, dict):
            raise Rejected('invalid_provider_response', 502)
        # Never accept saved history, a child agent, or a thread from a different
        # session tree. Path equality is only a constraint, never the proof.
        status = thread.get('status')
        if (thread.get('id') != self.thread_id or thread.get('sessionId') != self.session_id
                or 'parentThreadId' not in thread or thread['parentThreadId'] is not None
                or thread.get('agentRole') is not None
                or thread.get('agentNickname') is not None
                or thread.get('source') != 'cli'
                or thread.get('cwd') != str(self.workspace)
                or not isinstance(status, dict) or status.get('type') not in ('idle', 'active')):
            raise Rejected('provider_thread_mismatch', 409)


class SessionBinding:
    """Used only while holding Runtime.lock. Secrets are never part of a snapshot."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.state = 'transport_only'
        self.reason = 'native_session_handoff_required'
        self.candidate = None
        self._challenge = self._proof = None
        self._deadline = 0

    def begin(self, candidate):
        if self.state in ('checking', 'route_verified'):
            raise Rejected('provider_binding_already_started')
        if self.state == 'closed':
            raise Rejected('service_stopped', 503)
        if not isinstance(candidate, Candidate):
            raise Rejected('invalid_provider_identity', 422)
        self.candidate = candidate
        self.state, self.reason = 'checking', None
        self._challenge, self._proof = secrets.token_urlsafe(32), None
        self._deadline = self.clock() + 10
        return self._challenge

    def _active(self, challenge):
        if self.state != 'checking' or not isinstance(challenge, str):
            raise Rejected('provider_probe_not_pending')
        if self.clock() >= self._deadline:
            self.fail('provider_probe_expired')
            raise Rejected('provider_probe_expired')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', challenge) or not secrets.compare_digest(challenge, self._challenge):
            raise Rejected('provider_probe_mismatch')

    def answer(self, challenge):
        self._active(challenge)
        if self._proof is not None:
            raise Rejected('provider_probe_already_used')
        # An echo of a caller-provided nonce would not prove this runtime handled
        # the call. Generate a separate secret, consumed once by finish().
        self._proof = secrets.token_urlsafe(32)
        return {'proof': self._proof}

    def finish(self, challenge, proof):
        self._active(challenge)
        if (self._proof is None or not isinstance(proof, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', proof)
                or not secrets.compare_digest(proof, self._proof)):
            self.fail('provider_probe_mismatch')
            raise Rejected('provider_probe_mismatch')
        self.state, self.reason = 'route_verified', None
        self._challenge = self._proof = None

    def fail(self, reason):
        if self.state == 'closed':
            return
        self.state = 'unavailable'
        self.reason = reason if reason in FAILURE_REASONS else 'provider_control_unavailable'
        self.candidate = self._challenge = self._proof = None

    def fail_if_current(self, challenge, reason):
        # A timed-out adapter can return after a new verification has begun.
        # Its late failure must not revoke that new attempt or a finished proof.
        if (self.state == 'checking' and isinstance(challenge, str)
                and re.fullmatch(r'[A-Za-z0-9_-]{43}', challenge)
                and secrets.compare_digest(challenge, self._challenge)):
            self.fail(reason)

    def close(self):
        self.state, self.reason = 'closed', 'service_stopped'
        self.candidate = self._challenge = self._proof = None

    def snapshot(self):
        if self.state == 'checking' and self.clock() >= self._deadline:
            self.fail('provider_probe_expired')
        return {'state': self.state, 'reason': self.reason,
                'route_verified': self.state == 'route_verified',
                # The native lifecycle/terminal handoff is a separate gate.
                # A successful synthetic or control probe must not bypass it.
                'native_identity_verified': False,
                'automatic_submission': False}
