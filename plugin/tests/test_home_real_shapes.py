"""Home on real-shaped data: titles, status lines, asks, duplicates, pictures, staleness, the pill.

Every fixture is invented, shaped like what real work stores: handle-style worker titles about 80
characters long, three-clause worker updates, voice asks with filler and the call's own notes, a
call that started ten jobs (three near-duplicates), a wide contact sheet named like a file.
"""
from __future__ import annotations

import datetime as dt
import io
import os

import pytest

from speakeasy import home_words as W
from speakeasy import now as now_mod
from speakeasy.stream import build_stream, widget_state
from speakeasy.text import public_result
from test_history import state  # noqa: F401  (fixture)

# -- 1. short human titles ----------------------------------------------------------------------

LONG_TITLES = [
    "QRX-41: Video attachments — send clips from Photos/Files/Share, real-model checks, fixes, suites",
    "r9 debug: tablet and phone walkthrough vs 8 — triage, fix, regression tests",
    "Lantern mockups v4b: additions (tablet, top tracker states, idle screen, no speaker) — restart",
    "Lantern v4 (Plain): every work use case through the real card components",
    "Refactor Lantern live activities for release 12 (continued)",
    "Check flight times #311 and rebook if the 6 PM is late",
    "PR 88 steward: triage incoming changes nightly",
]


@pytest.mark.parametrize("raw", LONG_TITLES)
def test_titles_are_short_plain_and_never_cut_mid_phrase(raw):
    t = W.short_title(raw)
    assert 0 < len(t) <= W.TITLE_MAX, (raw, t)
    assert "\u2026" not in t and "QRX-41" not in t and "#311" not in t and "PR 88" not in t, t
    assert "(" not in t and "restart" not in t.lower() and "continued" not in t.lower(), t
    last = t.split()[-1].lower()
    assert last not in {"for", "with", "to", "and", "the", "of", "a", "vs", "not"}, t


def test_title_examples_read_like_a_person_wrote_them():
    assert W.short_title(LONG_TITLES[0]) == "Video attachments: send clips"
    assert W.short_title(LONG_TITLES[1]) == "Tablet and phone walkthrough"
    assert W.short_title(LONG_TITLES[3]) == "Lantern v4: every work use case"
    assert W.short_title("Check the boiler") == "Check the boiler"       # short ones stay as they are
    # A long ask keeps its subject's place, not its ask verb (live: "Get the weather forecast").
    assert W.short_title("Get the weather forecast for Tucson for tomorrow") == "Weather forecast for Tucson"
    assert W.short_title("Look up the sunrise time in Boston for tomorrow") == "Sunrise time in Boston"
    assert W.short_title("iPhone layout pass") == "iPhone layout pass"     # brand casing kept


def test_coordinator_job_titles_keep_their_subject_when_cut():
    # Shapes the Home coordinator writes for real jobs: a handle at the end, a long ask verb.
    assert W.short_title("Create a Linear issue card for ABC-42") == "Create a Linear issue card"
    assert W.short_title("Create and display an invented example Linear issue") == "Invented example Linear issue"
    assert W.short_title("Look up three easy weeknight dinner recipes online") == "Easy weeknight dinner recipes"
    assert W.short_title("Find a hardware store open Sunday near the station") == "Hardware store open Sunday"
    assert not W.short_title("Find out what time the user's market opens on weekends").endswith("user's")


def test_a_rescoped_job_is_named_for_what_it_became():
    # The titler's retitle input carries the newest ask; the name must fit Home either way.
    from speakeasy.router import clean_task_title
    title, why = clean_task_title('{"title": "Reconcile the integration candidate acceptance documentation"}',
                                  "Request: now do build 12")
    assert title is None and "characters" in why                      # far too long for Home's line: retried
    title, why = clean_task_title('{"title": "Build 12 release notes"}', "Request: now do build 12")
    assert title == "Build 12 release notes", why


