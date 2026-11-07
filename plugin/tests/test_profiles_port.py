"""Two Hermes profiles on one machine: setup never pairs against the other profile's voice server,
moves to a free port when another profile holds its own, and says which profile it set up."""
from __future__ import annotations

from fakes import yaml
import pytest

from speakeasy import agent_model as AM, cli, profiles as P, setup_flow as F
from test_setup import Fake, args


def _profile(path, port=None):
    path.mkdir(parents=True, exist_ok=True)
    cfg = {"model": {"default": "m"}}
    if port:
        cfg["gateway"] = {"platforms": {"voice": {"enabled": True, "extra": {"port": port}}}}
    (path / "config.yaml").write_text(yaml.safe_dump(cfg))
    return path


@pytest.fixture
def two(tmp_path, monkeypatch):
    root = _profile(tmp_path / "hermes", 8795)          # the default profile, Speakeasy on 8795
    work = _profile(root / "profiles" / "work")          # a second profile, Speakeasy not set up yet
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(F, "gateway_running", lambda h: True)
    monkeypatch.setattr(F, "find_codex", lambda configured="": tmp_path / "codex")
    monkeypatch.setattr(F, "codex_candidates", lambda: [])  # never this machine's real Codex copies
    return root, work


def _setup(home, servers: dict, **kw):
    """servers: port -> the home whose voice server answers there."""
    fake = Fake(interactive=False)
    env = fake.env()
    env.health = None
    env.agent_model = lambda: AM.OK  # identity/port fakes must not consult the host provider
    env.identity = lambda url: ({"platform": "voice", "home_id": P.home_id(servers[int(url.rsplit(":", 1)[1])]),
                                 "profile": P.profile_name(servers[int(url.rsplit(":", 1)[1])])}
                                if int(url.rsplit(":", 1)[1]) in servers else None)
    env.port_free = lambda port: port not in servers
    rc = cli.cmd_setup(args(**kw), home, env=env)
    return rc, "\n".join(fake.lines)


def _port(home):
    return yaml.safe_load((home / "config.yaml").read_text())["gateway"]["platforms"]["voice"]["extra"]["port"]


def test_second_profile_gets_its_own_port_and_never_pairs_with_the_first(two):
    root, work = two
    rc, text = _setup(work, {8795: root}, yes=True)
    assert _port(work) == 8805 and _port(root) == 8795   # the other profile's config is untouched
    assert "Port 8795 is already used by the Hermes profile 'default'" in text and "('work') uses port 8805" in text
    assert rc == 2 and "✓ Voice server is running" not in text and "#server=" not in text
    assert not (work / "speakeasy" / "devices.json").exists() or "code" not in (work / "speakeasy" / "devices.json").read_text()


def test_profile_already_on_a_clashing_port_moves(two):
    root, work = two
    _profile(work, 8795)
    rc, text = _setup(work, {8795: root}, yes=True)
    assert _port(work) == 8805 and rc == 2


def test_its_own_server_is_recognised_and_named(two):
    root, work = two
    _profile(work, 8805)
    rc, text = _setup(work, {8795: root, 8805: work}, yes=True)
    assert rc == 0 and "✓ Voice server is running (Hermes profile 'work', port 8805)" in text
    assert "127.0.0.1%3A8805" in text and "8795" not in text.split("Voice server is running")[1]


def test_default_profile_keeps_its_port(two):
    root, work = two
    rc, text = _setup(root, {8795: root}, yes=True)
    assert rc == 0 and _port(root) == 8795 and "(Hermes profile 'default', port 8795)" in text


@pytest.mark.parametrize("profile, port", [("default", 8795), ("work", 8805)])
@pytest.mark.parametrize("host_state", [AM.MISSING, AM.UNKNOWN, "raises"])
def test_setup_never_resolves_the_host_model(two, monkeypatch, profile, port, host_state):
    root, work = two
    home = root if profile == "default" else work
    _profile(home, port)
    calls = []

    def host_model():
        calls.append(True)
        if host_state == "raises":
            raise AssertionError("Setup must use its fake model, not the host resolver")
        return host_state

    monkeypatch.setattr(AM, "resolve", host_model)
    rc, text = _setup(home, {8795: root, 8805: work}, yes=True)
    assert calls == []
    assert rc == 0
    assert f"✓ Voice server is running (Hermes profile '{profile}', port {port})" in text
    assert f"127.0.0.1%3A{port}" in text
    assert "voice brief in the background" in text and AM.SETUP_LINE not in text
    assert _port(home) == port


def test_an_older_server_without_a_profile_id_on_a_claimed_port_is_not_ours(two):
    root, work = two
    _profile(work, 8795)
    claimed = {8795: "default"}
    assert P.whose(work, {"platform": "voice"}, 8795, claimed) == "default"
    assert P.whose(work, {"platform": "voice"}, 8805, claimed) == "ours"   # nobody else claims it
    assert P.whose(work, None, 8795, claimed) == "none"


def test_a_port_held_by_some_other_program_is_skipped(two):
    root, work = two
    port, holder = P.choose_port(root, identity=lambda url: None, free=lambda p: p != 8795)
    assert (port, holder) == (8805, "another program")


def test_cloudflare_step_names_the_profile_and_its_ports(two):
    root, work = two
    _profile(work, 8805)
    lines = cli.cloudflare_commands(work, 8805)
    assert lines[0] == cli.CLOUDFLARE_DOWNLOAD
    assert lines[1] == f"{cli.CLOUDFLARE_QUICK} --hermes-home {work} --port 8805 --metrics-port 8807"
    assert cli.cloudflare_commands(root, 8795) == [cli.CLOUDFLARE_DOWNLOAD, cli.CLOUDFLARE_QUICK]


def test_local_health_names_the_profile(server, service):
    from fakes import http
    status, body = http(server.base_url, "GET", "/health")
    assert status == 200 and body["home_id"] == P.home_id(service.home) and body["profile"]
    assert P.whose(service.home, body, 1, {}) == "ours"
