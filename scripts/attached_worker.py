"""Experimental lifecycle boundary. Transport owns bounded API and process I/O.
No provider or launcher is started here. Caller supplies the existing server
transport and holds the repository ownership lock when stopping the session.
"""
import uuid
from contextlib import nullcontext
from timeout_adapter import stop_attached

def run_attached(transport, worker_id, prompt, *, persist_guard, clear_guard, ownership_context=nullcontext):
    session_id='ses_'+uuid.uuid4().hex
    # Guard before create: a network failure can hide a successful server create.
    persist_guard(session_id)
    checkpoint=''
    transport.stop_receipt_reason='not-yet-stopped'
    phase='create'
    try:
        transport.create(session_id, worker_id)
        phase='execute'
        result=transport.execute(session_id, prompt)
        status=result['status'];checkpoint=result.get('checkpoint','')
        if status not in {'completed','failed','timeout','idle-timeout','killed'}:
            return 'quarantined',session_id,checkpoint
        phase='stop'
        with ownership_context():
            receipt=stop_attached(worker_id,session_id,
                owns_session=transport.owns_session, interrupt=transport.interrupt,
                is_idle=transport.is_idle, wait_client=transport.wait_client,
                terminate_client=transport.terminate_client)
            transport.stop_receipt_reason=receipt.reason
            if not receipt.release_allowed:
                return 'quarantined',session_id,checkpoint
            clear_guard(session_id)
        return status,session_id,checkpoint
    except Exception as exc:
        transport.stop_receipt_reason=phase+'-'+type(exc).__name__
        if phase=='create' and hasattr(transport,'confirm_unstarted'):
            try:
                with ownership_context():
                    if transport.owns_session(worker_id,session_id) and transport.confirm_unstarted(session_id):
                        clear_guard(session_id)
                        transport.stop_receipt_reason='create-failed-confirmed-unstarted'
                        return 'failed',session_id,checkpoint
            except Exception:
                pass  # Unknown ownership/state keeps the guard.
        # Unknown server ownership is never converted to ordinary retry/release.
        return 'quarantined',session_id,checkpoint
