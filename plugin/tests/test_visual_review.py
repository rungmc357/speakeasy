"""Visual review: finished images wait on a review card until dismissed (across calls), the task
prompt asks for rendered results, the live 'looking at' image, and spoken 'show me'."""
from __future__ import annotations

import json

import pytest

from fakes import SDP, http, wait_for
from speakeasy.prompt import builder as P

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


def _image(home, name: str = "design.png"):
    folder = home / "cache" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(PNG)
    return path


def _finished_with_images(store, key: str, run_id: str, n: int = 2, settled_offset: float = 0.0) -> None:
    import time
    store.reserve_run(key, "vi_" + key, "item_" + key, 1)
    store.update_run(key, run_id, "completed")
    cards = [{"kind": "image", "name": f"design-{i}.png", "path": f"/nowhere/design-{i}.png"} for i in range(n)]
    store.set_result(key, {"spoken": "Here it is.", "full": "Done.", "cards": cards})
    if settled_offset:
        with store._lock, store._db:
            store._db.execute("UPDATE runs SET settled_at=? WHERE idem_key=?", (time.time() + settled_offset, key))


# -- task prompt ------------------------------------------------------------------------------------

def test_task_prompt_asks_for_rendered_visual_results():
    from speakeasy.prompt import builder as P
    prompt = P.build_task_prompt(P.Names("Hermes", "Sam"), 1, "User: design a landing page")
    assert "render or screenshot the finished result" in prompt
    assert "MEDIA:<absolute path>" in prompt and "what you are looking at" in prompt
    assert "{user_name}" not in prompt and "Sam can review it" in prompt


# -- store: review state survives calls, capped to the latest three ---------------------------------

def test_review_waits_until_dismissed_and_survives_new_calls(service):
    store = service.store
    _finished_with_images(store, "se_design", "run_design", n=2)
    work = store.work(idem_key="se_design")
    assert work["review"]["images"] == [1, 2]
    # a later call: the waiting review is carried onto its task list, like a waiting draft
    store.reserve_run("se_new", "vi_new", "item_new", 1)
    store.update_run("se_new", "run_new", "running")
    carried = [t for t in store.latest_tasks() if t["task_id"] == "item_se_design"]
    assert carried and carried[0]["review"]["images"] == [1, 2]
    assert carried[0]["carried"] is True  # the app never turns it into a text reply of this call
    assert "carried" not in next(t for t in store.latest_tasks() if t["task_id"] == "item_new")
    assert all("path" not in c for c in carried[0]["result"]["cards"])  # never a raw path to the app
    assert store.dismiss_review("run_design", [1]) == [1]
    assert store.work(idem_key="se_design")["review"]["images"] == [2]
    store.dismiss_review("run_design")
    assert "review" not in store.work(idem_key="se_design")
    # the finished task stays on the board (the workspace outlives a call) until it's cleared
    assert [t for t in store.latest_tasks() if t["task_id"] == "item_se_design"]
    store.dismiss(["run_design"])
    assert not [t for t in store.latest_tasks() if t["task_id"] == "item_se_design"]
    # the images stay in the task
    assert len(store.work(idem_key="se_design")["result"]["cards"]) == 2


def test_only_the_three_most_recent_reviews_are_carried(service):
    store = service.store
    for i in range(5):
        _finished_with_images(store, f"se_{i}", f"run_{i}", n=1, settled_offset=i)
    assert store.keys_with_pending_reviews() == ["se_2", "se_3", "se_4"]


def test_running_or_failed_tasks_have_no_review(service):
    store = service.store
    store.reserve_run("se_run", "vi_run", "item_run", 1)
    store.update_run("se_run", "run_run", "running")
    assert "review" not in store.work(idem_key="se_run")
    assert store.dismiss_review("run_run") == []


# -- HTTP: authenticated dismiss route -------------------------------------------------------------

