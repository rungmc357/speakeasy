"""Instant home control: offered only when Hermes has Home Assistant, plans checked against the
chosen devices, anything unclear or failed before a device moved goes to Hermes unchanged."""
from __future__ import annotations

import json

import pytest

from fakes import SDP, FakeLiveWorker, FakeTransport, http, wait_for
from speakeasy import home_control as H


def st(entity_id, name, state="off", **attrs):
    return {"entity_id": entity_id, "state": state, "attributes": {"friendly_name": name, **attrs}}


STATES = [
    st("light.kitchen", "Kitchen Lights", "on", brightness=255),
    st("light.bedroom_lamp", "Bedroom Lamp", "on"),
    st("light.office", "Office Lights"),
    st("climate.den", "Den", "cool", temperature=24.0, hvac_modes=["off", "heat", "cool"], min_temp=10, max_temp=32),
    st("climate.bed_climate", "Bed Climate", "heat_cool", temperature=26, hvac_modes=["heat_cool"], min_temp=13, max_temp=43),
    st("fan.dyson", "Bedroom Fan"),
    st("lock.front_door", "Front Door", "locked"),
    st("cover.garage", "Garage Door", "closed", device_class="garage"),
    st("cover.blinds", "Living Room Blinds", "open", current_position=100),
    st("switch.kettle", "Kettle"),
    st("sensor.temp", "Temperature"),
]


class FakeHA:
    def __init__(self, states=STATES, unit="°C", fail=None):
        # Its own copy: on/off calls change states the way Home Assistant does.
        self._states, self.unit, self.fail = [dict(s) for s in states], unit, fail
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, url, token):  # client factory
        assert url and token
        return self

    def states(self):
        if self.fail:
            raise H.HomeAssistantError(self.fail)
        return self._states

    def config(self):
        return {"unit_system": {"temperature": self.unit}}

    def call_service(self, domain, service, data):
        if self.fail:
            raise H.HomeAssistantError(self.fail)
        self.calls.append((domain, service, data))
        if service in ("turn_on", "turn_off") and domain in ("light", "switch", "fan"):
            for s in self._states:
                if s["entity_id"] in data.get("entity_id", ()):
                    s["state"] = "on" if service == "turn_on" else "off"
        return []


def control(ha, plan=None, enabled=True, entities=None, creds=("http://ha.local:8123", "tok")):
    settings = {"home_control": {"enabled": enabled, "entities": entities}}
    replies = iter(plan if isinstance(plan, list) else [plan])
    return H.HomeControl(lambda: settings, lambda: creds,
                         plan_call=lambda messages: json.dumps(next(replies)) if plan is not None else None,
                         client_factory=ha)


# -- what is offered -----------------------------------------------------------------------------

def test_security_devices_and_sensors_are_never_offered():
    offered = {s["entity_id"] for s in STATES if H.offerable(s)}
    assert "lock.front_door" not in offered and "cover.garage" not in offered and "sensor.temp" not in offered
    assert {"light.kitchen", "cover.blinds", "switch.kettle", "fan.dyson"} <= offered


def test_default_picks_are_lights_thermostats_fans_but_not_a_bed():
    picks = H.default_selection(STATES)
    assert picks == ["climate.den", "fan.dyson", "light.bedroom_lamp", "light.kitchen", "light.office"]


def test_detect_without_home_assistant_says_why():
    hc = control(FakeHA(), creds=None)
    found = hc.detect()
    assert found["available"] is False and "HASS_TOKEN" in found["reason"]


def test_detect_reports_an_unreachable_home_assistant():
    found = control(FakeHA(fail="unreachable")).detect()
    assert found == {"available": False, "reason": "Home Assistant couldn't be reached."}


def test_overview_lists_every_offerable_device_with_what_is_ticked():
    view = control(FakeHA(), entities=["light.kitchen", "light.gone"]).overview()
    ticked = {d["entity_id"] for d in view["devices"] if d["included"]}
    assert ticked == {"light.kitchen"}
    assert view["missing"] == ["light.gone"]
    assert "lock.front_door" not in {d["entity_id"] for d in view["devices"]}


