"""Messages sent to one task on purpose: typed in its reply bar or a decision card's "Something else…".

Targeting here is by task (run id) only and never goes through the router: live speech routing is
unchanged. Two pieces live here, so calls.py only needs small hooks that do nothing in a call
where none of this is used:

* ``deliver``: hand text to a task the way a tapped answer is (a steer for a running task, the
  answer for one waiting on the user, a follow-up job for a finished one).
* Saying where a spoken follow-up went ("Adding that to Pricing page.") and moving it when the
  user says right away that it was the wrong task ("no, the dashboard one").
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any

from . import router
from .prompt import builder as P
from .text import notice_text, short_title

if TYPE_CHECKING:  # pragma: no cover
    from .calls import SidebandWorker

logger = logging.getLogger(__name__)

MAX_TEXT = 4000
RETARGET_WINDOW_S = 30.0  # "no, the dashboard one" this soon after a spoken follow-up moves it
ROUTE_SAID_WINDOW_S = 120.0  # where a follow-up went is said once; more for that task this soon goes quietly
TYPED_PREFIX = "typed_"
START_WAIT_S = 20.0     # how long a message waits to see the task take it (or its follow-up start)


def is_message_id(delegation_id: str | None) -> bool:
    return bool(delegation_id) and delegation_id.startswith(TYPED_PREFIX)  # type: ignore[union-attr]


class AttachmentsNotTaken(Exception):
    """This message can't carry pictures or files (an approval or spoken choice waiting): 409."""


# -- delivery -------------------------------------------------------------------------------------

def _waiting_question(work: dict[str, Any]) -> bool:
    result = work.get("result") or {}
    return bool(result.get("question")) and not work.get("set_aside")


def carry(worker: "SidebandWorker", delegation_id: str, staged: list[Any] | None) -> None:
    """Pictures and files staged on the device (``attachments.StagedPool``) go with this handoff the
    way the Mac's panel attachments do: bound to it, so ``collect_shared`` takes them when its run
    starts (images as image parts, files by saved path, all on the task's card). Never a capture."""
    if not staged:
        return
    worker.interaction.attachments.attach_bound(delegation_id, list(staged))
    worker.share_refs[delegation_id] = False
    worker.no_capture.add(delegation_id)


