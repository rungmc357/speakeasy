"""SPK-158 (the SPK-118 copy table): what the plugin sends to the apps never says "restart Hermes",
"Hermes's" / "Hermes'", "Work failed" or an error code, and unfinished work has one label."""
import ast
from pathlib import Path

from speakeasy import failures, push, recovery  # noqa: F401  (imported so the modules exist)
from speakeasy.service import RESTART_NEEDED

PKG = Path(__file__).resolve().parent.parent / "speakeasy"
# Terminal setup text (SPK-137), and agent-facing prompt text that tells the agent not to restart.
EXEMPT = {"cli.py", "tunnel.py", "setup_flow.py", "builder.py"}
RETIRED = ("restart Hermes", "Restart Hermes", "Hermes's", "Hermes' ", "Work failed", "Work interrupted",
           "(code ", "safety filter refused", "too many pairing attempts")


def _user_strings(tree: ast.AST):
    """String constants outside docstrings and logger/print calls (those reach a log, not a person)."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                skip.add(id(body[0].value))
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            owner = getattr(f.value, "id", "") if isinstance(f, ast.Attribute) else ""
            # logged only, or an exception that is caught and logged (ThreadError: threads.py)
            if owner in {"logger", "log", "logging"} or name in {"print", "ThreadError", "compile"}:
                for sub in ast.walk(node):
                    skip.add(id(sub))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            yield node


def test_no_retired_lines_in_plugin_strings():
    hits = []
    paths = sorted(PKG.rglob("*.py"))
    assert len(paths) > 50
    for path in paths:
        if path.name in EXEMPT:
            continue
        tree = ast.parse(path.read_text())
        for node in _user_strings(tree):
            for old in RETIRED:
                if old in node.value:
                    hits.append(f"{path.relative_to(PKG)}:{node.lineno}: {old}")
    # Owned by SPK-156 (Codex sign-in rows): not this pass.
    hits = [h for h in hits if not h.startswith("codex_transport.py")]
    assert hits == []


def test_unfinished_work_is_one_label():
    assert push.PREFIX["failed"] == "Didn't finish"
    assert failures.REASONS["content_refused"][0] == "AI model declined"
    assert "safety filter" not in failures.voice_text("content_refused")


def test_restart_line_names_the_chat_command():
    assert "/restart" in RESTART_NEEDED and "your Hermes" in RESTART_NEEDED
    assert "gateway" not in RESTART_NEEDED
