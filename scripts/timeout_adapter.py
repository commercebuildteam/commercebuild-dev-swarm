"""Staged attached-client timeout handling; not imported by the live launcher.
Caller must hold its worker ownership lock across this operation. A false
release_allowed result MUST quarantine the claim and prevent redispatch.
Never signal the shared server or another session. HTTP implementation must
apply bounded timeouts and must not expose authentication material in receipts.
"""
from dataclasses import dataclass

@dataclass(frozen=True)
class StopReceipt:
    release_allowed: bool
    reason: str

def stop_attached(worker_id, session_id, *, owns_session, interrupt, is_idle,
                  wait_client, terminate_client):
    if not session_id or not owns_session(worker_id, session_id):
        return StopReceipt(False, 'session-ownership-unverified')
    try:
        reply = interrupt(session_id)
        if not isinstance(reply, dict) or type(reply.get('interrupted')) is not bool:
            return StopReceipt(False, 'invalid-interrupt-response')
        # interrupted=false is legitimate only for an independently idle session.
        if not is_idle(session_id):
            return StopReceipt(False, 'session-still-active')
        if not owns_session(worker_id, session_id):
            return StopReceipt(False, 'session-ownership-changed')
        if not wait_client():
            terminate_client()  # Client process only, never server process group.
            if not wait_client():
                return StopReceipt(False, 'client-exit-unconfirmed')
        if not is_idle(session_id):
            return StopReceipt(False, 'session-reactivated')
        return StopReceipt(True, 'session-idle-client-exited')
    except Exception:
        # Preserve ownership on transport/auth failures; never include raw exception
        # messages, which may contain credentials or request URLs.
        return StopReceipt(False, 'cancellation-unconfirmed')