async def deliver(worker: "SidebandWorker", task_id: str, text: str,
                  choice_task_id: str | None = None, staged: list[Any] | None = None) -> dict[str, Any]:
    """Give ``text`` to one task of this worker's call; runs on the worker's loop. Returns
    ``{delivered_as, task_id}`` (the task that got it: a follow-up job is a new task of the same
    job). Never says anything to the voice; the caller sends one silent note."""
    from .calls import TERMINAL
    if staged and choice_task_id:
        raise AttachmentsNotTaken("a spoken choice takes words only")
    if choice_task_id and choice_task_id in worker.choice_tasks:
        await worker.pick_choice(choice_task_id, text)
        return {"delivered_as": "answer", "task_id": choice_task_id}
    with worker.interaction.lock:
        run = worker.interaction.runs.get(task_id)
        status, key = (run.status, run.idem_key) if run else (None, None)
        approval = dict(run.approval) if run is not None and run.approval else None
    if run is None or key is None:
        raise LookupError(task_id)
    worker.touch()
    if status == "waiting_for_approval" and approval and run.run_id and staged:
        raise AttachmentsNotTaken("answer the approval first")
    if status == "waiting_for_approval" and approval and run.run_id:
        # Free text instead of a button: a plain yes/no answers it; anything else declines the
        # request and the text becomes the instruction.
        said = router.approval_answer(text)
        choice = "once" if said == "yes" else said or ""
        offered = approval.get("choices") or ["once", "deny"]
        if choice in offered and choice != "always":
            if await worker.resolve_approval(task_id, choice) is None:
                raise RuntimeError("approval not answered")
            return {"delivered_as": "answer", "task_id": task_id}
        if await worker.resolve_approval(task_id, "deny") is None:
            raise RuntimeError("approval not answered")
        await asyncio.to_thread(worker.hermes.steer, run.run_id, P.typed_steer_text(worker.names, text))
        worker.store.progress(key, "milestone", f"You answered: {notice_text(text, 200)}")
        worker.publish()
        return {"delivered_as": "answer", "task_id": task_id}
    work = worker.store.work(idem_key=key) or {}
    answers = status == "completed" and _waiting_question(work)
    if answers or (work.get("result") or {}).get("question"):
        # A reply on the task's page to its question answers it (on every device), as a tapped option does.
        answered = getattr(worker, "question_answered", None)
        if callable(answered):
            answered(task_id)
    delegation_id = TYPED_PREFIX + secrets.token_hex(8)
    worker.delegations.add(delegation_id)
    worker.silent_ids.add(delegation_id)
    carry(worker, delegation_id, staged)
    follow = asyncio.get_running_loop().create_task(
        worker.follow_up(delegation_id, 0, f"User: {text}", router.Part("follow_up", text, task_id)))
    worker.dispatch_tasks.add(follow)
    follow.add_done_callback(worker.dispatch_tasks.discard)
    deadline = time.monotonic() + START_WAIT_S
    started = None
    while time.monotonic() < deadline and not follow.done():
        with worker.interaction.lock:
            started = worker.interaction.runs.get(delegation_id)
            begun = started is not None and (started.run_id or started.status in TERMINAL | {"rejected"})
        if begun:
            break
        await asyncio.sleep(0.05)
    with worker.interaction.lock:
        started = worker.interaction.runs.get(delegation_id)
    if follow.done() and not follow.cancelled() and follow.exception() is not None and started is None:
        worker.silent_ids.discard(delegation_id)
        raise follow.exception()  # type: ignore[misc]
    if started is None:
        # It went into the running task itself (steer, a task waiting to start, its chat thread).
        follow.add_done_callback(lambda _t: worker.silent_ids.discard(delegation_id))
        return {"delivered_as": "steer", "task_id": task_id}
    # A follow-up job: from here on it is a task like any other (its result is said as usual).
    worker.silent_ids.discard(delegation_id)
    return {"delivered_as": "answer" if answers else "follow_up", "task_id": delegation_id}


def note_message(worker: "SidebandWorker", task_name: str, text: str) -> None:
    """Tell the live call, silently, that the user typed something to a task. Never read back; a few
    words at most, nothing while the user is muted. On the worker's loop."""
    muted = bool(getattr(worker, "muted", False))
    worker.typed.append({"text": text[:MAX_TEXT], "task": task_name[:120], "at": time.monotonic(),
                         "wall": time.time()})
    del worker.typed[:-50]
    note = P.typed_note(task_name, text, muted)
    schedule(worker, worker.append("session.thinking.append", None, note))


def typed_echo(worker: "SidebandWorker", request: str) -> bool:
    """The voice handed off words the user just typed to a task (it only heard them in a note)."""
    from .calls import _words
    typed = getattr(worker, "typed", None)
    if not typed:
        return False
    words, now = _words(request), time.monotonic()
    return bool(words) and any(now - t["at"] < 120 and _words(t["text"]) == words for t in typed)


def schedule(worker: "SidebandWorker", coro: Any) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    worker.dispatch_tasks.add(task)
    task.add_done_callback(worker.dispatch_tasks.discard)


# -- saying where a spoken follow-up went, and moving it ------------------------------------------

_LEAD = r"(?:(?:um+|uh+|er+|oh|wait|sorry|actually|no|nope|nah)[\s,.;:!-]+)*"
_INTO = (r"(?:(?:i\s+meant|i\s+mean|it'?s\s+for|it\s+was\s+for|that'?s\s+for|meant\s+for|put\s+it\s+(?:in|on|with)"
         r"|add\s+it\s+to|send\s+it\s+to|for|to|in)\s+)?")
