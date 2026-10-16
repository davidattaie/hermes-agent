"""API-server per-request phase timing: one ``api_request_timing`` line per agent-running request.

Behaviour contract: a chat/completions request (stream and non-stream) and a rejected request
each produce exactly one JSON timing line carrying ordered monotonic marks, the model and tool
calls the agent reported (including from a thread that does not inherit contextvars), and no
prompt/response/secret text. Real adapter, real aiohttp server, only the agent is faked.
"""

import json
import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent import request_timing
from agent.request_timing import RequestTimer
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter

LOGGER = "gateway.api_server.timing"
SECRET_PROMPT = "PROMPT-TEXT-MUST-NOT-LEAK"
SECRET_REPLY = "REPLY-TEXT-MUST-NOT-LEAK"
API_KEY = "sk-timing-test-key"


def _adapter() -> APIServerAdapter:
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": API_KEY}))
    adapter._memory_sessions = MagicMock()  # parked-provider bookkeeping is not under test
    return adapter


def _app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    return app


def _fake_agent():
    """An agent whose turn reports through the same hooks the real loop uses."""
    agent = MagicMock()
    agent.session_id = "sess-timing"
    agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 1

    def run_conversation(**kwargs):
        request_timing.mark_agent(agent, "turn_start", once=True)
        with request_timing.span("system_prompt_build", agent):
            time.sleep(0.01)
        request_timing.note_model_call(agent, 0.05, 120, 7, 100, 20)
        # Tool executor workers are plain threads: the agent-pinned timer must reach them.
        worker = threading.Thread(target=request_timing.note_tool_call, args=(agent, "terminal", 0.03, True))
        worker.start()
        worker.join()
        request_timing.note_model_call(agent, 0.04, 150, 9, 140, 0)
        request_timing.mark_agent(agent, "turn_end")
        return {"final_response": SECRET_REPLY, "messages": [], "api_calls": 2}

    agent.run_conversation.side_effect = run_conversation
    return agent


def _with_agent(adapter, agent):
    """Replace the agent factory's body but keep its production timing decorator."""
    factory = request_timing.timed_agent_build(lambda self, **kwargs: agent)
    return patch.object(type(adapter), "_create_agent", factory)


def _timing_lines(caplog) -> list[dict]:
    out = []
    for rec in caplog.records:
        msg = rec.getMessage()
        if rec.name == LOGGER and msg.startswith("api_request_timing "):
            out.append(json.loads(msg.split(" ", 1)[1]))
    return out


async def _post(adapter, payload, headers=None):
    async with TestClient(TestServer(_app(adapter))) as cli:
        resp = await cli.post(
            "/v1/chat/completions", json=payload,
            headers={"Authorization": f"Bearer {API_KEY}", **(headers or {})})
        body = await resp.read()
        return resp.status, body


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_chat_completion_emits_one_ordered_timing_line(stream, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    adapter = _adapter()
    with _with_agent(adapter, _fake_agent()):
        status, body = await _post(adapter, {
            "model": "hermes", "stream": stream,
            "messages": [{"role": "user", "content": SECRET_PROMPT}]})
    assert status == 200 and SECRET_REPLY.encode() in body

    lines = _timing_lines(caplog)
    assert len(lines) == 1
    t = lines[0]
    assert t["route"] == "/v1/chat/completions" and t["stream"] is stream
    assert t["status"] == "ok" and t["http"] == 200
    assert t["session"]
    m = t["marks_ms"]
    order = ["received", "auth_done", "worker_submit", "worker_start", "agent_built",
             "turn_start", "turn_end", "response_complete"]
    assert all(k in m for k in order + ["first_byte"]), m
    assert [m[k] for k in order] == sorted(m[k] for k in order)
    # A stream opens (headers + role chunk) before the agent runs; a JSON body leaves after it.
    assert (m["first_byte"] <= m["worker_submit"]) if stream else (m["first_byte"] >= m["turn_end"])
    assert m["first_byte"] <= m["response_complete"]
    assert t["total_ms"] >= m["response_complete"] - 0.2

    assert [c["in"] for c in t["model_calls"]] == [120, 150]
    assert t["model_calls"][0]["cache_read"] == 100 and t["model_calls"][0]["cache_write"] == 20
    assert [(c["name"], c["ok"]) for c in t["tool_calls"]] == [("terminal", True)]
    assert t["spans_ms"]["system_prompt_build"] >= 10
    assert "agent_build" in t["spans_ms"]
    p = t["phases_ms"]
    assert set(p) >= {"setup", "turn", "model", "tools", "turn_other", "response"}
    assert p["model"] == pytest.approx(90, abs=1) and p["tools"] == pytest.approx(30, abs=1)


@pytest.mark.asyncio
async def test_timing_line_carries_no_content_or_secrets(caplog):
    caplog.set_level(logging.DEBUG)
    adapter = _adapter()
    with _with_agent(adapter, _fake_agent()):
        await _post(adapter, {"model": "hermes", "messages": [{"role": "user", "content": SECRET_PROMPT}]})
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("api_request_timing "))
    for needle in (SECRET_PROMPT, SECRET_REPLY, API_KEY):
        assert needle not in line


@pytest.mark.asyncio
async def test_rejected_request_still_logs_timing(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    adapter = _adapter()
    async with TestClient(TestServer(_app(adapter))) as cli:
        resp = await cli.post("/v1/chat/completions", json={"messages": []},
                              headers={"Authorization": "Bearer wrong"})
        await resp.read()
    (t,) = _timing_lines(caplog)
    assert resp.status == 401 and t["http"] == 401 and t["status"] == "http_error"
    assert "auth_done" in t["marks_ms"] and "turn_start" not in t["marks_ms"]


def test_hooks_are_noops_without_a_timer():
    agent = object()
    request_timing.note_model_call(agent, 1.0, 1, 1, 0, 0)
    request_timing.note_tool_call(agent, "x", 1.0)
    request_timing.mark_agent(agent, "turn_start")
    request_timing.pin_to_agent(agent)
    with request_timing.span("system_prompt_build", agent):
        pass
    assert not hasattr(agent, "_request_timer")


def test_finish_is_idempotent_and_summary_phases_add_up(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    t = RequestTimer("/v1/x")
    t.marks.update({"turn_start": 0.5, "turn_end": 2.5, "response_complete": 3.0})
    t.add_model_call(1.0, 10, 2, 0, 0)
    t.add_tool_call("terminal", 0.5)
    t.finish()
    t.finish()
    assert len(_timing_lines(caplog)) == 1
    p = t.summary()["phases_ms"]
    assert p == {"setup": 500.0, "turn": 2000.0, "model": 1000.0, "tools": 500.0,
                 "turn_other": 500.0, "response": 500.0}
