"""Offline approval transport and fake executor integration (never real actions)."""
import asyncio
import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from speakeasy import confirms as C, confirm_actions as A, confirm_voice as V
from speakeasy.calls import BackendRun, Interaction
from test_confirms import checkout, store


class Immediate:
    def __init__(self, target, args, **kwargs):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)


def service(tmp_path, monkeypatch, *, output="", status="completed", fail_start=False):
    view = C.clean(checkout()); s = store(tmp_path, view)
    s.key_for_run = lambda run: "key" if run == "run_card" else None
    s.session_for = lambda key: "session_example"
    s.drafts_for = lambda key: []
    s.run_result = lambda run: ("key", json.loads(s._db.execute("SELECT result_json FROM runs").fetchone()[0]))
    starts = []
    def start(*args):
        starts.append(args)
        if fail_start:
            raise TimeoutError("uncertain")
        return "run_execution"
    agent = SimpleNamespace(start_run=start, events=lambda run, cb: cb({"event": "run." + status, "output": output}),
                            get_run=lambda run: {"status": status, "output": output})
    svc = SimpleNamespace(store=s, agent=agent, publish_all=lambda key: None, image_roots=lambda: ())
    monkeypatch.setattr(A.threading, "Thread", Immediate)
    return svc, view, starts


def state(svc):
    return svc.store.run_result("run_card")[1]["views"][0]


def receipt(view):
    return '```speakeasy-receipt\n' + json.dumps({"sha256": view["sha256"], "reference": "EXAMPLE-001", "summary": "Order confirmed"}) + '\n```'


def test_nothing_executes_before_approval_then_receipt_replaces_preview(tmp_path, monkeypatch):
    svc, view, starts = service(tmp_path, monkeypatch)
    svc.agent.events = lambda run, cb: cb({"event": "run.completed", "output": receipt(view)})
    assert starts == [] and state(svc)["state"] == "pending"
    A.decide(svc, "run_card", 0, view["sha256"], "approve")
    assert len(starts) == 1 and view["sha256"] in starts[0][0]
    assert state(svc)["state"] == "done"
    assert state(svc)["receipt"]["reference"] == "EXAMPLE-001"
    assert state(svc)["actions"] == []
    A.decide(svc, "run_card", 0, view["sha256"], "approve")
    assert len(starts) == 1


def test_receipt_without_confirmation_never_means_done(tmp_path, monkeypatch):
    svc, view, starts = service(tmp_path, monkeypatch, output="All done!")
    A.decide(svc, "run_card", 0, view["sha256"], "approve")
    assert state(svc)["state"] == "unverified" and "receipt" not in state(svc)
    A.decide(svc, "run_card", 0, view["sha256"], "approve")
    assert len(starts) == 1


def test_changed_total_replaces_in_place_and_requires_new_decision(tmp_path, monkeypatch):
    raw = checkout(); raw["details"].update(subtotal="90.00", total="96.40")
    svc, view, starts = service(tmp_path, monkeypatch, output='```speakeasy-views\n' + json.dumps([raw]) + '\n```')
    A.decide(svc, "run_card", 0, view["sha256"], "approve")
    current = state(svc)
    assert current["state"] == "pending" and current["details"]["total"] == "96.40"
    assert len(starts) == 1 and current["sha256"] != view["sha256"]
    with pytest.raises(C.Conflict):
        A.decide(svc, "run_card", 0, view["sha256"], "approve")
    assert len(starts) == 1


def test_uncertain_dispatch_is_never_retried(tmp_path, monkeypatch):
    svc, view, starts = service(tmp_path, monkeypatch, fail_start=True)
    assert A.decide(svc, "run_card", 0, view["sha256"], "approve")["ok"] is False
    assert state(svc)["state"] == "unverified"
    A.decide(svc, "run_card", 0, view["sha256"], "approve")
    assert len(starts) == 1


def test_cancel_keeps_cart_and_never_dispatches(tmp_path, monkeypatch):
    svc, view, starts = service(tmp_path, monkeypatch)
    assert "cart was left unchanged" in A.decide(svc, "run_card", 0, view["sha256"], "cancel")["message"]
    assert state(svc)["state"] == "cancelled" and starts == []


