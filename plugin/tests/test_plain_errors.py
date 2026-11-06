"""SPK-154: no error text the apps show names a program error ("TimeoutError", "internal error (KeyError)").

The apps display ServiceError/HermesError messages, task-card errors and route errors unchanged, so a
Python exception name in them reached people as "Voice session creation failed: TimeoutError". The
exception name belongs in the log only; reason codes the apps branch on stay as they are.
"""
from __future__ import annotations

import ast
import json
import logging
import re
import subprocess
import urllib.error
from pathlib import Path

import pytest

from speakeasy import plain_errors
from speakeasy.calls import ServiceError
from speakeasy.hermes_api import HermesAPI, HermesError

PROGRAM_ERROR = re.compile(r"[A-Z][a-z]+(Error|Exception)\b")
SOURCE = Path(__file__).resolve().parents[1] / "speakeasy"
# Exceptions whose message an app (or a task card / brief status) shows as it is.
USER_FACING = {"ServiceError", "HermesError", "CodexLoginError", "CodexStartError", "BriefInvalid",
               "ScheduleError"}


def _names_an_exception(node: ast.AST) -> bool:
    """An f-string piece like {type(exc).__name__}, {exc} or {exc!r}: text the program chose, not us."""
    for part in ast.walk(node):
        if isinstance(part, ast.Attribute) and part.attr == "__name__":
            return True
        if isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name) and \
                part.value.id in {"exc", "e", "err", "error", "caught", "cause"}:
            return True
    return False


def _message_args(call: ast.Call) -> list[ast.AST]:
    return [a for a in call.args if isinstance(a, (ast.JoinedStr, ast.Constant))]


def _literal_text(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return ""


def _user_facing_messages():
    """(where, node) for every message handed to a user-facing exception, an ``.error`` the apps
    read (interaction/backend error), or a route's {"error": …} body."""
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            where = f"{path.name}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                if name in USER_FACING:
                    for arg in _message_args(node):
                        yield where, arg
                if name == "_reply":
                    for arg in node.args:
                        if isinstance(arg, ast.Dict):
                            for key, value in zip(arg.keys, arg.values):
                                if isinstance(key, ast.Constant) and key.value == "error":
                                    yield where, value
            elif isinstance(node, ast.Assign):
                targets = []
                for target in node.targets:
                    targets.extend(target.elts if isinstance(target, ast.Tuple) else [target])
                values = node.value.elts if isinstance(node.value, ast.Tuple) else [node.value]
                for target, value in zip(targets, values if len(values) == len(targets) else [node.value] * len(targets)):
                    if isinstance(target, ast.Attribute) and target.attr == "error":
                        yield where, value


def test_no_user_facing_error_names_a_program_error():
    found = []
    for where, node in _user_facing_messages():
        if _names_an_exception(node) or PROGRAM_ERROR.search(_literal_text(node)):
            found.append(f"{where}: {ast.unparse(node)}")
    assert not found, "error text the apps show names a program error:\n" + "\n".join(found)


def test_the_scan_sees_the_known_places():
    """Guard the guard: it really reads the route errors, task-card errors and raised messages."""
    places = {where.split(":")[0] for where, _ in _user_facing_messages()}
    assert {"server.py", "calls.py", "service.py", "hermes_api.py", "openai_live.py", "grok_live.py"} <= places


def test_the_shared_sentences_are_plain():
    for name in dir(plain_errors):
        if name.isupper():
            text = getattr(plain_errors, name)
            assert not PROGRAM_ERROR.search(text), name
            assert text.endswith("."), name  # a sentence, not a fragment like "internal error"
    assert plain_errors.HERMES_UNAVAILABLE.startswith("Hermes unavailable")  # failures.start_failure_kind
    assert plain_errors.VOICE_LOST.startswith("Voice connection lost")  # iPhone CallHealth keys on it


def test_hermes_unreachable_says_a_sentence_and_logs_the_cause(caplog):
    def opener(_req, timeout=0):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))
    client = HermesAPI("http://127.0.0.1:1", lambda: "k", opener=opener)
    with caplog.at_level(logging.INFO, logger="speakeasy.hermes_api"), pytest.raises(HermesError) as raised:
        client._json("GET", "/v1/capabilities")
    assert raised.value.message == plain_errors.HERMES_UNAVAILABLE
    assert not PROGRAM_ERROR.search(raised.value.message)
    assert "URLError" in caplog.text
    from speakeasy import failures
    assert failures.start_failure_kind(raised.value) == "hermes_unreachable"  # still recognized


def test_voice_provider_unreachable_is_a_sentence(caplog):
    from speakeasy.openai_live import negotiate

    def opener(_req, timeout=0):
        raise TimeoutError()
    with caplog.at_level(logging.WARNING, logger="speakeasy.openai_live"), pytest.raises(ServiceError) as raised:
        negotiate("k", {}, opener=opener)
    assert raised.value.message == plain_errors.VOICE_UNREACHABLE
    assert "TimeoutError" in caplog.text


def test_codex_that_wont_run_is_a_sentence(tmp_path):
    from speakeasy import codex_transport

    def runner(*_a, **_k):
        raise subprocess.TimeoutExpired("codex", 10)
    codex_transport._LOGIN_CACHE.clear()
    ok, text = codex_transport.login_status(tmp_path / "codex", runner=runner)
    assert not ok and not PROGRAM_ERROR.search(text) and "Expired" not in text
    codex_transport._LOGIN_CACHE.clear()


def test_a_route_500_is_a_sentence(server, service, monkeypatch):
    from fakes import http

    def boom(_body):
        raise KeyError("missing")
    monkeypatch.setattr(service, "patch_settings", boom)
    status, body = http(server.base_url, "PATCH", "/voice/settings", {"assistant_name": "Nova"}, server.token)
    assert status == 500 and body == {"error": plain_errors.INTERNAL_ERROR}
    assert "KeyError" not in json.dumps(body)
