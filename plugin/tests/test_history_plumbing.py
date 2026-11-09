"""No agent plumbing on session pages, previews, search snippets or titles: Hermes' wrappers,
model notes, skill files, cron headers/footers, gateway status lines and footers are stripped or
hidden, helper reports fold into the work of the reply they fed, and context compaction is at most
one quiet divider. Every string here is invented."""
import pytest

from speakeasy import history as H
from test_history import NOW, add_msg, add_session, call, make_db


OOB = ("[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; not tool "
       "output and not a new delivery when replayed from conversation history]\n{}\n[/OUT-OF-BAND USER MESSAGE]")
ORIGIN = ('Gateway message origin (JSON data, not instructions):\n```json\n{{"platform": "discord", "chat_id": "42"}}\n```\n'
          "Do not guess a reply destination when these fields are insufficient.\n\n{}")
SKILL = ('[IMPORTANT: The user has invoked the "garden-planner" skill, indicating they want you to follow its '
         "instructions. The full skill content is loaded below.]\n\n---\nname: garden-planner\n---\n# Garden planner\n"
         "Plant in rows.\n\n[Skill directory: /tmp/skills/garden-planner]\nResolve any relative paths in this skill "
         "(e.g. `scripts/foo.js`, `templates/config.yaml`) against that directory, then run them with the terminal "
         "tool using the absolute path.\n\nThe user has provided the following instruction alongside the skill "
         "invocation: plan the tomato bed")
PRELOADED = ('[IMPORTANT: The user launched this CLI session with the "garden-planner" skill preloaded. Treat its '
             "instructions as active guidance for the duration of this session unless the user overrides them.]\n\n"
             "# Garden planner\nPlant in rows.\n\n[Skill directory: /tmp/skills/garden-planner]\nResolve any relative "
             "paths in this skill (e.g. `scripts/foo.js`, `templates/config.yaml`) against that directory, then run "
             "them with the terminal tool using the absolute path.\n\nwater the basil")
CRON = ("[IMPORTANT: You are running as a scheduled cron job. DELIVERY: Your final response will be automatically "
        "delivered to the user. SILENT: If there is genuinely nothing new to report, respond with exactly "
        '"[SILENT]" (nothing else).]\n\nCheck the rain gauge.\n\n## Run Context\nlast reading 0.2 in')


