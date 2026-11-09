"""Plugin 0.3.45 Home: stopped cards say what and why, a retry replaces its stopped card, several
stopped jobs with one cause are one card, one card per conversation, and answers more than 4 h old
fold into one "Earlier today · N" row. Every fixture here is invented (shaped like real findings)."""
from __future__ import annotations

import os

import pytest

from speakeasy import now as N
from test_history import add_session, make_db, state  # noqa: F401
from test_now import NY, _at, _clock, task

NOON = _at(NY, 2026, 11, 6, 12, 0)
_HERMES: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _hermes_db(service):
    """The conversations these fixtures name exist in Hermes (a stopped card only links one that does,
    SPK-307), unless a test leaves its session out on purpose (``lost=True``)."""
    path = service.rt.state_db
    if not os.path.exists(path):
        make_db(path, routed=True)
    _HERMES["path"] = path
    yield
    _HERMES.clear()


def _has(session):
    if session and _HERMES.get("path"):
        try:
            add_session(_HERMES["path"], session, "api_server", title=session)
        except Exception:
            pass   # already there


def _fail(store, key, kind=None, hint=None, *, session=None, request="Move the photo archive", settled=NOON - 60,
          interaction="vi_typed_f", lost=False):
    task(store, key, "failed", session=session, request=request, settled=settled, created=settled - 5,
         interaction=interaction)
    store.set_failure(key, kind, hint)
    if not lost:
        _has(session)


def _done(store, key, *, session=None, request="Move the photo archive", settled=NOON - 30, interaction="vi_typed_d",
          headline="Moved."):
    task(store, key, "completed", session=session, request=request, result={"spoken": headline, "headline": headline},
         settled=settled, created=settled - 5, interaction=interaction)
    _has(session)


def _stopped(body):
    return [i for i in body["items"] if i["kind"] == "stopped"]


# -- what stopped and why ---------------------------------------------------------------------------

def test_a_stopped_card_says_what_stopped_why_and_the_one_next_step(service):
    _clock(service, NOON)
    _fail(service.store, "f1", "content_refused", session="s_f1", request="Draft the patio quote")
    (card,) = _stopped(service.now({"tz": NY}))
    assert card["line"].startswith(card["title"] + " stopped: ")
    assert card["line"].endswith("Retry or dismiss.") and card["fix"] == {"kind": "retry"}
    assert "It stopped." not in card["line"]


@pytest.mark.parametrize("kind,hint,step", [
    ("billing", None, "Add credits, then retry."),
    ("hermes_unreachable", None, "See how to fix it, or dismiss."),
    (None, "offline", "Open it to retry, or dismiss."),
])
def test_every_known_reason_reads_as_one_plain_line(service, kind, hint, step):
    _clock(service, NOON)
    _fail(service.store, "f1", kind, hint, session="s_f1")
    (card,) = _stopped(service.now({"tz": NY}))
    what, _, rest = card["line"].partition(" stopped: ")
    assert what == card["title"] and rest.endswith(step)
    assert rest[0].islower() or rest.startswith("Speakeasy"), rest


def test_with_no_reason_at_all_it_says_stopped_without_finishing_and_the_title(service):
    _clock(service, NOON)
    _fail(service.store, "f1", "unknown", session="s_f1", request="Sort the receipts folder")
    (card,) = _stopped(service.now({"tz": NY}))
    assert card["line"] == f"Stopped without finishing: {card['title']}. Open it to retry, or dismiss."


def test_stopped_line_never_bare():
    assert N.stopped_line("Photo archive move", "The disk was full.", "retry") == \
        "Photo archive move stopped: the disk was full. Retry or dismiss."
    assert N.stopped_line(None, None, None) == "Stopped without finishing: A task. Open it to retry, or dismiss."
    assert N.stopped_line("Sync", "Speakeasy restarted while it was running.", "retry").startswith(
        "Sync stopped: Speakeasy restarted")


# -- superseded -------------------------------------------------------------------------------------

