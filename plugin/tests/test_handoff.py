"""Pairing-link handoff: web links keep the code out of the web server's logs, and a link queued by
setup is delivered once the voice platform starts after the user's own restart."""
from __future__ import annotations

import json

from speakeasy import handoff as H
from speakeasy.devices import DeviceStore


def test_web_link_puts_server_and_code_in_the_fragment():
    link = H.web_link("https://box.tail123.ts.net:8795", "123456")
    assert link == "https://speakeasyvoice.ai/pair#server=https%3A%2F%2Fbox.tail123.ts.net%3A8795&code=123456"
    assert "?" not in link  # nothing in the query string, so the page's host never sees it


def test_app_link_is_unchanged():
    assert H.app_link("http://127.0.0.1:8795", "123456") == "speakeasy://pair?server=http%3A%2F%2F127.0.0.1%3A8795&code=123456"


def test_chat_message_mentions_the_download_and_the_expiry():
    text = H.chat_message("https://speakeasyvoice.ai/pair#x")
    assert "download" in text and "30 minutes" in text and "https://speakeasyvoice.ai/pair#x" in text


def test_pending_link_round_trip_and_expiry(tmp_path):
    H.save_pending(tmp_path, "discord:123", now=1000)
    assert H.pending_target(tmp_path, now=1000 + 60) == "discord:123"
    assert H.pending_target(tmp_path, now=1000 + H.PENDING_MAX_AGE_S + 1) == ""
    assert not (tmp_path / "speakeasy" / "pending_link.json").exists()


def test_corrupt_pending_file_is_ignored(tmp_path):
    (tmp_path / "speakeasy").mkdir()
    (tmp_path / "speakeasy" / "pending_link.json").write_text("{not json")
    assert H.pending_target(tmp_path) == ""


def test_deliver_pending_sends_one_fresh_link_and_clears(tmp_path):
    H.save_pending(tmp_path, "telegram")
    store = DeviceStore(tmp_path)
    sent = []
    ok = H.deliver_pending(tmp_path, "https://box.tail123.ts.net:8795",
                           send=lambda target, text: sent.append((target, text)) or True,
                           new_code=lambda: store.new_pairing_code(ttl=H.LINK_TTL_S), sleep=lambda s: None)
    assert ok and len(sent) == 1 and sent[0][0] == "telegram"
    assert "https://speakeasyvoice.ai/pair#server=https%3A%2F%2Fbox.tail123.ts.net%3A8795&code=" in sent[0][1]
    assert H.pending_target(tmp_path) == ""
    # The code in the message actually pairs.
    code = sent[0][1].split("code=")[1][:6]
    assert store.redeem(code, "Mac") is not None


def test_deliver_pending_retries_then_gives_up_and_keeps_the_record(tmp_path):
    H.save_pending(tmp_path, "telegram")
    tries = []
    ok = H.deliver_pending(tmp_path, "http://127.0.0.1:8795", send=lambda t, x: tries.append(t) and False,
                           new_code=lambda: "123456", sleep=lambda s: None, attempts=3)
    assert not ok and len(tries) == 3 and H.pending_target(tmp_path) == "telegram"


def test_nothing_pending_does_nothing(tmp_path):
    called = []
    assert H.deliver_pending(tmp_path, "x", send=lambda *a: called.append(a) or True,
                             new_code=lambda: "1", sleep=lambda s: None) is False
    assert called == []


def test_longer_code_lifetime_for_links_sent_to_chat(tmp_path):
    store = DeviceStore(tmp_path)
    code = store.new_pairing_code(now=0, ttl=H.LINK_TTL_S)
    assert store.redeem(code, "Mac", now=H.LINK_TTL_S - 5) is not None
    code = store.new_pairing_code(now=0)
    assert store.redeem(code, "Mac", now=601) is None  # the 10-minute default is unchanged


# -- the expiry the user reads matches the code's real lifetime in each mode -----------------------

def test_pair_link_carries_the_real_lifetime_per_mode(tmp_path):
    from speakeasy.settings import Settings
    from speakeasy.devices import DeviceStore
    assert H.pair_link(tmp_path, "https://box.tail123.ts.net:8795", "123456").endswith("&code=123456&minutes=30")
    Settings(tmp_path).patch({"server": {"tunnel": "cloudflare"}})
    assert H.link_ttl(tmp_path) == 600
    assert H.pair_link(tmp_path, "https://x.trycloudflare.com", "123456").endswith("&minutes=10")
    code = DeviceStore(tmp_path).new_pairing_code(ttl=H.link_ttl(tmp_path))
    assert code  # minted with the same lifetime the link states


def test_pending_delivery_states_ten_minutes_in_cloudflare_mode(tmp_path):
    from speakeasy.settings import Settings
    Settings(tmp_path).patch({"server": {"tunnel": "cloudflare"}})
    H.save_pending(tmp_path, "telegram")
    sent = []
    assert H.deliver_pending(tmp_path, "https://x.trycloudflare.com", send=lambda t, m: sent.append(m) or True,
                             new_code=lambda: "123456", sleep=lambda s: None)
    assert "for 10 minutes" in sent[0] and "&minutes=10" in sent[0] and "30 minutes" not in sent[0]


def test_setup_and_pair_print_the_same_lifetime_as_the_code(tmp_path, monkeypatch, capsys):
    from argparse import Namespace
    from speakeasy import cli
    from speakeasy.settings import Settings
    Settings(tmp_path).patch({"server": {"tunnel": "cloudflare", "advertised_url": "https://x.trycloudflare.com"}})
    assert cli.cmd_pair(Namespace(server="", send=""), tmp_path) == 0
    out = capsys.readouterr().out
    assert "expires in 10 minutes" in out and "&minutes=10" in out


def test_the_agent_guide_states_both_lifetimes():
    import re
    from pathlib import Path
    doc = re.sub(r"\s+", " ", (Path(__file__).resolve().parents[2] / "docs" / "AGENT_SETUP.md").read_text())
    assert "expires after 30 minutes, or 10 minutes once Cloudflare mode is on" in doc
