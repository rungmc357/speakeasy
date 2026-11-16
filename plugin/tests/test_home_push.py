"""Build 86 (Geo, 11-16: "if it's responding to me in something I wrote in home, then I should get a
notification if I'm not on the app anymore"): Home's answer to the user's own Home message wakes a
closed phone, once, with the assistant's name over the answer's words; never for an acknowledgement
or a status tick; a Home job's result notifies as Home's answer instead of (never besides) its Done.
Also: the relay's real "unknown field" answer falls back to a plain wake-up (every sealed push died
there from 11-07), the app's away/here presence, and one log line per send. Invented data only."""
from __future__ import annotations

import base64
import json
import logging
import threading
import time

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from fakes import http, wait_for
from speakeasy import push as push_mod
from test_home_ack import post, script
from test_push import APNS, FakeRelay, device_id, register, relay  # noqa: F401

# What the deployed relay (from before sealed words) really answers a body with ``sealed`` in it.
REAL_OLD_RELAY_400 = (400, {"ok": False, "reason": "invalid",
                            "field": "exactly device_token, environment, kind, nonce, collapse_id, event_id"})


def _open(key: str, sealed: str) -> dict:
    raw = base64.urlsafe_b64decode(sealed + "=" * (-len(sealed) % 4))
    k = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
    return json.loads(ChaCha20Poly1305(k).decrypt(raw[:12], raw[12:], b"speakeasy-push-v1"))


def presence(server, state):
    return http(server.base_url, "POST", "/voice/push/presence", {"state": state}, server.token)


def away_phone(server, service, **register_extra):
    """A registered phone whose app said it left the screen. Returns its seal key."""
    _, reply = register(server, **register_extra)
    assert presence(server, "away")[0] == 200
    return reply["seal_key"]


# -- Home's answer -------------------------------------------------------------------------------

def test_a_home_answer_wakes_the_phone_when_the_app_is_away(server, service, hermes, relay):
    service.settings.patch({"assistant_name": "Juniper"})
    key = away_phone(server, service)
    script(service, {"reply": "Tomorrow is 61°F and dry, so no umbrella.", "action": "none"})
    status, body = post(server, {"text": "do I need an umbrella tomorrow", "client_id": "c_umb"})
    assert status == 200 and body["action"] == "none"
    (payload, _), = wait_for(lambda: relay.notifies)
    assert payload["kind"] == "reply"                     # the relay only knows its four kinds
    opened = _open(key, payload["sealed"])
    assert opened["title"] == "Juniper"                    # the assistant's name, no "New reply:" prefix
    assert opened["body"] == "Tomorrow is 61°F and dry, so no umbrella."
    assert opened["home"] == body["reply"]["id"]           # a tap opens Home at it ("m:ha_...")
    assert opened["run_id"] == ""
    # The words never reach the relay in the clear.
    assert "umbrella" not in json.dumps(payload)
    # The extension's read-back carries the Home entry too.
    _, text = http(server.base_url, "GET", f"/voice/push/events/{payload['event_id']}", token=server.token)
    assert text["home"] == opened["home"] and text["title"] == "Juniper"


def test_the_body_is_the_first_180_characters_of_the_answer(server, service, hermes, relay):
    key = away_phone(server, service)
    long = "The three quiet cafes near the river are " + ", ".join(f"Cafe {n} on Water Street" for n in range(20)) + "."
    script(service, {"reply": long, "action": "none"})
    post(server, {"text": "quiet cafes near the river?", "client_id": "c_long"})
    (payload, _), = wait_for(lambda: relay.notifies)
    words = _open(key, payload["sealed"])["body"]
    assert len(words) <= 180 and words.endswith("…") and long.startswith(words[:-1].rstrip())


def test_no_notification_while_the_app_is_open(server, service, hermes, relay):
    register(server)
    assert presence(server, "here")[0] == 200
    script(service, {"reply": "It's 3:10 PM.", "action": "none"})
    post(server, {"text": "what time is it", "client_id": "c_open"})
    time.sleep(0.3)
    assert relay.notifies == []


