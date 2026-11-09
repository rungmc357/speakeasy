"""SPK-320: a Speakeasy task's conversation row names what it is doing now.

A task's session is named once, after its first task; later requests (follow-ups, corrections, a
new ask steered in) run in the same session. The row kept the first name while its preview showed
the newest work. Now a session Speakeasy minted shows its newest task's own name, unless the user
renamed it (always wins) or another Hermes app did. Chats Speakeasy only continues keep their own
name. Invented sessions and tasks only."""
from speakeasy import history as H
from test_history import NOW, add_msg, add_session, get, make_db, never_routed, post, state  # noqa: F401

SID = "speakeasy_task_lamp1"


def _task(store, key, sid, title):
    store.reserve_run(key, "vi_lamp", "d_" + key, 0)
    store.set_session(key, sid)
    store.update_run(key, "run_" + key, "completed")
    if title:
        store.set_title(key, title)


def _session_with_follow_ups(service, path, hermes_title="Review the porch lamp photos"):
    add_session(path, SID, "api_server", title=hermes_title, at=NOW - 60)
    add_msg(path, SID, "user", "Look at these porch lamp photos", at=NOW - 600)
    add_msg(path, SID, "assistant", "The left lamp is crooked.", at=NOW - 590)
    add_msg(path, SID, "user", "Now plan the garden lights wiring", at=NOW - 120)
    add_msg(path, SID, "assistant", "Wiring plan: two circuits along the fence.", at=NOW - 60)
    _task(service.store, "lamp_a", SID, "Review the porch lamp photos")
    _task(service.store, "lamp_b", SID, "Plan the garden lights wiring")


def _row(service, sid):
    return next(r for r in service.history({})["sessions"] if r["id"] == sid)


def test_a_task_row_names_its_newest_task_not_its_first(service):
    path = service.rt.state_db
    make_db(path)
    _session_with_follow_ups(service, path)
    row = _row(service, SID)
    assert row["title"] == "Plan the garden lights wiring"
    assert row["preview"] == "Wiring plan: two circuits along the fence."
    # The session page says the same.
    assert service.history_session(SID, {})["session"]["title"] == "Plan the garden lights wiring"


def test_a_dated_speakeasy_name_is_still_speakeasys(service):
    path = service.rt.state_db
    make_db(path)
    _session_with_follow_ups(service, path, hermes_title="Review the porch lamp photos (Nov 3, 10:02am)")
    assert _row(service, SID)["title"] == "Plan the garden lights wiring"


def test_a_follow_up_that_names_nothing_keeps_the_real_name(service):
    path = service.rt.state_db
    make_db(path)
    _session_with_follow_ups(service, path)
    _task(service.store, "lamp_c", SID, "Check the earlier task")
    assert _row(service, SID)["title"] == "Plan the garden lights wiring"


def test_a_rename_from_the_app_always_wins(server, service, hermes):
    path = service.rt.state_db
    make_db(path)
    hermes.state_db = path
    _session_with_follow_ups(service, path)
    # Even a name that happens to match one of its tasks: the user chose it.
    status, body = post(server, f"/voice/history/{SID}/rename", {"title": "Review the porch lamp photos"})
    assert status == 200, body
    assert service.store.user_title(SID) == "Review the porch lamp photos"
    assert _row(service, SID)["title"] == "Review the porch lamp photos"
    _task(service.store, "lamp_d", SID, "Order the fence light bulbs")
    service.forget_history_cache()
    assert _row(service, SID)["title"] == "Review the porch lamp photos"
    assert service.history_session(SID, {})["session"]["title"] == "Review the porch lamp photos"


def test_a_name_from_another_hermes_app_stays(service):
    path = service.rt.state_db
    make_db(path)
    _session_with_follow_ups(service, path, hermes_title="Lamp project")
    assert _row(service, SID)["title"] == "Lamp project"


def test_a_chat_speakeasy_only_continues_keeps_its_name(service):
    path = service.rt.state_db
    make_db(path)
    add_session(path, "deskchat1", "api_server", title="Porch lamp notes", at=NOW - 60)
    add_msg(path, "deskchat1", "user", "Plan the garden lights wiring", at=NOW - 60)
    _task(service.store, "desk_a", "deskchat1", "Porch lamp notes")
    _task(service.store, "desk_b", "deskchat1", "Plan the garden lights wiring")
    assert _row(service, "deskchat1")["title"] == "Porch lamp notes"


def test_an_untitled_task_session_shows_its_newest_task():
    link = H.Linked(keys=["b", "a"], name="Porch lamp", running=False, needs_you=False, run_ids=[],
                    current="Plan the garden lights wiring", ours=frozenset({"review the porch lamp photos"}))
    assert H.shown_title(SID, "", link) == "Plan the garden lights wiring"
    assert H.shown_title(SID, "Review the porch lamp photos", link) == "Plan the garden lights wiring"
    assert H.shown_title(SID, "Something the user typed", link) is None
    old = H.Linked(keys=["a"], name="Porch lamp", running=False, needs_you=False, run_ids=[])
    assert H.shown_title(SID, "", old) == "Porch lamp"   # no own name yet: the job's, as before
    assert H.shown_title(SID, "Review the porch lamp photos", old) is None