# -- checking plans ------------------------------------------------------------------------------

def by_id(ids=None):
    devices = [H.device_from_state(s) for s in STATES if H.offerable(s)]
    return {d.entity_id: d for d in devices if ids is None or d.entity_id in ids}


def plan(*calls, say="Done."):
    return json.dumps({"calls": list(calls), "say": say})


def test_an_invented_device_sends_the_whole_request_to_hermes():
    text = plan({"service": "light.turn_off", "entity_id": ["light.kitchen"]},
                {"service": "light.turn_off", "entity_id": ["light.hallway"]})
    assert H.parse_plan(text, by_id(), "C") is None


def test_a_device_not_ticked_is_refused():
    assert H.parse_plan(plan({"service": "fan.turn_on", "entity_id": "fan.dyson"}),
                        by_id({"light.kitchen"}), "C") is None


def test_wrong_service_for_the_device_kind_is_refused():
    assert H.parse_plan(plan({"service": "light.turn_off", "entity_id": ["climate.den"]}), by_id(), "C") is None
    assert H.parse_plan(plan({"service": "light.unlock", "entity_id": ["light.kitchen"]}), by_id(), "C") is None


def test_out_of_range_values_are_refused():
    assert H.parse_plan(plan({"service": "light.turn_on", "entity_id": ["light.kitchen"],
                              "data": {"brightness_pct": 140}}), by_id(), "C") is None
    assert H.parse_plan(plan({"service": "climate.set_temperature", "entity_id": ["climate.den"],
                              "data": {"temperature": 90, "unit": "C"}}), by_id(), "C") is None


def test_fahrenheit_spoken_in_a_celsius_home_is_converted():
    p = H.parse_plan(plan({"service": "climate.set_temperature", "entity_id": ["climate.den"],
                           "data": {"temperature": 72, "unit": "F"}}), by_id(), "C")
    assert p.calls[0].data == {"temperature": 22.0}


def test_a_bare_fahrenheit_number_in_a_celsius_home_is_read_as_fahrenheit():
    p = H.parse_plan(plan({"service": "climate.set_temperature", "entity_id": ["climate.den"],
                           "data": {"temperature": 68}}), by_id(), "C")
    assert p.calls[0].data == {"temperature": 20.0}
    p = H.parse_plan(plan({"service": "climate.set_temperature", "entity_id": ["climate.den"],
                           "data": {"temperature": 21}}), by_id(), "C")
    assert p.calls[0].data == {"temperature": 21}


def test_a_short_device_name_reaches_the_planner():
    hc = control(FakeHA(), {"answer": "The den is set to 24."})
    assert hc.respond("what's the den set to") == H.Reply(True, "The den is set to 24.")


def test_an_hvac_mode_the_device_lacks_is_refused():
    assert H.parse_plan(plan({"service": "climate.set_hvac_mode", "entity_id": ["climate.den"],
                              "data": {"hvac_mode": "dry"}}), by_id(), "C") is None


def test_a_question_is_answered_without_calls():
    p = H.parse_plan(json.dumps({"answer": "The den is set to 24 degrees."}), by_id(), "C")
    assert p.calls == () and p.say == "The den is set to 24 degrees."


def test_handoff_and_garbage_go_to_hermes():
    assert H.parse_plan('{"handoff": true}', by_id(), "C") is None
    assert H.parse_plan("sure! I'll turn them off", by_id(), "C") is None


# -- running -------------------------------------------------------------------------------------

