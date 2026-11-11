"""Card-route dispatch. No action starts before the durable exact-preview claim."""
from __future__ import annotations
import json
import re
import threading
from typing import Any

from . import confirms as C
from .text import split_result


def _update(service: Any, run_id: str, index: int, expected: str, changes: dict) -> None:
    store = service.store
    with store._lock, store._db:
        row = store._db.execute("SELECT idem_key,result_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return
        key, encoded = row
        result = json.loads(encoded or "{}")
        views = result.get("views", [])
        if not 0 <= index < len(views):
            return
        view = views[index]
        if view.get("sha256") != expected or view.get("state") != "approved":
            return  # a newer preview always wins
        if changes.get("kind") == "confirm":
            if changes.get("sha256") == expected:
                changes = {"state": "unverified", "error": "The service asked again without changing the preview. Check before trying again."}
                view.update(changes)
                claim_state = "unverified"
            else:
                spent = store._db.execute(
                    "SELECT state FROM confirmation_claims WHERE run_id=? AND card_index=? AND sha256=?",
                    (run_id, index, changes["sha256"])).fetchone()
                if spent:
                    changes = {**changes, "state": "unverified", "actions": [],
                               "error": "This preview was already used and replaced. Check the service before starting a new action."}
                views[index] = changes  # replace completely; never carry old fields into a new digest
                claim_state = "replaced"
        else:
            view.update(changes)
            claim_state = view["state"]
        store._db.execute("UPDATE runs SET result_json=? WHERE idem_key=?", (C.canonical(result), key))
        store._db.execute("UPDATE confirmation_claims SET state=? WHERE run_id=? AND card_index=? AND sha256=?",
                          (claim_state, run_id, index, expected))
    service.publish_all(key)


def decide(service: Any, run_id: str, index: int, expected: str, action: str, *, source: str = "tap", interaction: Any = None) -> dict:
    if action == "edit":
        if source != "tap":
            raise C.Conflict("Edit the draft in the app.")
        key = C.edit(service.store, run_id, index, expected)
        service.publish_all(key)
        return {"ok": True, "message": "Draft updated. Review it before sending."}
    session = None
    if action == "approve":
        key = service.store.key_for_run(run_id)
        session = service.store.session_for(key) if key else None
        if not session:
            raise C.Conflict("The conversation is unavailable. Nothing was approved.")
    if source == "voice":
        if interaction is None:
            raise C.Conflict("Outside a live call, tap the card to approve.")
        with interaction.lock:
            live = (not interaction.call_closed and not interaction.paused and not interaction.abandoned
                    and interaction.successor is None and interaction.status not in {"connecting", "closed", "error"})
            belongs = any(r.run_id == run_id for r in interaction.runs.values())
            if not live or not belongs:
                raise C.Conflict("That confirmation is not on this live call.")
            key, view, dispatch = C.claim(service.store, run_id, index, expected, action, source="voice", live_call=True)
    else:
        key, view, dispatch = C.claim(service.store, run_id, index, expected, action, source=source)
    if view["state"] == "cancelled":
        # SPK-357: whoever cancelled was looking at the card and nothing more will arrive, so Home doesn't
        # bring the job back as a new result.
        service.store.mark_seen(key)
    service.publish_all(key)
    if not dispatch:
        return {"ok": True, "message": "Cancelled. The cart was left unchanged." if view["state"] == "cancelled" and view["type"] == "checkout"
                else "Cancelled." if view["state"] == "cancelled" else "That decision is already recorded."}
    # The hash is both the exact-content authorization and the agent-run idempotency key.
    try:
        execution = service.agent.start_run(C.execution_message(view) + (
            '\nOn confirmed success, include a ```speakeasy-receipt block with a JSON object '
            '{"sha256":"' + expected + '","reference":"service confirmation id","summary":"what actually happened",'
            '"delivery":"if applicable","track_url":"https URL if available"}. '
            'Do not invent a confirmation. If terms changed, return a fresh speakeasy-views confirm card instead.'),
            "speakeasy_confirm_" + run_id + "_" + str(index) + "_" + expected, session)
    except Exception:
        # The server may have accepted the action: never reset to pending or automatically resend.
        _update(service, run_id, index, expected, {"state": "unverified", "error": "Could not confirm whether the action started. Check before trying again."})
        return {"ok": False, "message": "Could not confirm whether the action started. Check before trying again."}
    threading.Thread(target=follow, args=(service, run_id, index, expected, execution), daemon=True,
                     name="speakeasy-confirm").start()
    return {"ok": True, "message": "Approved. Waiting for confirmation."}


def follow(service: Any, run_id: str, index: int, expected: str, execution: str) -> None:
    final: dict = {}
    def event(e: dict) -> None:
        if e.get("event") in {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}:
            final.update(status=e["event"][4:], output=e.get("output", ""))
    try:
        service.agent.events(execution, event)
        if not final.get("output"):
            final.update(service.agent.get_run(execution))
        output = final.get("output") or ""
        if final.get("status") != "completed":
            raise ValueError("not completed")
        result = split_result(output, service.image_roots())
        replacement = next((v for v in result.get("views", []) if v.get("kind") == "confirm"), None)
        if replacement:
            # In-place re-ask, with a new digest. The old claim stays spent forever.
            _update(service, run_id, index, expected, replacement)
            return
        match = re.search(r"```speakeasy-receipt\s*\n(.*?)\n```", output, re.S)
        receipt = json.loads(match[1]) if match else None
        if not isinstance(receipt, dict) or receipt.get("sha256") != expected:
            raise ValueError("no bound receipt")
        clean = {}
        for key in ("reference", "summary", "delivery", "track_url"):
            if key in receipt:
                value = C._text(receipt[key], 2000)
                if not value or (key == "track_url" and not re.fullmatch(r"https://[^\s]+", value)):
                    raise ValueError("invalid receipt")
                clean[key] = value
        if not {"reference", "summary"} <= clean.keys():
            raise ValueError("missing confirmation")
        _update(service, run_id, index, expected, {"state": "done", "receipt": clean})
    except Exception:
        _update(service, run_id, index, expected, {"state": "unverified", "error": "No confirmed receipt yet. Check the service before trying again."})
