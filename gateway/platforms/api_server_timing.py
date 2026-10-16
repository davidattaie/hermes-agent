"""HTTP-edge half of API-server request timing (the recorder is ``agent/request_timing.py``).

``_admit_api_agent_request`` opens a timer per agent-running request; this module owns the
aiohttp-specific parts: naming the route, hooking response ``prepare``/``write_eof`` so
"first byte" and "response complete" are measured where bytes actually leave (after the handler
returns), and the SSE first-write hook. Instrumentation only: nothing here changes a response.
"""

from __future__ import annotations

import contextlib
import functools
from typing import Any

from agent import request_timing
from agent.request_timing import RequestTimer


def begin(request: Any) -> RequestTimer:
    """New timer for ``request`` (mark ``received`` = now); the caller activates it."""
    route = ""
    with contextlib.suppress(Exception):
        route = request.match_info.route.resource.canonical  # template, never the raw path/ids
    return RequestTimer(route or "unknown", getattr(request, "method", "POST"))


def _wrap(resp: Any, name: str, before=None, after=None) -> None:
    """Instance-level wrapper around an async response method; ``after(failed)`` always runs."""
    original = getattr(resp, name)

    @functools.wraps(original)
    async def _timed(*args, **kwargs):
        if before is not None:
            before()
        failed = True
        try:
            result = await original(*args, **kwargs)
            failed = False
            return result
        finally:
            if after is not None:
                after(failed)
    setattr(resp, name, _timed)


def _final_status(timer: RequestTimer, failed: bool) -> str:
    if failed:
        return "write_failed"  # client went away mid-response
    return "ok" if (timer.http_status or 200) < 400 else "http_error"


def hook_response(timer: RequestTimer, resp: Any, *, finish: bool = True) -> None:
    """Mark ``first_byte`` at prepare/first write and finish the timer at ``write_eof``.

    aiohttp calls ``write_eof`` after the handler returns for every response (stream and
    non-stream), so ``response_complete`` is the last-byte time, not handler-return time.
    ``finish=False`` (``/v1/runs``: a background task ends the request) only marks first byte.
    """
    timer.http_status = getattr(resp, "status", None)
    with contextlib.suppress(Exception):
        if not getattr(resp, "prepared", False):
            _wrap(resp, "prepare", before=lambda: timer.mark_once("first_byte"))
        if finish:
            def _done(failed: bool) -> None:
                timer.mark_once("response_complete")
                timer.finish(_final_status(timer, failed))
            _wrap(resp, "write_eof", before=lambda: timer.mark_once("first_byte"), after=_done)


def hook_stream_first_write(response: Any) -> None:
    """Mark ``first_byte`` on the first body write of an already-prepared SSE response."""
    timer = request_timing.current()
    if timer is None:
        return
    timer.stream = True
    timer.mark_once("stream_open")
    original = response.write

    async def _first_write(data, *args, **kwargs):
        try:
            return await original(data, *args, **kwargs)
        finally:
            timer.mark_once("first_byte")
            response.write = original
    with contextlib.suppress(Exception):
        response.write = _first_write
