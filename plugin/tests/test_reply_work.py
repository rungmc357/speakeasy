"""Additive narration evidence for every history source; no real conversations."""
import json
from speakeasy import history as H


def row(n, role, content="", calls=None, call_id=None, tool=None):
    return dict(id=n, role=role, content=content, timestamp=float(n),
                tool_calls=json.dumps(calls) if calls is not None else None,
                tool_call_id=call_id, tool_name=tool)


def test_tool_commentary_is_not_an_answer_and_keeps_status_detail():
    entries = H.build_entries([
        row(1, "user", "Check the build"),
        row(2, "assistant", "STATUS: Checking the report\nDETAIL: Reading the saved log", [
            {"id": "a", "function": {"name": "terminal", "arguments": json.dumps({"command": "printf sample"})}}]),
        row(3, "tool", "sample", call_id="a", tool="terminal"),
        row(4, "assistant", "The build passed.\n```sh\nprintf sample\n```"),
    ])
    narration = entries[1]
    assert narration["narration"] is True
    assert narration["text"] == "Checking the report"
    assert narration["work_detail"] == "Reading the saved log"
    assert "narration" not in entries[-1]
    assert "```sh\nprintf sample\n```" in entries[-1]["text"]
    assert entries[2]["kind"] == "tools"
    assert entries[2]["items"][0]["result"] == "sample"


def test_status_only_without_tool_calls_stays_narration():
    entry = H.build_entries([row(1, "assistant", "STATUS: Checking the report\nDETAIL: Reading its log")])[0]
    assert entry["narration"] is True
    assert entry["text"] == "Checking the report"
    assert entry["work_detail"] == "Reading its log"


def test_ordinary_final_with_status_keeps_full_answer():
    entry = H.build_entries([row(1, "assistant", "STATUS: Build checked\n\nThe written answer has all the findings.")])[0]
    assert "narration" not in entry
    assert "The written answer has all the findings." in entry["text"]


def test_plain_tool_commentary_is_marked_without_keyword_guessing():
    entry = H.build_entries([row(1, "assistant", "Reading the test report", [
        {"id": "a", "function": {"name": "read_file", "arguments": "{}"}}])])[0]
    assert entry["narration"] is True
    assert entry["text"] == "Reading the test report"


def test_plain_answer_is_not_inferred_from_its_wording():
    entry = H.build_entries([row(1, "assistant", "Now checking is unnecessary: the report already passed.")])[0]
    assert "narration" not in entry


def test_detail_redaction_is_preserved():
    redact_text = H.redact_text
    text = "Contact build@example.test about the saved report"
    entry = H.build_entries([row(1, "assistant", "STATUS: Checking the report\nDETAIL: " + text)])[0]
    assert entry["work_detail"] == redact_text(text)
