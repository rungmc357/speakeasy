"""Onboarding copy the user (or their agent, relaying it) reads: the restart step points at Hermes'
own `/restart` chat command first, with the terminal form only as a fallback."""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_restart_note_leads_with_the_chat_command():
    from speakeasy import setup_flow as F
    note = F.RESTART_NOTE
    assert "/restart" in note and note.index("/restart") < note.index("hermes gateway restart")
    assert "From a terminal instead" in note


def test_app_facing_restart_text_says_send_restart():
    from speakeasy.service import RESTART_NEEDED
    assert "/restart" in RESTART_NEEDED
    assert RESTART_NEEDED.index("/restart") < RESTART_NEEDED.index("hermes gateway restart")


def test_docs_say_send_restart_wherever_they_say_gateway_restart():
    """Every user-aimed `hermes gateway restart` in README and setup.md comes with `/restart` first."""
    for rel in ("README.md", "docs/AGENT_SETUP.md"):
        flat = _flat((REPO / rel).read_text())
        for m in re.finditer(r"hermes gateway restart", flat):
            before = flat[max(0, m.start() - 260):m.start()]
            assert "/restart" in before, f"{rel}: '{flat[m.start() - 120:m.end()]}'"


def test_reload_fallback_mentions_restart_command(tmp_path, capsys):
    from speakeasy import cli
    assert cli.cmd_reload(tmp_path, timeout=0) == 1
    out = capsys.readouterr().out
    assert "/restart" in out and out.index("/restart") < out.index("hermes gateway restart")
