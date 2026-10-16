"""Admission of API-server turns: drain check + pending-work reservation + request timer.

Split out of ``api_server.py`` (the facade) so the HTTP edge of a turn has one home. Only
``api_server.py`` imports these; nothing here reaches back into it.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Optional

from agent import request_timing
from gateway.platforms import api_server_runs as _api_runs
from gateway.platforms import api_server_timing as _api_timing


_api_agent_request_reservation: ContextVar[Optional[dict[str, bool]]] = ContextVar(
    "api_agent_request_reservation", default=None)


def _admit_api_agent_request(handler):
    """Reserve an authenticated API turn before its handler first awaits: drain check +
    reservation in one non-awaiting block so a request admitted just before shutdown can't go
    invisible while parsing its body. The mutable reservation releases the slot exactly once."""
    @wraps(handler)
    async def _wrapped(self, request, *args, **kwargs):
        # Per-request phase timer (agent/request_timing.py): one `api_request_timing` log line.
        timer = _api_timing.begin(request)
        timing_token = request_timing.activate(timer)
        try:
            resp = await _admit_and_run(self, handler, request, timer, *args, **kwargs)
        except BaseException:
            timer.finish("error")
            raise
        finally:
            request_timing.deactivate(timing_token)
        _api_timing.hook_response(timer, resp, finish=not timer.deferred)
        return resp
    return _wrapped


async def _admit_and_run(self, handler, request, timer, *args, **kwargs):
    auth_err = (
        self._check_run_auth(request, permission="dispatch")
        if _api_runs._uses_room_run_auth(self, request)
        else self._check_auth(request))
    timer.mark("auth_done")
    if auth_err:
        return auth_err
    draining = self._draining_response()
    if draining is not None:
        return draining
    reservation = {"active": True}
    token = _api_agent_request_reservation.set(reservation)
    self._pending_agent_requests += 1
    try:
        return await handler(self, request, *args, **kwargs)
    finally:
        _release_pending_api_work(self, reservation)
        _api_agent_request_reservation.reset(token)


def _release_pending_api_work(adapter, reservation: dict[str, bool]) -> None:
    """Release a pending-work reservation exactly once."""
    if reservation["active"]:
        reservation["active"] = False
        adapter._pending_agent_requests = max(0, adapter._pending_agent_requests - 1)


@contextmanager
def _reserve_pending_api_work(adapter):
    """Keep externally-triggered background work visible across awaits; a handler may detach
    the reservation to a task whose done callback then owns release."""
    reservation = {"active": True, "detached": False}
    adapter._pending_agent_requests += 1
    try:
        yield reservation
    finally:
        if not reservation["detached"]:
            _release_pending_api_work(adapter, reservation)