def test_a_retry_that_succeeded_13_seconds_later_replaces_the_stopped_card_even_once_read(service):
    _clock(service, NOON)
    store = service.store
    _fail(store, "f1", "content_refused", session="s_vault", settled=NOON - 3600)
    _done(store, "r1", session="s_vault", settled=NOON - 3600 + 13)
    store.mark_seen("r1")
    body = service.now({"tz": NY})
    assert _stopped(body) == [] and body["counts"]["stopped"] == 0


def test_a_retry_still_running_replaces_the_stopped_card(service):
    _clock(service, NOON)
    store = service.store
    _fail(store, "f1", "rate_limit", session="s_vault")
    task(store, "r1", "working", session="s_vault", request="Move the photo archive", created=NOON - 20)
    assert [i["kind"] for i in service.now({"tz": NY})["items"]] == ["working"]


def test_a_retry_in_its_own_session_replaces_its_parent(service):
    _clock(service, NOON)
    store = service.store
    _fail(store, "f1", "rate_limit", session="s_a")
    _done(store, "r1", session="s_b")
    store.set_parent("r1", "f1")
    assert _stopped(service.now({"tz": NY})) == []


def test_a_success_in_another_conversation_leaves_the_stopped_card(service):
    _clock(service, NOON)
    _fail(service.store, "f1", "rate_limit", session="s_a", request="Book the cabin")
    _done(service.store, "d1", session="s_b", request="Weather for Saturday")
    assert len(_stopped(service.now({"tz": NY}))) == 1


def test_a_stopped_job_from_before_the_4am_rollover_is_in_earlier(service):
    _clock(service, NOON)
    _fail(service.store, "f1", "rate_limit", session="s_a", settled=_at(NY, 2026, 11, 6, 3, 50))
    assert _stopped(service.now({"tz": NY})) == []


# -- one card per cause -----------------------------------------------------------------------------

def test_three_jobs_stopped_by_one_cause_are_one_card_and_it_can_be_dismissed(server, service):
    _clock(service, NOON)
    store = service.store
    for n, req in enumerate(["Check the garden sensors", "List the open invoices", "Summarize the board deck"]):
        _fail(store, f"f{n}", "hermes_unreachable", session=f"s_{n}", request=req, settled=NOON - 300 + n)
    _fail(store, "g1", "billing", session="s_g", request="Price the flights")
    body = service.now({"tz": NY})
    cards = _stopped(body)
    assert len(cards) == 2
    grouped = next(c for c in cards if c.get("stopped_count"))
    assert grouped["stopped_count"] == 3 and grouped["line"].startswith("3 jobs stopped: ")
    assert grouped["line"].endswith("or dismiss.") and grouped["fix"] == {"kind": "open"}
    assert grouped["id"].startswith("g:stopped:") and len(grouped["group"]) == 3
    assert "Summarize the board deck" in grouped["title"] and "1 more" in grouped["title"]
    assert service.now_dismiss(grouped["id"], {})["dismissed"] is True
    assert len(_stopped(service.now({"tz": NY}))) == 1


# -- one card per conversation ----------------------------------------------------------------------

def test_repeated_answers_on_one_call_fold_into_the_newest(service):
    _clock(service, NOON)
    store = service.store
    for n, req in enumerate(["Desk lamp on", "Desk lamp off", "Desk lamp on again"]):
        _done(store, f"l{n}", request=req, settled=NOON - 600 + 60 * n, interaction="vi_lamps", headline=req)
    _done(store, "w1", request="Weather for Saturday", settled=NOON - 100, interaction="vi_other", headline="Sunny.")
    items = service.now({"tz": NY})["items"]
    lamp = [i for i in items if "lamp" in i["title"].lower()]
    assert len(lamp) == 1 and lamp[0]["title"] == "Desk lamp on again" and lamp[0]["folded"] == 2
    assert len(items) == 2