@pytest.mark.parametrize("raw,shown", [
    (OOB.format("Use the blue paint instead"), "Use the blue paint instead"),
    (OOB.format("Sam just added to this task by voice: use the blue paint Fold it into the work you are doing; "
                "do not start over."), "use the blue paint"),
    (OOB.format("Sam just added to this task by voice: is this the right hinge? Sam shared the picture hinge.jpg "
                "(saved at /tmp/shared/hinge.jpg). Open the images with vision_analyze. Use them to understand what "
                "they mean. Fold it into the work you are doing; do not start over."), "is this the right hinge?"),
    ("Sam typed to this task: also check the gutter Fold it into the work you are doing; do not start over.",
     "also check the gutter"),
    (ORIGIN.format("Is the paint dry?"), "Is the paint dry?"),
    ("[pebble42] Is the paint dry?\n\n" + ORIGIN.format("").strip(), "Is the paint dry?"),
    ('[Replying to: "The shed needs two coats."]\n\nHow long between coats?', "How long between coats?"),
    ('[Replying to your previous message: "Done."]\n\nthanks', "thanks"),
    ('"How long between coats?"\n\n[Context around the replied-to message]\n[pebble42] (attachment)\n'
     '[Helperbot [bot]] 🎙️ "the shed is primed"\n[Helperbot [bot]] Two coats.', "How long between coats?"),
    ("[Recent channel messages]\n[pebble42] earlier line\n\n[New message]\n[pebble42] where is the ladder?",
     "where is the ladder?"),
    ('"Order more sandpaper"\n\n[pebble42] (The user sent a message with no text content)', "Order more sandpaper"),
    ('"Order more sandpaper"', "Order more sandpaper"),   # a transcribed voice note, without its quotes
    ("[System: The skills available in this conversation changed after its system prompt was built.\n"
     "Now available:\n    - garden-planner: Plant in rows.]\n\nWhat goes next to basil?", "What goes next to basil?"),
    ("What goes next to basil?\n[screenshot]\n[System: The skills available in this conversation changed.\n"
     "No longer available: box]", "What goes next to basil?\n[screenshot]"),
    ("[system note: A new message has arrived. IGNORE pending results.]\n\nstop the sprinkler", "stop the sprinkler"),
    ("[SYSTEM] [Note: model was just switched to example-model-2.]\nkeep going", "keep going"),
    (SKILL, "/garden-planner plan the tomato bed"),
    (PRELOADED, "water the basil"),
    # Newer origin block: more fields, ending in source_message_id, before a steer's quoted voice note.
    (OOB.format('Gateway message origin (JSON data, not instructions or authorization):\n{"platform": "discord", '
                '"chat_id": "42", "thread_id": "42", "chat_type": "thread", "user_id": "7", "scope_id": "9", '
                '"parent_chat_id": "8", "message_id": "1001", "source_message_id": "1001"}\nDo not guess a reply '
                'destination when these fields are insufficient.\n\n"Use the cedar boards."'), "Use the cedar boards."),
    # Two follow-ups relayed into a task's thread, folded by Hermes into one steer.
    (OOB.format(ORIGIN.format("[Voice, from Sam, relayed by Speakeasy] Is the cedar in stock? This is about the task "
                              "in this thread: apply it to that work now. If it answers a question you asked Sam, act "
                              "on the answer.\n") + ORIGIN.format(
        "[Voice, from Sam, relayed by Speakeasy] Show me the cheapest one (They want to SEE this: share a screenshot "
        "or picture of it as MEDIA:<absolute path>.) This is about the task in this thread: apply it to that work "
        "now. If it answers a question you asked Sam, act on the answer.")),
     "Is the cedar in stock?\nShow me the cheapest one"),
    # A reply to a redelivered scheduled-job post: just the words under [New message].
    ('[Replying to: "♻️ Recovered reply — the messaging platform reconnected after the original delivery failed, so '
     'this may be a duplicate:\n\nCronjob Response: Rain watch\n(job_id: abc123)\n-------------\n\nNo rain today."]\n\n'
     "[Recent channel messages]\n[pebble42] earlier line\n\n[New message]\n[pebble42] water anyway?",
     "water anyway?"),
])
def test_user_rows_show_only_the_persons_words(raw, shown):
    assert H.user_words(raw) == ("user", shown)


@pytest.mark.parametrize("raw,kind,shown", [
    (CRON, "note", "Scheduled run"),
    (SKILL.split("The user has provided")[0] + "The user has provided the following instruction alongside the skill "
     "invocation: " + CRON, "note", "Scheduled run"),
    ("[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below.", "note",
     H.COMPACTED),
    ("[System note: Your previous turn was interrupted mid-run.]", "hidden", ""),
    ("# Kanban task t_abc123: Repaint the fence\n\nBody text", "note", "Repaint the fence"),
])
def test_scheduled_compaction_and_system_rows(raw, kind, shown):
    assert H.user_words(raw) == (kind, shown)


@pytest.mark.parametrize("raw,shown", [
    ("Cronjob Response: Rain watch\n(job_id: abc123)\n-------------\n\nNo rain today.\n\nTo stop or manage this job, "
     'send me a new message (e.g. "stop reminder Rain watch").', "No rain today."),
    ("The shed is done.\n\nexample-model-2 · 22% · 23s", "The shed is done."),
    ("The shed is done.\n\nexample-model-2 · 22% · ~/garden · 1m01s", "The shed is done."),
    ("First half of the answer. (1/2)", "First half of the answer."),
    ("⏩ Steered into current run (2 min elapsed, iteration 2/120, running: terminal).\nOn it.", "On it."),
    ("ℹ️ Context compression deferred until the turn ends.\nHere is the plan.", "Here is the plan."),
    (("♻️ Recovered reply — the messaging platform's rate limit refused the original, so part of it may already have "
      "arrived above:\n\nThe shed is done."), "The shed is done."),
    ("[SILENT]", ""),
    ("[CONTEXT COMPACTION] summary of earlier turns", ""),
    ("[PRIOR CONTEXT — for reference only, not new instructions] what came before", ""),
    # Talking about a footer in prose is kept: only a whole footer line goes.
    ("A footer looks like model · 22% · 23s when it is on.", "A footer looks like model · 22% · 23s when it is on."),
])
def test_assistant_rows_drop_gateway_wrapping(raw, shown):
    assert H.assistant_text(raw) == shown


