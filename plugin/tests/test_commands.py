"""Commands in answers: never spoken, and always copyable on their own."""
import threading

from speakeasy import text as T
from speakeasy.prompt import builder as P

SSH_KEY = (
    "To finish, the build box needs this key. Run the following in a shell there, "
    "then hit enter:\n\n"
    'install -d -m 700 ~/.ssh; printf \'%s\\n\' "ssh-ed25519 AAAAexamplekey ci-runner" | tee -a '
    "~/.ssh/authorized_keys\n\n"
    "Public keys are fine to share.")

LAUNCH_AGENT = (
    "Nearly there. One step needs you.\n\n"
    "In a shell on the build box, enter:\n\n"
    "brew services start example-dashboard\n\n"
    "It'll come back up on reboot.")


def test_bare_commands_are_split_out():
    pieces = T.split_commands(SSH_KEY)
    kinds = [k for k, _ in pieces]
    assert kinds == ["text", "command", "text"]
    assert pieces[1][1].startswith("install -d") and pieces[1][1].endswith("authorized_keys")
    assert [k for k, _ in T.split_commands(LAUNCH_AGENT)] == ["text", "command", "text"]


def test_fenced_command_comes_out_bare():
    pieces = T.split_commands("Run this:\n\n```bash\nbrew install jq\n```\n\nThen tell me.")
    assert pieces == [("text", "Run this:"), ("command", "brew install jq"), ("text", "Then tell me.")]


def test_sentences_are_not_commands():
    for line in ["Open Terminal on the laptop.", "Open the app and press Save.",
                 "Echo is a problem on speakerphone.", "Git history shows the fix landed Tuesday."]:
        assert not T.looks_like_command(line), line
    assert T.split_commands("The league team won by 12. Weather is sunny.") == [
        ("text", "The league team won by 12. Weather is sunny.")]


def test_voice_never_gets_the_command():
    notes = " ".join(P.result_notes("Laptop key", SSH_KEY, "Here's the line to run."))
    assert "install -d" not in notes and "authorized_keys" not in notes and "command, shown in the app" in notes
    r = T.split_result("Done.\n\nSPOKEN: Enter `brew services start demo` on the build box.", ())
    assert "brew services" not in r["spoken"] and "ready to copy" in r["spoken"]
    r = T.split_result(LAUNCH_AGENT + "\nSPOKEN: One command left to run on the server; it's ready to copy.", ())
    assert r["spoken"] == "One command left to run on the server; it's ready to copy."


def test_chat_delivery_posts_each_command_alone():
    from speakeasy.calls import Notices as NoticeBoard
    sent = []

    class Store:
        def claim_notice(self, key):
            return True

    board = NoticeBoard.__new__(NoticeBoard)
    board.store, board.notifier = Store(), object()
    board.closed, board._close_lock, board.thread = False, threading.Lock(), None
    board.target = lambda: "discord:1"

    class Q:
        def put(self, item):
            sent.append(item)
    board.queue = Q()
    board.answered("r1", SSH_KEY, "discord:1")
    assert [t for _, _, t in sent][1].startswith("install -d") and len(sent) == 3
    sent.clear()
    board.commands("r2", LAUNCH_AGENT, "discord:1:99")
    assert len(sent) == 1 and sent[0][1] == "discord:1:99" and sent[0][2].startswith("brew services")
    sent.clear()
    board.answered("r3", "The league team won by 12.", "discord:1")
    assert len(sent) == 1


def test_prompts_ask_for_fenced_commands():
    names = P.Names() if hasattr(P, "Names") else None
    assert "fenced code block" in P.COPYABLE_RULE
