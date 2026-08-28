"""Read-only Codex binding probe. No daemon start, session resume, or model work.

Not enabled by product startup until the native lifecycle handoff is verified.
The transport must be supplied explicitly by an adapter; no "latest session",
directory search, credential discovery, or implicit app-server creation.
"""
import json

from .store import Rejected


def proof_from_result(response):
    if not isinstance(response, dict) or response.get('isError') is True:
        raise Rejected('provider_probe_rejected', 502)
    content = response.get('content')
    if (not isinstance(content, list) or len(content) != 1
            or not isinstance(content[0], dict) or content[0].get('type') != 'text'):
        raise Rejected('invalid_provider_response', 502)
    raw = content[0].get('text')
    if not isinstance(raw, str) or len(raw) > 1024:
        raise Rejected('invalid_provider_response', 502)
    try:
        result = json.loads(raw)
    except (ValueError, RecursionError):
        raise Rejected('invalid_provider_response', 502) from None
    if not isinstance(result, dict) or set(result) != {'proof'} or not isinstance(result['proof'], str):
        raise Rejected('invalid_provider_response', 502)
    return result['proof']


def verify_mcp_route(runtime, candidate, request):
    """`request` belongs to an already-connected, explicitly selected owner.

    This proves only the MCP route, not the native lifecycle identity. A future
    adapter must also authenticate the lifecycle/terminal source; neither this
    function nor the UI can promote a route proof to provider_session_verified.
    Model arguments, HTTP input, and saved state must never call it.
    Calls are bounded by the adapter's transport timeout.
    No runtime lock is held while waiting on MCP: its answer uses that lock.
    """
    challenge = runtime.begin_session_check(candidate)
    try:
        candidate.check_thread(request('thread/read', {'threadId': candidate.thread_id, 'includeTurns': False}))
        response = request('mcpServer/tool/call', {
            'threadId': candidate.thread_id, 'server': 'clipen',
            'tool': 'clipen_session_probe', 'arguments': {'challenge': challenge},
        })
        proof = proof_from_result(response)
        # Recheck after the round trip: a thread can be unloaded or changed while
        # tool approval is pending. finish() also checks the local lifetime/TTL.
        candidate.check_thread(request('thread/read', {'threadId': candidate.thread_id, 'includeTurns': False}))
        runtime.finish_session_check(challenge, proof)
    except Rejected as error:
        runtime.fail_session_check(challenge, error.code)
        raise
    except Exception:
        runtime.fail_session_check(challenge, 'provider_control_unavailable')
        raise Rejected('provider_control_unavailable', 503) from None