_NAMED = r"the\s+(?P<name>[a-z0-9][\w'&+./ -]{0,58}?)\s+(?:one|task|job)"
_WRONG = re.compile(rf"(?i)^\W*{_LEAD}(?:that'?s\s+(?:the\s+)?)?(?:wrong|different|another)\s+(?:task|one|job)"
                    rf"(?:[\s,.;:!-]+{_INTO}{_NAMED})?[\s,.;:!?-]*$")
_NOT_THAT = re.compile(rf"(?i)^\W*{_LEAD}not\s+(?:that|this)\s+(?:one|task|job)"
                       rf"(?:[\s,.;:!-]+{_INTO}{_NAMED})?[\s,.;:!?-]*$")
_NO_THE = re.compile(rf"(?i)^\W*(?:(?:um+|uh+|oh|wait)[\s,.;:!-]+)*(?:no|nope|nah|sorry)[\s,.;:!-]+"
                     rf"(?:{_LEAD}){_INTO}{_NAMED}[\s,.;:!?-]*$")


def retarget_phrase(text: str) -> str | None:
    """The task named in a "wrong task" correction ("no, the dashboard one" -> "dashboard"), "" for
    one that names none ("wrong task", "not that one"), None when it isn't one."""
    said = " ".join((text or "").split())
    if not said or len(said.split()) > 12:
        return None
    for pattern in (_NO_THE, _WRONG, _NOT_THAT):
        found = pattern.match(said)
        if found:
            return (found.groupdict().get("name") or "").strip()
    return None


_NAME_STOP = set("""the a an one task job that this it to for of and my our your on in with please""".split())


def _name_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _NAME_STOP and len(w) > 1}


def task_name(worker: "SidebandWorker", task_id: str) -> str:
    """The short name the user sees for a task: its job's name, else its title, else its words."""
    with worker.interaction.lock:
        run = worker.interaction.runs.get(task_id)
        key = run.idem_key if run else None
    return name_for_key(worker.store, key)


def name_for_key(store: Any, key: str | None) -> str:
    if key is None:
        return "that task"
    work = store.work(idem_key=key) or {}
    return (((work.get("job") or {}).get("name")) or store.title(key)
            or short_title(store.request_text(key) or "") or "that task")


def match_task(worker: "SidebandWorker", name: str, exclude: str | None) -> list[str]:
    """Open tasks of this call best matching ``name`` by their name, title and words. With no name,
    every other open task."""
    want = _name_words(name)
    scored: list[tuple[int, str]] = []
    for task in worker.open_tasks():
        if task.task_id == exclude or task.task_id.startswith("choice_"):
            continue
        have = _name_words(" ".join([task_name(worker, task.task_id), task.request]))
        scored.append((len(want & have), task.task_id))
    if not want:
        return [t for _, t in scored]
    best = max((s for s, _ in scored), default=0)
    return [t for s, t in scored if s == best and best > 0]


def should_say_route(worker: "SidebandWorker", task_id: str, obvious: bool = False) -> bool:
    """Whether the voice says where a spoken follow-up went: once per task within a few minutes, and
    never when it can only have gone one place (one task open, or the one they picked or are looking
    at). Several handoffs in one breath each used to open with "Adding that to …", three times in a reply."""
    now = time.monotonic()
    said = getattr(worker, "route_said", None)
    if said is None:
        said = worker.route_said = {}
    for key in [k for k, at in said.items() if now - at > ROUTE_SAID_WINDOW_S]:
        said.pop(key, None)
    if obvious:
        return False
    others = [t for t in worker.open_tasks() if not t.task_id.startswith("choice_") and t.task_id != task_id]
    if not others:
        return False
    repeat = task_id in said
    said[task_id] = now
    return not repeat


