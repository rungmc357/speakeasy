"""Asks said seconds apart in one call join the task that just started, unless clearly unrelated."""
from speakeasy import router


def never_called(_messages):
    raise AssertionError("the routing model must not be asked")


def task(request, age_s, status="running", task_id="t1"):
    return router.OpenTask(task_id, request, status, "", age_s)


BUTTON = task("There should be a button in the pop-up that opens it without typing a command", 8.6)
THERMOSTAT = task("What's the thermostat set to in the living room", 8.7)
TABLE = task("Book a table for two tonight", 4.0)


def test_an_alternative_said_seconds_later_joins_the_running_task():
    for said in ("or using the menu bar", "Or maybe from the settings page", "or just a keyboard shortcut"):
        d = router.decide(said, [BUTTON], None, [], never_called)
        assert d.parts[0].kind == "follow_up" and d.parts[0].task_id == "t1", said
        assert d.source in {"fragment", "just_started"}


def test_an_action_on_the_thing_just_asked_about_joins_it():
    for said in ("Can you set it to 75", "turn that off", "ok, make it warmer", "send them over"):
        d = router.decide(said, [THERMOSTAT], None, [], never_called)
        assert d.parts[0].kind == "follow_up" and d.parts[0].task_id == "t1", said


def test_an_ask_with_its_own_subject_is_left_to_the_routing_model():
    for said in ("Find the cheapest flight to Rome next week", "and also, are we pulling the new model?",
                 "check that the server is up", "is there anything on my calendar today",
                 "Can you set it up for the PRs"):
        assert router.leans_on_newest(said, [TABLE]) is None, said


def test_the_routing_model_is_told_a_task_just_started_and_still_splits_unrelated_work():
    seen = {}

    def model(messages):
        seen["user"] = messages[1]["content"]
        seen["system"] = messages[0]["content"]
        return '{"follow_up_task_id": null, "parts": ["Find the cheapest flight to Rome next week"]}'

    d = router.decide("Find the cheapest flight to Rome next week", [TABLE], None, [], model)
    assert d.parts[0].kind == "new"
    assert "Task t1 started 4 seconds ago" in seen["user"] and "unless it is clearly about something else" in seen["user"]
    assert "never a second task running beside it" in seen["system"]


def test_the_routing_model_joins_a_related_ask_it_was_hinted_about():
    d = router.decide("Still doesn't have enough App Store context", [task("What are the next steps on the app", 15.4)],
                      None, [], lambda m: '{"follow_up_task_id": "t1", "parts": []}')
    assert d.parts[0].kind == "follow_up" and d.parts[0].task_id == "t1"


def test_no_hint_or_join_once_the_window_has_passed_or_the_task_is_done():
    late = task("What's the thermostat set to in the living room", 45.0)
    done = task("What's the thermostat set to in the living room", 5.0, status="completed")
    for tasks in ([late], [done]):
        assert router.leans_on_newest("Can you set it to 75", tasks) is None
        assert router.just_started_line(tasks) == ""
        assert "seconds ago in this call" not in router.route_messages("Can you set it to 75", tasks, [])[1]["content"]


def test_a_focused_task_keeps_its_own_hint():
    focus = router.Focus("t1", "Thermostat")
    msgs = router.route_messages("warmer", [THERMOSTAT], [], focus=focus)
    assert "The user is looking at task t1" in msgs[1]["content"]
    assert "seconds ago in this call" not in msgs[1]["content"]


def test_without_the_routing_model_the_rules_still_join_and_split():
    def broken(_messages):
        raise TimeoutError

    assert router.decide("turn that off", [THERMOSTAT], None, [], broken).parts[0].task_id == "t1"
    assert router.route("turn that off", [THERMOSTAT])[0].kind == "follow_up"
    d = router.decide("Find the cheapest flight to Rome next week", [TABLE], None, [], broken)
    assert d.parts[0].kind == "new"


def test_with_several_tasks_running_the_routing_model_says_which_one_it_is():
    pricing = task("Update the pricing page copy", 9.0, task_id="t0")
    asked = []
    d = router.decide("make it shorter", [pricing, task("Book the team lunch for Friday", 3.0)], None, [],
                      lambda m: asked.append(m) or '{"follow_up_task_id": "t0", "parts": []}')
    assert asked and d.parts[0].task_id == "t0"


def test_a_marked_task_elsewhere_wins():
    other = task("Find a taco spot open near me", 300.0, task_id="t0")
    d = router.decide("Can you set it to 75", [other, THERMOSTAT], "t0", [], never_called)
    assert d.parts[0].task_id == "t0"


# -- a real call: the second ask steers the first run, no second run ---------------------------------

def test_an_ask_seconds_later_in_a_call_steers_the_run_instead_of_starting_a_twin(server, service, hermes):
    from test_call_fixes import say, start_call, tasks, voice
    from fakes import wait_for
    import time

    hermes.hold = True
    _, worker = start_call(server, service, "req_join_1")
    say(worker, "call_1", "There should be a button in the pop-up that opens it without typing a command", 10)
    first = wait_for(lambda: [t for t in tasks(server) if t.get("run_id")])[0]
    voice(worker, "On it.", 20)
    say(worker, "call_2", "or using the menu bar", 30)
    wait_for(lambda: any("menu bar" in text for _, text in hermes.steers), timeout=8)
    time.sleep(0.5)
    assert len(hermes.calls) == 1 and len(tasks(server)) == 1
    assert hermes.steers[-1][0] == first["run_id"]
    for run in hermes.runs.values():
        run["done"].set()
