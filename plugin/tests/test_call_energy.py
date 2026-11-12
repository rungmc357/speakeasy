"""The app's one energy record per call (`POST /voice/telemetry/call-energy`): one call.energy line."""
from __future__ import annotations

import logging

from fakes import http

from speakeasy import call_energy as CE

GOOD = {
    "duration_s": 312.4, "cpu_s": 41.2, "cpu_pct": 13.2,
    "layout_s": {"home": 250.0, "transcript": 40.0, "background": 22.4},
    "thermal_start": "nominal", "thermal_max": "serious",
    "thermal": [{"state": "fair", "at_s": 95.0}, {"state": "serious", "at_s": 260.5}],
    "low_power": False, "low_power_s": 0, "backoff_s": 51.9, "orb_fps": 29.6,
    "app": "77.0 (3)", "device": "iPhone18,1", "interaction_id": "vi_" + "a" * 32,
}


def post(server, body, token=True):
    return http(server.base_url, "POST", "/voice/telemetry/call-energy", body, server.token if token else None)


def test_a_record_logs_one_call_energy_line(server, caplog):
    with caplog.at_level(logging.INFO, logger="speakeasy.service"):
        status, body = post(server, GOOD)
    assert status == 200 and body["logged"] is True
    lines = [r.getMessage() for r in caplog.records if "call.energy" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    for part in ("duration_s=312.4", "cpu_s=41.2", "cpu_pct=13.2", "layout=background:22.4,home:250,transcript:40",
                 "thermal=nominal>serious", "thermal_at=fair@95,serious@260.5", "low_power=no", "orb_fps=29.6",
                 "device=iPhone18,1", "app=77.0_(3)"):
        assert part in line, (part, line)
    # Hot calls stand out in the log.
    assert any(r.levelno == logging.WARNING for r in caplog.records if "call.energy" in r.getMessage())


def test_a_cool_call_logs_at_info():
    rec = CE.parse({**GOOD, "thermal_max": "fair", "thermal": [{"state": "fair", "at_s": 3}]})
    assert CE.line(rec).startswith("call.energy id=vi_aaaaaaaaaa duration_s=312.4")


def test_only_numbers_and_short_names_are_taken(server):
    assert post(server, {**GOOD, "transcript": "hello"})[0] == 400
    assert post(server, {**GOOD, "duration_s": "long"})[0] == 400
    assert post(server, {**GOOD, "thermal_max": "boiling"})[0] == 400
    assert post(server, {**GOOD, "layout_s": {"kitchen": 3}})[0] == 400
    assert post(server, {**GOOD, "thermal": [{"state": "fair", "at_s": 1}] * 25})[0] == 400
    assert post(server, {**GOOD, "app": "x" * 41})[0] == 400
    assert post(server, {"cpu_s": 1})[0] == 400


def test_the_route_needs_the_paired_token(server):
    assert post(server, GOOD, token=False)[0] == 401


def test_the_call_may_already_be_gone():
    # No interaction lookup: the record lands after the call ended (and old ids are fine).
    assert CE.record({**GOOD, "interaction_id": "vi_" + "0" * 32}) == {"logged": True}
    assert CE.record({"duration_s": 1, "cpu_s": 0.1}) == {"logged": True}


def test_the_quality_loop_can_parse_the_line():
    import re
    line = CE.line(CE.parse(GOOD))
    m = re.search(r"call\.energy id=\S+ duration_s=([\d.]+) cpu_s=([\d.]+)(?: cpu_pct=([\d.]+))?", line)
    assert m and m.group(1) == "312.4" and m.group(3) == "13.2"
    hot = re.search(r"thermal=\w+>(\w+)", line)
    assert hot and hot.group(1) == "serious"
