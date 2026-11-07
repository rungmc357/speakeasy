"""Live-call speech adapter for held confirmation cards; never a typed-chat approval path."""
import asyncio
import time
from . import router
from .confirms import Conflict


async def answer(worker, delegation_id, revision, request, marked=None):
    callback = getattr(getattr(worker, "rt", None), "confirm_card", None)
    if callback is None or getattr(worker, "VOICELESS", False):
        return False
    choice = router.approval_answer(request)
    if choice is None:
        return False
    with worker.interaction.lock:
        rows = [(task, run.run_id) for task, run in worker.interaction.runs.items() if run.run_id]
    pending = []
    for task, run_id in rows:
        found = worker.store.run_result(run_id)
        if found:
            for index, view in enumerate(found[1].get("views", [])):
                if view.get("kind") == "confirm" and view.get("state") == "pending":
                    pending.append((task, run_id, index, view))
    if not pending:
        return False
    focused, _ = worker.focus()
    # A single pointed-at card can win; never guess between cards on the same task.
    addressed = marked or focused
    target = [p for p in pending if p[0] == addressed] if addressed else pending
    if addressed and not target:
        if any(t == addressed for t, _ in worker.pending_approvals()):
            return False
        await worker._drop(delegation_id, revision, "No matching confirmation", None)
        await worker.append("session.commentary.append", delegation_id,
                            "There is no pending preview for that conversation. Name the preview you want, or tap it.")
        return True
    tool_approvals = worker.pending_approvals()
    ambiguous = len(target) != 1 or any(t == target[0][0] for t, _ in tool_approvals)
    if tool_approvals and not marked and not focused:
        ambiguous = True
    await worker._drop(delegation_id, revision, "Answered the confirmation", None)
    if ambiguous:
        await worker.append("session.commentary.append", delegation_id,
                            "Which preview do you want to approve? Name it, or tap its button.")
        return True
    task, run_id, index, view = target[0]
    if choice not in {"yes", "once", "deny"}:
        await worker.append("session.commentary.append", delegation_id,
                            "This approves only the exact preview, one time. Say yes or no, or tap its button.")
        return True
    # A spoken yes spends only the digest actually offered on this call, never a replacement
    # that silently arrived from a checkout re-check. The app sends its displayed digest directly.
    offered = getattr(worker, "confirmation_offers", {})
    offered_key = (task, index)
    previous = offered.get(offered_key)
    if choice != "deny" and (not previous or previous[0] != view["sha256"] or time.monotonic() - previous[1] > 120):
        await present(worker, delegation_id, task, index, view, changed=bool(previous))
        return True
    offered.pop(offered_key, None)
    try:
        reply = await asyncio.to_thread(callback, run_id, index, view["sha256"],
                                        "cancel" if choice == "deny" else "approve", worker.interaction)
        said = reply.get("message") or "The decision was recorded."
    except Conflict as exc:
        said = str(exc)
    await worker.append("session.commentary.append", delegation_id, said)
    return True


async def present(worker, delegation_id, task, index, view, *, changed=False):
    details = view["details"]
    kind = view["type"]
    if kind in {"checkout", "pay"}:
        destination = details.get("merchant") or details.get("payee")
        preview = ("Buy from " if kind == "checkout" else "Pay ") + destination + " for " + details["currency"] + " " + details["total"]
    elif kind == "send":
        preview = "Send the full draft shown to " + details["recipient"]
    elif kind == "post":
        preview = "Post the exact preview shown from " + details["account"]
    elif kind == "book":
        preview = "Book " + view["title"] + " for " + details["when"] + ", " + details["price"]
    elif kind == "invite":
        preview = "Send the invitation shown to " + details["attendees"] + " for " + details["when"]
    elif kind == "home":
        preview = {"unlock": "Unlock ", "open_garage": "Open ", "disarm": "Disarm "}[details["operation"]] + details["device"]
    elif kind == "delete_share":
        preview = ("Delete " if details["operation"] == "delete" else "Share ") + details["items_description"]
    else:
        preview = view["title"] + ", exactly as shown"
    prefix = "The preview changed. " if changed else "Before I do that: "
    await worker.append("session.commentary.append", delegation_id, prefix + preview[:1500] + ". Approve this preview?")
    if not hasattr(worker, "confirmation_offers"):
        worker.confirmation_offers = {}
    worker.confirmation_offers[(task, index)] = (view["sha256"], time.monotonic())