@pytest.mark.parametrize("closed,paused,status,belongs", [(True, False, "listening", True),
    (False, True, "listening", True), (False, False, "error", True), (False, False, "listening", False)])
def test_voice_cannot_authorize_outside_own_live_call(tmp_path, monkeypatch, closed, paused, status, belongs):
    svc, view, starts = service(tmp_path, monkeypatch)
    call = SimpleNamespace(lock=threading.Lock(), call_closed=closed, paused=paused, abandoned=False,
        successor=None, status=status, runs={"task": SimpleNamespace(run_id="run_card" if belongs else "run_other")})
    with pytest.raises(C.Conflict):
        A.decide(svc, "run_card", 0, view["sha256"], "approve", source="voice", interaction=call)
    assert starts == [] and state(svc)["state"] == "pending"


def test_voice_on_live_call_approves_same_exact_preview(tmp_path, monkeypatch):
    svc, view, starts = service(tmp_path, monkeypatch)
    call = SimpleNamespace(lock=threading.Lock(), call_closed=False, paused=False, abandoned=False,
        successor=None, status="listening", runs={"task": SimpleNamespace(run_id="run_card")})
    A.decide(svc, "run_card", 0, view["sha256"], "approve", source="voice", interaction=call)
    assert len(starts) == 1
    row = svc.store._db.execute("SELECT source FROM confirmation_claims").fetchone()
    assert row[0] == "voice"


def voice_worker(n=1, *, voiceless=False):
    views = [C.clean(checkout()) for _ in range(n)]
    decisions = []
    worker = SimpleNamespace(rt=SimpleNamespace(confirm_card=lambda *a: decisions.append(a) or {"message": "Approved"}),
        VOICELESS=voiceless, interaction=SimpleNamespace(lock=threading.Lock(), runs={"task": SimpleNamespace(run_id="run_card")}),
        store=SimpleNamespace(run_result=lambda run: ("key", {"views": views})), focus=lambda: (None, ""),
        pending_approvals=lambda: [], _drop=AsyncMock(), append=AsyncMock())
    return worker, decisions


def test_spoken_yes_reaches_exact_card_once():
    worker, decisions = voice_worker()
    assert asyncio.run(V.answer(worker, "new", 1, "yes"))
    assert decisions == []  # the first yes asks against an explicitly spoken preview
    assert asyncio.run(V.answer(worker, "confirm", 2, "yes"))
    assert decisions[0][0:4] == ("run_card", 0, C.clean(checkout())["sha256"], "approve")


def test_spoken_no_cancels():
    worker, decisions = voice_worker()
    assert asyncio.run(V.answer(worker, "new", 1, "no"))
    assert decisions == []
    assert asyncio.run(V.answer(worker, "confirm", 2, "no"))
    assert decisions[0][3] == "cancel"


@pytest.mark.parametrize("count,voiceless", [(2, False), (1, True)])
def test_ambiguous_or_typed_worker_never_approves(count, voiceless):
    worker, decisions = voice_worker(count, voiceless=voiceless)
    asyncio.run(V.answer(worker, "new", 1, "yes"))
    assert decisions == []


def test_money_public_and_delete_wait_for_decision(tmp_path, monkeypatch):
    from speakeasy.service import VoiceService
    svc, view, starts = service(tmp_path, monkeypatch)
    for kind, details in [("pay", {"payee": "Example billing", "total": "10.00", "currency": "USD", "payment_last4": "4242", "arrival": "Friday"}),
                          ("post", {"account": "Example account", "body": "An invented public update"}),
                          ("delete_share", {"operation": "delete", "items_description": "Two sample files", "recipient": "Nobody", "count": "2"})]:
        card = C.clean({"type": kind, "title": "Example", "details": details})
        with svc.store._db:
            svc.store._db.execute("UPDATE runs SET result_json=?", (json.dumps({"views": [card]}),))
        before = len(starts)
        with pytest.raises(Exception):
            VoiceService.card_action(svc, "run_card", {"view": 0, "action": "approve", "value": "wrong"})
        assert len(starts) == before
        VoiceService.card_action(svc, "run_card", {"view": 0, "action": "approve", "value": card["sha256"]})
        assert len(starts) == before + 1
