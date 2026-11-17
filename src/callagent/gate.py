"""Whether a call may be placed at all. Decided in code, before any model is involved.

United States rules for marketing calls, as I read them. This is an engineer's reading,
not legal advice:

- A call that uses an artificial or prerecorded voice needs the person's prior consent,
  and the FCC said in February 2024 that an AI-generated voice counts as artificial.
- Calls may be placed only between 8 a.m. and 9 p.m. in the time zone of the person called.
- Anyone who asks not to be called must be put on the caller's own do-not-call list.

The checks run in a fixed order and the first one that fails is the reason given.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from callagent.book import Book, Lead
from callagent.playbook import Playbook

FINISHED = {"appointment_booked", "transferred", "wrong_number"}  # nothing left to call about


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def may_call(lead: Lead, now: datetime, book: Book, playbook: Playbook) -> Decision:
    """``now`` must carry a time zone. It is converted to the lead's own."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if book.on_do_not_call(lead.phone):
        return Decision(False, "on the do-not-call list")
    if not lead.consent:
        return Decision(False, "no consent to automated calls on record")

    local = now.astimezone(lead.tz)
    if not (playbook.earliest <= local.time() < playbook.latest):
        return Decision(False, f"outside calling hours ({local:%H:%M} {local:%Z} for the contact)")

    attempts = book.attempts(lead.id)
    if any(outcome in FINISHED for _, outcome in attempts):
        return Decision(False, "already handled on an earlier call")
    if book.appointment(lead.id):
        return Decision(False, "already has an appointment")
    if len(attempts) >= playbook.max_attempts:
        return Decision(False, f"attempt limit reached ({playbook.max_attempts})")
    if attempts:
        wait = timedelta(hours=playbook.min_hours_between_attempts)
        if now - attempts[-1][0] < wait:
            return Decision(False, "called too recently")
    return Decision(True, "ok")
