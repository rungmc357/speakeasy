"""Reinstalling the plugin into a running gateway leaves old and new code mixed (live: every
handoff died with 'Runtime.route() takes 4 positional arguments but 6 were given'). Calls
must refuse with a plain 'send /restart to your Hermes' instead of silently waiting forever."""
import sys
import types

import pytest

from fakes import SDP


def test_healthy_install_is_not_flagged(service):
    assert service.updated_underneath() is False
    assert service.status()["restart_needed"] is False


def test_reinstall_into_a_running_gateway_asks_for_a_restart(service, monkeypatch):
    from speakeasy.calls import SidebandWorker
    from speakeasy.service import RESTART_NEEDED, ServiceError
    reloaded = types.ModuleType(SidebandWorker.__module__)
    reloaded.SidebandWorker = type("SidebandWorker", (), {})  # the freshly re-imported copy
    monkeypatch.setitem(sys.modules, SidebandWorker.__module__, reloaded)
    assert service.updated_underneath() is True
    status = service.status()
    assert status["restart_needed"] is True and status["voice_ready"] is False
    with pytest.raises(ServiceError) as err:
        service.create_session({"sdp": SDP}, "req_restart_1")
    assert str(err.value) == RESTART_NEEDED
    assert "Send /restart to your Hermes" in RESTART_NEEDED
