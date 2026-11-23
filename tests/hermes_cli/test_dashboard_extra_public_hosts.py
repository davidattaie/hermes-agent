"""dashboard.extra_public_hosts: further exact hostnames accepted by the Host guard."""

import pytest

from hermes_cli.dashboard_auth import prefix
from hermes_cli.web_server_boundary import _dashboard_public_hosts, _is_accepted_host


def _config(monkeypatch, public_url="", extra=None):
    section = {"public_url": public_url}
    if extra is not None:
        section["extra_public_hosts"] = extra
    monkeypatch.setattr(prefix, "_load_dashboard_section", lambda: section)
    monkeypatch.delenv("HERMES_DASHBOARD_PUBLIC_URL", raising=False)


def test_extra_host_is_accepted_next_to_the_public_url_host(monkeypatch):
    _config(monkeypatch, "http://box.tail.ts.net:9119", ["Hermes.svc.example.com"])
    trusted = _dashboard_public_hosts()
    assert trusted == {"box.tail.ts.net", "hermes.svc.example.com"}
    for host in ("hermes.svc.example.com", "hermes.svc.example.com:443", "box.tail.ts.net:9119", "100.1.2.3:9119"):
        assert _is_accepted_host(host, "100.1.2.3", trusted), host
    # exact match only: no suffix, prefix or sibling
    for host in ("hermes.svc.example.com.evil.test", "x.hermes.svc.example.com", "svc.example.com"):
        assert not _is_accepted_host(host, "100.1.2.3", trusted), host


@pytest.mark.parametrize("extra", [None, [], "hermes.example.com", 5])
def test_unset_or_non_list_adds_nothing(monkeypatch, extra):
    _config(monkeypatch, "", extra)
    assert prefix.extra_public_hosts() == frozenset()


def test_invalid_entries_are_dropped_not_trusted(monkeypatch):
    bad = ["https://a.example.com", "a.example.com:9119", "*.example.com", "a b", "", "-a.example.com", "a/b"]
    _config(monkeypatch, "", bad + ["ok.example.com"])
    assert prefix.extra_public_hosts() == {"ok.example.com"}