async def announce_routed(worker: "SidebandWorker", delegation_id: str, revision: int, context: str,
                          part: router.Part, obvious: bool = False) -> None:
    """The router attached spoken words to an existing task: the voice says where (once, and only
    when it isn't obvious), and it is remembered so an immediate "no, the X one" moves it instead of
    starting something new."""
    with worker.interaction.lock:
        run = worker.interaction.runs.get(part.task_id or "")
    # Said aloud: the task's own title in full, never a job name cut to a few words.
    name = (worker.spoken_task_name(run.idem_key) if run is not None and hasattr(worker, "spoken_task_name")
            else "") or task_name(worker, part.task_id or "")
    worker.last_routed = {"delegation_id": delegation_id, "task_id": part.task_id, "request": part.request,
                          "context": context, "at": time.monotonic(), "asked": False}
    say = should_say_route(worker, part.task_id or "", obvious)
    await worker.append("session.thinking.append", delegation_id, P.adding_to_note(name, say=say))


def _recent_route(worker: "SidebandWorker") -> dict[str, Any] | None:
    routed = worker.last_routed
    if routed is None:
        return None
    if time.monotonic() - routed["at"] > RETARGET_WINDOW_S:
        worker.last_routed = None
        return None
    return routed


async def retarget(worker: "SidebandWorker", delegation_id: str, revision: int, request: str,
                   marked: Any = None) -> bool:
    """A "wrong task" correction right after a spoken follow-up: move what was added to the task they
    name (or ask "Which one?"). True when handled; False for anything else (routing goes on)."""
    routed = _recent_route(worker)
    if routed is None:
        return False
    name = retarget_phrase(request)
    answering = name is None and routed["asked"]
    if name is None and not answering:
        return False
    if answering and len(request.split()) > 6:
        worker.last_routed = None
        return False
    marked_ok = isinstance(marked, str) and marked != routed["task_id"] and any(
        t.task_id == marked for t in worker.open_tasks())
    found = [marked] if marked_ok else match_task(worker, request if answering else name or "", routed["task_id"])
    if len(found) != 1:
        if answering:
            worker.last_routed = None
            return False  # not a reply naming one task after all: routed as usual
        routed["asked"], routed["at"] = True, time.monotonic()
        worker.handoff_at.pop(delegation_id, None)
        await worker._drop(delegation_id, revision, "Asked which task", P.WHICH_ONE_LINE)
        return True
    target = found[0]
    await _take_back(worker, routed)
    worker.last_routed = None  # moved once; another "no" after this is about the move, as usual
    logger.info("speakeasy: a spoken follow-up was moved to the task the user named")
    worker.handoff_at.pop(delegation_id, None)
    await worker.append("session.thinking.append", delegation_id, P.moved_note(task_name(worker, target)))
    worker._note_started(delegation_id, routed["request"])
    await worker.follow_up(delegation_id, revision, routed["context"],
                           router.Part("follow_up", routed["request"], target))
    return True


async def _take_back(worker: "SidebandWorker", routed: dict[str, Any]) -> None:
    """Undo what the wrong task got: a follow-up job is stopped, words waiting with a task not yet
    started are removed, and a running task is told to ignore them."""
    from .calls import ACTIVE_RUN_STATES
    words = routed["request"]
    with worker.interaction.lock:
        job = worker.interaction.runs.get(routed["delegation_id"])
        old = worker.interaction.runs.get(routed["task_id"] or "")
    if job is not None and job.status != "rejected":
        await worker._supersede(routed["delegation_id"])
        if job.status not in ACTIVE_RUN_STATES or not job.run_id:
            with worker.interaction.lock:
                job.status, job.error = "rejected", "Moved to another task"
            worker.store.update_run(job.idem_key, None, "cancelled")
            worker.store.dismiss_key(job.idem_key)
        worker.publish()
        return
    if old is None:
        return
    with worker.interaction.lock:
        if words in old.held_words:
            old.held_words.remove(words)
            return
        run_id, status, key = old.run_id, old.status, old.idem_key
    if run_id and status in ACTIVE_RUN_STATES:
        try:
            await asyncio.to_thread(worker.hermes.steer, run_id, P.retract_steer_text(worker.names, words))
        except Exception as exc:
            logger.info("speakeasy: could not tell the wrong task to ignore it (%s)", type(exc).__name__)
    worker.store.progress(key, "milestone", f"Moved to another task: {notice_text(words, 160)}")
    worker.publish()


