"""A task run's status: the one list of states, the groups the rest of the plugin asks about, and
which changes are allowed.

Every status write goes through ``Store.transition`` (a compare-and-set on the ``runs`` row, the
in-memory copy updated under the same lock), which checks the change against ``ALLOWED_FROM``. A
late "working" event can no longer reopen a finished task, and a Stop is no longer undone by the
next tool call.

Every state that is not final has a named way out (kept next to it below): the run's own events,
the user, or the sweep (``recovery.sweep_orphans``) that settles rows nothing in this process is
following any more — after a restart, or a wait that died.

The groups are plain ``frozenset`` of strings: statuses are stored and compared as strings
everywhere (database rows, JSON, the apps), and the enum only names them.
"""
from __future__ import annotations

import enum


class RunState(str, enum.Enum):
    ADMITTING = "admitting"                    # reserved, being handed to Hermes. Out: start ok/error, sweep
    WAITING_ON_TASK = "waiting_on_task"        # held behind a task on the same project. Out: blocker ends, "go anyway", 4 h, sweep
    RUNNING = "running"                        # Hermes took it. Out: its events, Stop, stream loss, sweep
    WORKING = "working"                        # a tool/interim event arrived. Out: as running
    WAITING_FOR_APPROVAL = "waiting_for_approval"  # Hermes asks the user. Out: an answer, set aside, sweep
    RESOLVING_APPROVAL = "resolving_approval"  # an answer is on its way (memory only). Out: Hermes' reply
    CANCEL_REQUESTED = "cancel_requested"      # Stop sent. Out: run.cancelled (or any end), sweep
    AMBIGUOUS = "ambiguous"                    # lost track while it ran. Out: recovery from its session, a late event
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    REJECTED = "rejected"                      # never started (refused at the door)

    def __str__(self) -> str:
        return self.value


def _s(*states: RunState) -> frozenset[str]:
    return frozenset(s.value for s in states)


ALL = _s(*RunState)
# The run ended; nothing more will happen to it.
TERMINAL = _s(RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.INTERRUPTED)
# Ended or never started: the row is final.
FINAL = TERMINAL | _s(RunState.REJECTED)
# Settled for the board's "finished at" stamp (a question waiting on the user counts).
SETTLED = TERMINAL | _s(RunState.WAITING_FOR_APPROVAL)
# Being followed by this process right now (was calls.ACTIVE_RUN_STATES).
LIVE = _s(RunState.ADMITTING, RunState.RUNNING, RunState.WORKING, RunState.WAITING_FOR_APPROVAL,
          RunState.RESOLVING_APPROVAL, RunState.CANCEL_REQUESTED, RunState.WAITING_ON_TASK)
# Not final: what the orphan sweep looks at.
OPEN = ALL - FINAL
# A new message queues behind a task in these (a stopping task takes none).
QUEUE_BEHIND = LIVE - _s(RunState.CANCEL_REQUESTED)
# Hermes is doing the work right now.
WORKING = _s(RunState.RUNNING, RunState.WORKING)
# In flight as the voice describes it (status answers, "what's running").
IN_FLIGHT = _s(RunState.ADMITTING, RunState.RUNNING, RunState.WORKING, RunState.WAITING_FOR_APPROVAL)
# Started and not yet being decided about (router joins, "what's it doing").
UNDER_WAY = _s(RunState.ADMITTING, RunState.RUNNING, RunState.WORKING)
# Running in Hermes as the call board shows it (the pulse, the latest-task focus).
STARTED_SHOWN = _s(RunState.RUNNING, RunState.WORKING, RunState.WAITING_FOR_APPROVAL)
# Never reached Hermes yet.
NOT_STARTED = _s(RunState.ADMITTING, RunState.WAITING_ON_TASK)
# Waiting on the user's answer.
ASKING = _s(RunState.WAITING_FOR_APPROVAL, RunState.RESOLVING_APPROVAL)
# Guidance can still reach the run.
STEERABLE = WORKING | ASKING
# The x and "Clear done" may remove it (an unconfirmed run can be cleared too).
DISMISSABLE = FINAL | _s(RunState.AMBIGUOUS)

_STARTED = _s(RunState.RUNNING, RunState.WORKING, RunState.WAITING_FOR_APPROVAL, RunState.RESOLVING_APPROVAL)

# Where each state may be entered from. A same-state write is allowed only where listed (a run id
# arriving with "running", repeated tool events, a repeated final event).
ALLOWED_FROM: dict[str, frozenset[str]] = {
    RunState.ADMITTING.value: _s(RunState.WAITING_ON_TASK, RunState.ADMITTING),
    RunState.WAITING_ON_TASK.value: _s(RunState.ADMITTING),
    RunState.RUNNING.value: _s(RunState.ADMITTING, RunState.WAITING_ON_TASK, RunState.RUNNING),
    RunState.WORKING.value: _STARTED,
    RunState.WAITING_FOR_APPROVAL.value: _STARTED,
    RunState.RESOLVING_APPROVAL.value: _s(RunState.WAITING_FOR_APPROVAL),
    RunState.CANCEL_REQUESTED.value: LIVE,
    RunState.AMBIGUOUS.value: LIVE | _s(RunState.AMBIGUOUS),
    # A final event from Hermes also corrects an "interrupted" the sweep inferred; a repeated final
    # event is refused (the answer is stored and spoken once).
    **{s: OPEN | (_s(RunState.INTERRUPTED) - {s}) for s in TERMINAL},
    RunState.REJECTED.value: _s(RunState.ADMITTING, RunState.WAITING_ON_TASK, RunState.REJECTED),
}


def allowed_from(to: str) -> frozenset[str]:
    """The states *to* may be entered from (KeyError for a status that doesn't exist)."""
    return ALLOWED_FROM[RunState(to).value]


def is_terminal(status: str | None) -> bool:
    return status in TERMINAL


def is_live(status: str | None) -> bool:
    return status in LIVE