# -- 2. status lines ----------------------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ("Fixed on the branch with a test; waiting for the release step to pick it up.",
     "Waiting for the release step"),
    ("Resuming after an earlier run stopped: reshooting all 40 screens in light and dark",
     "Reshooting all 40 screens"),
    ("Fixed on the release branch and verified on the phone and tablet simulators",
     "Fixed on the release branch and verified"),
    ("Checking project files", "Checking project files"),
])
def test_status_is_one_short_present_tense_line(raw, want):
    got = W.status_line(raw)
    assert got == want and len(got) <= W.STATUS_MAX


@pytest.mark.parametrize("raw", [
    "Running `pytest -q tests/test_x.py` on the build host",
    "ssh build-2 'pkill -f xcodebuild'",
    "read_file + 3 commands",
    "Calling search_files(pattern='x')",
])
def test_tool_narration_is_never_a_status(raw):
    assert W.status_line(raw) is None
    assert now_mod.live_line(raw) == ""


def test_a_finished_job_has_no_status_line():
    assert W.status_line("Resuming: reviewing the release", finished=True) is None


# -- 3. asks as the thread shows them -----------------------------------------------------------

def test_asks_lose_filler_and_the_calls_notes_and_use_digits():
    raw = ("Okay so um can you, can you check whether the three studios are online "
           "(Sam is looking at: the dashboard) (following up: earlier check)")
    assert W.clean_ask(raw) == "Can you check whether the 3 studios are online"
    assert W.clean_ask("Hey Juniper, book twenty five minutes at seven pm", "Juniper") == "Book 25 minutes at 7 pm"
    assert W.clean_ask(": No worries. Want to tidy the notes?") == "No worries. Want to tidy the notes?"
    # "kind of"/"sort of" are words, not filler: "what kind of job" keeps them.
    assert W.clean_ask("Do we have, like, some sort of job for that") == "Do we have some sort of job for that"
    assert W.clean_ask("Do we have a, like, some sort of job") == "Do we have some sort of job"
    assert W.clean_ask("hey") == "Hey"                                 # a bare greeting is the message


def test_one_stays_a_word_where_it_is_not_a_count():
    assert W.digits("the one on the left") == "the one on the left"
    assert W.digits("one more thing") == "one more thing"
    assert W.digits("one hour and two tickets") == "1 hour and 2 tickets"


# -- 4. replaced questions and stale next steps -------------------------------------------------

def _q(qid, session, asked, call=None, prompt="Which layout should the panel use?"):
    return {"id": qid, "kind": now_mod.QUESTION, "session_id": session, "_asked": asked, "_call": call,
            "_topic": prompt, "title": "Panel layout", "question": {"prompt": prompt}, "unread": True}


def test_a_newer_question_in_the_same_conversation_replaces_the_older():
    items = [_q("q:r1", "s1", 100.0), _q("q:r2", "s1", 200.0, prompt="Wide or narrow, now that both fit?"),
             _q("q:r3", "s2", 150.0)]
    out = now_mod.mark_replaced(items, {i["id"]: i["session_id"] for i in items})
    old = next(i for i in out if i["id"] == "q:r1")
    assert old["kind"] == now_mod.WAITING and old["replaced"] == {"by": "q:r2", "at": 200.0}
    assert old["line"] == now_mod.REPLACED_LINE and old["question"]["replaced"]["by"] == "q:r2"
    assert [i["kind"] for i in out if i["id"] != "q:r1"] == [now_mod.QUESTION, now_mod.QUESTION]
    assert widget_state(old, False) == "replaced"


def test_the_same_call_asking_again_replaces_its_earlier_ask():
    items = [_q("q:a", "s1", 100.0, call="vi_1"), _q("q:b", "s9", 160.0, call="vi_1",
                                                     prompt="Which layout should the side panel use now?")]
    out = now_mod.mark_replaced(items, {i["id"]: i["session_id"] for i in items})
    assert out[0]["replaced"]["by"] == "q:b" and out[1]["kind"] == now_mod.QUESTION


def test_a_replaced_question_shows_closed_in_the_thread():
    item = {"id": "q:r1", "run_id": "r1", "kind": now_mod.WAITING, "started_at": 10.0,
            "replaced": {"by": "q:r2", "at": 20.0}}
    s = build_stream([item], {}, set(), 100.0, dt.timezone.utc)
    card = next(e for e in s["entries"] if e["kind"] == "card")
    assert card["widget"] == "replaced" and card["replaced_by"] == "q:r2" and s["needs_you"] == []