def test_a_follow_up_folds_into_its_parents_conversation(service):
    _clock(service, NOON)
    store = service.store
    _done(store, "p1", session="s_lamp", request="Desk lamp on", settled=NOON - 120)
    _done(store, "c1", request="Make it purple", settled=NOON - 60, interaction="vi_x")
    store.set_parent("c1", "p1")
    assert [i["title"] for i in service.now({"tz": NY})["items"]] == ["Make it purple"]


# -- Earlier today ----------------------------------------------------------------------------------

def test_answers_more_than_four_hours_old_fold_into_one_earlier_today_row(service):
    _clock(service, NOON)
    store = service.store
    _done(store, "a1", session="s1", request="Fresh one", settled=NOON - 3600)
    _done(store, "a2", session="s2", request="Five hours ago", settled=NOON - 5 * 3600)
    _done(store, "a3", session="s3", request="Six hours ago", settled=NOON - 6 * 3600)
    body = service.now({"tz": NY})
    assert [i["title"] for i in body["items"]] == ["Fresh one"]
    today = body["earlier"]["today"]
    assert today["count"] == 2 and today["label"] == "Earlier today \u00b7 2"
    assert [i["title"] for i in today["items"]] == ["Five hours ago", "Six hours ago"]
    assert all(i["kind"] in N.KINDS for i in body["items"]), "a build 68 app only ever sees kinds it knows"


def test_no_earlier_today_row_when_nothing_is_old(service):
    _clock(service, NOON)
    _done(service.store, "a1", session="s1", settled=NOON - 60)
    assert service.now({"tz": NY})["earlier"]["today"] is None


def test_a_grouped_line_speaks_in_the_plural():
    items = [{"id": f"s:{n}", "kind": "stopped", "title": f"Job {n}", "finished_at": float(n), "run_id": f"r{n}",
              "line": N.stopped_line(f"Job {n}", "The AI model declined this one.", "retry"), "fix": {"kind": "retry"},
              "_cause": "kind:content_refused"} for n in range(2)]
    (card,) = N.group_stopped(items)
    assert card["line"] == "2 jobs stopped: the AI model declined them. Open one to retry, or dismiss."


def test_pure_fold_keeps_call_answers_and_counts_folded_ones():
    items = [{"id": "a", "kind": "new_answer", "finished_at": 0.0, "folded": 2, "unread": True},
             {"id": "b", "kind": "seen_answer", "finished_at": 0.0, "on_call": "heard"},
             {"id": "c", "kind": "working", "started_at": 0.0}]
    keep, today = N.fold_earlier(items, 5 * 3600)
    assert [i["id"] for i in keep] == ["b", "c"] and today["count"] == 3 and today["unread"] == 1


# -- SPK-307: a stopped job Hermes never saved a conversation for -----------------------------------
# Two jobs queued behind each other, Speakeasy restarted before either started: Home's card linked a
# conversation Hermes never had ("I couldn't find that conversation") and Try again wrote into it.

def test_a_lost_job_links_no_conversation_and_offers_try_again(service):
    _clock(service, NOON)
    _fail(service.store, "f1", None, "offline", session="speakeasy_task_gone", lost=True, request="Add a photo viewer")
    body = service.now({"tz": NY})
    (card,) = _stopped(body)
    assert card["session_id"] is None
    assert card["fix"] == {"kind": "retry"} and card["line"].endswith("Retry or dismiss.")
    assert card["retry_fresh"] is True


def test_a_lost_job_keeps_a_specific_fix(service):
    _clock(service, NOON)
    _fail(service.store, "f1", "billing", session="speakeasy_task_gone", lost=True)
    (card,) = _stopped(service.now({"tz": NY}))
    assert card["session_id"] is None and card["fix"] == {"kind": "credits"}


def test_a_saved_conversation_still_opens(service):
    _clock(service, NOON)
    _fail(service.store, "f1", None, "offline", session="s_here")
    (card,) = _stopped(service.now({"tz": NY}))
    assert card["session_id"] == "s_here" and card["fix"] == {"kind": "open"} and not card.get("retry_fresh")


