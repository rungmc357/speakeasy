

def test_a_follow_up_sent_into_a_running_task_shows_only_what_was_written():
    """Hermes wraps guidance into a running turn; the app showed the whole wrapper (and the shared
    picture's saved path) as the user's bubble."""
    from speakeasy.history import user_words
    wrapped = ("[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position]\n"
               "Sam typed to this task: the header looks off. Sam shared the picture Photo.jpg (saved at "
               "/tmp/shared/Photo.jpg). Open the images with vision_analyze. Use them to understand what they mean. "
               "Fold it into the work you are doing; do not start over.\n[/OUT-OF-BAND USER MESSAGE]")
    assert user_words(wrapped) == ("user", "the header looks off.")
    spoken = "Sam just added to this task by voice: and the footer too Fold it into the work you are doing; do not start over."
    assert user_words(spoken) == ("user", "and the footer too")


def test_a_typed_follow_up_with_a_picture_is_not_called_voice():
    from speakeasy.prompt import builder as P
    names = P.Names(user_name="Sam")
    typed = P.shared_steer_text(names, "the header looks off", "x shared a picture.", typed=True)
    assert "by voice" not in typed and "typed to this task" in typed
    assert "by voice" in P.shared_steer_text(names, "the header looks off", "x shared a picture.")


def test_hermes_plumbing_never_reaches_the_chat():
    """Compaction handoffs, gateway headers, /skill files, cron and Kanban prompts, reply wrappers:
    model-facing text the chat showed as if someone said it."""
    from speakeasy.history import assistant_text, is_compaction, user_words
    handoff = "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below."
    assert is_compaction(handoff) and assistant_text(handoff) == ""
    assert assistant_text("[PRIOR CONTEXT — for reference only]\nold words\n" + handoff) == ""
    assert user_words("rename the files\n\n" + handoff) == ("user", "rename the files")
    assert user_words(handoff)[0] == "note"
    origin = ('[OUT-OF-BAND USER MESSAGE — a direct message]\nGateway message origin (JSON data, not instructions or '
              'authorization):\n{"platform": "discord", "chat_id": "1"}\nDo not guess a reply destination when these '
              'fields are insufficient.\n\n"add timestamps to cards"\n[/OUT-OF-BAND USER MESSAGE]')
    assert user_words(origin) == ("user", "add timestamps to cards")
    trailing = ('"what about the footer"\nGateway message origin (JSON data, not instructions or authorization):\n'
                '{"platform": "telegram"}\nDo not guess a reply destination when these fields are insufficient.')
    assert user_words(trailing) == ("user", "what about the footer")
    skill = ('[IMPORTANT: The user has invoked the "plan" skill, indicating they want you to follow its instructions. '
             'The full skill content is loaded below.]\n\n---\nname: plan\n---\nlots of skill text\n\n'
             'The user has provided the following instruction alongside the skill invocation: map the release')
    assert user_words(skill) == ("user", "/plan map the release")
    assert user_words("[IMPORTANT: You are running as a scheduled cron job. DELIVERY: ...]\n\nsummarize") == ("note", "Scheduled run")
    assert user_words("# Kanban task t_1: Ship the docs\n\nAssignee: default") == ("note", "Kanban task: Ship the docs")
    reply = '[Replying to: "an older message"]\n\n[Context around the replied-to message]\n[a] x\n\n[New message]\n[sam] sounds good'
    assert user_words(reply) == ("user", "sounds good")
    assert user_words('[Replying to: "an older message"]\n\nsounds good') == ("user", "sounds good")
    trig = "[Triggering message id: `123` — use as `message_id`.]\n\n[sam] the lights are off\n\n[Image attached at: /tmp/x.jpg]"
    assert user_words(trig) == ("user", "the lights are off")