def test_dismiss_route_is_authenticated_and_validated(server, service):
    _finished_with_images(service.store, "se_http", "run_http", n=2)
    url = "/voice/reviews/run_http/dismiss"
    assert http(server.base_url, "POST", url, {})[0] == 401
    assert http(server.base_url, "POST", url, {"cards": ["1"]}, server.token)[0] == 400
    assert http(server.base_url, "POST", url, {"cards": [9]}, server.token)[0] == 400
    assert http(server.base_url, "POST", url, {"extra": 1}, server.token)[0] == 400
    assert http(server.base_url, "POST", "/voice/reviews/run_nope/dismiss", {}, server.token)[0] == 404
    status, body = http(server.base_url, "POST", url, {"cards": [2]}, server.token)
    assert status == 200 and body["dismissed"] == [2]
    status, body = http(server.base_url, "POST", url, {}, server.token)
    assert status == 200 and body["dismissed"] == [1, 2]
    assert "review" not in service.store.work(run_id="run_http")


def test_finished_design_pops_a_review_card_end_to_end(server, service, hermes, home):
    design = _image(home)
    hermes.responder = lambda prompt, sid: f"Here's the landing page.\nMEDIA:{design}\nDONE: Design done\nSPOKEN: It's ready."
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": "req_design_1"})
    assert status == 201, session
    worker = service.workers[-1]
    worker.delegate("call_design_1", "Design a landing page for the bakery")
    task = wait_for(lambda: next((t for t in service.interaction(session["interaction_id"]).feed.last["tasks"] or []
                                  if t.get("review")), None))
    assert task["review"]["images"] == [1]
    raw = json.dumps(task)
    assert str(design) not in raw and "MEDIA:" not in raw
    status, data = http(server.base_url, "GET", f"/voice/card-image/{task['run_id']}/1", token=server.token)
    assert status == 200 and data == PNG
    http(server.base_url, "POST", f"/voice/reviews/{task['run_id']}/dismiss", {}, server.token)
    wait_for(lambda: not any(t.get("review") for t in service.interaction(session["interaction_id"]).feed.last["tasks"]))


# -- live view: what the running task is looking at -------------------------------------------------

def test_live_refs_are_vetted_against_roots(tmp_path):
    from speakeasy.cards import default_image_roots
    from speakeasy.text import live_images_in
    root = tmp_path / "home"
    shot = _image(root, "browser_screenshot_1.png")
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)
    roots = default_image_roots(root)
    assert roots[0] == root
    # its path string starts with the root's: refused only if containment is by path component
    beside = root.with_name(root.name + "-outside.png")
    beside.write_bytes(PNG)
    preview = json.dumps({"success": True, "analysis": "A login page", "screenshot_path": str(shot)})
    assert live_images_in(preview, roots) == [("path", str(shot.resolve()), shot.name)]
    assert live_images_in(f"Looking now.\nMEDIA:{shot}\n", roots)[0][1] == str(shot.resolve())
    assert live_images_in(json.dumps({"note": f"share via MEDIA:{shot}"}), roots)[0][1] == str(shot.resolve())
    assert live_images_in(f"MEDIA:{outside}", roots) == []
    assert live_images_in(json.dumps({"screenshot_path": str(outside)}), roots) == []
    assert str(beside).startswith(str(root))
    assert live_images_in(f"MEDIA:{beside}", roots) == []
    assert live_images_in(json.dumps({"screenshot_path": str(beside)}), roots) == []
    assert live_images_in("MEDIA:/etc/passwd", roots) == []
    assert live_images_in("MEDIA:https://127.0.0.1/a.png", roots) == []


def _held_run_with(server, service, hermes, events, key="req_live_1"):
    hermes.hold = True
    hermes.live_events = events
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": key})
    assert status == 201, session
    worker = service.workers[-1]
    worker.delegate("call_live_1", "Redesign the bakery landing page")
    return session, worker