def test_two_lost_jobs_fold_into_one_card_that_opens_nothing_missing_and_retries(service):
    _clock(service, NOON)
    store = service.store
    _fail(store, "a", None, "offline", session="speakeasy_task_a", lost=True, request="Add a photo viewer",
          settled=NOON - 120)
    _fail(store, "b", None, "offline", session="speakeasy_task_b", lost=True, request="Build the call header",
          settled=NOON - 119)
    (card,) = _stopped(service.now({"tz": NY}))
    assert card["stopped_count"] == 2 and card["session_id"] is None
    assert card["fix"] == {"kind": "retry"} and card["line"].endswith("Retry or dismiss.")
    assert "Open one" not in card["line"]


def test_a_folded_card_opens_its_newest_saved_conversation(service):
    _clock(service, NOON)
    store = service.store
    _fail(store, "a", None, "offline", session="s_saved", request="Add a photo viewer", settled=NOON - 120)
    _fail(store, "b", None, "offline", session="speakeasy_task_b", lost=True, request="Build the call header",
          settled=NOON - 119)
    (card,) = _stopped(service.now({"tz": NY}))
    assert card["stopped_count"] == 2 and card["session_id"] == "s_saved"


def test_the_pill_counts_stopped_work_instead_of_all_quiet(service):
    _clock(service, NOON)
    _fail(service.store, "f1", None, "offline", session="speakeasy_task_gone", lost=True)
    assert service.now({"tz": NY})["pill"] == "1 stopped"


def test_pill_text_counts():
    from speakeasy.home_words import pill_text
    assert pill_text(0, 0) == "All quiet"
    assert pill_text(0, 2, stopped=1) == "1 stopped \u00b7 2 running"
    assert pill_text(1, 0, one_title="Pick a flight", stopped=1) == "1 needs you \u00b7 1 stopped"


def test_try_again_starts_lost_jobs_afresh_from_their_requests_and_the_card_leaves(service, monkeypatch):
    _clock(service, NOON)
    store = service.store
    _fail(store, "a", None, "offline", session="speakeasy_task_a", lost=True, request="Add a photo viewer",
          settled=NOON - 120)
    _fail(store, "b", None, "offline", session="s_saved", request="Build the call header", settled=NOON - 119)
    calls = []

    def fake_start(text, staged=None, session_id=None, **kw):
        n = len(calls)
        calls.append((text, session_id))
        task(store, f"new{n}", "working", request=text, created=NOON - 10)
        return {"ok": True, "run_id": f"run_new{n}", "session_id": None}
    monkeypatch.setattr(service, "_start_job", fake_start)
    (card,) = _stopped(service.now({"tz": NY}))
    got = service.now_retry(card["id"], {})
    assert sorted(calls) == [("Add a photo viewer", None), ("Try again", "s_saved")]
    assert {s["fresh"] for s in got["started"]} == {True, False}
    assert _stopped(service.now({"tz": NY})) == []


def test_try_again_is_only_for_stopped_cards(service):
    from speakeasy.service import ServiceError
    _clock(service, NOON)
    _done(service.store, "d1", session="s1")
    item = service.now({"tz": NY})["items"][0]
    with pytest.raises(ServiceError):
        service.now_retry(item["id"], {})
    with pytest.raises(ServiceError):
        service.now_retry("s:nope", {})


def test_try_again_route(server, service, monkeypatch):
    from conftest import http
    _clock(service, NOON)
    _fail(service.store, "f1", None, "offline", session="speakeasy_task_gone", lost=True, request="Add a photo viewer")
    monkeypatch.setattr(service, "_start_job", lambda text, staged=None, session_id=None, **kw:
                        {"ok": True, "run_id": None, "session_id": None})
    (card,) = _stopped(service.now({"tz": NY}))
    from urllib.parse import quote
    status, body = http(server.base_url, "POST", f"/voice/now/{quote(card['id'], safe='')}/retry", {},
                        token=server.token)
    assert status == 200 and body["started"][0]["fresh"] is True
