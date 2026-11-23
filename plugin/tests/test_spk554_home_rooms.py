"""SPK-554: a room-wide on/off covers every chosen device whose name, entity id or Home Assistant
area carries the room word, the spoken line is built from the room's state after the calls, and a
"no, they're not" re-reads the room fresh instead of repeating the earlier claim. Invented devices."""
from __future__ import annotations

import copy

import pytest

from fakes import SDP, wait_for
from speakeasy import home_control as H
from test_home_control import FakeHA, _planner, _svc, control, st


@pytest.fixture(autouse=True)
def _no_verify_wait(monkeypatch):
    monkeypatch.setattr(H, "VERIFY_WAIT_S", 0)


class LiveHA(FakeHA):
    """A fake Home Assistant whose on/off calls change its states, with optional stuck devices
    (accept the call, keep their state) and areas."""

    def __init__(self, states, areas=None, stuck=(), **kw):
        super().__init__(copy.deepcopy(states), **kw)
        self._areas, self.stuck = dict(areas or {}), set(stuck)
        self.reads = 0

    def states(self):
        self.reads += 1
        return copy.deepcopy(super().states())

    def areas(self):
        return dict(self._areas)

    def call_service(self, domain, service, data):
        kept = {s["entity_id"]: s["state"] for s in self._states if s["entity_id"] in self.stuck}
        super().call_service(domain, service, data)
        for s in self._states:
            if s["entity_id"] in kept:
                s["state"] = kept[s["entity_id"]]  # accepted the call, didn't move
        return []

    def state_of(self, entity_id):
        return next(s["state"] for s in self._states if s["entity_id"] == entity_id)


DEN = [
    st("light.den_floor_lamp", "Den Floor Lamp", "off"),
    st("light.den_den", "Den Den", "off"),
    st("light.upstairs_den_main", "Upstairs Den Main Lights", "on", brightness=200),
    st("light.kitchen", "Kitchen Lights", "on"),
    st("light.garden_path", "Garden Path", "on"),  # "den" inside a word is not the den
    st("fan.den_ceiling", "Den Ceiling Fan", "on"),
]
# What a planner that matches the room to the start of a name does (the reported miss).
SHORT_PLAN = {"calls": [{"service": "light.turn_off", "entity_id": ["light.den_floor_lamp", "light.den_den"]}],
              "say": "They're off now."}


def _ids(ha):
    return [tuple(c[2]["entity_id"]) for c in ha.calls]


# -- step 1: the room covers every light named for it ----------------------------------------------

def test_den_lights_include_the_light_named_upstairs_den():
    ha = LiveHA(DEN)
    reply = control(ha, SHORT_PLAN).respond("Turn off my den lights.")
    assert reply.ok
    assert _ids(ha) == [("light.den_floor_lamp", "light.den_den", "light.upstairs_den_main")]
    assert ha.state_of("light.upstairs_den_main") == "off"
    # Built from the room after the call, not the planner's claim.
    assert reply.spoken == "Den lights are off."
    # Never a light from another room, a word that merely contains "den", or another kind.
    for other in ("light.kitchen", "light.garden_path", "fan.den_ceiling"):
        assert ha.state_of(other) == "on"


def test_a_home_assistant_area_names_the_room_too():
    states = [st("light.den_floor_lamp", "Den Floor Lamp", "off"), st("light.reading", "Reading Light", "on"),
              st("light.hall", "Hall Light", "on")]
    ha = LiveHA(states, areas={"light.reading": "Den", "light.hall": "Hallway"})
    reply = control(ha, {"calls": [{"service": "light.turn_off", "entity_id": ["light.den_floor_lamp"]}],
                         "say": "Done."}).respond("turn off the den lights")
    assert _ids(ha) == [("light.den_floor_lamp", "light.reading")]
    assert ha.state_of("light.hall") == "on"
    assert reply.spoken == "Den lights are off."


def test_the_planner_sees_each_device_area():
    devices = [H.device_from_state(st("light.reading", "Reading Light"), {"light.reading": "Den"})]
    listing = H.planner_messages("den lights off", devices, "F")[0]["content"]
    assert "light.reading | Reading Light (area Den) | off" in listing


