"""Per-request phase timing for API-server turns (instrumentation only).

One ``RequestTimer`` per HTTP request records monotonic-clock marks (request received, auth,
agent built, turn start/end, first byte, response complete), accumulated spans (agent build,
memory-provider activation, system-prompt build, memory prefetch), every model call (latency +
token counts) and every tool call (name + duration). ``finish()`` emits exactly one INFO line,
``api_request_timing {json}``, to ``agent.log`` and ``gateway.log``.

Content policy: the line carries ids, route, tool *names*, durations and token counts only.
No prompt, response, header, argument or tool-result text ever reaches a timer.

The timer rides a ContextVar (event-loop task and the ``_run_agent`` worker thread, which
re-enters it) and is pinned on the agent as ``agent._request_timer`` so tool-executor worker
threads, which do not inherit contextvars, still reach it. Every hook is a no-op without a
timer, so CLI/TUI/gateway-platform turns pay one ``getattr`` and nothing else.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import logging
import threading
import time
import uuid
from typing import Any, Iterator, Optional

# Named under ``gateway.`` so the line lands in gateway.log as well as agent.log.
logger = logging.getLogger("gateway.api_server.timing")

_MAX_LISTED_CALLS = 64
_current: contextvars.ContextVar[Optional["RequestTimer"]] = contextvars.ContextVar(
    "api_request_timer", default=None)


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 1)


class RequestTimer:
    """Collects one request's timings; thread-safe appends, idempotent ``finish()``."""

    def __init__(self, route: str, method: str = "POST", request_id: Optional[str] = None) -> None:
        self.t0 = time.perf_counter()
        self.request_id = request_id or f"req_{uuid.uuid4().hex[:12]}"
        self.route = route
        self.method = method
        self.session_id = ""
        self.stream = False
        self.http_status: Optional[int] = None
        self.marks: dict[str, float] = {"received": 0.0}  # name -> seconds since t0
        self.spans: dict[str, float] = {}  # name -> accumulated seconds
        self.model_calls: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.deferred = False  # a background task, not the HTTP handler, ends this request
        self._lock = threading.Lock()
        self._finished = False

    def now(self) -> float:
        return time.perf_counter() - self.t0

    def mark(self, name: str) -> None:
        self.marks[name] = self.now()

    def mark_once(self, name: str) -> None:
        if name not in self.marks:
            self.marks[name] = self.now()

    def add_span(self, name: str, seconds: float) -> None:
        with self._lock:
            self.spans[name] = self.spans.get(name, 0.0) + seconds

    @contextlib.contextmanager
    def span(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.add_span(name, time.perf_counter() - started)

    def set_session(self, session_id: Optional[str]) -> None:
        if session_id:
            self.session_id = str(session_id)

    def add_model_call(self, seconds: float, tokens_in: Optional[int] = None, tokens_out: Optional[int] = None,
                       cache_read: Optional[int] = None, cache_write: Optional[int] = None) -> None:
        self.model_calls.append({
            "at": self.now(), "s": seconds, "in": tokens_in, "out": tokens_out,
            "cache_read": cache_read, "cache_write": cache_write})

    def add_tool_call(self, name: str, seconds: float, ok: bool = True) -> None:
        self.tool_calls.append({"at": self.now(), "name": str(name), "s": seconds, "ok": bool(ok)})

    def summary(self, status: str = "ok") -> dict[str, Any]:
        """The structured payload (also what the log line serialises)."""
        marks = dict(self.marks)
        total = marks.get("response_complete", self.now())
        model_s = sum(c["s"] for c in self.model_calls)
        tool_s = sum(c["s"] for c in self.tool_calls)
        turn_start, turn_end = marks.get("turn_start"), marks.get("turn_end")
        phases: dict[str, float] = {}
        if turn_start is not None:
            phases["setup"] = _ms(turn_start)
        if turn_start is not None and turn_end is not None:
            turn_s = max(turn_end - turn_start, 0.0)
            phases["turn"] = _ms(turn_s)
            phases["model"] = _ms(model_s)
            phases["tools"] = _ms(tool_s)  # sum of durations; concurrent tools overlap
            phases["turn_other"] = _ms(max(turn_s - model_s - tool_s, 0.0))
        if turn_end is not None:
            phases["response"] = _ms(max(total - turn_end, 0.0))
        calls_m, calls_t = self.model_calls[:_MAX_LISTED_CALLS], self.tool_calls[:_MAX_LISTED_CALLS]
        out: dict[str, Any] = {
            "rid": self.request_id, "route": self.route, "method": self.method,
            "session": self.session_id, "stream": self.stream, "status": status,
            "http": self.http_status, "total_ms": _ms(total),
            "marks_ms": {k: _ms(v) for k, v in marks.items()},
            "spans_ms": {k: _ms(v) for k, v in self.spans.items()},
            "phases_ms": phases,
            "model_calls": [
                {"n": i + 1, "at_ms": _ms(c["at"]), "ms": _ms(c["s"]), "in": c["in"], "out": c["out"],
                 "cache_read": c["cache_read"], "cache_write": c["cache_write"]}
                for i, c in enumerate(calls_m)],
            "tool_calls": [
                {"name": c["name"], "at_ms": _ms(c["at"]), "ms": _ms(c["s"]), "ok": c["ok"]} for c in calls_t],
        }
        if len(self.model_calls) > _MAX_LISTED_CALLS or len(self.tool_calls) > _MAX_LISTED_CALLS:
            out["truncated"] = {"model_calls": len(self.model_calls), "tool_calls": len(self.tool_calls)}
        return out

    def finish(self, status: str = "ok") -> None:
        """Emit the single timing line; later calls are no-ops. Never raises."""
        with self._lock:
            if self._finished:
                return
            self._finished = True
        try:
            self.marks.setdefault("response_complete", self.now())
            logger.info("api_request_timing %s", json.dumps(self.summary(status), separators=(",", ":")))
        except Exception:  # instrumentation must never break a request
            logger.debug("api_request_timing emit failed", exc_info=True)


def current() -> Optional[RequestTimer]:
    return _current.get()


def activate(timer: Optional[RequestTimer]) -> contextvars.Token:
    return _current.set(timer)


def deactivate(token: contextvars.Token) -> None:
    with contextlib.suppress(Exception):
        _current.reset(token)


def timer_for(agent: Any) -> Optional[RequestTimer]:
    """The timer for ``agent``'s turn: pinned on the agent, else the ambient one."""
    return getattr(agent, "_request_timer", None) or _current.get()


def pin_to_agent(agent: Any) -> None:
    """Make the ambient timer reachable from threads that do not inherit contextvars."""
    timer = _current.get()
    if timer is not None:
        with contextlib.suppress(Exception):
            agent._request_timer = timer


def mark(name: str, *, once: bool = False) -> None:
    """Mark on the ambient (contextvar) timer, if any."""
    timer = _current.get()
    if timer is not None:
        (timer.mark_once if once else timer.mark)(name)


def mark_agent(agent: Any, name: str, *, once: bool = False) -> None:
    timer = timer_for(agent)
    if timer is not None:
        (timer.mark_once if once else timer.mark)(name)


@contextlib.contextmanager
def span(name: str, agent: Any = None) -> Iterator[None]:
    """Accumulate a named span on the active timer; zero-cost passthrough without one."""
    timer = timer_for(agent) if agent is not None else _current.get()
    if timer is None:
        yield
        return
    with timer.span(name):
        yield


def defer_finish_to_background_task() -> None:
    """The HTTP response is only an ack (``/v1/runs``): the spawned task calls ``finish()``."""
    timer = _current.get()
    if timer is not None:
        timer.deferred = True


def timed_agent_build(build):
    """Decorator for the per-request agent factory: time it as the ``agent_build`` span, pin the
    timer to the new agent (tool-executor threads inherit no contextvars) and mark ``agent_built``."""
    @functools.wraps(build)
    def _timed(*args: Any, **kwargs: Any) -> Any:
        with span("agent_build"):
            agent = build(*args, **kwargs)
        pin_to_agent(agent)
        mark_agent(agent, "agent_built")
        return agent
    return _timed


def note_model_call(agent: Any, seconds: float, tokens_in=None, tokens_out=None,
                    cache_read=None, cache_write=None) -> None:
    timer = timer_for(agent)
    if timer is not None:
        timer.add_model_call(float(seconds), tokens_in, tokens_out, cache_read, cache_write)


def note_tool_call(agent: Any, name: str, seconds: float, ok: bool = True) -> None:
    timer = timer_for(agent)
    if timer is not None:
        timer.add_tool_call(name, float(seconds), ok)
