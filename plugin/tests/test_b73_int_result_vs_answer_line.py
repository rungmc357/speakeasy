"""b73 integration: email-card73's posted ``result`` entry and SPK-314's ``d:<run>`` answer line both
arrive at the bottom of Home's thread for a job that finished after a newer message. A job whose
result was posted gets the result entry only, not a second bottom post; every other job still gets
its answer line. Invented data only; drives stream.build_stream directly."""
from __future__ import annotations

import datetime as dt

from speakeasy.stream import build_stream

T0 = 1_800_000_000.0
TZ = dt.timezone.utc


def _job(n: int, *, started: float, finished: float) -> dict:
    return {"id": f"r:run{n}", "run_id": f"run{n}", "kind": "new_answer", "title": f"Job {n}",
            "line": f"Answer {n}.", "started_at": started, "finished_at": finished, "updated_at": finished}


def _msg(i: int, at: float) -> dict:
    return {"id": f"m:{i}", "kind": "message", "role": "user", "text": "something newer", "at": at}


def test_a_posted_result_replaces_the_answer_line():
    items = [_job(1, started=T0, finished=T0 + 60)]
    out = build_stream(items, {}, set(), T0 + 61, TZ, messages=[_msg(1, T0 + 10)],
                       results={"run1": {"id": "e:run1", "at": T0 + 60}})
    ids = [e["id"] for e in out["entries"]]
    assert "e:run1" in ids
    assert "d:run1" not in ids
    card = next(e for e in out["entries"] if e["kind"] == "card")
    assert card["result_id"] == "e:run1"


def test_a_job_without_a_posted_result_keeps_its_answer_line():
    items = [_job(1, started=T0, finished=T0 + 60), _job(2, started=T0 + 1, finished=T0 + 70)]
    out = build_stream(items, {}, set(), T0 + 71, TZ, messages=[_msg(1, T0 + 10)],
                       results={"run1": {"id": "e:run1", "at": T0 + 60}})
    ids = [e["id"] for e in out["entries"]]
    assert "d:run2" in ids and "d:run1" not in ids
    assert ids.index("m:1") < ids.index("e:run1") < ids.index("d:run2")
