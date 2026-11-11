"""A job is never shown under a generic stand-in ("Voice task", "Quick answer"): until a real title
exists it is named from the user's own words (build 75: a typed Home "Catch me up on what came in
today." showed as "Voice task" because Home's brief carried an email address). Made-up data only."""
from __future__ import annotations

import json

from fakes import http, wait_for
from speakeasy.live_status import _now_line
from speakeasy.store import PLACEHOLDER_TITLES
from speakeasy.text import fallback_title, short_title
from test_home_chat import coordinator, post
from test_routing import tasks

GENERIC = ("Voice task", "Quick answer", "A quick choice")
BRIEF = ("Catch the user up on everything that came in today (Wednesday Nov 4): new emails in their inbox "
         "(including pat@example.com), calendar changes, and anything that needs a reply.")


def _routes(server) -> str:
    """Every route a client reads job names from, as one string."""
    out = []
    for path in ("/voice/work/latest", "/voice/now?tz=America/New_York", "/voice/titles", "/voice/work"):
        status, body = http(server.base_url, "GET", path, None, server.token)
        out.append(json.dumps(body))
    return "\n".join(out)


def test_a_brief_with_an_email_address_still_gets_a_word_cut_name():
    assert short_title(BRIEF) == "Catch me up on everything that came"
    assert fallback_title("Catch me up on what came in today.", BRIEF) == "Catch me up on what came in today"
    assert fallback_title(None, "") not in GENERIC
    assert all(g in PLACEHOLDER_TITLES for g in GENERIC)


def test_a_typed_home_job_is_named_from_the_users_words_when_the_titler_has_nothing(server, service, hermes):
    service.rt.title_call = lambda _context: None  # the cloud titler is down
    coordinator(service, {"reply": "Pulling together what's landed today.", "action": "start", "request": BRIEF})
    status, body = post(server, {"text": "Catch me up on what came in today.", "client_id": "c_catchup"})
    assert status == 200 and body["action"] == "start" and body["run_id"]
    task = wait_for(lambda: [t for t in tasks(server) if t.get("run_id") == body["run_id"]])[0]
    assert task["job"]["name"] == "Catch me up"
    assert task["title"] == "Catch me up on what came in today"
    routes = _routes(server)
    for generic in GENERIC:
        assert generic not in routes, generic


def test_the_titler_runs_for_a_brief_that_carries_an_address(server, service, hermes):
    seen: list[str] = []

    def titler(context: str) -> str:
        seen.append(context)
        return "Catch up on today's email"
    service.rt.title_call = titler
    coordinator(service, {"reply": "Pulling together what's landed today.", "action": "start", "request": BRIEF})
    body = post(server, {"text": "Catch me up on what came in today.", "client_id": "c_catchup2"})[1]
    wait_for(lambda: seen)
    task = wait_for(lambda: [t for t in tasks(server) if t.get("run_id") == body["run_id"]
                             and t.get("title") == "Catch up on today's email"])[0]
    assert task["job"]["name"] == "Catch up on today's"


def test_an_old_placeholder_title_is_never_returned(server, service, hermes):
    service.rt.title_call = lambda _context: None
    coordinator(service, {"reply": "On it.", "action": "start", "request": "Find a quiet cafe near the river"})
    body = post(server, {"text": "find me a quiet cafe near the river", "client_id": "c_cafe_old"})[1]
    wait_for(lambda: [t for t in tasks(server) if t.get("run_id") == body["run_id"]])
    key = service.store.key_for_run(body["run_id"])
    service.store._db.execute("UPDATE runs SET title='Voice task', own_title=NULL WHERE idem_key=?", (key,))
    service.store._db.commit()
    routes = _routes(server)
    assert "Voice task" not in routes
    assert service.store.shown_title(key) == "Find me a quiet cafe near the river"


def test_reading_notes_is_not_reading_code():
    notes = [{"kind": "read", "text": "Read its notes on email-operations", "at": 1},
             {"kind": "read", "text": "Read inbox-playbook.md", "at": 2},
             {"kind": "read", "text": "Read its notes on calendar-direct", "at": 3}]
    assert _now_line(notes) != "Reading through the code"
    code = [{"kind": "read", "text": "Read store.py", "at": 1}, {"kind": "read", "text": "Read calls.py", "at": 2},
            {"kind": "read", "text": "Read 3 more", "at": 3}]
    assert _now_line(code) == "Reading through the code"


def test_each_home_card_shows_its_own_jobs_step(server, service, hermes):
    """Two Home jobs at once: the card under each exchange is that exchange's job, with that job's
    own latest step and name; never the other job's."""
    hermes.hold = True
    service.rt.title_call = lambda _context: None
    coordinator(service, {"reply": "Pulling together today.", "action": "start", "request": BRIEF},
                {"reply": "Looking at the build.", "action": "start", "request": "Review the parser code for the bug"})
    first = post(server, {"text": "Catch me up on what came in today.", "client_id": "c_two_a"})[1]
    second = post(server, {"text": "look at the parser bug", "client_id": "c_two_b"})[1]
    a, b = service.store.key_for_run(first["run_id"]), service.store.key_for_run(second["run_id"])
    wait_for(lambda: a and b)
    service.store.user_progress(a, "Checking the inbox", "Reading today's email.")
    service.store.user_progress(b, "Reading the parser", "Reading parser.py.")

    def cards():
        now = http(server.base_url, "GET", "/voice/now?tz=America/New_York", None, server.token)[1]
        by_run = {i.get("run_id"): i for i in now.get("items", []) if i.get("run_id")}
        return by_run if {first["run_id"], second["run_id"]} <= set(by_run) else None
    by_run = wait_for(cards)
    assert by_run[first["run_id"]]["line"] == "Checking the inbox", by_run[first["run_id"]]
    assert by_run[second["run_id"]]["line"] == "Reading the parser", by_run[second["run_id"]]
    assert by_run[first["run_id"]]["title"].startswith("Catch me up")
    assert "Voice task" not in json.dumps(by_run)
    hermes.hold = False
