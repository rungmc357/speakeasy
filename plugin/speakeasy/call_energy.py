"""One call's energy record from the app (`POST /voice/telemetry/call-energy`).

The iPhone sends it once when a call ends: how long the call ran, where it was seen from, the app's
CPU seconds, the phone's thermal state changes, Low Power Mode, and the orb's frame rate. Numbers
and short names only (no words, no audio). The plugin writes one ``call.energy ...`` line to
agent.log, which the quality loop's call watch reads, so heat on real phones is measured instead of
guessed from the simulator.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("speakeasy.service")

HEAT = ("nominal", "fair", "serious", "critical")
LAYOUTS = ("full", "home", "transcript", "other", "background")
_SHORT = re.compile(r"[^\w .,()+:-]")
_MAX_CHANGES = 24


class CallEnergyError(ValueError):
    pass


def _num(body: dict[str, Any], key: str, hi: float, *, required: bool = False) -> float | None:
    v = body.get(key)
    if v is None:
        if required:
            raise CallEnergyError(f"{key} is required")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= hi:
        raise CallEnergyError(f"{key} must be a number from 0 to {hi:g}")
    return round(float(v), 1)


def _short(v: Any, key: str, limit: int = 40) -> str:
    if v is None:
        return "?"
    if not isinstance(v, str) or len(v) > limit:
        raise CallEnergyError(f"{key} must be a short string")
    return _SHORT.sub("", v) or "?"


def parse(body: Any) -> dict[str, Any]:
    """Validate the app's record; returns the clean fields (raises CallEnergyError)."""
    if not isinstance(body, dict):
        raise CallEnergyError("body must be an object")
    allowed = {"duration_s", "cpu_s", "cpu_pct", "layout_s", "thermal_start", "thermal_max", "thermal",
               "low_power", "low_power_s", "backoff_s", "orb_fps", "app", "device", "interaction_id"}
    extra = set(body) - allowed
    if extra:
        raise CallEnergyError("unknown fields: " + ", ".join(sorted(extra)[:5]))
    day = 24 * 3600.0
    out: dict[str, Any] = {
        "duration_s": _num(body, "duration_s", day, required=True),
        "cpu_s": _num(body, "cpu_s", day * 64, required=True),
        "cpu_pct": _num(body, "cpu_pct", 6400),
        "low_power_s": _num(body, "low_power_s", day),
        "backoff_s": _num(body, "backoff_s", day),
        "orb_fps": _num(body, "orb_fps", 240),
    }
    layout = body.get("layout_s") or {}
    if not isinstance(layout, dict) or len(layout) > len(LAYOUTS):
        raise CallEnergyError("layout_s must be an object of seconds by layout")
    out["layout_s"] = {}
    for name, secs in layout.items():
        if name not in LAYOUTS:
            raise CallEnergyError(f"unknown layout {_short(name, 'layout')}")
        out["layout_s"][name] = _num({"s": secs}, "s", day)
    for key in ("thermal_start", "thermal_max"):
        v = body.get(key, "nominal")
        if v not in HEAT:
            raise CallEnergyError(f"{key} must be one of {', '.join(HEAT)}")
        out[key] = v
    changes = body.get("thermal") or []
    if not isinstance(changes, list) or len(changes) > _MAX_CHANGES:
        raise CallEnergyError("thermal must be a short list")
    out["thermal"] = []
    for c in changes:
        if not isinstance(c, dict) or c.get("state") not in HEAT:
            raise CallEnergyError("thermal entries need a state")
        out["thermal"].append((c["state"], _num(c, "at_s", day, required=True)))
    lp = body.get("low_power", False)
    if not isinstance(lp, bool):
        raise CallEnergyError("low_power must be true or false")
    out["low_power"] = lp
    out["app"] = _short(body.get("app"), "app")
    out["device"] = _short(body.get("device"), "device")
    iid = body.get("interaction_id")
    out["interaction_id"] = _short(iid, "interaction_id", 64) if iid is not None else None
    return out


def line(rec: dict[str, Any]) -> str:
    """The one agent.log line: ``call.energy id=... duration_s=... cpu_pct=... ...``."""
    parts = [f"id={(rec.get('interaction_id') or '-')[:13]}", f"duration_s={rec['duration_s']:g}",
             f"cpu_s={rec['cpu_s']:g}"]
    if rec.get("cpu_pct") is not None:
        parts.append(f"cpu_pct={rec['cpu_pct']:g}")
    layout = rec.get("layout_s") or {}
    if layout:
        parts.append("layout=" + ",".join(f"{k}:{v:g}" for k, v in sorted(layout.items())))
    parts.append(f"thermal={rec['thermal_start']}>{rec['thermal_max']}")
    if rec.get("thermal"):
        parts.append("thermal_at=" + ",".join(f"{s}@{t:g}" for s, t in rec["thermal"]))
    parts.append(f"low_power={'yes' if rec['low_power'] else 'no'}")
    for key in ("low_power_s", "backoff_s", "orb_fps"):
        if rec.get(key) is not None:
            parts.append(f"{key}={rec[key]:g}")
    parts.append(f"device={rec['device'].replace(' ', '_')}")
    parts.append(f"app={rec['app'].replace(' ', '_')}")
    return "call.energy " + " ".join(parts)


def record(body: Any) -> dict[str, Any]:
    rec = parse(body)
    text = line(rec)
    level = logging.WARNING if rec["thermal_max"] in ("serious", "critical") else logging.INFO
    logger.log(level, "speakeasy: %s", text)
    return {"logged": True}
