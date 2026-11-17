"""The actions the agent can take during a call, and the rules each one enforces.

The model asks for an action. Code decides whether it happens. A time that is not on the
calendar cannot be booked, an appointment cannot be offered before the contact has been
qualified, and the outcome of the call is worked out from what was actually done.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from callagent.book import PERIODS, Book, Lead, business_days, slot_label
from callagent.playbook import Playbook

END_REASONS = ("declined", "wrong_number", "voicemail", "completed")


@dataclass
class CallState:
    lead: Lead
    now: datetime  # in the contact's time zone
    answers: dict[str, Any] = field(default_factory=dict)
    appointment: str | None = None
    callback: tuple[str, str] | None = None  # (date, period)
    transferred: str | None = None  # the reason given
    do_not_call: bool = False
    ended: str | None = None  # the reason passed to end_call
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        """What the call achieved, from what was done. The model does not report it."""
        if self.do_not_call:
            return "do_not_call"
        if self.ended in ("wrong_number", "voicemail"):
            return self.ended
        if self.transferred:
            return "transferred"
        if self.appointment:
            return "appointment_booked"
        if self.callback:
            return "callback_scheduled"
        if self.ended == "declined":
            return "declined"
        return "no_outcome"

    @property
    def finished(self) -> bool:
        return self.do_not_call or self.transferred is not None or self.ended is not None


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    wait: bool = False  # the agent must see this result before it speaks


def parse_dollars(value: str) -> int | None:
    """'$400,000', '400k', '1.2 million' and 'four hundred thousand'-free forms, as dollars."""
    text = value.lower().replace(",", "").replace("$", "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)\s*(k|thousand|m|million)?", text)
    if not match:
        return None
    scale = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}.get(match.group(2), 1)
    return int(float(match.group(1)) * scale)


def schemas(playbook: Playbook, callback_days: list[str]) -> list[dict[str, Any]]:
    fields = [question.id for question in playbook.questions]
    return [
        {
            "name": "record_answer",
            "description": (
                "Save something the contact told you about their situation. Write your "
                "spoken reply first, then call this in the same response."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": fields},
                    "value": {"type": "string"},
                },
                "required": ["field", "value"],
            },
        },
        {
            "name": "book_appointment",
            "description": (
                "Book a call with a loan officer at one of the open times from the call "
                "context. Call it only after the contact has agreed to that time. Wait for "
                "the result before telling the contact it is booked."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"slot": {"type": "string", "description": "e.g. 2026-11-19T10:00"}},
                "required": ["slot"],
            },
        },
        {
            "name": "schedule_callback",
            "description": (
                "The contact cannot talk now and asked to be called another time. Wait for "
                "the result before confirming it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "day": {"type": "string", "enum": callback_days},
                    "period": {"type": "string", "enum": list(PERIODS)},
                },
                "required": ["day", "period"],
            },
        },
        {
            "name": "transfer_to_officer",
            "description": (
                "Hand the call to a person. Use it when the contact asks for a person or wants "
                "something only a loan officer can give. Your spoken reply in the same turn "
                "is the last thing you say."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
        {
            "name": "mark_do_not_call",
            "description": (
                "The contact asked, in any words, not to be called again. Call it at once, "
                "confirm in one sentence, and say goodbye. The call ends after that reply."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "end_call",
            "description": (
                "End the call. Your spoken reply in the same turn is the last thing you say. "
                "declined: the contact is not interested. wrong_number: this is not the "
                "person you were calling. voicemail: you reached a voicemail greeting. "
                "completed: the conversation reached its natural end."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"reason": {"type": "string", "enum": list(END_REASONS)}},
                "required": ["reason"],
            },
        },
    ]


class Toolbox:
    def __init__(self, state: CallState, book: Book, playbook: Playbook) -> None:
        self.state, self.book, self.playbook = state, book, playbook
        self.callback_days = [f"{day:%Y-%m-%d}" for day in business_days(state.now, 5)]
        self.schemas = schemas(playbook, self.callback_days)

    def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            return ToolResult(f"There is no tool named {name}.", is_error=True)
        try:
            result = handler(**args)
        except TypeError:
            return ToolResult(f"Wrong arguments for {name}: {args}", is_error=True)
        self.state.events.append(
            {"type": "tool", "name": name, "input": args, "ok": not result.is_error}
        )
        return result

    def _record_answer(self, field: str, value: str) -> ToolResult:
        question = self.playbook.question(field)
        if question is None:
            return ToolResult(f"There is no question named {field}.", is_error=True)
        if question.is_number:
            dollars = parse_dollars(value)
            if dollars is None:
                return ToolResult("Give property_value as a dollar amount.", is_error=True)
            self.state.answers[field] = dollars
        else:
            choice = value.strip().lower().replace(" ", "_").replace("-", "_")
            if choice not in question.values:
                allowed = ", ".join(question.values)
                return ToolResult(f"{field} must be one of: {allowed}.", is_error=True)
            self.state.answers[field] = choice
        return ToolResult("Saved.")

    def _book_appointment(self, slot: str) -> ToolResult:
        if self.state.appointment:
            booked = slot_label(self.state.appointment)
            return ToolResult(f"Already booked for {booked}.", is_error=True, wait=True)
        needed = self.playbook.questions_needed_to_book
        if len(self.state.answers) < needed:
            return ToolResult(
                f"Not booked. Ask at least {needed} of the qualifying questions first, and "
                "save the answers with record_answer.",
                is_error=True,
                wait=True,
            )
        if not self.book.take_slot(slot, self.state.lead.id):
            free = "; ".join(f"{s} ({slot_label(s)})" for s in self.book.free_slots())
            return ToolResult(
                f"Not booked. {slot} is not an open time. Open times: {free or 'none'}.",
                is_error=True,
                wait=True,
            )
        self.state.appointment = slot
        return ToolResult(f"Booked: {slot_label(slot)}.", wait=True)

    def _schedule_callback(self, day: str, period: str) -> ToolResult:
        if day not in self.callback_days or period not in PERIODS:
            days = ", ".join(self.callback_days)
            return ToolResult(
                f"Not scheduled. day must be one of {days}; period one of {', '.join(PERIODS)}.",
                is_error=True,
                wait=True,
            )
        self.book.add_callback(self.state.lead.id, day, period, self.state.now)
        self.state.callback = (day, period)
        when = datetime.fromisoformat(day)
        return ToolResult(f"Callback set for {when:%A} {when.day} {when:%B}, {period}.", wait=True)

    def _transfer_to_officer(self, reason: str) -> ToolResult:
        self.state.transferred = reason
        return ToolResult("Transferring.")

    def _mark_do_not_call(self) -> ToolResult:
        self.do_not_call("the agent")
        return ToolResult("Added to the do-not-call list.")

    def _end_call(self, reason: str) -> ToolResult:
        if reason not in END_REASONS:
            return ToolResult(f"reason must be one of: {', '.join(END_REASONS)}.", is_error=True)
        self.state.ended = reason
        return ToolResult("Ended.")

    def do_not_call(self, source: str) -> None:
        """Put the contact on the do-not-call list. Used by the tool and by the guard."""
        if not self.state.do_not_call:
            self.book.add_do_not_call(self.state.lead.phone, self.state.now, source)
            self.state.do_not_call = True
            self.state.events.append({"type": "do_not_call", "source": source})
