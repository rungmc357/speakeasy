"""SPK-238: a route the apps ask for only behind a capability is advertised exactly when it's served.

Build 70 reached a phone before its plugin: reopening a saved call (GET /voice/calls/{id}) got
0.3.46's unknown-route 404 and the sheet said "This call is no longer saved on your Hermes.", and
Home's Recent (GET /voice/now/recent) failed and showed nothing. The apps now ask only when
/voice/status advertises the route's flag (ROUTE_CAPABILITIES), so a flag on without its route
brings the bug back, and a route without its flag is never used. A served route answers an id it
doesn't have with its own plain 404 line, never the unknown-route "not found". Every id is invented."""
from __future__ import annotations

import pytest

from test_history import get

from speakeasy.service import ROUTE_CAPABILITIES

UNKNOWN = "/voice/no-such-route-spk238"


def _path(template: str) -> str:
    return template.replace("{id}", "c_spk238_invented")


def _unknown_route(server):
    return get(server, UNKNOWN)


def test_unknown_route_answer_is_the_one_older_plugins_give(server):
    status, body = _unknown_route(server)
    assert status == 404 and body.get("error") == "not found", (status, body)


@pytest.mark.parametrize("flag", sorted(ROUTE_CAPABILITIES))
def test_flag_is_on_exactly_when_its_route_is_served(server, flag):
    _, status_body = get(server, "/voice/status")
    advertised = status_body["capabilities"].get(flag) is True
    served = get(server, _path(ROUTE_CAPABILITIES[flag])) != _unknown_route(server)
    assert advertised == served, (
        f"{flag}: route {'served' if served else 'missing'} but flag {'on' if advertised else 'off'}. "
        "Set the flag in the same release as the route (service.py ROUTE_CAPABILITIES), and answer "
        "an unknown id with the route's own 404 line, not 'not found'.")
