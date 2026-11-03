"""Task titles read as clean to-do names: no half clauses, no slips, no filler, never empty."""
from speakeasy import text
from speakeasy.text import home_title, legacy_short_title, short_title


def test_titles_cut_at_a_clause_and_never_end_on_a_dangling_word():
    assert short_title("Turn off the office lamp, turn") == "Turn off the office lamp"
    assert short_title("Also, can you turn on the lights in the living room") == "Turn on the lights in the living room"
    assert short_title("Turn on the lamps in my bedroom please") == "Turn on the lamps in my bedroom"
    assert short_title("Um, what work do we have outstanding") == "What work do we have outstanding"


def test_a_self_correction_names_the_task_for_what_was_meant():
    assert short_title("Turn on my be- er, sorry, turn off the bedroom ceiling light") == \
        "Turn off the bedroom ceiling light"


def test_filler_and_bare_confirmations_make_no_title_of_their_own():
    for said in ("Uh, sure", "that", "Leave it", "Yeah, ship it"):
        assert short_title(said) is None, said


def test_old_stand_in_labels_still_count_as_stand_ins():
    from speakeasy.store import _is_stand_in
    request = "Turn off the office lamp, turn on the bedroom lamps"
    assert legacy_short_title(request) == "Turn off the office lamp, turn"
    assert _is_stand_in("Turn off the office lamp, turn", request)
    assert _is_stand_in(short_title(request), request)
    assert not _is_stand_in("Evening lights", request)


def test_home_cards_are_named_for_what_the_house_did():
    assert home_title("Office lamp is off.") == "Office lamp off"
    assert home_title("Office lamp is off. Bedroom lamps are on.") == "Office lamp off · Bedroom lamps on"
    assert home_title("The bedroom lamps are now at 40%.") == "Bedroom lamps at 40%"
    for spoken in ("Done.", "Den or bedroom?", "I couldn't reach the hub, so nothing changed."):
        assert home_title(spoken) is None, spoken


def test_a_bare_confirmation_task_takes_the_name_of_the_task_it_answers(server, service, hermes):
    from fakes import wait_for
    from test_routing import start_call, tasks
    _, worker = start_call(server, service)
    worker.delegate("call_t1", "Research the best standing desks under five hundred dollars")
    wait_for(lambda: hermes.calls, timeout=10)
    first = next(t["title"] for t in tasks(server) if t.get("title"))
    worker.delegate("call_t2", "Leave it")
    wait_for(lambda: len([t for t in tasks(server) if t["status"] != "rejected"]) >= 2, timeout=10)
    titles = [t.get("title") for t in tasks(server) if t["status"] != "rejected"]
    assert all(titles), titles
    assert titles.count(first) == 2, titles