def test_next_steps_expire_once_their_time_has_passed_or_the_job_finished():
    views = [{"kind": "timeline", "days": [{"label": "Saturday", "items": [
        {"when": "2026-03-07T09:00", "what": "Pick a layout", "state": "next"},
        {"when": "2026-03-09T09:00", "what": "Ship the release", "state": "next"},
        {"when": "2026-03-07T08:00", "what": "Shoot screens", "state": "now"},
        {"when": "Tonight", "what": "Read the notes", "state": "next"}]}]}]
    now = dt.datetime(2026, 3, 8, 12, 0, tzinfo=dt.timezone.utc).timestamp()
    out = W.expire_steps(views, True, now, dt.timezone.utc)[0]["days"][0]["items"]
    assert [i.get("state") for i in out] == [None, "next", None, "next"]
    assert len(out) == 4                                       # rows stay; only stale states go
    steps = [{"id": "a", "title": "Pick a layout", "due_at": now - 60}, {"id": "b", "title": "Ship it"},
             {"id": "c", "title": "Decide on the update", "decides": "q9"}, {"id": "d", "title": "Ship it"}]
    assert [s["id"] for s in W.fresh_steps(steps, now, decided={"q9"})] == ["b"]


# -- 5. one call, one card per thing -----------------------------------------------------------

def test_a_ten_job_call_folds_near_duplicates_and_keeps_distinct_jobs():
    titles = ["Fix picture sending and carousels", "Fix picture sending and the carousel",
              "Fix broken picture sending and carousels", "Check the flight to Lisbon",
              "Book a table for Friday", "Draft the landlord email", "Turn off the porch light",
              "Find the boiler manual", "Summarize the release notes", "Order more coffee filters"]
    items = [{"id": f"t:{n}", "kind": now_mod.WORKING, "_call": "vi_7", "title": t} for n, t in enumerate(titles)]
    items.append({"id": "t:other", "kind": now_mod.WORKING, "_call": "vi_8", "title": titles[0]})
    out = now_mod.fold_call_duplicates(items)
    assert len(out) == 9 and out[0]["also"] == 2 and out[0]["also_ids"] == ["t:1", "t:2"]
    assert all("_fold" not in i for i in items)
    assert not W.near_duplicate("Check the flight to Lisbon", "Check the flight to Denver")


def test_the_call_card_list_collapses_past_a_few():
    assert W.collapse(list("abcd")) == (list("abcd"), 0)
    assert W.collapse(list("abcdefghij")) == (list("abc"), 7)


# -- 6. pictures -------------------------------------------------------------------------------

def test_captions_are_words_never_file_names():
    for bad in ("sheet-light-v3.png", "IMG_2041", "/tmp/out/shot.png", "home_v7_contact", "a-b-c-d", "Screenshot 12"):
        assert W.picture_caption(bad) is None, bad
    assert W.picture_caption("contact-sheet.jpg", "Home screens, light and dark") == "Home screens, light and dark"


