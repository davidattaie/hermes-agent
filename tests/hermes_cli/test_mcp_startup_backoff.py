"""Discovery is quiet without servers and rate-limited after failed connections."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli import config, mcp_startup as startup


@pytest.fixture
def discovery(monkeypatch):
    state = SimpleNamespace(config={}, now=100.0, threads=[], connected=False, pending=[])
    monkeypatch.setattr(startup, "_mcp_discovery_started", set())
    monkeypatch.setattr(startup, "_mcp_discovery_thread", {})
    monkeypatch.setattr(startup, "_mcp_server_filter", None)
    monkeypatch.setattr(startup, "_mcp_discovery_backoff", {}, raising=False)
    monkeypatch.setattr(startup, "_monotonic", lambda: state.now, raising=False)
    monkeypatch.setattr(config, "read_raw_config", lambda: state.config)
    monkeypatch.setattr(startup, "_any_mcp_connected", lambda: state.connected)
    monkeypatch.setattr(startup, "_servers_awaiting_connect", lambda: state.pending)
    monkeypatch.setattr(startup, "_discover_mcp_tools_without_interactive_oauth", lambda: None)
    from hermes_cli import agent_plugins
    monkeypatch.setattr(agent_plugins, "has_enabled_agent_plugin_mcp", lambda raw: False)

    class Thread:
        def __init__(self, *, target, args, **kwargs):
            self.target, self.args = target, args
            self.alive = False
            state.threads.append(self)

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def finish(self):
            self.target(*self.args)
            self.alive = False

    monkeypatch.setattr(startup.threading, "Thread", Thread)
    state.logger = Mock()
    state.start = lambda: startup.start_background_mcp_discovery(logger=state.logger, thread_name="test")
    return state


@pytest.mark.parametrize("raw,allow", [({}, None), ({"mcp_servers": {}}, None),
    ({"mcp_servers": {"demo": {"enabled": False}}}, None),
    ({"mcp_servers": {"demo": {"command": "demo"}}}, ["terminal"])])
def test_no_eligible_servers_remain_quiet_and_unstarted(discovery, monkeypatch, raw, allow):
    discovery.config = raw
    monkeypatch.setattr(startup, "_mcp_server_filter", allow)
    for _ in range(4):
        discovery.start()
    assert discovery.threads == []
    assert not startup._mcp_discovery_started
    discovery.logger.warning.assert_not_called()


def test_server_added_after_empty_config_starts_next_build(discovery):
    discovery.start()
    discovery.start()
    discovery.config = {"mcp_servers": {"demo": {"command": "demo"}}}
    discovery.connected = True
    discovery.start()
    assert len(discovery.threads) == 1
    discovery.threads[-1].finish()
    discovery.start()
    assert len(discovery.threads) == 1
    discovery.logger.warning.assert_not_called()


SERVERS = {"mcp_servers": {"demo": {"command": "demo"}}}


def _retry_warnings(discovery):
    return [c for c in discovery.logger.warning.call_args_list if "retrying discovery thread" in c.args[0]]


def _run_failed_discovery(discovery):
    discovery.start()
    discovery.threads[-1].finish()


def test_failing_servers_back_off_exponentially_up_to_the_cap(discovery):
    discovery.config = SERVERS
    _run_failed_discovery(discovery)  # first run, no warning yet
    spawned, delays = [len(discovery.threads)], []
    for _ in range(9):
        discovery.start()
        discovery.start()  # same instant: inside the window, nothing spawned or logged
        spawned.append(len(discovery.threads))
        discovery.threads[-1].finish()
        delays.append(_retry_warnings(discovery)[-1].args[-1])
        discovery.now += delays[-1] - 1
        discovery.start()  # one second early: still backing off
        assert len(discovery.threads) == spawned[-1]
        discovery.now += 1
    assert spawned == list(range(1, 11))
    assert delays == [30, 60, 120, 240, 480, 960, 1920, 3600, 3600]
    assert [c.args[-2] for c in _retry_warnings(discovery)] == list(range(1, 10))


def test_backoff_resets_when_servers_connect(discovery):
    discovery.config = SERVERS
    _run_failed_discovery(discovery)
    discovery.start()
    discovery.threads[-1].finish()
    discovery.now += 30
    discovery.connected = True
    discovery.start()  # something connected: nothing pending, no retry
    assert len(discovery.threads) == 2
    discovery.connected = False
    discovery.start()  # failing again: the schedule starts over at attempt 1
    assert _retry_warnings(discovery)[-1].args[-2:] == (1, 30)


def test_backoff_resets_when_configured_servers_change(discovery):
    discovery.config = SERVERS
    _run_failed_discovery(discovery)
    discovery.start()
    discovery.threads[-1].finish()
    assert _retry_warnings(discovery)[-1].args[-2:] == (1, 30)
    discovery.config = {"mcp_servers": {"demo": {"command": "demo"}, "other": {"command": "o"}}}
    discovery.start()  # inside the old window, but the new server restarts the schedule
    assert len(discovery.threads) == 3
    assert _retry_warnings(discovery)[-1].args[-2:] == (1, 30)


def test_disabled_servers_do_not_count_toward_the_server_set(discovery):
    discovery.config = {"mcp_servers": {"off": {"enabled": False}, "on": {"command": "x"}}}
    discovery.start()
    assert len(discovery.threads) == 1
    assert startup._configured_mcp_servers() == frozenset({"on"})


def test_agent_plugin_mcp_still_opens_the_gate(discovery, monkeypatch):
    from hermes_cli import agent_plugins
    monkeypatch.setattr(agent_plugins, "has_enabled_agent_plugin_mcp", lambda raw: True)
    discovery.start()
    assert len(discovery.threads) == 1


def test_unreadable_config_is_conservative(discovery, monkeypatch):
    monkeypatch.setattr(config, "read_raw_config", Mock(side_effect=OSError))
    assert startup._has_configured_mcp_servers() is True
    discovery.start()
    assert len(discovery.threads) == 1


def test_all_lazy_servers_are_not_retried(discovery, monkeypatch):
    """#111717: a lazy registration is usable, so the run neither warns nor re-spawns."""
    lazy = [{"name": "demo", "connected": False, "status": "lazy"}]
    monkeypatch.setattr(startup, "_any_mcp_connected", lambda: startup._discovery_registered_servers(lazy))
    discovery.config = SERVERS
    discovery.start()
    discovery.threads[-1].finish()
    for _ in range(3):
        discovery.start()
    assert len(discovery.threads) == 1
    discovery.logger.warning.assert_not_called()


def test_connected_but_pending_server_runs_discovery_without_backoff(discovery):
    discovery.config = SERVERS
    discovery.start()
    discovery.threads[-1].finish()
    discovery.connected, discovery.pending = True, ["slow"]
    discovery.start()
    discovery.threads[-1].finish()
    discovery.start()  # pending again at the same instant: no window applies to this path (#76954)
    assert len(discovery.threads) == 3
    assert discovery.logger.info.call_count == 2
    assert _retry_warnings(discovery) == []
