"""A profile-scope plugin offering dashboard auth that the launch scope owns is expected noise.

Every non-launch profile that bundles the ``basic`` provider hits this on each start; at WARNING it
buried real errors in every profile's errors.log. The registration must still be refused, and the
launch scope must still register for real.
"""
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hermes_cli import plugins as plugins_mod
from hermes_cli.dashboard_auth.base import DashboardAuthProvider


def _ctx(scope_key):
    manager = MagicMock()
    manager.scope_key = scope_key
    return plugins_mod.PluginContext(SimpleNamespace(name="basic"), manager)


@pytest.fixture
def launch_scope(monkeypatch):
    monkeypatch.setattr(plugins_mod, "get_process_hermes_home", lambda: "launch-home")
    monkeypatch.setattr(plugins_mod, "hermes_home_key", lambda home: "launch-scope")


def _provider():
    provider = MagicMock(spec=DashboardAuthProvider)
    provider.name = "basic"
    return provider


def test_cross_scope_registration_is_refused_without_warning(launch_scope, monkeypatch, caplog):
    registered = MagicMock()
    monkeypatch.setattr("hermes_cli.dashboard_auth.registry.register_global_provider", registered)
    with caplog.at_level(logging.DEBUG, logger=plugins_mod.logger.name):
        result = _ctx("profile-scope").register_dashboard_auth_provider(_provider())
    assert result is None
    registered.assert_not_called()
    msgs = [r for r in caplog.records if "dashboard-auth provider" in r.getMessage()]
    assert msgs, "the refusal should still be logged at debug"
    assert all(r.levelno == logging.DEBUG for r in msgs)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_launch_scope_still_registers_dashboard_auth(launch_scope, monkeypatch):
    registered = MagicMock()
    monkeypatch.setattr("hermes_cli.dashboard_auth.registry.register_global_provider", registered)
    provider = _provider()
    _ctx("launch-scope").register_dashboard_auth_provider(provider)
    registered.assert_called_once_with(provider)