def test_a_multi_step_request_runs_every_call():
    ha = FakeHA()
    hc = control(ha, {"calls": [
        {"service": "light.turn_off", "entity_id": ["light.kitchen", "light.office"]},
        {"service": "climate.set_temperature", "entity_id": ["climate.den"], "data": {"temperature": 68, "unit": "F"}},
        {"service": "fan.turn_on", "entity_id": ["fan.dyson"]}], "say": "Lights off, den at 68, fan on."})
    reply = hc.respond("turn off the lights except the bedroom, set the den to 68 and turn on the fan")
    assert reply == H.Reply(True, "Lights off, den at 68, fan on.")
    assert ha.calls == [("light", "turn_off", {"entity_id": ["light.kitchen", "light.office"]}),
                        ("climate", "set_temperature", {"entity_id": ["climate.den"], "temperature": 20.0}),
                        ("fan", "turn_on", {"entity_id": ["fan.dyson"]})]


def test_off_or_not_set_up_never_plans():
    assert control(FakeHA(), {"calls": []}, enabled=False).respond("lights off") is None
    assert control(FakeHA(), {"calls": []}, creds=None).respond("lights off") is None


def test_timing_and_non_home_requests_skip_the_planner():
    asked = []
    hc = H.HomeControl(lambda: {"home_control": {"enabled": True, "entities": None}}, lambda: ("http://h", "t"),
                       plan_call=lambda m: asked.append(m) or '{"handoff": true}', client_factory=FakeHA())
    assert hc.respond("turn off the lights at 11") is None
    assert hc.respond("remind me to call Dana") is None
    assert hc.respond("what's the weather tomorrow") is None
    assert asked == []


def test_a_device_named_without_home_words_still_reaches_the_planner():
    hc = control(FakeHA(), {"calls": [{"service": "fan.turn_on", "entity_id": ["fan.dyson"]}], "say": "Fan's on."})
    assert hc.respond("bedroom fan please") == H.Reply(True, "Fan's on.")


def test_home_assistant_down_before_anything_moved_says_nothing_changed():
    ha = FakeHA()
    hc = control(ha, {"calls": [{"service": "light.turn_off", "entity_id": ["light.kitchen"]}], "say": "Off."})
    hc.snapshot()
    ha.fail = "unreachable"
    reply = hc.respond("kitchen lights off")
    assert reply.ok is False and "Nothing changed" in reply.spoken


def test_a_planner_timeout_goes_to_hermes():
    def slow(messages):
        raise TimeoutError
    hc = H.HomeControl(lambda: {"home_control": {"enabled": True, "entities": None}}, lambda: ("http://h", "t"),
                       plan_call=slow, client_factory=FakeHA())
    assert hc.respond("kitchen lights off") is None


# -- in a call -----------------------------------------------------------------------------------

def _planner(plan_reply):
    """One reply for every call, or a list served in order; records each prompt it was given."""
    replies = list(plan_reply) if isinstance(plan_reply, list) else None

    def call(messages):
        _planner.prompts.append(messages)
        return json.dumps(replies.pop(0) if replies is not None else plan_reply)
    _planner.prompts = []
    return call


def _svc(home, ha, plan_reply, **kw):
    from speakeasy.service import VoiceService
    workers: list[FakeLiveWorker] = []
    svc = VoiceService(home, notifier=None, start_threads=False, codex_factory=FakeTransport,
                       openai_negotiate=lambda key, payload: {"session": {"id": "sess_fake"}, "transport": {"sdp": "v=0\r\n"}},
                       openai_worker=lambda rt, i: workers.append(FakeLiveWorker(rt, i)) or workers[-1],
                       home_plan_call=_planner(plan_reply), home_client=ha, **kw)
    svc.settings.patch({"voice": {"provider": "openai"}})
    svc.home_control._creds_fn = lambda: ("http://ha.local:8123", "tok")
    return svc, workers