def test_a_single_named_device_is_not_widened():
    ha = LiveHA(DEN)
    control(ha, {"calls": [{"service": "light.turn_off", "entity_id": ["light.den_floor_lamp"]}],
                 "say": "Den floor lamp is off."}).respond("turn off the den floor lamp")
    assert _ids(ha) == [("light.den_floor_lamp",)]


def test_den_lamps_mean_the_den_lamps_only():
    states = DEN + [st("light.den_table_lamp", "Den Table Lamp", "on")]
    ha = LiveHA(states)
    reply = control(ha, {"calls": [{"service": "light.turn_off", "entity_id": ["light.den_floor_lamp"]}],
                         "say": "Done."}).respond("turn off the den lamps")
    assert _ids(ha) == [("light.den_floor_lamp", "light.den_table_lamp")]
    assert ha.state_of("light.upstairs_den_main") == "on"
    assert reply.spoken == "Den lamps are off."


def test_a_plan_with_values_or_an_exclusion_is_left_alone():
    ha = LiveHA(DEN)
    control(ha, {"calls": [{"service": "light.turn_on", "entity_id": ["light.den_den"],
                            "data": {"brightness_pct": 40}}], "say": "Done."}).respond("dim the den lights to 40")
    assert _ids(ha) == [("light.den_den",)]
    ha = LiveHA(DEN)
    control(ha, SHORT_PLAN).respond("turn off the den lights except upstairs")
    assert _ids(ha) == [("light.den_floor_lamp", "light.den_den")]


def test_two_rooms_widen_each():
    states = DEN + [st("light.kitchen_counter", "Kitchen Counter Strip", "on")]
    ha = LiveHA(states)
    reply = control(ha, {"calls": [{"service": "light.turn_off", "entity_id": ["light.den_den", "light.kitchen"]}],
                         "say": "Done."}).respond("turn off the den and kitchen lights")
    assert set(_ids(ha)[0]) == {"light.den_den", "light.kitchen", "light.den_floor_lamp", "light.upstairs_den_main",
                                "light.kitchen_counter"}
    assert reply.spoken == "Den and kitchen lights are off."


def test_a_light_that_ignores_the_call_is_named_as_still_on():
    ha = LiveHA(DEN, stuck={"light.upstairs_den_main"})
    reply = control(ha, SHORT_PLAN).respond("Turn off my den lights.")
    assert reply.spoken == "Den Floor Lamp and Den Den are off, but Upstairs Den Main Lights are still on."
    assert "They're off" not in reply.spoken


# -- step 2: "no, they're not" re-checks the room fresh ---------------------------------------------

def _room_scope(hc, request="Turn off my den lights."):
    reply = hc.respond(request)
    assert reply.scope is not None
    return reply.scope


def test_a_dispute_finds_the_light_still_on_and_turns_it_off():
    ha = LiveHA(DEN, stuck={"light.upstairs_den_main"})
    hc = control(ha, SHORT_PLAN)
    scope = _room_scope(hc)
    ha.stuck.clear()  # it answers the second time
    ha.calls.clear()
    reads = ha.reads
    reply = hc.recheck("No, they're not.", scope)
    assert ha.reads > reads  # a fresh read, not the cached snapshot
    assert ha.calls == [("light", "turn_off", {"entity_id": ["light.upstairs_den_main"]})]
    assert reply.spoken == "You're right, Upstairs Den Main Lights were still on. They are off now."
    assert ha.state_of("light.upstairs_den_main") == "off"


def test_a_dispute_after_the_original_miss_lists_what_is_on_by_name():
    """The reported shape: the first plan missed a light. Even a scope built from it re-reads every
    light named for the den, so the dispute turns the missed one off instead of saying "They're off"."""
    ha = LiveHA(DEN)
    hc = control(ha, SHORT_PLAN)
    scope = H.Scope("light", "off", (("den",),), ("light.den_floor_lamp", "light.den_den"))
    reply = hc.recheck("no they're not", scope)
    assert ha.calls == [("light", "turn_off", {"entity_id": ["light.upstairs_den_main"]})]
    assert "Upstairs Den Main Lights" in reply.spoken and "They're off" not in reply.spoken


