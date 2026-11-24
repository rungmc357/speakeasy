"""The voice channel is a complete log: work that lands elsewhere leaves a pointer there."""
import threading

from speakeasy.calls import Notices
from speakeasy.prompt import builder as P


class Store:
    def __init__(self):
        self.claimed = set()

    def claim_notice(self, key):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True


class Q:
    def __init__(self):
        self.sent = []

    def put(self, item):
        self.sent.append(item)


def board(home="discord:100"):
    b = Notices.__new__(Notices)
    b.store, b.notifier, b.queue = Store(), object(), Q()
    b.closed, b._close_lock, b.thread = False, threading.Lock(), None
    b.target = lambda: home
    return b


def test_answer_in_another_channel_leaves_a_link_in_the_voice_channel():
    b = board()
    b.answered("r1", "Chess, Stocks and Tips are gone.", "discord:200")
    b.pointer("r1", "Remove Chess, Stocks, and Tips apps", "discord:200")
    assert [(t, x) for _, t, x in b.queue.sent] == [
        ("discord:200", "Chess, Stocks and Tips are gone."),
        ("discord:100", "Done: Remove Chess, Stocks, and Tips apps → <#200>")]


def test_thread_answer_links_the_thread():
    b = board()
    b.pointer("r2", "Fix dashboard API connection failure", "discord:555")
    assert b.queue.sent[0][1:] == ("discord:100", "Done: Fix dashboard API connection failure → <#555>")


def test_answer_already_in_the_voice_channel_gets_no_pointer():
    b = board()
    b.pointer("r3", "Dim the lights", "discord:100")
    b.pointer("r4", "Dim the lights", None)
    assert b.queue.sent == []


def test_pointer_is_posted_once():
    b = board()
    b.pointer("r5", "Task", "discord:9")
    b.pointer("r5", "Task", "discord:9")
    assert len(b.queue.sent) == 1


def test_no_voice_channel_means_no_pointer():
    b = board(home="none")
    b.pointer("r6", "Task", "discord:9")
    assert b.queue.sent == []


def test_other_platforms_name_the_platform():
    assert P.pointer_notice("Weekly report", "telegram:-100123:7") == "Done: Weekly report → your Telegram"
    assert P.pointer_notice(None, "discord:42") == "Done: a voice task → <#42>"