def test_in_a_call_a_home_request_is_done_without_hermes(home, hermes):
    ha = FakeHA()
    svc, workers = _svc(home, ha, {"calls": [{"service": "light.turn_off", "entity_id": ["light.kitchen"]}],
                                   "say": "Kitchen lights are off."})
    try:
        svc.put_home({"enabled": True})
        created = svc.create_session({"sdp": SDP}, "req_home_1")
        worker = workers[-1]
        worker.delegate("call_home_1", "turn off the kitchen lights")
        wait_for(lambda: any("Kitchen lights are off." in str(x) for x in worker.sent), timeout=10)
        assert ha.calls == [("light", "turn_off", {"entity_id": ["light.kitchen"]})]
        assert hermes.calls == []
        feed = svc.interaction(created["interaction_id"]).feed
        task = wait_for(lambda: next((t for t in feed.last.get("tasks") or [] if t["task_id"] == "call_home_1"), None))
        assert task["status"] == "completed"
        # Heard on the call, nothing to read: the apps don't show it as a new answer.
        assert task.get("seen") is True
    finally:
        svc.close()


def test_only_a_marked_task_is_sent_as_seen(tmp_path):
    from speakeasy.store import StateStore
    store = StateStore(tmp_path / "state.sqlite3")
    for key in ("k_home", "k_other"):
        store.reserve_run(key, "int_1", key, 0)
        store.update_run(key, None, "completed")
        store.set_result(key, {"text": "Done."})
    store.mark_seen("k_home")
    assert store.work(idem_key="k_home").get("seen") is True
    assert "seen" not in store.work(idem_key="k_other")


def test_in_a_call_a_handoff_goes_to_hermes(home, hermes):
    ha = FakeHA()
    svc, workers = _svc(home, ha, {"handoff": True})
    try:
        svc.put_home({"enabled": True})
        svc.create_session({"sdp": SDP}, "req_home_2")
        workers[-1].delegate("call_home_2", "make the lights feel cozy for a movie")
        wait_for(lambda: hermes.calls, timeout=10)
        assert ha.calls == []
    finally:
        svc.close()


def test_turned_off_every_request_goes_to_hermes(home, hermes):
    ha = FakeHA()
    svc, workers = _svc(home, ha, {"calls": [{"service": "light.turn_off", "entity_id": ["light.kitchen"]}], "say": "Off."})
    try:
        svc.create_session({"sdp": SDP}, "req_home_3")
        workers[-1].delegate("call_home_3", "turn off the kitchen lights")
        wait_for(lambda: hermes.calls, timeout=10)
        assert ha.calls == []
    finally:
        svc.close()


# -- settings and setup ----------------------------------------------------------------------------

def test_turning_it_on_saves_the_default_picks_once(home, hermes):
    svc, _ = _svc(home, FakeHA(), {"handoff": True})
    try:
        view = svc.put_home({"enabled": True})
        assert view["enabled"] and svc.settings.get()["home_control"]["entities"] == H.default_selection(STATES)
        svc.put_home({"entities": ["light.kitchen", "cover.blinds"]})
        svc.put_home({"enabled": False})
        svc.put_home({"enabled": True})
        assert svc.settings.get()["home_control"]["entities"] == ["cover.blinds", "light.kitchen"]
    finally:
        svc.close()


def test_turning_it_on_without_home_assistant_is_refused(home, hermes):
    from speakeasy.service import ServiceError
    svc, _ = _svc(home, FakeHA(), {"handoff": True})
    svc.home_control._creds_fn = lambda: None
    try:
        with pytest.raises(ServiceError) as err:
            svc.put_home({"enabled": True})
        assert err.value.status == 409
        assert svc.settings.get()["home_control"]["enabled"] is False
    finally:
        svc.close()


def test_bad_entity_lists_are_refused(home, hermes):
    from speakeasy.service import ServiceError
    svc, _ = _svc(home, FakeHA(), {"handoff": True})
    try:
        for bad in (["not an id"], "light.kitchen", [1]):
            with pytest.raises(ServiceError):
                svc.put_home({"entities": bad})
    finally:
        svc.close()


def test_setup_answer_is_recorded_either_way(home, hermes):
    svc, _ = _svc(home, FakeHA(), {"handoff": True})
    try:
        out = svc.save_onboarding({"home_enabled": False})
        assert out["steps"]["home_offered"] is True and out["home_control_enabled"] is False
        out = svc.save_onboarding({"home_enabled": True})
        assert out["home_control_enabled"] is True
    finally:
        svc.close()