def test_an_older_app_counts_as_open_only_just_after_a_request(server, service, relay):
    """No away/here from the app (build 85 and before): the message's own request counts as open
    for HOME_PRESENCE_S, not the 45 s other kinds use (the answer lands seconds after the send)."""
    register(server)
    did = device_id(service)
    now = [1000.0]
    service.push.clock = lambda: now[0]
    service.push._seen.clear()
    service.push.seen(did)
    now[0] += push_mod.HOME_PRESENCE_S - 1
    assert service.push.notify("home", key="home:hu_a", title="Juniper", body="Yes.") == []
    now[0] += 2
    assert service.push.notify("home", key="home:hu_a", title="Juniper", body="Yes.") == [did]
    assert service.push.present(did)                       # Done etc. still use presence_s


def test_an_acknowledgement_never_notifies_and_the_jobs_result_does_once(server, service, hermes, relay):
    service.settings.patch({"assistant_name": "Juniper"})
    key = away_phone(server, service)
    hermes.hold = True
    gate = threading.Event()
    gate.set()
    script(service, {"reply": "Looking into the beta build now.", "action": "start",
                     "request": "Find why the beta build fails"},
           ack="I'll dig into why the beta build keeps failing.", gate=gate)
    status, body = post(server, {"text": "why does the beta build keep failing", "client_id": "c_job"})
    assert status == 200
    run_id = wait_for(lambda: service.home_chat.runs_for(body["message"]["id"]))
    run_id = next(iter(run_id))
    time.sleep(0.3)
    assert relay.notifies == []                            # the reply/ack announced work; nothing yet
    for run in hermes.runs.values():
        run["done"].set()
    (payload, _), = wait_for(lambda: relay.notifies)
    opened = _open(key, payload["sealed"])
    assert opened["title"] == "Juniper" and opened["home"] == f"c:{run_id}" and opened["run_id"] == run_id
    time.sleep(0.5)
    assert len(relay.notifies) == 1                        # no Done besides it


def test_a_slow_answer_after_its_acknowledgement_notifies_only_the_answer(server, service, hermes, relay):
    key = away_phone(server, service)
    gate = threading.Event()
    script(service, {"reply": "Paris is 6 hours ahead of you.", "action": "none"},
           gate=gate, ack="Let me work out the time difference.", ack_delay=0.0)
    status, body = post(server, {"text": "how far ahead is paris", "client_id": "c_slow"})
    assert body.get("pending") is True                     # the acknowledgement is up
    time.sleep(0.3)
    assert relay.notifies == []                            # ...and it never notifies
    gate.set()
    (payload, _), = wait_for(lambda: relay.notifies)
    assert "6 hours ahead" in _open(key, payload["sealed"])["body"]
    time.sleep(0.3)
    assert len(relay.notifies) == 1


def test_a_job_not_started_from_home_keeps_its_own_kind(server, service, hermes, relay):
    key = away_phone(server, service)
    assert http(server.base_url, "POST", "/voice/work", {"text": "Find why the checkout test fails"}, server.token)[0] == 202
    (payload, _), = wait_for(lambda: relay.notifies)
    opened = _open(key, payload["sealed"])
    assert opened["kind"] == "finished" and opened["title"].startswith("Done: ") and "home" not in opened


# -- kinds the phone picked ----------------------------------------------------------------------

def test_an_app_from_before_home_answers_gets_them_with_replies(server, service, relay):
    register(server, kinds=["finished", "reply"])
    did = device_id(service)
    service.push.away(did)
    assert service.push.notify("home", key="home:hu_b", title="Juniper", body="Yes.") == [did]
    service.devices.set_push(did, APNS, "sandbox", ["finished"])
    assert service.push.notify("home", key="home:hu_c", title="Juniper", body="Yes.") == []


