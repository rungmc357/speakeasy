"""GET /voice/approvals: the user's Hermes approval rules, read-only and in plain words."""
from __future__ import annotations

import json

import pytest

from fakes import http
from speakeasy import approval_rules as A


def _rules(server):
    status, body = http(server.base_url, "GET", "/voice/approvals", token=server.token)
    assert status == 200
    return body["approvals"]


def _section(rules, sid):
    return next(s for s in rules["sections"] if s["id"] == sid)


def _row(rules, sid, rid):
    return next(r for r in _section(rules, sid)["rows"] if r["id"] == rid)


def test_manual_rules_read_in_plain_words_and_the_file_is_untouched(server, home):
    config = ("approvals:\n  mode: manual\n  timeout: 90\n  cron_mode: approve\n"
              "  deny:\n    - 'git push --force*'\n    - 'curl * | sh'\n"
              "command_allowlist:\n  - recursive delete\n  - recursive delete (long flag)\n  - find -delete\n"
              "  - execute_code\n  - git force push (rewrites remote history)\n"
              "memory:\n  write_approval: true\n")
    (home / "config.yaml").write_text(config)
    rules = _rules(server)
    assert (home / "config.yaml").read_text() == config  # read-only
    assert rules["mode"] == "manual" and rules["read_only"] is True
    assert _row(rules, "mode", "mode")["title"] == "Ask you every time"
    assert _row(rules, "mode", "timeout")["value"] == "2 minutes"
    allowed = [r["title"] for r in _section(rules, "always_allowed")["rows"]]
    # Three Hermes rule names fold into one kind; no raw rule or tool name reaches the screen.
    assert allowed == ["Deleting folders and everything in them", "Force-pushing over a project's shared history",
                       "Running scripts and code snippets"]
    text = json.dumps(rules)
    for raw in ("execute_code", "recursive delete", "long flag", "rewrites remote history", "find -delete"):
        assert raw not in text
    assert [r["code"] for r in _section(rules, "always_blocked")["rows"]] == ["git push --force*", "curl * | sh"]
    assert _row(rules, "nobody_there", "cron_mode")["value"] == "Run without asking"
    assert _row(rules, "nobody_there", "unattended_mode")["value"] == "Blocked"
    assert _row(rules, "writes", "memory")["value"] == "Waits for your OK"
    assert _row(rules, "writes", "skills")["value"] == "Saved without asking"
    assert not any(r["id"] == "smart_policy" for r in _section(rules, "mode")["rows"])


def test_bare_off_in_yaml_means_never_ask_and_has_no_wait(server, home):
    # YAML 1.1 reads a bare `off` as False; Hermes treats it as "off", so must we.
    (home / "config.yaml").write_text("approvals:\n  mode: off\n")
    rules = _rules(server)
    assert rules["mode"] == "off"
    rows = _section(rules, "mode")["rows"]
    assert rows[0]["title"] == "Never ask" and "still blocked" in rows[0]["detail"]
    assert [r["id"] for r in rows] == ["mode"]
    allowed = _section(rules, "always_allowed")
    assert allowed["rows"] == [] and allowed["empty"].startswith("Nothing yet")


def test_smart_mode_shows_the_users_extra_instructions(server, home):
    (home / "config.yaml").write_text("approvals:\n  mode: smart\n  timeout: 30\n"
                                      "  smart_policy: Always ask before touching the shared drive\n")
    rules = _rules(server)
    assert _row(rules, "mode", "mode")["title"] == "Quick safety check first"
    assert _row(rules, "mode", "smart_policy")["detail"] == "Always ask before touching the shared drive"
    assert _row(rules, "mode", "timeout")["value"] == "30 seconds"


def test_missing_keys_use_hermes_defaults(server, home):
    (home / "config.yaml").write_text("model:\n  default: some-model\n")
    rules = _rules(server)
    try:
        from hermes_cli.config import DEFAULT_CONFIG
        expected = DEFAULT_CONFIG["approvals"]["mode"]
    except Exception:  # noqa: BLE001 - no Hermes on this machine
        expected = A._FALLBACK_DEFAULTS["mode"]
    assert rules["mode"] == expected
    assert _row(rules, "nobody_there", "cron_mode")["value"] == "Blocked"


def test_unknown_mode_reads_as_ask_every_time_like_hermes(server, home):
    (home / "config.yaml").write_text("approvals:\n  mode: sometimes\ncommand_allowlist: not-a-list\n")
    rules = _rules(server)
    assert rules["mode"] == "manual"


def test_route_needs_a_device_token_and_capability_is_advertised(server):
    status, _ = http(server.base_url, "GET", "/voice/approvals")
    assert status == 401
    status, body = http(server.base_url, "GET", "/voice/status", token=server.token)
    assert status == 200 and body["capabilities"]["approval_rules"] is True


@pytest.mark.parametrize("key,label", [
    ("git reset --hard (destroys uncommitted changes)", "Throwing away unsaved code changes"),
    ("SQL DELETE without WHERE", "Deleting database tables or rows"),
    ("in-place edit of Hermes config/env", "Changing your Hermes settings or keys"),
    ("docker restart/stop/kill (container lifecycle)", "Managing containers"),
    ("registry delete (reg delete)", "Changing Windows system settings"),
    ("sudo with privilege flag (stdin/askpass/shell/list)", "Running commands as administrator"),
    ("a_brand_new_rule (detail)", "A brand new rule"),
])
def test_kind_labels(key, label):
    assert A.kind_label(key) == label


def test_every_hermes_rule_name_gets_a_plain_label():
    detection = pytest.importorskip("tools.approval_detection")
    names = {desc for _pattern, desc in detection.DANGEROUS_PATTERNS_COMPILED}
    for name in names:
        label = A.kind_label(name)
        assert label != name and "_" not in label and "(" not in label, name
