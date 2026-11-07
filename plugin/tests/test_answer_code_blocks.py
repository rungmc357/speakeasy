"""SPK-187: a task's final answer keeps its code blocks wherever the app shows it, and the voice only
says a command is on screen when the written answer really has it. Invented answers only."""
from speakeasy import history as H
from speakeasy import text as T
from test_history import NOW, add_msg, add_session, make_db

ANSWER = (
    "Two steps on the build box. First give the helper account a home for its key:\n\n"
    "```bash\n"
    "mkdir -p ~/.ssh && echo 'ssh-ed25519 AAAAexampleexampleexample helper@build-box' >> ~/.ssh/authorized_keys\n"
    "```\n\n"
    "Then restart the sync job:\n"
    "```\nsudo launchctl kickstart -k system/com.example.sync\n```\n\n"
    "```speakeasy-views\n"
    '[{"kind":"stats","title":"Build box","items":[{"label":"Reachable","value":"Yes"}]}]\n'
    "```\n\n"
    "DONE: Needs the key pasted\n"
    "SPOKEN: The build box answers but won't take the key yet. Paste the one line on screen and I'll do the rest."
)


def test_report_shows_its_written_answer_with_code():
    shown = H.assistant_text(ANSWER)
    assert "```bash\nmkdir -p ~/.ssh && echo 'ssh-ed25519" in shown
    assert "sudo launchctl kickstart -k system/com.example.sync" in shown
    assert shown.startswith("Two steps on the build box.")
    for plumbing in ("speakeasy-views", '"kind"', "DONE:", "SPOKEN:", "Paste the one line on screen"):
        assert plumbing not in shown


def test_report_without_written_part_still_shows_the_spoken_line():
    assert H.assistant_text("DONE: noted\nSPOKEN: Green it is.") == "Green it is."
    assert H.assistant_text("DONE: watered the ferns") == "watered the ferns"


def test_list_preview_keeps_the_short_spoken_line():
    assert H.assistant_text(ANSWER, brief=True).startswith("The build box answers")


def test_session_page_keeps_code_blocks(tmp_path):
    db = tmp_path / "state.db"
    make_db(db)
    add_session(db, "speakeasy_task_code01", "api_server", at=NOW - 100)
    add_msg(db, "speakeasy_task_code01", "user", "Can you get onto the build box?", at=NOW - 90)
    add_msg(db, "speakeasy_task_code01", "assistant", ANSWER, at=NOW - 10)
    entries = H.session_messages(db, "speakeasy_task_code01")["entries"]
    answer = [e for e in entries if e["kind"] == "assistant"][-1]["text"]
    assert answer.count("```") == 4   # both blocks, fences intact for the app's code renderer
    assert "ssh-ed25519 AAAAexampleexampleexample helper@build-box" in answer
    assert "speakeasy-views" not in answer


def test_split_result_keeps_code_in_full():
    r = T.split_result(ANSWER, ())
    assert "```bash\nmkdir -p ~/.ssh" in r["full"] and "speakeasy-views" not in r["full"]
    assert r["spoken"].endswith("Paste the one line on screen and I'll do the rest.")
    assert r["views"][0]["kind"] == "stats"


def test_voice_never_claims_a_missing_command_is_on_screen():
    r = T.split_result("The sync job is stuck on the build box.\n\n"
                       "SPOKEN: The sync job is stuck. The three commands are in chat, ready to copy.", ())
    assert r["spoken"] == "The sync job is stuck."
    r = T.split_result("SPOKEN: Paste the one line on screen into Terminal.", ())
    assert "on screen" not in r["spoken"]


def test_a_command_only_the_voice_had_lands_on_screen():
    r = T.split_result("Done.\n\nSPOKEN: Enter `brew services start demo` on the build box.", ())
    assert "brew services" not in r["spoken"] and "on screen" in r["spoken"]
    assert "```\nbrew services start demo\n```" in r["full"]