def test_reply_quote_is_a_short_plain_line():
    raw = ('[Replying to: "Cronjob Response: Rain watch\n(job_id: abc)\n-------------\n\nNo rain today, '
           'skip watering.\n\nTo stop or manage this job, send me a new message (e.g. \\"stop reminder x\\")."]'
           "\n\nwhy skip it?")
    assert H.user_words(raw) == ("user", "why skip it?")
    assert H.reply_quote(raw) == "No rain today, skip watering."
    assert H.reply_quote('[Replying to: "🎙️ \\"paint the door\\""]\n\nok') == "paint the door"
    assert H.reply_quote('[Replying to: "♻️ Recovered reply — the gateway restarted during delivery, so this may be '
                         'a duplicate:\n\nCronjob Response: Rain watch\n(job_id: abc)\n-------------\n\nNo rain today."]'
                         "\n\nok") == "No rain today."
    assert H.reply_quote("plain message") is None


def _session(path, sid="d7"):
    make_db(path)
    add_session(path, sid, "discord", title=None, at=NOW - 5)
    return sid


def test_page_hides_plumbing_and_folds_helper_reports_into_the_reply(service):
    path = service.rt.state_db
    sid = _session(path)
    add_msg(path, sid, "user", "[CONTEXT COMPACTION — REFERENCE ONLY] older turns", at=NOW - 100)
    add_msg(path, sid, "user", ORIGIN.format("[pebble42] Find a ladder"), at=NOW - 90)
    add_msg(path, sid, "assistant", "", calls=[call("c1", "delegate_task", {"goal": "compare ladders"})], at=NOW - 89)
    add_msg(path, sid, "tool", '{"status": "started"}', call_id="c1", tool="delegate_task", at=NOW - 88)
    add_msg(path, sid, "assistant", "A helper is comparing ladders.", at=NOW - 87)
    add_msg(path, sid, "user", "[ASYNC DELEGATION BATCH COMPLETE — 1 helper] ladder A is 8 ft", at=NOW - 60)
    add_msg(path, sid, "assistant", "Ladder A, 8 ft, is the one.\n\nexample-model-2 · 30% · 12s", at=NOW - 59)
    add_msg(path, sid, "user", "[CONTEXT COMPACTION — REFERENCE ONLY] more turns", at=NOW - 50)
    add_msg(path, sid, "user", "[INTERNAL NOTIFICATION — a timer fired]", at=NOW - 40)
    add_msg(path, sid, "user", OOB.format("[pebble42] and a step stool"), at=NOW - 30)
    add_msg(path, sid, "assistant", "⏩ Steered into current run (1 min elapsed).\nAdded a step stool.", at=NOW - 29)
    add_msg(path, sid, "user", CRON, at=NOW - 20)
    add_msg(path, sid, "assistant", "[SILENT]", at=NOW - 19)
    page = H.session_messages(path, sid)
    shown = [(e["kind"], e.get("text")) for e in page["entries"] if e["kind"] != "tools"]
    assert shown == [
        ("note", H.COMPACTED), ("user", "Find a ladder"), ("assistant", "A helper is comparing ladders."),
        ("assistant", "Ladder A, 8 ft, is the one."), ("user", "and a step stool"), ("assistant", "Added a step stool."),
        ("note", "Scheduled run")]
    tools = [e for e in page["entries"] if e["kind"] == "tools"]
    steps = [i["text"] for t in tools for i in t["items"]]
    assert "A helper finished" in steps          # the report is a step of the reply it fed, not a message
    idx = [e["kind"] for e in page["entries"]]
    assert idx.index("tools", idx.index("assistant") + 1) < idx.index("assistant", idx.index("assistant") + 1)
    row = next(s for s in H.list_sessions(path)["sessions"] if s["id"] == sid)
    assert row["title"] == "Find a ladder" and row["preview"] == "Added a step stool."
    for e in page["entries"]:
        blob = str(e)
        for bad in ("OUT-OF-BAND", "Gateway message origin", "Steered", "SILENT", "· 30% ·", "pebble42", "CONTEXT"):
            assert bad not in blob, (bad, e)


