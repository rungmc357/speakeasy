"""Cloudflare mode: tunnel service + address tracking, the encrypted address drop, public hardening."""
from __future__ import annotations

import json
import os
import stat
import sys
import urllib.request

import pytest

from fakes import http


QUICK = "https://calm-river-sound-test.trycloudflare.com"
QUICK2 = "https://other-words-here-now.trycloudflare.com"


def _raw(base, method, path, headers=None, body=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {}


def _tunnel_on(service, url=QUICK):
    service.settings.patch({"server": {"tunnel": "cloudflare", "tunnel_url": url, "advertised_url": url}})


# -- settings -----------------------------------------------------------------------------------

def test_tunnel_settings_validate(home):
    from speakeasy.settings import Settings, SettingsError
    s = Settings(home)
    assert s.get()["server"]["tunnel"] == "" and s.get()["address_drop"] == {"enabled": True, "relay": "https://ntfy.sh"}
    with pytest.raises(SettingsError):
        s.patch({"server": {"tunnel": "funnel"}})
    with pytest.raises(SettingsError):
        s.patch({"address_drop": {"relay": "http://ntfy.sh"}})  # https only
    s.patch({"address_drop": {"relay": "https://ntfy.example.com/"}})
    assert s.get()["address_drop"]["relay"] == "https://ntfy.example.com"


def test_app_cannot_change_where_the_server_is_published(server):
    status, _ = http(server.base_url, "PATCH", "/voice/settings", {"server": {"tunnel": "cloudflare"}}, token=server.token)
    assert status == 403
    status, _ = http(server.base_url, "PATCH", "/voice/settings", {"address_drop": {"enabled": False}}, token=server.token)
    assert status == 403


# -- the tunnel service script (scripts/speakeasy-cloudflare.py: outside the plugin) -------------

def _script():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts" / "speakeasy-cloudflare.py"
    spec = importlib.util.spec_from_file_location("speakeasy_cloudflare", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_plugin_ships_no_tunnel_tooling():
    """Hermes' install scan blocks tunnel tooling inside a plugin; the service lives in scripts/."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "speakeasy"
    word = "cloud" + "flared"
    assert not [p.name for p in root.rglob("*.py") if word in p.read_text()]


def test_quick_and_named_tunnel_commands():
    T = _script()
    quick = T.tunnel_command("/bin/cf", 8795, 8797)
    assert quick[-2:] == ["--url", "http://127.0.0.1:8795"] and "run" not in quick
    assert "127.0.0.1:8797" in quick  # metrics on loopback only
    named = T.tunnel_command("/bin/cf", 8795, 8797, named="speakeasy")
    assert named[-4:] == ["run", "--url", "http://127.0.0.1:8795", "speakeasy"]
    assert all("funnel" not in a for a in quick + named)


def test_service_files_keep_the_tunnel_alive(home):
    T = _script()
    plist = T.launchd_plist("ai.x", T.tunnel_command("cf", 1, 2), T.log_path(home))
    assert "<key>KeepAlive</key>\n    <true/>" in plist and "<key>RunAtLoad</key>" in plist
    unit = T.systemd_unit(T.tunnel_command("cf", 1, 2), T.log_path(home))
    assert "Restart=always" in unit and "WantedBy=default.target" in unit
    assert T.service_label(home) == T.service_label(home) and T.service_label(home).startswith("ai.speakeasyvoice.tunnel.")
    from speakeasy import tunnel
    assert T.log_path(home) == tunnel.log_path(home)  # the plugin reads the log the service writes


def test_install_service_writes_and_starts_it(home, monkeypatch, tmp_path):
    T = _script()
    monkeypatch.setattr(T.Path, "home", classmethod(lambda cls: tmp_path))
    calls = []
    path = T.install_service(home, "/bin/cf", 8795, 8797, run=lambda cmd, **kw: calls.append(cmd))
    assert path.exists() and "/bin/cf" in path.read_text()
    if sys.platform == "darwin":
        assert any(c[:2] == ["launchctl", "bootstrap"] for c in calls)
    T.remove_service(home, run=lambda cmd, **kw: calls.append(cmd))
    assert not path.exists()


def test_named_tunnel_create_and_route():
    T = _script()

    class Done:
        def __init__(self, rc=0, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return Done(out="[]") if cmd[2] == "list" else Done()
    assert T.create_named_tunnel("cf", "speakeasy.example.com", run=run) == ""
    assert ["cf", "tunnel", "create", "speakeasy"] in calls
    assert calls[-1][-2:] == ["speakeasy", "speakeasy.example.com"]
    calls.clear()

    def run_existing(cmd, **kw):
        calls.append(cmd)
        return Done(out=json.dumps([{"name": "speakeasy"}])) if cmd[2] == "list" else Done()
    assert T.create_named_tunnel("cf", "speakeasy.example.com", run=run_existing) == ""
    assert not any(c[2] == "create" for c in calls)  # reused, not recreated
    assert "failed" in T.create_named_tunnel("cf", "x.example.com", run=lambda cmd, **kw: Done(rc=1, out="[]", err="no cert"))
    assert T.valid_hostname("speakeasy.example.com") and not T.valid_hostname("a.trycloudflare.com")
    assert not T.valid_hostname("bad host")


def test_named_tunnel_settings_are_advertised(server, service):
    service.settings.patch({"server": {"tunnel": "cloudflare", "tunnel_hostname": "speakeasy.example.com"}})
    status, body = http(server.base_url, "GET", "/voice/endpoints", token=server.token)
    assert body["endpoints"]["cloudflare"] == "https://speakeasy.example.com"
    assert body["endpoints"]["cloudflare_permanent"] is True and body["address_drop"] is None  # never moves


# -- reading the address ------------------------------------------------------------------------

def test_url_is_read_from_the_log_when_metrics_are_down(home):
    from speakeasy import tunnel as T
    T.log_path(home).parent.mkdir(parents=True, exist_ok=True)
    T.log_path(home).write_text(f"INF |  {QUICK}  |\nINF later\nINF | {QUICK2} |\n")
    assert T.read_url(home, 1) == QUICK2  # the newest one
    assert T.valid_tunnel_url(QUICK) and not T.valid_tunnel_url("https://evil.example.com")


def test_watcher_updates_the_address_and_messages_the_user_once(service):
    from speakeasy import tunnel as T
    service.settings.patch({"server": {"tunnel": "cloudflare"}, "delivery": {"target": "telegram"}})
    seen = {"url": QUICK}
    sent, drops = [], []
    w = T.TunnelWatcher(service, reader=lambda home, port: seen["url"],
                        send=lambda target, text: sent.append((target, text)) or True,
                        drop=lambda url, body: drops.append((url, body)) or True)
    w.tick()
    s = service.settings.get()["server"]
    assert s["tunnel_url"] == QUICK and s["advertised_url"] == QUICK
    assert sent == []  # the first address isn't a change
    assert len(drops) == 1
    w.tick()
    assert sent == [] and len(drops) == 1  # nothing new
    seen["url"] = QUICK2
    w.tick()
    assert service.settings.get()["server"]["advertised_url"] == QUICK2
    assert len(sent) == 1 and sent[0][0] == "telegram"
    assert "server=https%3A%2F%2Fother-words-here-now.trycloudflare.com&update=1" in sent[0][1]
    assert "code=" not in sent[0][1]  # an address update pairs nothing
    assert len(drops) == 2
    w.tick()
    assert len(sent) == 1


def test_watcher_keeps_a_tailnet_address_advertised(service):
    from speakeasy import tunnel as T
    service.settings.patch({"server": {"tunnel": "cloudflare", "advertised_url": "https://mini.tail1.ts.net:8795"}})
    T.TunnelWatcher(service, reader=lambda h, p: QUICK, send=lambda *a: True, drop=lambda *a: True).tick()
    s = service.settings.get()["server"]
    assert s["advertised_url"] == "https://mini.tail1.ts.net:8795" and s["tunnel_url"] == QUICK


def test_watcher_ignores_named_tunnels_and_garbage(service):
    from speakeasy import tunnel as T
    service.settings.patch({"server": {"tunnel": "cloudflare", "tunnel_hostname": "speakeasy.example.com",
                                       "tunnel_url": "https://speakeasy.example.com"}})
    T.TunnelWatcher(service, reader=lambda h, p: QUICK, send=lambda *a: True, drop=lambda *a: True).tick()
    assert service.settings.get()["server"]["tunnel_url"] == "https://speakeasy.example.com"
    service.settings.patch({"server": {"tunnel_hostname": "", "tunnel_url": ""}})
    T.TunnelWatcher(service, reader=lambda h, p: "https://evil.example.com", send=lambda *a: True,
                    drop=lambda *a: True).tick()
    assert service.settings.get()["server"]["tunnel_url"] == ""


# -- address drop -------------------------------------------------------------------------------

def test_drop_keys_are_private_and_stable(home):
    from speakeasy import address_drop as A
    first = A.ensure(home)
    assert len(first["topic"]) >= 3 + 32 and len(A._unb64(first["key"])) == 32
    assert stat.S_IMODE(os.stat(A.drop_path(home)).st_mode) == 0o600
    assert A.ensure(home)["key"] == first["key"]


def test_drop_round_trip_and_tamper(home):
    from speakeasy import address_drop as A
    d = A.ensure(home)
    sealed = A.seal(d["key"], d["topic"], QUICK, 1700000000, 3)
    assert QUICK not in sealed and "trycloudflare" not in sealed
    assert A.open_sealed(d["key"], d["topic"], sealed) == {"url": QUICK, "ts": 1700000000, "seq": 3}
    assert A.open_sealed(d["key"], "se-other-topic", sealed) is None  # bound to its topic
    bad = sealed[:-2] + ("A" if sealed[-2] != "A" else "B") + sealed[-1]
    assert A.open_sealed(d["key"], d["topic"], bad) is None
    assert A.open_sealed(A._b64(os.urandom(32)), d["topic"], sealed) is None


def test_drop_publishes_on_change_with_a_rising_counter(home):
    from speakeasy import address_drop as A
    from speakeasy.settings import Settings
    s = Settings(home)
    s.patch({"server": {"tunnel": "cloudflare", "tunnel_url": QUICK}})
    posts = []
    send = lambda url, body: posts.append((url, body)) or True  # noqa: E731
    assert A.publish_if_needed(home, s.get(), now=1000, send=send)
    assert not A.publish_if_needed(home, s.get(), now=1001, send=send)
    s.patch({"server": {"tunnel_url": QUICK2}})
    assert A.publish_if_needed(home, s.get(), now=1002, send=send)
    assert A.publish_if_needed(home, s.get(), now=1002 + A.REPUBLISH_S, send=send)  # refreshed before the relay forgets
    d = A.load(home)
    seqs = [A.open_sealed(d["key"], d["topic"], b)["seq"] for _, b in posts]
    assert seqs == [1, 2, 3] and posts[0][0] == f"https://ntfy.sh/{d['topic']}"
    s.patch({"address_drop": {"enabled": False}})
    s.patch({"server": {"tunnel_url": QUICK}})
    assert not A.publish_if_needed(home, s.get(), now=9e9, send=send)  # opted out


# -- endpoints + pairing ------------------------------------------------------------------------

def test_endpoints_need_a_token_and_list_addresses(server, service):
    status, _ = http(server.base_url, "GET", "/voice/endpoints")
    assert status == 401
    status, body = http(server.base_url, "GET", "/voice/endpoints", token=server.token)
    assert status == 200 and body["endpoints"]["cloudflare"] is None and body["address_drop"] is None
    _tunnel_on(service)
    service.settings.patch({"server": {"tailscale_name": "mini.tail1.ts.net"}})
    status, body = http(server.base_url, "GET", "/voice/endpoints", token=server.token)
    assert body["endpoints"] == {"tailnet": "https://mini.tail1.ts.net", "cloudflare": QUICK,
                                 "cloudflare_permanent": False}
    assert body["address_drop"]["relay"] == "https://ntfy.sh" and body["address_drop"]["topic"].startswith("se-")
    status, caps = http(server.base_url, "GET", "/voice/status", token=server.token)
    assert caps["capabilities"]["endpoints"] is True


def test_pair_response_carries_the_drop_but_links_never_do(service, server, home):
    from speakeasy import handoff as H
    _tunnel_on(service)
    code = service.devices.new_pairing_code()
    status, body = http(server.base_url, "POST", "/voice/pair", {"code": code, "device_name": "Phone"})
    assert status == 201 and body["address_drop"]["key"] and body["endpoints"]["cloudflare"] == QUICK
    link = H.web_link(QUICK, "123456")
    assert body["address_drop"]["key"] not in link and body["address_drop"]["topic"] not in link
    assert H.link_ttl(home) == 600


# -- public hardening ---------------------------------------------------------------------------

TUNNEL_HDRS = {"Host": "calm-river-sound-test.trycloudflare.com", "Cf-Connecting-Ip": "203.0.113.9",
               "X-Forwarded-For": "203.0.113.9"}


def test_unexpected_host_is_refused_when_a_tunnel_is_on(server, service):
    _tunnel_on(service)
    status, _ = _raw(server.base_url, "GET", "/health", {"Host": "evil.example.com"})
    assert status == 421
    status, body = _raw(server.base_url, "GET", "/health", TUNNEL_HDRS)
    assert status == 200 and body == {"ok": True, "platform": "voice"}  # no version, no provider
    status, body = _raw(server.base_url, "GET", "/health")
    assert status == 200 and "version" in body  # this machine still sees it


def test_every_route_but_pairing_needs_a_token_through_the_tunnel(server, service):
    _tunnel_on(service)
    for method, path in [("GET", "/voice/status"), ("GET", "/voice/settings"), ("GET", "/voice/endpoints"),
                         ("GET", "/voice/work/latest"), ("POST", "/voice/sessions"), ("PATCH", "/voice/settings")]:
        status, _ = _raw(server.base_url, method, path, TUNNEL_HDRS, body={} if method != "GET" else None)
        assert status == 401, (method, path, status)


def test_pairing_through_the_tunnel_is_rate_limited_with_lockout(server, service):
    _tunnel_on(service)
    code = service.devices.new_pairing_code()
    wrong = "000000" if code != "000000" else "111111"
    statuses = [_raw(server.base_url, "POST", "/voice/pair", TUNNEL_HDRS, {"code": wrong, "device_name": "x"})[0]
                for _ in range(7)]
    assert statuses[:5] == [403] * 5 and statuses[5:] == [429, 429]
    # locked out: even the right code is refused from that address
    assert _raw(server.base_url, "POST", "/voice/pair", TUNNEL_HDRS, {"code": code, "device_name": "x"})[0] == 429
    other = dict(TUNNEL_HDRS, **{"Cf-Connecting-Ip": "198.51.100.7", "X-Forwarded-For": "198.51.100.7"})
    assert _raw(server.base_url, "POST", "/voice/pair", other, {"code": code, "device_name": "Phone"})[0] == 201


def test_pair_limiter_window_and_global_cap():
    from speakeasy.server import PairLimiter
    t = {"now": 0.0}
    lim = PairLimiter(per_ip=2, total=3, window_s=60, lockout_s=600, clock=lambda: t["now"])
    assert lim.allow("a") and lim.allow("a") and not lim.allow("a")
    assert lim.allow("b") and not lim.allow("c")  # global cap
    t["now"] = 61
    assert lim.allow("c") and not lim.allow("a")  # a is still locked out
    t["now"] = 700
    assert lim.allow("a")


def test_setup_without_tailscale_recommends_it_and_offers_cloudflare():
    from speakeasy import cli
    assert "Recommended: Tailscale" in cli.TAILSCALE_RECOMMENDED
    assert "AGENT_SETUP.md#cloudflare-mode" in cli.CLOUDFLARE_OFFER and "public" in cli.CLOUDFLARE_NOTE


def test_prove_binds_token_nonce_and_host_without_revealing_the_token(server, service, home):
    import hashlib, hmac
    code = service.devices.new_pairing_code()
    _, paired = http(server.base_url, "POST", "/voice/pair", {"code": code, "device_name": "Phone"})
    nonce = "n" * 32
    _tunnel_on(service)
    status, body = _raw(server.base_url, "POST", "/voice/prove", TUNNEL_HDRS, {"device_id": paired["device_id"], "nonce": nonce})
    key = hashlib.sha256(paired["token"].encode()).hexdigest().encode()
    want = hmac.new(key, f"speakeasy-prove-v1|{nonce}|calm-river-sound-test.trycloudflare.com".encode(), hashlib.sha256).hexdigest()
    assert status == 200 and body["proof"] == want and paired["token"] not in json.dumps(body)
    _, other = _raw(server.base_url, "POST", "/voice/prove", TUNNEL_HDRS, {"device_id": "ffffffff", "nonce": nonce})
    _, again = _raw(server.base_url, "POST", "/voice/prove", TUNNEL_HDRS, {"device_id": "ffffffff", "nonce": nonce})
    assert other["proof"] != again["proof"]  # unknown ids get noise
    assert _raw(server.base_url, "POST", "/voice/prove", TUNNEL_HDRS, {"device_id": "x", "nonce": "short"})[0] == 400
