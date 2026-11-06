"""'What do those speakers look like?': when the user wants to SEE something, the answer comes back
as a picture that opens on screen, not just words."""
from __future__ import annotations

import json

import pytest

from fakes import SDP, FakeLiveWorker, FakeTransport, wait_for
from speakeasy import router
from speakeasy.prompt import builder as P

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
SHOW = '{"follow_up_task_id": null, "conversation": null, "parts": ["x"], "channel": null, "show": true}'
TELL = '{"follow_up_task_id": null, "conversation": null, "parts": ["x"], "channel": null, "show": false}'


@pytest.mark.parametrize("reply,expected", [(SHOW, True), (TELL, False),
                                            ('{"follow_up_task_id": null, "parts": ["x"], "channel": null}', False),
                                            ('{"parts": ["x"], "show": "yes"}', False)])
def test_model_show_flag_is_strict(reply, expected):
    d = router.parse_decision(reply, "what do the office speakers look like", [], [])
    assert d is not None and d.show is expected


def test_prompt_tells_the_model_what_show_means():
    system = router.route_messages("what do the speakers look like", [], [])[0]["content"]
    assert '"show"' in system and "look like" in system


def _svc(home, reply):
    from speakeasy.service import VoiceService
    workers: list[FakeLiveWorker] = []
    svc = VoiceService(home, notifier=None, start_threads=False, codex_factory=FakeTransport,
                       openai_negotiate=lambda key, payload: {"session": {"id": "sess_fake"}, "transport": {"sdp": "v=0\r\n"}},
                       openai_worker=lambda rt, i: workers.append(FakeLiveWorker(rt, i)) or workers[-1],
                       route_call=lambda m: reply)
    svc.settings.patch({"voice": {"provider": "openai"}})
    return svc, workers


def _shows(feed):
    return [p for _, kind, p in list(feed.ring) if kind == "show"]


def test_wanting_to_see_something_asks_for_a_picture_and_opens_it(home, hermes):
    folder = home / "cache" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    photo = folder / "speakers.png"
    photo.write_bytes(PNG)
    hermes.responder = lambda prompt, sid: (f"The Kanto YU4 in black.\nMEDIA:{photo}\n"
                                            "DONE: Speakers shown\nSPOKEN: Those are the Kantos.")
    svc, workers = _svc(home, SHOW)
    try:
        created = svc.create_session({"sdp": SDP}, "req_see_1")
        workers[-1].delegate("call_see_1", "How do the office speakers you recommended look?")
        wait_for(lambda: hermes.calls)
        assert P.SHOW_IT_FOCUS.strip() in hermes.calls[-1]["input"], "the task is told to bring back a picture"
        feed = svc.interaction(created["interaction_id"]).feed
        show = wait_for(lambda: _shows(feed))[-1]
        assert show["image"] == "review" and show["task_id"] == "call_see_1"
        assert str(photo) not in json.dumps(show)
    finally:
        svc.close()


def test_ordinary_questions_do_not_pop_anything(home, hermes):
    svc, workers = _svc(home, TELL)
    try:
        created = svc.create_session({"sdp": SDP}, "req_see_2")
        workers[-1].delegate("call_see_2", "How much were the office speakers?")
        wait_for(lambda: hermes.calls)
        assert P.SHOW_IT_FOCUS.strip() not in hermes.calls[-1]["input"]
        wait_for(lambda: any(t.get("status") == "completed" for t in
                             svc.interaction(created["interaction_id"]).feed.last["tasks"] or []))
        assert not _shows(svc.interaction(created["interaction_id"]).feed)
        notes = wait_for(lambda: [c for _, _, c in workers[-1].sent if "sent no picture" in c])
        assert "Don't say anything is on screen" in notes[0]
    finally:
        svc.close()


def test_a_picture_from_a_thread_task_still_pops_up(home, hermes):
    """The Lisbon hotel case: a channel that opens a thread per task. Hermes answers in the thread
    (no Hermes run id), with two photos. The review card and the on-screen opening still happen."""
    from speakeasy import threads
    folder = home / "cache" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    photos = [folder / "me_c.jpg", folder / "me_b.jpg"]
    for p in photos:
        p.write_bytes(b"\xff\xd8\xff\xe0" + b"\0" * 64)

    class Threads:
        def __init__(self):
            self.opened = []

        def available(self, target):
            return True

        def open(self, target, *, message, title, delivery_id):
            self.opened.append(message)
            return threads.Opened("speakeasy-x", "999", "discord")

        def wait(self, opened, on_session, on_title=None):
            on_session("thread_session_1")
            return (f"Voice: hotel\nThe Lisbon hotel, front view:\nMEDIA:{photos[0]}\n"
                    f"And a typical room:\nMEDIA:{photos[1]}")

    svc, workers = _svc(home, SHOW.replace('"channel": null', '"channel": "#life"'))
    runner = Threads()
    svc.rt.threads = runner
    svc.settings.patch({"delivery": {"target": "telegram:555", "channels": [
        {"label": "#life", "target": "discord:222", "topic": "life", "new_thread": True}], "mode": "topic"}})
    try:
        created = svc.create_session({"sdp": SDP}, "req_see_3")
        workers[-1].delegate("call_see_3", "show the Lisbon hotel photo")
        feed = svc.interaction(created["interaction_id"]).feed
        show = wait_for(lambda: _shows(feed))[-1]
        assert P.SHOW_IT_FOCUS.strip() in runner.opened[0], "the thread task is told to bring back a picture"
        assert show["image"] == "review" and show["run_id"]
        task = next(t for t in feed.last["tasks"] if t.get("task_id") == "call_see_3")
        assert task["review"]["images"] == [1, 2]
        data, mime = svc.card_image(show["run_id"], 1)
        assert data.startswith(b"\xff\xd8") and mime == "image/jpeg"
        assert all(str(p) not in json.dumps(feed.last) for p in photos)
        # The picture goes up first; the voice's note about it follows once the answer is told.
        wait_for(lambda: any("2 pictures from this task are on the user's screen" in c for _, _, c in workers[-1].sent))
    finally:
        svc.close()