def test_preview_and_title_skip_plumbing(service):
    path = service.rt.state_db
    sid = _session(path, "d8")
    add_msg(path, sid, "user", SKILL, at=NOW - 30)
    add_msg(path, sid, "assistant", "Tomatoes go in the sunny row.\n\nexample-model-2 · 12% · 9s", at=NOW - 20)
    add_msg(path, sid, "user", "[IMPORTANT: Background process proc_1 completed normally]", at=NOW - 10)
    add_msg(path, sid, "assistant", "[SILENT]", at=NOW - 9)
    row = next(s for s in H.list_sessions(path)["sessions"] if s["id"] == sid)
    assert row["preview"] == "Tomatoes go in the sunny row."
    assert row["title"] == "/garden-planner plan the tomato bed"


def test_search_snippets_come_from_what_the_page_shows(service):
    path = service.rt.state_db
    sid = _session(path, "d6")
    add_msg(path, sid, "user", '"Is the gate latch fixed?"\n\n[Context around the replied-to message]\n'
                               "[pebble42] the trellis is leaning", at=NOW - 30)
    add_msg(path, sid, "assistant", "Cronjob Response: Yard check\n(job_id: x1)\n-------------\n\nThe gate latch is "
                                    'fixed.\n\nTo stop or manage this job, send me a new message (e.g. "stop x").',
            at=NOW - 20)
    hit = next(s for s in H.list_sessions(path, q="latch")["sessions"] if s["id"] == sid)
    assert "latch" in hit["match"] and "Cronjob" not in hit["match"] and "To stop" not in hit["match"]
    # A word only in the quoted chat context the page never shows is no hit at all.
    assert not [s for s in H.list_sessions(path, q="trellis")["sessions"] if s["id"] == sid]


def test_one_compaction_divider_even_with_a_linked_task():
    own = [{"kind": "note", "id": "m1", "at": 1.0, "text": H.COMPACTED}, {"kind": "user", "id": "m2", "at": 5.0, "text": "a"}]
    linked = [{"kind": "note", "id": "x1", "at": 2.0, "text": H.COMPACTED}, {"kind": "user", "id": "x2", "at": 3.0, "text": "b"}]
    out = H.merge_linked(own, linked, lower=None, upper=None)
    assert [e["id"] for e in out] == ["m1", "x2", "m2"]


# -- SPK-133: Speakeasy's own notes to the task never show inside the person's words ---------------
from speakeasy.prompt import builder as _P  # noqa: E402

_NAMES = _P.Names("Hermes", "Sam")


def test_a_show_it_request_shows_only_the_words():
    asked = "Show me the porch lights" + _P.SHOW_IT_FOCUS
    row = _P.thread_task_message(_NAMES, asked, "Show me the porch lights", "User: Show me the porch lights")
    assert H.user_words(row) == ("user", "Show me the porch lights")
    cont = _P.continuation_message(_NAMES, "How does the garden look" + _P.SHOW_IT_FOCUS, "garden")
    assert H.user_words(cont) == ("user", "How does the garden look")