def test_a_dispute_when_everything_reads_off_names_each_light_and_a_light_off_the_list():
    states = DEN + [st("light.den_sconce", "Den Sconce", "on")]
    ha = LiveHA(states)
    hc = control(ha, SHORT_PLAN, entities=[s["entity_id"] for s in DEN])
    scope = _room_scope(hc)
    ha.calls.clear()
    reply = hc.recheck("they're still on", scope)
    assert ha.calls == []
    assert reply.spoken == ("I just checked: Den Floor Lamp, Den Den and Upstairs Den Main Lights all read off in "
                            "Home Assistant. Den Sconce is on too, but it is not on the list I can control.")


def test_a_dispute_that_still_fails_says_so():
    ha = LiveHA(DEN, stuck={"light.upstairs_den_main"})
    hc = control(ha, SHORT_PLAN)
    reply = hc.recheck("that didn't work", _room_scope(hc))
    assert reply.spoken == ("I sent it again, but Upstairs Den Main Lights still read on in Home Assistant.")


@pytest.mark.parametrize("words", ["no, the kitchen ones", "turn them back on", "what's the weather",
                                   "set the den to 72 degrees and make it cooler please right now ok thanks"])
def test_other_requests_are_not_disputes(words):
    ha = LiveHA(DEN)
    hc = control(ha, SHORT_PLAN)
    assert hc.recheck(words, _room_scope(hc)) is None


def test_a_stale_scope_is_not_rechecked():
    ha = LiveHA(DEN)
    hc = control(ha, SHORT_PLAN)
    scope = H.Scope("light", "off", (("den",),), ("light.den_den",), at=0.0)
    assert hc.recheck("no they're not", scope) is None


# -- on a call ----------------------------------------------------------------------------------------

def test_on_a_call_the_room_is_covered_and_a_dispute_re_checks_it(home, hermes):
    ha = LiveHA(DEN, stuck={"light.upstairs_den_main"})
    # The planner would repeat itself if asked again; the dispute must never reach it.
    svc, workers = _svc(home, ha, [SHORT_PLAN, SHORT_PLAN, SHORT_PLAN])
    try:
        svc.put_home({"enabled": True, "entities": [s["entity_id"] for s in DEN]})
        svc.create_session({"sdp": SDP}, "req_den_1")
        worker = workers[-1]
        worker.delegate("call_den_1", "Turn off my den lights.")
        wait_for(lambda: any("Upstairs Den Main Lights are still on" in str(x) for x in worker.sent), timeout=10)
        assert "light.upstairs_den_main" in ha.calls[0][2]["entity_id"]
        ha.stuck.clear()
        worker.feed({"type": "session.output_transcript.delta", "delta": "Den Floor Lamp and Den Den are off.",
                     "start_ms": 3, "end_ms": 4})
        worker.delegate("call_den_2", "No, they're not.")
        wait_for(lambda: any("They are off now." in str(x) for x in worker.sent), timeout=10)
        assert ha.calls[-1] == ("light", "turn_off", {"entity_id": ["light.upstairs_den_main"]})
        assert len(_planner.prompts) == 1  # the dispute was re-checked, not re-planned
        assert hermes.calls == []
        assert not any("They're off" in str(x) for x in worker.sent)
    finally:
        svc.close()


def test_areas_parse_from_the_template_text():
    assert H.parse_areas("light.a\tUpstairs Den\nbad line\nlight.b\t\nswitch.c\tKitchen\n") == {
        "light.a": "Upstairs Den", "switch.c": "Kitchen"}


@pytest.mark.parametrize("words", ["No, they're not.", "no it's still on", "nope, that didn't work",
                                   "No, they are still on", "no, nothing happened"])
def test_a_denial_is_never_spliced_into_the_last_request_as_a_correction(words):
    from speakeasy import router
    assert router.correction(words) is None


@pytest.mark.parametrize("words,want", [("No, turn them on", ("replace", "turn them on")),
                                        ("Our guest room, sorry", ("phrase", "Our guest room"))])
def test_real_corrections_still_count(words, want):
    from speakeasy import router
    got = router.correction(words)
    assert got is not None and got[0] == want[0]
