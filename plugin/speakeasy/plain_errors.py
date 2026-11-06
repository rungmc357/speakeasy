"""Plain sentences for errors the apps show as they are (call problems, task cards, route errors).

The apps display these strings unchanged, so none of them names a program error (``TimeoutError``,
``KeyError``…): that detail goes to the log at the place the error is caught. Reason codes the apps
branch on (``reason`` on a ServiceError) are separate and stay as they are.
"""
from __future__ import annotations

# Starting a call: the voice provider didn't hand back a session.
VOICE_START_FAILED = "The voice service didn't answer. Try again in a moment."
# The voice provider couldn't be reached at all (network, DNS, timeout).
VOICE_UNREACHABLE = "Couldn't reach the voice service. Try again in a moment."
# A live call's voice connection dropped. The apps key on the "Voice connection lost" start.
VOICE_LOST = "Voice connection lost."
# A typed message during a call didn't reach the voice.
TYPED_SEND_FAILED = "Couldn't get that message to the call. Try sending it again."
# A handoff or run that never got going (no better reason is known).
TASK_START_FAILED = "Couldn't start this task. Try again."
# Waiting for a chat thread's answer stopped; the answer still lands in the thread.
THREAD_WAIT_LOST = "Stopped following the answer here. It will still arrive in the thread."
# A run's live updates ended before it said how it finished.
STREAM_ENDED_EARLY = "Lost touch with this task before it finished, so it may or may not be done."
# A request that hit an unexpected problem inside the plugin (HTTP 500).
INTERNAL_ERROR = "Your Hermes hit a problem with that. Try again."
# Hermes' API server couldn't be reached. failures.start_failure_kind keys on the "Hermes unavailable"
# start, so keep it.
HERMES_UNAVAILABLE = "Hermes unavailable right now. Try again in a moment."
# A run's live updates from Hermes broke off.
HERMES_STREAM_FAILED = "Lost touch with your Hermes while it was working. Try again."
# Changing Hermes' own settings was refused as unsafe (couldn't read them, or it would drop some).
CONFIG_LEFT_UNCHANGED = "Your Hermes settings couldn't be changed safely, so they were left as they were."
# Writing or tuning the voice brief failed for an unexpected reason.
BRIEF_FAILED = "Couldn't write the brief this time. Press Rewrite to try again."
TUNE_FAILED = "The tune-up didn't finish this time. Try again."