def test_a_relayed_follow_up_steer_shows_only_the_words():
    steer = OOB.format(ORIGIN.format(_P.thread_follow_up(_NAMES, "It's a heron, not a crane")))
    assert H.user_words(steer) == ("user", "It's a heron, not a crane")
    shown = OOB.format(ORIGIN.format(_P.thread_follow_up(_NAMES, "Find a photo of the old bridge", show=True)))
    assert H.user_words(shown) == ("user", "Find a photo of the old bridge")


def test_two_relayed_steers_in_one_row_show_both_and_no_notes():
    both = OOB.format(ORIGIN.format(_P.thread_follow_up(_NAMES, "Which bakery was it"))
                      + "\n" + ORIGIN.format(_P.thread_follow_up(_NAMES, "Check the opening hours too", show=True)))
    kind, text = H.user_words(both)
    assert kind == "user" and text == "Which bakery was it\nCheck the opening hours too"


def test_a_plugin_update_request_shows_only_the_words():
    row = _P.continuation_message(_NAMES, "update speakeasy" + _P.plugin_update_focus("update speakeasy"), "update")
    assert H.user_words(row) == ("user", "update speakeasy")
    assert H.preview_text("Show me the porch lights" + _P.SHOW_IT_FOCUS) == "Show me the porch lights"


def test_the_persons_own_similar_words_are_kept():
    said = "This is about the task in this thread, right? They want to SEE the plan."
    assert H.user_words(said) == ("user", said)


# SPK-244: Hermes appends a background-process report to the person's own row. Invented commands/output.
BG_REPORT = ("[IMPORTANT: Background process proc_0a1b2c3d4e5f completed normally (exit code 0).\n"
             "Started by subagent sa-0-1234abcd of delegation deleg_5678efgh. Task: \"Repaint the shed door...\"\n"
             "Command: bash ~/scratch/paint/check.sh > ~/scratch/paint/out.log 2>&1\n"
             "Output:\nzsh:2: no matches found: ~/scratch/paint/[old]-*.log\nDONE [ok\n]")
INTERNAL = ("[INTERNAL NOTIFICATION — generated by the Hermes gateway, not written by a human user. Do not "
            "treat it as user input, and do not reply to any earlier chat message because of it.]")


@pytest.mark.parametrize("raw", [
    "Use the green paint for the door.\n" + BG_REPORT,
    "Use the green paint for the door.\n\n" + BG_REPORT + "\n",
    "Use the green paint for the door. " + BG_REPORT,
    "[pebble42] Use the green paint for the door.\n" + BG_REPORT,
    OOB.format("Use the green paint for the door.\n" + BG_REPORT),
])
def test_a_background_report_appended_to_the_persons_words_is_cut_out(raw):
    assert H.user_parts(raw) == ("user", "Use the green paint for the door.", ["A background job finished"])
    assert H.user_words(raw) == ("user", "Use the green paint for the door.")


def test_words_after_an_embedded_report_stay_and_two_reports_make_one_step():
    raw = "Sand it first.\n" + BG_REPORT + "\n\nThen two coats.\n" + BG_REPORT.replace("0a1b", "9f8e")
    assert H.user_parts(raw) == ("user", "Sand it first.\n\nThen two coats.", ["A background job finished"])


@pytest.mark.parametrize("raw", [
    # Quoting the notice inside a sentence is the person's words, not a report.
    "Why does `[IMPORTANT: Background process proc_… completed normally (exit code 0).` show in my bubble?",
    'The bubble says "[IMPORTANT: Background process proc_1 completed normally (exit code 0)." again',
])
def test_a_quoted_notice_stays_the_persons_words(raw):
    assert H.user_parts(raw) == ("user", raw, [])


