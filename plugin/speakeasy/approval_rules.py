"""The user's Hermes approval rules, read-only and in plain words (``GET /voice/approvals``,
capability ``approval_rules``).

Hermes decides when a risky command needs the user's OK: ``approvals.mode`` (ask every time, a
quick safety check by a second model, or never ask), ``command_allowlist`` (kinds of commands the
user answered "Always" to), ``approvals.deny`` (patterns that are always blocked), what scheduled
jobs do when nobody can answer (``approvals.cron_mode``), how long a question waits
(``approvals.timeout``) and whether memory and skill changes wait for an OK (``*.write_approval``).

This module only reads this profile's config.yaml (merged over Hermes' defaults, the way Hermes
itself reads it) and turns it into sections the apps show as they are, so the wording improves
with a plugin update and no app build. It never writes: changing rules is a later step.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .settings import hermes_config

# What Hermes uses when a key is missing, for Hermes versions whose defaults can't be imported.
_FALLBACK_DEFAULTS = {"mode": "smart", "timeout": 300, "cron_mode": "deny", "unattended_mode": "deny",
                      "smart_policy": "", "deny": []}

MODES = {
    "manual": ("Ask you every time",
               "Before a risky command runs, Hermes stops and asks you."),
    "smart": ("Quick safety check first",
              "A second model looks at each risky command. Clearly safe ones run, clearly harmful ones are "
              "blocked, and anything it isn't sure about comes to you."),
    "off": ("Never ask",
            "Risky commands run without asking. The few that could wreck the computer are still blocked."),
}

# Kinds of commands, in plain words. Hermes names each rule with a short technical description
# (e.g. "git force push (rewrites remote history)"); the first pattern that matches gives the
# label. Several rules often share one label ("recursive delete", "recursive delete (long flag)").
_KINDS: list[tuple[str, str]] = [
    (r"hermes (?:gateway|launchd|upd)|start gateway|hermes/gateway|self-termination",
     "Stopping or restarting Hermes itself"),
    (r"hermes config|hermes secrets", "Changing your Hermes settings or keys"),
    (r"git force push", "Force-pushing over a project's shared history"),
    (r"git reset --hard", "Throwing away unsaved code changes"),
    (r"git clean", "Deleting files a project doesn't track"),
    (r"git branch force delete", "Force-deleting code branches"),
    (r"\bsql\b", "Deleting database tables or rows"),
    (r"shutdown|reboot|poweroff|init 0", "Shutting down or restarting the computer"),
    (r"fork bomb", "Commands that could freeze the computer"),
    (r"format|wipe|disk|block device|partition|shadow cop|backups", "Erasing disks or backups"),
    (r"exfiltration|credential|secret|ssh|cloud metadata|ssh_config", "Touching passwords, keys or secrets"),
    (r"docker|podman|container", "Managing containers"),
    (r"remote content|remote script|invoke-expression", "Running scripts straight from the internet"),
    (r"obfuscation|encoded|unicode", "Running disguised or encoded commands"),
    (r"registry|bcdedit|boot configuration", "Changing Windows system settings"),
    (r"system service|stop-service|delete service", "Stopping or restarting system services"),
    (r"delete|\brm\b|unlink", "Deleting folders and everything in them"),
    (r"kill|stop-process|taskkill", "Force-stopping other programs"),
    (r"sudo|privilege", "Running commands as administrator"),
    (r"permission|writable|chown|icacls|acl", "Changing who can read or change files"),
    (r"system config|system file", "Changing the computer's system files"),
    (r"project (?:config|env)", "Overwriting a project's settings file"),
    (r"package manager uninstall", "Uninstalling software"),
    (r"parser limit|malformed", "Commands too complex to check"),
    (r"protected_instruction_file", "Editing the agent's instruction files"),
    (r"mcp_elicitation", "Answering a connected app's questions"),
    (r"heredoc|-c/|-e/|script execution|execute_code|chmod \+x|shell word|execution flag",
     "Running scripts and code snippets"),
]
_KINDS_RE = [(re.compile(pattern, re.IGNORECASE), label) for pattern, label in _KINDS]

ALWAYS_BLOCKED = ("A few commands are always blocked, whatever the setting: wiping the computer or your home "
                  "folder, erasing a disk, freezing the computer and shutting it down.")


def kind_label(key: str) -> str:
    """Plain words for one Hermes approval rule name (never the raw name or a tool identifier)."""
    text = str(key or "").strip()
    for pattern, label in _KINDS_RE:
        if pattern.search(text):
            return label
    plain = re.sub(r"\s*\([^)]*\)", "", text.replace("_", " ")).strip() or "Other commands"
    return plain[:1].upper() + plain[1:]


def _defaults() -> dict[str, Any]:
    try:
        from hermes_cli.config import DEFAULT_CONFIG  # type: ignore
    except Exception:  # noqa: BLE001 - older or missing Hermes
        DEFAULT_CONFIG = {}
    out = {"approvals": dict(_FALLBACK_DEFAULTS), "memory": {"write_approval": False},
           "skills": {"write_approval": False}}
    for section in out:
        block = DEFAULT_CONFIG.get(section) if isinstance(DEFAULT_CONFIG, dict) else None
        if isinstance(block, dict):
            out[section].update({k: v for k, v in block.items() if k in out[section] or section == "approvals"})
    return out


def _mode(value: Any) -> str:
    # Same reading as Hermes: YAML turns a bare ``off`` into False; unknown words mean "manual".
    if isinstance(value, bool):
        return "off" if value is False else "manual"
    if isinstance(value, str) and value.strip().lower() in MODES:
        return value.strip().lower()
    return "manual"


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _wait(seconds: Any) -> str:
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError):
        s = 300
    if s < 60:
        return f"{s} seconds"
    if s < 3600:
        m = round(s / 60)
        return "1 minute" if m == 1 else f"{m} minutes"
    h = round(s / 3600)
    return "1 hour" if h == 1 else f"{h} hours"


def rules(hermes_home: Path) -> dict[str, Any]:
    """``GET /voice/approvals``: this profile's approval rules as sections of plain rows."""
    from .profiles import profile_name

    cfg = hermes_config(hermes_home)
    base = _defaults()
    raw = cfg.get("approvals") if isinstance(cfg.get("approvals"), dict) else {}
    approvals = {**base["approvals"], **raw}
    mode = _mode(approvals.get("mode"))
    title, detail = MODES[mode]

    def write_gate(section: str) -> bool:
        block = cfg.get(section) if isinstance(cfg.get(section), dict) else {}
        return bool(block.get("write_approval", base[section]["write_approval"]))

    sections: list[dict[str, Any]] = []
    mode_rows = [{"id": "mode", "title": title, "detail": detail}]
    policy = str(approvals.get("smart_policy") or "").strip()
    if mode == "smart" and policy:
        mode_rows.append({"id": "smart_policy", "title": "Your extra instructions for the safety check",
                          "detail": policy})
    if mode != "off":
        mode_rows.append({"id": "timeout", "title": "Waits for your answer",
                          "value": _wait(approvals.get("timeout")),
                          "detail": "If nobody answers in time, the command is blocked."})
    sections.append({"id": "mode", "title": "Risky commands", "rows": mode_rows})

    # Always allowed: one row per plain kind, however many Hermes rule names fold into it.
    kinds: dict[str, int] = {}
    for key in sorted(set(_strings(cfg.get("command_allowlist")))):
        label = kind_label(key)
        kinds[label] = kinds.get(label, 0) + 1
    allowed = [{"id": f"allow_{i}", "title": label} for i, label in enumerate(sorted(kinds))]
    sections.append({
        "id": "always_allowed", "title": "Always allowed",
        "rows": allowed,
        "empty": "Nothing yet. When you answer Always to a question, that kind of command shows up here.",
        "footer": ("These run without asking because you answered Always once. "
                   "Speakeasy only ever allows a command once or for one task, never always."),
    })

    blocked = [{"id": f"deny_{i}", "title": "Commands matching", "code": glob}
               for i, glob in enumerate(_strings(approvals.get("deny")))]
    sections.append({"id": "always_blocked", "title": "Always blocked", "rows": blocked,
                     "footer": ALWAYS_BLOCKED + (" Your own rules above apply even when Hermes never asks."
                                                 if blocked else "")})

    def unattended(value: Any) -> str:
        return "Run without asking" if str(value).strip().lower() == "approve" else "Blocked"
    sections.append({"id": "nobody_there", "title": "When nobody can answer", "rows": [
        {"id": "cron_mode", "title": "Scheduled jobs", "value": unattended(approvals.get("cron_mode"))},
        {"id": "unattended_mode", "title": "Webhooks and other automatic runs",
         "value": unattended(approvals.get("unattended_mode"))},
    ], "footer": "What happens to a risky command when no one is there to answer the question."})

    def gate(on: bool) -> str:
        return "Waits for your OK" if on else "Saved without asking"
    sections.append({"id": "writes", "title": "Memory and skills", "rows": [
        {"id": "memory", "title": "Changes to memory", "value": gate(write_gate("memory"))},
        {"id": "skills", "title": "Changes to skills", "value": gate(write_gate("skills"))},
    ]})

    return {"approvals": {
        "profile": profile_name(hermes_home),
        "mode": mode,
        "read_only": True,
        "sections": sections,
        "note": "You can't change these from Speakeasy yet. In a Hermes chat, /approvals switches how risky "
                "commands are handled.",
    }}