def test_live_image_from_tool_preview_and_media_seen(server, service, hermes, home, tmp_path):
    shot = _image(home, "browser_screenshot_live.png")
    design = _image(home, "draft-hero.png")
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    session, _ = _held_run_with(server, service, hermes, [
        {"event": "tool.completed", "tool": "browser_vision",
         "preview": json.dumps({"success": True, "screenshot_path": str(shot)})},
        {"event": "media.seen", "path": str(outside), "name": "nope", "source": "viewed"},  # ignored
        {"event": "media.seen", "path": str(design), "name": "Hero draft", "source": "generated"},
    ])
    feed = service.interaction(session["interaction_id"]).feed
    task = wait_for(lambda: next((t for t in feed.last["tasks"] or [] if (t.get("live_image") or {}).get("name") == "Hero draft"), None))
    live = task["live_image"]
    assert live["source"] == "generated" and live["seq"] == 2 and set(live) == {"name", "source", "seq", "at"}
    assert str(design) not in json.dumps(feed.last["tasks"]) and str(shot) not in json.dumps(feed.last["tasks"])
    run_id = task["run_id"]
    assert http(server.base_url, "GET", f"/voice/live-image/{run_id}")[0] == 401
    status, data = http(server.base_url, "GET", f"/voice/live-image/{run_id}", token=server.token)
    assert status == 200 and data == PNG
    assert http(server.base_url, "GET", "/voice/live-image/run_nope", token=server.token)[0] == 404
    # a file swapped for a symlink after it was seen is refused on read
    design.unlink()
    design.symlink_to(outside)
    assert http(server.base_url, "GET", f"/voice/live-image/{run_id}", token=server.token)[0] == 404
    hermes.hold = False
    for run in hermes.runs.values():
        run["done"].set()


def test_live_image_from_interim_media_tag(server, service, hermes, home):
    shot = _image(home, "page.png")
    session, _ = _held_run_with(server, service, hermes, [
        {"event": "message.interim", "text": f"STATUS: Checking the page\nDETAIL: Here it is\nMEDIA:{shot}"}])
    feed = service.interaction(session["interaction_id"]).feed
    task = wait_for(lambda: next((t for t in feed.last["tasks"] or [] if t.get("live_image")), None))
    assert task["live_image"]["name"] == "page.png"
    for run in hermes.runs.values():
        run["done"].set()


# -- "show me" --------------------------------------------------------------------------------------

@pytest.mark.parametrize("said", ["Show me", "show me.", "What are you looking at?", "Let me see it", "let me see",
                                  "Can I see the design?", "can you show me what you're looking at",
                                  "Pull it up", "show me the landing page design", "OK, show me what you got"])
def test_show_me_intent(said):
    from speakeasy import router
    assert router.is_show_me(said), said


@pytest.mark.parametrize("said", ["Show me flights to Paris next Friday", "show me how to cook rice",
                                  "Design a landing page for the bakery", "what are you doing tonight with the kids",
                                  "Let me see if Pat is free on Tuesday"])
def test_show_me_leaves_real_work_alone(said):
    from speakeasy import router
    assert not router.is_show_me(said), said


def test_show_me_target_prefers_named_then_imaged_then_running():
    from speakeasy.router import OpenTask, show_me_target
    tasks = [OpenTask("t1", "Design the bakery landing page", "completed"),
             OpenTask("t2", "Compare flour suppliers", "running"),
             OpenTask("t3", "Book a dentist", "running")]
    assert show_me_target("show me the bakery page", tasks, set()).task_id == "t1"
    assert show_me_target("show me", tasks, {"t1"}).task_id == "t1"
    assert show_me_target("show me", tasks, {"t1", "t2"}).task_id == "t2"
    assert show_me_target("show me", tasks, set()).task_id == "t3"
    assert show_me_target("show me", [OpenTask("t1", "x", "completed")], set()) is None


def _spoken(worker):
    return [c for k, _, c in worker.sent if k == "session.commentary.append"]


def _shows(feed):
    return [p for _, kind, p in list(feed.ring) if kind == "show"]