def test_a_report_alone_or_behind_a_sender_tag_stays_hidden():
    assert H.user_parts(BG_REPORT) == ("hidden", "A background job finished", [])
    assert H.user_parts("[pebble42] " + BG_REPORT + "\n\n" + INTERNAL)[0] == "hidden"
    # The gateway's footer marks the whole row as its notification, never the person's words.
    assert H.user_parts("Looks done.\n" + BG_REPORT + "\n\n" + INTERNAL) == ("hidden", "A background job finished", [])
    trig = ("[Triggering message id: `[\"completion\",\"proc_0a1b2c3d4e5f\",1.5]` — use as `message_id` for "
            "reply/react/pin via the discord tools.]\n\n[pebble42] ")
    assert H.user_parts(trig + BG_REPORT + "\n\n" + INTERNAL) == ("hidden", "A background job finished", [])
    batch = ("[pebble42] [IMPORTANT: 2 background process results arrived while this session was busy. Treat them as "
             "one batch.]\n\n" + BG_REPORT + "\n\n" + BG_REPORT.replace("0a1b", "9f8e") + "\n\n" + INTERNAL)
    assert H.user_parts(batch) == ("hidden", "A background job finished", [])


def test_a_speakeasy_brief_quoting_a_report_still_shows_the_last_user_line():
    brief = ("You are Todd, handling one task the user sent you as a message in the Speakeasy app, with no call.\n"
             "Recent messages typed in the Speakeasy app:\nUser: \"Check the fence\"\n" + BG_REPORT + "\n\n"
             "Assistant: The fence is fine.\n\nUser: \"Great, now the gate.\"")
    assert H.user_parts(brief) == (*H.user_words(brief), [])
    assert "gate" in H.user_words(brief)[1] and "Background" not in H.user_words(brief)[1]


def test_page_shows_the_words_and_folds_an_appended_report_into_the_reply(service):
    path = service.rt.state_db
    sid = _session(path, "d244")
    add_msg(path, sid, "user", "Use the green paint for the door.\n" + BG_REPORT, at=NOW - 30)
    add_msg(path, sid, "assistant", "Green it is; the check passed.", at=NOW - 20)
    page = H.session_messages(path, sid)
    shown = [(e["kind"], e.get("text")) for e in page["entries"] if e["kind"] != "tools"]
    assert shown == [("user", "Use the green paint for the door."), ("assistant", "Green it is; the check passed.")]
    steps = [i["text"] for e in page["entries"] if e["kind"] == "tools" for i in e["items"]]
    assert steps.count("A background job finished") == 1
    blob = str(page["entries"])
    for bad in ("IMPORTANT", "proc_", "Command:", "INTERNAL NOTIFICATION", "subagent"):
        assert bad not in blob, bad
    row = next(s for s in H.list_sessions(path)["sessions"] if s["id"] == sid)
    assert row["title"] == "Use the green paint for the door."


# SPK-256: the shape the release leak scan found on real rows: a steer (out-of-band wrapper) of several
# lines, then a report a *subagent* started, in each of Hermes' exit wordings. Invented words/commands.
SUBAGENT_REPORT = ("[IMPORTANT: Background process proc_7c6b5a4f3e2d {head}.\n"
                   "Started by subagent sa-0-9a8b7c6d of delegation deleg_0f1e2d3c. Task: \"Repaint the shed door "
                   "and the gate (blue 2.1, green 1.4) to the porch in...\"\n"
                   "Command: rm -rf ~/scratch/paint/run-*.out; bash ~/scratch/paint/check.sh > ~/scratch/paint/out.log 2>&1\n"
                   "Output:\nzsh:1: no matches found: ~/scratch/paint/run-*.out\n]")


@pytest.mark.parametrize("head", ["completed normally (exit code 0)", "exited (exit code 1)",
                                  "exited (exit code -15, SIGTERM)", "exited (exit code 143, SIGTERM)"])
def test_a_subagent_started_report_after_a_multi_line_steer_is_cut_out(head):
    words = "From the porch: the gate needs a second coat.\nDo the door after it dries. Keep the shed as is."
    raw = OOB.format(words + "\n" + SUBAGENT_REPORT.format(head=head))
    assert H.user_parts(raw) == ("user", words, ["A background job finished"])
    kind, shown = H.user_words(raw)
    for bad in ("IMPORTANT", "proc_", "subagent", "Command:", "Output:", "OUT-OF-BAND"):
        assert bad not in shown, bad