def test_home_routes_over_http(server, service):
    service.home_control._creds_fn = lambda: ("http://ha.local:8123", "tok")
    service.home_control._client_factory = FakeHA()
    status, view = http(server.base_url, "GET", "/voice/home", token=server.token)
    assert status == 200 and view["available"] and view["enabled"] is False and view["explainer"]
    status, view = http(server.base_url, "PUT", "/voice/home", {"enabled": True, "entities": ["light.office"]},
                        token=server.token)
    assert status == 200 and view["enabled"]
    assert [d["entity_id"] for d in view["devices"] if d["included"]] == ["light.office"]
    status, err = http(server.base_url, "PUT", "/voice/home", {"enabled": "yes"}, token=server.token)
    assert status == 400


# -- asking once -----------------------------------------------------------------------------------

def test_the_planner_may_ask_which_one():
    hc = control(FakeHA(), {"ask": "Den or bedroom?"})
    assert hc.respond("set the thermostat to 20") == H.Reply(True, "Den or bedroom?", ask=True)


def test_an_answer_is_planned_with_the_original_request_and_never_asks_twice():
    seen = []
    hc = H.HomeControl(lambda: {"home_control": {"enabled": True, "entities": None}}, lambda: ("http://h", "t"),
                       plan_call=lambda m: seen.append(m) or json.dumps({"ask": "Which one?"}), client_factory=FakeHA())
    assert hc.respond("den", answering=("set the thermostat to 20", "Den or bedroom?")) is None
    prompt = seen[0]
    assert "set the thermostat to 20" in prompt[1]["content"] and "They answered: den" in prompt[1]["content"]
    assert "do not ask again" in prompt[0]["content"] and '"ask"' not in prompt[0]["content"].split("Reply with JSON")[1]


def test_an_answer_skips_the_home_words_check():
    """'The den' alone has no home words, but as an answer it still reaches the planner."""
    ha = FakeHA()
    hc = control(ha, {"calls": [{"service": "climate.set_temperature", "entity_id": ["climate.den"],
                                 "data": {"temperature": 20}}], "say": "Den set to 20."})
    reply = hc.respond("the upstairs one", answering=("set the thermostat to 20", "Den or bedroom?"))
    assert reply == H.Reply(True, "Den set to 20.")
    assert ha.calls == [("climate", "set_temperature", {"entity_id": ["climate.den"], "temperature": 20.0})]


def test_a_bad_question_goes_to_hermes():
    assert H.parse_plan(json.dumps({"ask": "Sure thing"}), by_id(), "C", may_ask=True) is None  # not a question
    assert H.parse_plan(json.dumps({"ask": "Which? " * 40}), by_id(), "C", may_ask=True) is None  # rambling
    assert H.parse_plan(json.dumps({"ask": "Den?"}), by_id(), "C", may_ask=False) is None  # already asked once


def test_earlier_answers_are_passed_to_the_planner():
    seen = []
    hc = H.HomeControl(lambda: {"home_control": {"enabled": True, "entities": None}}, lambda: ("http://h", "t"),
                       plan_call=lambda m: seen.append(m) or '{"handoff": true}', client_factory=FakeHA())
    hc.respond("thermostat to 21", notes=['For "thermostat to 20" I asked "Den or bedroom?" and they said "den".'])
    assert 'they said "den"' in seen[0][0]["content"]