def test_home_answers_turned_off_still_get_the_jobs_done(server, service, relay):
    """The app knows the kind (``known``) and left it out: a chat answer notifies nothing; a Home
    job's result comes as its plain Done instead."""
    key = away_phone(server, service, kinds=["finished", "needs_you", "failed", "reply"],
                     known=["finished", "needs_you", "failed", "reply", "home"])
    did = device_id(service)
    assert service.push.notify("home", key="home:hu_d", title="Juniper", body="Yes.") == []
    assert service.push.notify("home", key="run:run_x", title="Juniper", body="All green.", run_id="run_x",
                               home="c:run_x", fallback=("finished", "Check the tests", "All green.")) == [did]
    (payload, _), = relay.notifies
    opened = _open(key, payload["sealed"])
    assert opened["title"] == "Done: Check the tests" and "home" not in opened
    # The same job event never notifies twice, whichever way it went.
    assert service.push.notify("finished", key="run:run_x", title="Check the tests") == []


def test_registration_refuses_a_bad_known_list(server):
    assert register(server, known="home")[0] == 400


# -- presence ------------------------------------------------------------------------------------

def test_away_beats_a_lingering_stream_and_here_comes_back(server, service, relay):
    register(server)
    did = device_id(service)
    service.push.stream_opened(did)                        # a call's stream iOS hasn't closed yet
    assert service.push.present(did)
    assert presence(server, "away")[0] == 200
    assert not service.push.present(did)
    service.push.seen(did)                                 # a background request (Live Activity follow)
    assert not service.push.present(did)
    assert presence(server, "here")[0] == 200
    assert service.push.present(did)
    # An app killed in front stops counting once it goes quiet.
    now = [time.time() + push_mod.HERE_STALE_S + 1]
    service.push.clock = lambda: now[0]
    assert not service.push.present(did)


def test_presence_needs_a_known_state_and_the_device_token(server):
    assert presence(server, "asleep")[0] == 400
    assert http(server.base_url, "POST", "/voice/push/presence", {"state": "away"})[0] == 401


def test_status_says_home_answers_notify(server):
    assert http(server.base_url, "GET", "/voice/status", token=server.token)[1]["capabilities"]["push_home"] is True


# -- delivery ------------------------------------------------------------------------------------

def test_the_real_old_relay_answer_falls_back_to_a_plain_wake_up(server, service, relay, monkeypatch, caplog):
    """The deployed relay says which field it refused in ``field``, not ``reason``: the plugin missed
    it and every sealed notification died there (\"notification not sent (400 invalid)\" 11-07..11-15)."""
    register(server)
    monkeypatch.setattr(service.push, "present", lambda d: False)
    replies = iter([REAL_OLD_RELAY_400, (200, {"ok": True, "status": 200}), (200, {"ok": True, "status": 200})])

    def answer(url, body, headers):
        relay.requests.append((url, json.loads(body), dict(headers)))
        if url.endswith("/v1/register"):
            return 201, {"install_id": "ins_1abcdef", "secret": "s" * 43}
        return next(replies)
    service.push.relay._post = answer
    with caplog.at_level(logging.INFO, logger="hermes_plugins.speakeasy.push"):
        caplog.set_level(logging.INFO)
        service.push.notify("finished", key="run:r1", title="Tidy the downloads", run_id="r1")
        service.push.notify("failed", key="run:r2", title="Rename the photos", run_id="r2")
    sends = relay.notifies
    assert "sealed" in sends[0][0] and "sealed" not in sends[1][0]
    assert "sealed" not in sends[2][0]                    # and it stops trying sealed
    lines = [r.getMessage() for r in caplog.records if "notification" in r.getMessage()]
    assert any("notification sent (finished: relay 200, apns 200, plain)" in l for l in lines), lines
    assert any("notification sent (failed: relay 200, apns 200, plain)" in l for l in lines), lines
    assert not any("a1b2c3d4" in l for l in lines)         # never the token


def test_a_failed_send_logs_kind_and_relay_status_without_the_token(server, service, relay, monkeypatch, caplog):
    register(server)
    monkeypatch.setattr(service.push, "present", lambda d: False)
    relay.notify_reply = (429, {"ok": False, "reason": "rate_limited"})
    caplog.set_level(logging.INFO)
    service.push.notify("needs_you", key="q:z", title="Pick a venue", body="Which hall?")
    lines = [r.getMessage() for r in caplog.records if "notification" in r.getMessage()]
    assert any("notification not sent (needs_you: relay 429 rate_limited" in l for l in lines), lines
    assert not any("a1b2c3d4" in l or "Which hall" in l for l in lines)
