"""Card actions (docs/API.md "Card actions"): a home command's card turns its device on and off."""
from __future__ import annotations

import copy

from fakes import SDP, http, wait_for
from speakeasy import views
from test_home_control import STATES, FakeHA, _svc


class LiveHA(FakeHA):
    """Home Assistant whose devices really change when told to."""

    def __init__(self):
        super().__init__(copy.deepcopy(STATES))

    def call_service(self, domain, service, data):
        super().call_service(domain, service, data)
        for state in self._states:
            if state["entity_id"] in data["entity_id"]:
                state["state"] = "on" if service == "turn_on" else "off" if service == "turn_off" else state["state"]
                if "brightness_pct" in data:
                    state["attributes"]["brightness"] = round(data["brightness_pct"] * 2.55)
        return []


def _home_task(home, hermes):
    ha = LiveHA()
    svc, workers = _svc(home, ha, {"calls": [{"service": "light.turn_off", "entity_id": ["light.kitchen"]}],
                                   "say": "Kitchen lights are off."})
    svc.put_home({"enabled": True})
    created = svc.create_session({"sdp": SDP}, "req_card_1")
    workers[-1].delegate("call_card_1", "turn off the kitchen lights")
    feed = svc.interaction(created["interaction_id"]).feed
    task = wait_for(lambda: next((t for t in feed.last.get("tasks") or []
                                  if t["task_id"] == "call_card_1" and (t.get("result") or {}).get("views")), None),
                    timeout=10)
    return svc, ha, created["interaction_id"], task


def test_a_home_command_card_has_an_on_off_button(home, hermes):
    svc, ha, _, task = _home_task(home, hermes)
    try:
        card = task["result"]["views"][0]
        assert card["kind"] == "home" and card["devices"][0]["entity_id"] == "light.kitchen"
        assert card["devices"][0]["on"] is False
        assert card["actions"][0]["id"] == "toggle" and card["actions"][0]["label"] == "Turn on"
        assert task["title"] == "Kitchen lights off"
    finally:
        svc.close()


def test_the_button_flips_the_device_and_the_card(home, hermes):
    svc, ha, interaction_id, task = _home_task(home, hermes)
    try:
        out = svc.card_action(None, {"run_id": task["run_id"], "view": 0, "action": "toggle"},
                              interaction_id=interaction_id)
        assert out == {"ok": True, "message": "Kitchen Lights are on."}
        assert ha.calls[-1] == ("light", "turn_on", {"entity_id": ["light.kitchen"]})
        card = svc.store.work(task["run_id"])["result"]["views"][0]
        assert card["devices"][0]["on"] is True and card["actions"][0]["label"] == "Turn off"
        # with no call open, the board uses the run's own route; a value sets brightness
        out = svc.card_action(task["run_id"], {"view": 0, "action": "toggle", "value": 40})
        assert out["message"] == "Kitchen Lights are at 40%."
        assert ha.calls[-1] == ("light", "turn_on", {"entity_id": ["light.kitchen"], "brightness_pct": 40})
    finally:
        svc.close()


def test_a_stale_card_does_what_its_button_says_not_the_opposite(home, hermes):
    """The card says "Turn on" (the light was off). If the light came on since (by voice, the
    Home app, or the same card tapped on the Mac and the iPhone), the press leaves it on rather
    than flipping it off, and the card catches up."""
    svc, ha, interaction_id, task = _home_task(home, hermes)
    try:
        for state in ha._states:
            if state["entity_id"] == "light.kitchen":
                state["state"] = "on"
        before = len(ha.calls)
        out = svc.card_action(task["run_id"], {"view": 0, "action": "toggle"})
        assert out == {"ok": True, "message": "Kitchen Lights are already on."}
        assert len(ha.calls) == before  # nothing was switched
        card = svc.store.work(task["run_id"])["result"]["views"][0]
        assert card["devices"][0]["on"] is True and card["actions"][0]["label"] == "Turn off"
        # a double tap (the second press arrives with the refreshed card's label) turns it off once
        svc.card_action(task["run_id"], {"view": 0, "action": "toggle"})
        assert ha.calls[-1] == ("light", "turn_off", {"entity_id": ["light.kitchen"]})
    finally:
        svc.close()


def test_bad_presses_are_refused(home, hermes):
    from speakeasy.service import ServiceError
    svc, ha, interaction_id, task = _home_task(home, hermes)
    try:
        for run_id, body, status in [
            (task["run_id"], {"view": 3, "action": "toggle"}, 404),
            (task["run_id"], {"view": 0, "action": "explode"}, 400),
            (task["run_id"], {"view": 0, "action": "toggle", "value": 400}, 409),
            (task["run_id"], {"view": 0}, 400),
            ("lr_" + "0" * 32, {"view": 0, "action": "toggle"}, 404),
        ]:
            try:
                svc.card_action(run_id, body)
            except ServiceError as exc:
                assert exc.status == status, (body, exc.status, exc.message)
            else:
                raise AssertionError(body)
    finally:
        svc.close()


def test_card_action_routes_and_capabilities_over_http(server, service):
    status, body = http(server.base_url, "GET", "/voice/status", token=server.token)
    assert status == 200 and body["capabilities"]["card_actions"] is True
    assert body["capabilities"]["job_progress"] is True
    status, body = http(server.base_url, "POST", "/voice/work/lr_unknown/card-action",
                        {"view": 0, "action": "toggle"}, token=server.token)
    assert status == 404 and body["error"]


def test_any_card_may_carry_buttons_but_only_well_formed_ones():
    view = views.clean({"kind": "fact", "title": "Rain", "value": "40%", "actions": [
        {"id": "refresh", "label": "Refresh", "style": "primary"},
        {"id": "Bad Id", "label": "x"}, {"id": "copy"}, "nope",
        {"id": "dismiss", "label": "Dismiss", "style": "loud", "confirm": True}]})
    assert view["actions"] == [{"id": "refresh", "label": "Refresh", "style": "primary", "confirm": False},
                               {"id": "dismiss", "label": "Dismiss", "style": "secondary", "confirm": True}]