def test_in_a_call_it_asks_then_acts_on_the_answer(home, hermes):
    ha = FakeHA()
    svc, workers = _svc(home, ha, [
        {"ask": "Den or bedroom?"},
        {"calls": [{"service": "climate.set_temperature", "entity_id": ["climate.den"], "data": {"temperature": 20}}],
         "say": "Den's set to 20."},
        {"calls": [{"service": "climate.set_temperature", "entity_id": ["climate.den"], "data": {"temperature": 21}}],
         "say": "Den's set to 21."}])
    try:
        svc.put_home({"enabled": True})
        svc.create_session({"sdp": SDP}, "req_home_ask")
        worker = workers[-1]
        worker.delegate("call_ask_1", "set the thermostat to 20")
        wait_for(lambda: any("Den or bedroom?" in str(x) for x in worker.sent), timeout=10)
        assert ha.calls == [] and hermes.calls == []
        worker.feed({"type": "session.output_transcript.delta", "delta": "Den or bedroom?", "start_ms": 3, "end_ms": 4})
        worker.delegate("call_ask_2", "den")
        wait_for(lambda: any("Den's set to 20." in str(x) for x in worker.sent), timeout=10)
        assert "They answered: den" in _planner.prompts[1][1]["content"]
        assert ha.calls == [("climate", "set_temperature", {"entity_id": ["climate.den"], "temperature": 20.0})]
        # The answer is remembered for the rest of the call.
        worker.feed({"type": "session.output_transcript.delta", "delta": "Done.", "start_ms": 6, "end_ms": 7})
        worker.delegate("call_ask_3", "actually make the thermostat 21")
        wait_for(lambda: any("Den's set to 21." in str(x) for x in worker.sent), timeout=10)
        assert 'they said "den"' in _planner.prompts[-1][0]["content"]
        assert hermes.calls == []
    finally:
        svc.close()


def test_in_a_call_an_answer_that_is_still_unclear_goes_to_hermes(home, hermes):
    ha = FakeHA()
    svc, workers = _svc(home, ha, [{"ask": "Den or bedroom?"}, {"handoff": True}])
    try:
        svc.put_home({"enabled": True})
        svc.create_session({"sdp": SDP}, "req_home_ask2")
        worker = workers[-1]
        worker.delegate("call_ask_4", "set the thermostat to 20")
        wait_for(lambda: any("Den or bedroom?" in str(x) for x in worker.sent), timeout=10)
        worker.feed({"type": "session.output_transcript.delta", "delta": "Den or bedroom?", "start_ms": 3, "end_ms": 4})
        worker.delegate("call_ask_5", "whichever is warmer")
        wait_for(lambda: hermes.calls, timeout=10)
        assert ha.calls == []
    finally:
        svc.close()


def test_a_plan_that_touches_an_excluded_device_goes_to_hermes():
    ha = FakeHA()
    hc = control(ha, {"calls": [{"service": "light.turn_off", "entity_id": ["light.kitchen", "light.bedroom_lamp"]}],
                      "say": "Lights off."})
    assert hc.respond("turn off all the lights except the bedroom lamp") is None
    assert ha.calls == []


def test_a_plan_that_respects_the_exclusion_runs():
    ha = FakeHA()
    hc = control(ha, {"calls": [{"service": "light.turn_off", "entity_id": ["light.kitchen", "light.office"]}],
                      "say": "Lights off."})
    assert hc.respond("turn off all the lights except the bedroom lamps, and the fan on") == H.Reply(True, "Lights off.")


def test_excluded_words_keep_what_names_a_device():
    assert H.excluded_words("lights off except the bedroom lamps, and the fan on") == {"bedroom", "lamp"}
    assert H.excluded_words("all the lights but not Sam's office") == {"sam", "office"}
    assert H.excluded_words("kitchen lights off") == set()


def test_doing_part_and_asking_about_the_rest_only_asks():
    text = plan({"service": "light.turn_off", "entity_id": ["light.kitchen"]}, say="Kitchen's off. Which thermostat?")
    p = H.parse_plan(text, by_id(), "C", may_ask=True)
    assert p.ask and p.calls == () and p.say == "Which thermostat?"
    assert H.parse_plan(text, by_id(), "C") is None  # already asked once: Hermes takes it
    assert H.parse_plan(json.dumps({"answer": "The den is warmest, but which one should I set?"}), by_id(), "C") is None
