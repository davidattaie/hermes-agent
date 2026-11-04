"""Stop authority beats every verification continuation; prose is not authority."""
import threading
from types import SimpleNamespace

import pytest

from agent.interrupt_control import InterruptControlMixin
from agent.turn_stop_gates import apply_stop_gates


class InterruptedAgent(InterruptControlMixin):
    def __init__(self):
        self._execution_thread_id = None
        self._active_children_lock = threading.Lock()
        self._active_children = []
        self._hard_interrupt_requested = threading.Event()
        self.quiet_mode = True


def _apply(agent, text="Owner paused work; preserving unverified checkpoint."):
    messages = []
    verdict = apply_stop_gates(
        agent, {"role": "assistant", "content": text}, final_response=text,
        messages=messages, conversation_history=[], pending_verification_response=None,
        pending_verification_response_previewed=False,
    )
    return verdict, messages


@pytest.mark.parametrize("hard", [False, True])
def test_runtime_owner_interrupt_prevents_all_stop_gate_work(monkeypatch, hard):
    agent = InterruptedAgent()
    assert agent.interrupt("Pause work", hard_cancel=hard)

    def forbidden(*args):
        pytest.fail("Stopped turn must not settle LSP, invoke hooks, or schedule verification")

    for name in ("_late_lsp_verdicts", "_verify_on_stop_nudge", "_pre_verify_nudge", "_kanban_stop_nudge"):
        monkeypatch.setattr("agent.turn_stop_gates." + name, forbidden)
    verdict, messages = _apply(agent)
    assert not verdict.continue_turn
    assert verdict.final_response.endswith("checkpoint.")
    assert messages == []
    assert not hasattr(agent, "_verification_stop_nudges")


@pytest.mark.parametrize("gate", ["_late_lsp_verdicts", "_verify_on_stop_nudge", "_pre_verify_nudge", "_kanban_stop_nudge"])
def test_stop_arriving_during_gate_cannot_schedule_continuation(monkeypatch, gate):
    agent = InterruptedAgent()
    for name in ("_late_lsp_verdicts", "_verify_on_stop_nudge", "_pre_verify_nudge", "_kanban_stop_nudge"):
        monkeypatch.setattr("agent.turn_stop_gates." + name, lambda *a: None)

    def interrupted(*args):
        agent.interrupt("Pause work", hard_cancel=True)
        return "must not run this follow-up"

    monkeypatch.setattr("agent.turn_stop_gates." + gate, interrupted)
    verdict, messages = _apply(agent)
    assert not verdict.continue_turn
    assert messages == []


def test_assistant_pause_prose_does_not_set_owner_authority(monkeypatch):
    agent = SimpleNamespace(
        _emit_interim_assistant_message=lambda *a: None,
        _flush_messages_to_session_db=lambda *a: None,
        _interim_content_was_streamed=lambda *a: False,
    )
    monkeypatch.setattr("agent.turn_stop_gates._late_lsp_verdicts", lambda a: None)
    monkeypatch.setattr("agent.turn_stop_gates._verify_on_stop_nudge", lambda a: "verification debt")
    verdict, messages = _apply(agent)
    assert verdict.continue_turn
    assert messages[-1]["_verification_stop_synthetic"]
