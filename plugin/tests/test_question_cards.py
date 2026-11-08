"""A task that needs a decision asks it as a card: numbered answers, read aloud, answered by tap or voice."""
from speakeasy import text, views
from speakeasy.prompt import builder as P


ASK = """Voice: plan the garden beds
I sketched two bed layouts and priced the soil.

```speakeasy-question
{"question": "Which layout should I order soil for?", "options": ["Four raised beds", "One long bed"], "recommended": 1}
```
DONE: garden
SPOKEN: I sketched two layouts and priced the soil."""


def test_a_question_becomes_a_card_and_is_said_last():
    r = text.split_result(ASK, ())
    q = r["question"]
    assert q["kind"] == "question" and q["options"] == ["Four raised beds", "One long bed"] and q["recommended"] == 1
    assert r["views"][0] == q
    assert r["spoken"].startswith("I sketched two layouts")
    assert "which layout should I order soil for? Four raised beds or One long bed." in r["spoken"]
    assert r["spoken"].endswith("I'd go with one long bed.")
    assert "speakeasy-question" not in r["full"]
    assert "1. Four raised beds" in r["full"] and "2. One long bed (recommended)" in r["full"]


def test_bad_questions_are_dropped_quietly():
    for block in ('{"question": "Go?", "options": ["Yes"]}', "not json", '{"options": ["a", "b"]}'):
        out, q = views.extract_question(f"Done.\n```speakeasy-question\n{block}\n```")
        assert q is None and "speakeasy-question" not in out
    out, q = views.extract_question('x\n```speakeasy-question\n{"question": "Pick", "options": ["a", "b"], "recommended": 9}\n```')
    assert q and "recommended" not in q


def test_every_task_prompt_explains_how_to_ask():
    names = P.Names("Ava", "Sam") if hasattr(P, "Names") else None
    if names is None:
        return
    for msg in (P.continuation_message(names, "x", "x"), P.thread_task_message(names, "x", "x", "")):
        assert "speakeasy-question" in msg


def test_tapping_an_answer_continues_the_same_task(server, service, hermes):
    from fakes import http, wait_for
    from test_routing import start_call, tasks
    hermes.responder = lambda prompt, sid: (ASK if "garden" in prompt.lower() and "One long bed" not in prompt
                                            else "Ordered.\nDONE: soil\nSPOKEN: Soil is ordered for the long bed.")
    session, worker = start_call(server, service)
    worker.delegate("call_q", "Plan the garden beds for spring")
    done = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])[0]
    assert done["result"]["question"]["options"] == ["Four raised beds", "One long bed"]
    assert any("I'd go with one long bed" in c for k, _, c in worker.sent if k == "session.commentary.append") \
        or "I'd go with one long bed" in done["result"]["spoken"]
    status, body = http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/answer",
                        {"task_id": done["task_id"], "text": "One long bed"}, server.token)
    assert status == 200 and body["sent"], body
    wait_for(lambda: len(hermes.calls) >= 2)
    second = hermes.calls[1]
    assert "One long bed" in second["input"] and "garden" in second["input"].lower()   # same task, with context
    assert http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/answer",
                {"task_id": "nope", "text": "x"}, server.token)[0] == 404


def test_options_can_be_pictures(tmp_path):
    pics = []
    for name in ("wide", "tall"):
        p = tmp_path / f"{name}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
        pics.append(str(p))
    out = ("Two layouts ready.\n```speakeasy-question\n"
           '{"question": "Which banner?", "options": [{"text": "Wide", "image": "%s"}, {"text": "Tall", "image": "%s"}]}'
           "\n```\nSPOKEN: Two layouts ready." % tuple(pics))
    r = text.split_result(out, (tmp_path,))
    assert r["question"]["options"] == ["Wide", "Tall"] and r["question"]["images"] == [1, 2]
    assert [c["name"] for c in r["cards"]] == ["wide.png", "tall.png"]
    assert "They're on screen." in r["spoken"]
    assert "_option_images" not in r["question"]
    outside = out.replace(str(tmp_path), "/etc")   # a picture outside the allowed folders is just left out
    assert "images" not in text.split_result(outside, (tmp_path,))["question"]


def test_spoken_question_keeps_names_and_skips_a_pick_already_said():
    from speakeasy.views import spoken_question
    q = {"question": "Which one should I check for a table tonight?", "recommended": 0,
         "options": [{"text": "Rosalind"}, {"text": "Barnaby's"}, {"text": "Via Nova"}]}
    said = spoken_question(q)
    assert said.startswith("Quick question: which one should I check")
    assert said.endswith("I'd go with Rosalind.")
    assert "I'd go with" not in spoken_question(q, "My pick is Rosalind in Fort Greene.")
    q2 = {"question": "Ship it now or wait?", "options": ["Ship it now", "Wait a day"], "recommended": 1}
    assert spoken_question(q2).endswith("I'd go with wait a day.")