def _png(w, h):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    for x in range(w * 3 // 4, w - 10, 12):       # the busy frame sits on the right
        d.line([(x, 0), (x, h)], fill="black", width=3)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_a_wide_contact_sheet_gets_a_readable_portrait_hero():
    assert W.hero_box(1200, 900) is None                       # not wide: shown whole
    assert W.hero_box(4000, 1000) == (0, 0, 800, 1000)        # no detail map: the first frame
    data, mime = W.hero_crop(_png(4000, 1000))
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    assert mime == "image/jpeg" and img.size == (800, 1000)
    assert W.hero_crop(_png(800, 800)) is None


def test_missing_pictures_say_so_and_keep_their_place(tmp_path):
    there = tmp_path / "board.png"
    there.write_bytes(_png(400, 300))
    result = {"cards": [{"kind": "image", "path": str(tmp_path / "gone.png"), "title": "gone-v2.png"},
                        {"kind": "image", "path": str(there), "title": "The board, light"}]}
    cards = public_result(result)["cards"]
    assert len(cards) == 2 and cards[0]["available"] is False and "caption" not in cards[0]
    assert "available" not in cards[1] and cards[1]["caption"] == "The board, light"
    assert all("path" not in c for c in cards)


def test_home_thumbs_drop_dead_pictures_and_mark_wide_ones(tmp_path):
    wide, gone = tmp_path / "sheet.png", tmp_path / "gone.png"
    wide.write_bytes(_png(3000, 800))
    raw = [{"kind": "image", "path": str(gone)}, {"kind": "image", "path": str(wide), "title": "sheet.png"}]
    thumbs = now_mod._thumbs({"cards": [{"kind": "image"}, {"kind": "image"}]}, "r1", raw)
    assert thumbs == [{"url": "/voice/card-image/r1/2", "aspect": 3.75, "hero": "/voice/card-image/r1/2?v=hero"}]


# -- 7. tracker pill ---------------------------------------------------------------------------

def test_the_pill_fits_at_real_volume_and_never_truncates_a_title():
    assert W.pill_text(3, 5) == "3 need you \u00b7 5 running"
    assert len(W.pill_text(3, 5)) <= W.PILL_MAX
    assert W.pill_text(1, 0, one_title=LONG_TITLES[0]) == "Video attachments"
    assert W.pill_text(12, 140) == "12 need you"                # too long together: what needs you wins
    assert W.pill_text(0, 0) == "All quiet"


# -- through /voice/now: a ten-job call, invented ---------------------------------------------------

def test_a_ten_job_call_leaves_a_short_call_card_and_one_card_per_thing(server, service, state):
    import time as _t
    from test_history import get
    from test_now import task
    from test_now_call import live_call, on_call
    store, now = service.store, _t.time()
    call = live_call(service, interaction_id="vi_ten", started=now - 900)
    asks = ["Fix picture sending and carousels", "Fix picture sending and the carousel",
            "Fix broken picture sending and carousels", "Check the ferry to the island",
            "Book a table for Friday", "Draft the landlord email about the boiler", "Turn off the porch light",
            "Find the boiler manual", "Summarize the release notes", "Order more coffee filters"]
    for n, ask in enumerate(asks):
        task(store, f"k_{n}", "completed", session=f"s_{n}", request=ask, interaction="vi_ten",
             result={"spoken": f"Done with {ask.lower()}.", "headline": f"Done: {ask.lower()}."}, settled=now - 600 + n)
        store.set_title(f"k_{n}", ask)
        on_call(call, f"d_k_{n}", f"k_{n}")
    with call.lock:
        call.call_closed, call.ended_at = True, now - 30
    _, body = get(server, "/voice/now")
    card = body["call_summary"]
    assert len(card["things"]) == 3 and card["more_things"] == 5          # 8 distinct things, 3 shown
    assert sum("picture" in t.lower() for t in card["things"]) == 1
    titles = [i["title"] for i in body["items"]]
    assert sum("picture" in t.lower() for t in titles) == 1, titles
    assert all(len(t) <= W.TITLE_MAX for t in titles), titles
    pic = next(i for i in body["items"] if "picture" in i["title"].lower())
    assert pic["also"] == 2
    assert body["pill"] == "All quiet"                                    # all done: nothing to count


def test_a_job_with_no_real_step_yet_shows_no_stock_status():
    """Before the first real status a running card shows its title alone: never "Working…"."""
    assert now_mod.live_line(None) == ""
    assert now_mod.live_line("", None) == ""
    assert now_mod.live_line("Running web_search", "Running ls -la") == ""
    assert now_mod.live_line("Comparing three desks") == "Comparing three desks"


@pytest.mark.parametrize("stock", ["Working on it", "Running a command", "Working\u2026", "working."])
def test_stock_placeholders_are_not_a_status(stock):
    assert now_mod.live_line(stock) == ""


def test_a_running_line_names_the_site_not_the_url_path():
    from speakeasy.now import live_line
    assert live_line("Reading api.example-weather.com/v1/forecast") == "Reading api.example-weather.com"
    assert live_line("Reading example.org") == "Reading example.org"