# -- headless worker: messages to tasks whose call is gone ----------------------------------------

# The voiceless worker's interaction id starts with this (its tasks came from text in the app).
TYPED_INTERACTION = "vi_typed"


def make_text_worker(rt: Any) -> "SidebandWorker":
    """A worker with no voice for messages to tasks with no live call: it runs follow-ups and steers
    exactly like a call's worker and tracks their runs, but never speaks."""
    from .calls import Interaction, SidebandWorker

    class TextWorker(SidebandWorker):
        VOICELESS = True

        def start(self) -> None:
            ready = threading.Event()

            def runner() -> None:
                self.loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self.loop)
                ready.set()
                self.loop.run_forever()
            threading.Thread(target=runner, daemon=True, name="speakeasy-typed").start()
            ready.wait(5)

        async def _send(self, kind: str, delegation_id: str | None, content: str) -> None:
            return None

        def call_connected(self) -> bool:
            return False

        def stop(self) -> None:
            if self.loop is not None and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.loop.stop)

    interaction = Interaction(interaction_id=TYPED_INTERACTION + secrets.token_hex(12), live_session_id="typed",
                              status="closed", finalization="confirmed", call_closed=True)
    worker = TextWorker(rt, interaction)
    worker.start()
    return worker


JOB_PREFIX = "job_"


async def start_job(worker: "SidebandWorker", text: str, session_id: str | None = None,
                    context: str | None = None, staged: list[Any] | None = None,
                    choice: dict[str, Any] | None = None) -> dict[str, Any]:
    """A new job from text typed in the app, no call needed (``POST /voice/work``): the same
    ``start_task`` a spoken request reaches, on this worker. Runs on the worker's loop and returns
    ``{task_id}`` once Hermes has the run (or it ended), at most START_WAIT_S later; the job keeps
    running after that like any other. *session_id*: run it in that existing Hermes session (a history
    page's message to an API-server session Speakeasy didn't start), by id, never via routing.
    *context*: the transcript the task sees instead of just the request (a linked continuation).
    *choice*: {model?, reasoning?} picked for this job (models.parse_choice)."""
    from .calls import TERMINAL
    task_id = JOB_PREFIX + secrets.token_hex(8)
    worker.touch()
    if choice and (choice.get("model") or choice.get("reasoning")):
        # The model / reasoning picked for this job (models.py): its runs, follow-ups included, use it.
        worker.store.set_model_choice("task:" + task_id, choice.get("model"), choice.get("reasoning"))
    carry(worker, task_id, staged)
    job = asyncio.get_running_loop().create_task(worker.start_task(task_id, 0, context or f"User: {text}", text,
                                                                   session_id=session_id))
    worker.dispatch_tasks.add(job)
    job.add_done_callback(worker.dispatch_tasks.discard)
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline and not job.done():
        with worker.interaction.lock:
            run = worker.interaction.runs.get(task_id)
            begun = run is not None and (run.run_id or run.status in TERMINAL | {"rejected"})
        if begun:
            break
        await asyncio.sleep(0.05)
    if job.done() and not job.cancelled() and job.exception() is not None:
        raise job.exception()  # type: ignore[misc]
    return {"task_id": task_id}


def adopt_row(worker: "SidebandWorker", row: dict[str, Any]) -> str:
    """Make a task from the store known to a worker (by its task id) so it can be followed up."""
    from .calls import BackendRun
    task_id = row["delegation_id"] or row["idem_key"]
    with worker.interaction.lock:
        run = worker.interaction.runs.get(task_id)
        if run is None or run.idem_key != row["idem_key"]:
            run = BackendRun(task_id, int(row["revision"] or 0), row["idem_key"], run_id=row["run_id"],
                             status=row["status"])
            worker.interaction.runs[task_id] = run
        elif run.status not in {"running", "working", "waiting_for_approval", "resolving_approval"}:
            run.status, run.run_id = row["status"], row["run_id"] or run.run_id
    return task_id