def test_show_me_opens_live_image_without_new_work(server, service, hermes, home):
    shot = _image(home, "browser_screenshot_9.png")
    session, worker = _held_run_with(server, service, hermes, [
        {"event": "tool.completed", "tool": "browser_vision", "preview": json.dumps({"screenshot_path": str(shot)})}])
    feed = service.interaction(session["interaction_id"]).feed
    wait_for(lambda: any(t.get("live_image") for t in feed.last["tasks"] or []))
    runs_before = len(hermes.runs)
    worker.feed({"type": "session.output_transcript.delta", "delta": "On it.", "start_ms": 3, "end_ms": 4})
    worker.delegate("call_show_1", "What are you looking at?")
    show = wait_for(lambda: _shows(feed))[-1]
    assert show["image"] == "live" and show["task_id"] == "call_live_1" and set(show) == {"task_id", "run_id", "image", "seq"}
    assert str(shot) not in json.dumps(show)
    wait_for(lambda: P.SHOW_ME_ON_SCREEN in _spoken(worker))
    assert len(hermes.runs) == runs_before, "show me must not start new work"
    for run in hermes.runs.values():
        run["done"].set()


def test_show_me_without_image_steers_for_a_screenshot(server, service, hermes, home):
    session, worker = _held_run_with(server, service, hermes, [])
    feed = service.interaction(session["interaction_id"]).feed
    wait_for(lambda: any(t.get("run_id") for t in feed.last["tasks"] or []))
    runs_before = len(hermes.runs)
    worker.feed({"type": "session.output_transcript.delta", "delta": "On it.", "start_ms": 3, "end_ms": 4})
    worker.delegate("call_show_2", "show me")
    wait_for(lambda: hermes.steers)
    run_id, text = hermes.steers[-1]
    assert "MEDIA:" in text and "screenshot" in text
    wait_for(lambda: P.SHOW_ME_REQUESTED in _spoken(worker))
    assert P.SHOW_ME_ON_SCREEN not in _spoken(worker), "never claim it's on screen before the image exists"
    assert _shows(feed)[-1]["image"] == "detail"
    assert len(hermes.runs) == runs_before
    # the screenshot arrives: the panel is told to open it
    shot = _image(home, "asked.png")
    backend = next(r for r in service.interaction(session["interaction_id"]).runs.values() if r.run_id == run_id)
    import asyncio
    asyncio.run(worker.handle_hermes_event(backend, {"event": "message.interim", "text": f"Here it is\nMEDIA:{shot}"}))
    assert _shows(feed)[-1]["image"] == "live"
    for run in hermes.runs.values():
        run["done"].set()


def test_show_me_after_the_call_opens_the_waiting_review(server, service, hermes):
    _finished_with_images(service.store, "se_earlier", "run_earlier", n=1)
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": "req_show_b"})
    assert status == 201, session
    worker = service.workers[-1]
    feed = service.interaction(session["interaction_id"]).feed
    runs_before = len(hermes.runs)
    worker.delegate("call_show_3", "Can I see the design?")
    show = wait_for(lambda: _shows(feed))[-1]
    assert show["image"] == "review" and show["run_id"] == "run_earlier"
    carried = wait_for(lambda: [t for t in feed.last["tasks"] or [] if t.get("run_id") == "run_earlier"])
    assert carried and carried[0]["task_id"] == show["task_id"], "the app can find the task it's told to show"
    wait_for(lambda: P.SHOW_ME_ON_SCREEN in _spoken(worker))
    assert len(hermes.runs) == runs_before


def test_show_me_with_nothing_to_show_is_ordinary_work(server, service, hermes):
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": "req_show_c"})
    worker = service.workers[-1]
    worker.delegate("call_show_4", "show me")
    wait_for(lambda: len(hermes.runs) == 1)
    assert not _shows(service.interaction(session["interaction_id"]).feed)


def test_show_me_rule_never_teaches_a_premature_claim():
    rules = (P.PROMPT_DIR / "rules.md").read_text()
    assert "on your screen" not in rules.lower(), "the voice would parrot it before the image exists"
    assert "Images" in rules and "hand the request off" in rules
