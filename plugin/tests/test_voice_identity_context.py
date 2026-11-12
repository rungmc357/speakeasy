"""The call voice is the user's own assistant, never "ChatGPT", and hands personal questions to Hermes.

A public tester (11-12, a new user with no voice brief yet) asked the call what it knew about her:
the voice said it was ChatGPT, had no context, and the panel kept showing "I'm still checking."
These check the words the voice model is given on every call (the identity block closes the
instructions, after the brief, so the provider's own persona can't win) and replay that moment
through a call: the question reaches Hermes as a job, and the answer comes back to the call.
Every name and answer here is invented.
"""
from __future__ import annotations

from fakes import wait_for
from speakeasy.prompt import builder as P
from test_routing import start_call


def _instructions(names: P.Names, brief: str = "") -> str:
    return P.build_live_instructions(names, brief=brief)


def test_the_voice_is_the_users_assistant_never_chatgpt_even_with_no_brief():
    names = P.Names("Juniper", "Rae")
    for brief in ("", "Rae runs a small pottery studio."):
        text = _instructions(names, brief)
        block = text[text.index("# Who you are"):]
        assert "Your name is Juniper" in block and "Rae's own assistant" in block
        assert "never introduce yourself as ChatGPT, Codex, OpenAI or any model name" in block
        assert "never say you are made by OpenAI" in block
        # "what model are you": its own name, and the work model is a hand-off, not a guess.
        assert "which model you are: you are Juniper" in block
        assert "which model does their work, hand that off" in block
        # The identity block closes the instructions' head: after the brief, before per-call extras.
        if brief:
            assert text.index(brief) < text.index("# Who you are")


def test_personal_questions_go_to_hermes_and_a_slow_one_is_said_once():
    block = P.identity_reminder(P.Names("Juniper", "Rae"))
    assert "Asked what you know about Rae" in block and "hand it off" in block
    assert "Never say you know nothing about them, have no memory, or have no context" in block
    assert "Say at most once" in block and "never repeat \"I'm still checking\"" in block
    assert "When the result arrives, give it right away" in block
    # Unnamed users read naturally.
    plain = P.identity_reminder(P.Names())
    assert "the user's own assistant" in plain and "{" not in plain


def test_the_rules_still_delegate_facts_that_are_not_in_context():
    rules = P.rules_text(P.Names("Juniper", "Rae"))
    assert "the answer depends on facts that are not in this context" in rules


def test_replay_what_do_you_know_about_me_reaches_hermes_and_comes_back(server, service, hermes):
    """The tester's moment, replayed on a call with no brief: the question becomes a Hermes job in
    her words, and its answer reaches the call (it is never left at "still checking")."""
    service.settings.patch({"assistant_name": "Juniper", "user_name": "Rae"})
    hermes.responder = lambda prompt, sid: ("Rae runs a pottery studio and is planning a kiln upgrade.\n"
                                            "DONE: Told Rae what I know\nSPOKEN: You run a pottery studio and "
                                            "you're planning a kiln upgrade.")
    _, worker = start_call(server, service)
    worker.feed({"type": "session.output_transcript.delta", "delta": "Hi Rae, it's Juniper.", "start_ms": 1, "end_ms": 2})
    worker.delegate("call_me_1", "What do you know about me?")
    wait_for(lambda: hermes.calls)
    assert "What do you know about me" in hermes.calls[0]["input"]
    got = wait_for(lambda: [c for k, d, c in worker.sent if "pottery studio" in (c or "")], timeout=15)
    assert got, worker.sent
    said = " ".join(c or "" for _, _, c in worker.sent)
    assert "ChatGPT" not in said and "still checking" not in said.lower()
