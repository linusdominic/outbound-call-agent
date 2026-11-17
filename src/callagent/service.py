"""The call engine as a service that any front end can drive.

A front end is whatever carries the words: the demo page, a voice platform that treats
this server as its language model, or a platform that keeps its own model and calls this
server's tools. They all go through the same four operations: check whether a lead may be
called, start a call, pass on what the contact said, and finish.

Nothing in here knows about HTTP or about any vendor.
"""

import asyncio
import csv
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from callagent import guard
from callagent.agent import Call, Mode, Reply, system_prompt
from callagent.book import Book, Lead, business_days, slot_label
from callagent.gate import Decision, may_call
from callagent.llm import LLM
from callagent.playbook import Playbook
from callagent.tools import ToolResult

SESSION_TTL_S = 2 * 60 * 60
SLOT_TIMES = ("10:00", "13:00", "15:30")


class Refused(Exception):
    """The pre-call gate said no. ``reason`` is why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnknownCall(KeyError):
    """No call with that id is in progress."""


class UnknownLead(KeyError):
    """The request named a lead that is not in the lead list."""


@dataclass
class Session:
    call: Call
    started: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    recorded: bool = False  # the attempt has been written to the records


def load_leads(path: Path) -> dict[str, Lead]:
    """Leads from a CSV file with the columns of ``data/leads.csv``, by id."""
    leads = {}
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            leads[row["id"]] = Lead(
                id=row["id"],
                first=row["first"],
                last=row["last"],
                phone=row["phone"],
                timezone=row["timezone"],
                consent=row["consent"] == "yes",
                inquiry=row["inquiry"],
                inquiry_date=row["inquiry_date"],
            )
    return leads


class CallService:
    def __init__(
        self,
        playbook: Playbook,
        llm: Callable[[], LLM],
        book: Book | None = None,
        leads: dict[str, Lead] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        model: str = "claude-haiku-5-5",
    ) -> None:
        self.playbook = playbook
        self.book = book or Book()
        self.leads = leads or {}
        self.clock = clock
        self.model = model
        self._llm = llm  # a factory, so nothing needs an API key until a model is used
        self._client: LLM | None = None
        self.sessions: dict[str, Session] = {}

    @property
    def llm(self) -> LLM:
        if self._client is None:
            self._client = self._llm()
        return self._client

    # Leads

    def lead(self, data: dict[str, Any]) -> Lead:
        """The lead a request is about: looked up by ``lead_id``, or spelled out in full.

        A lead spelled out without ``consent`` has none, so the gate will refuse it.
        """
        lead_id = str(data.get("lead_id") or data.get("id") or "")
        if lead_id in self.leads:
            return self.leads[lead_id]
        if not data.get("phone") or not data.get("first"):
            if lead_id:
                raise UnknownLead(lead_id)
            raise UnknownLead("a lead needs a lead_id from the lead list, or first and phone")
        return Lead(
            id=lead_id or str(data["phone"]),
            first=str(data["first"]),
            last=str(data.get("last", "")),
            phone=str(data["phone"]),
            timezone=str(data.get("timezone", "America/New_York")),
            consent=data.get("consent") in (True, "yes", "true"),
            inquiry=str(data.get("inquiry", "Asked about a home loan.")),
            inquiry_date=str(data.get("inquiry_date", "recently")),
        )

    # The four operations

    def gate(self, lead: Lead, now: datetime | None = None) -> Decision:
        return may_call(lead, now or self.clock(), self.book, self.playbook)

    async def start(
        self,
        lead: Lead,
        *,
        mode: Mode = "guarded",
        call_id: str | None = None,
        now: datetime | None = None,
        speak_opening: bool = True,
    ) -> tuple[str, Reply]:
        """Run the gate and open a call. Returns its id and the first thing to say.

        ``speak_opening=False`` is for a platform that says the opening line itself: the
        call is opened in the same way, and the line is returned so it can be compared.
        """
        now = now or self.clock()
        decision = self.gate(lead, now)
        if not decision.allowed:
            raise Refused(decision.reason)
        self._forget_old_sessions()
        self._open_calendar(now.astimezone(lead.tz))
        call = Call(self.llm, self.playbook, lead, self.book, now, mode=mode, model=self.model)
        call_id = call_id or uuid.uuid4().hex[:12]
        self.sessions[call_id] = Session(call)
        if mode == "guarded" or speak_opening:
            reply = await call.open()
        else:
            reply = Reply("", ended=False)
        return call_id, reply

    async def hear(self, call_id: str, said: str) -> Reply:
        """What the contact said, in. What the agent says back, out."""
        session = self.session(call_id)
        async with session.lock:  # a platform may retry while a turn is still running
            if session.call.ended:
                return Reply("", ended=True)
            reply = await session.call.hear(said)
            if reply.ended:
                self.finish(call_id)
            return reply

    def finish(self, call_id: str, outcome: str | None = None) -> str:
        """Write the attempt to the records, once. Returns the call's outcome."""
        session = self.session(call_id)
        state = session.call.state
        final = state.outcome if state.outcome != "no_outcome" or not outcome else outcome
        if not session.recorded:
            self.book.record_attempt(session.call.lead.id, state.now, final)
            session.recorded = True
        return final

    # For a platform that keeps its own model and calls ours as tools

    def tool(self, call_id: str, name: str, args: dict[str, Any]) -> ToolResult:
        session = self.session(call_id)
        result = session.call.toolbox.call(name, args)
        if session.call.state.finished:
            self.finish(call_id)
        return result

    def heard(self, call_id: str, said: str) -> dict[str, Any]:
        """Run the guard on the contact's words. Acts on a request to stop calling."""
        session = self.session(call_id)
        stop, human = guard.asks_to_stop(said), guard.asks_if_human(said)
        if stop:
            session.call.toolbox.do_not_call("the guard")
            self.finish(call_id)
        return {"asked_to_stop": stop, "asked_if_human": human}

    def check_speech(self, text: str) -> dict[str, Any]:
        """Run the guard on words the agent is about to say."""
        spoken, problems = guard.withhold(text, self.playbook.fallback)
        return {"text": spoken, "withheld": problems, "changed": spoken != text.strip()}

    # Read-only views

    def session(self, call_id: str) -> Session:
        try:
            return self.sessions[call_id]
        except KeyError:
            raise UnknownCall(call_id) from None

    def snapshot(self, call_id: str) -> dict[str, Any]:
        call = self.session(call_id).call
        state = call.state
        return {
            "call_id": call_id,
            "lead_id": call.lead.id,
            "ended": call.ended,
            "outcome": state.outcome if call.ended else None,
            "answers": state.answers,
            "appointment": state.appointment,
            "appointment_label": slot_label(state.appointment) if state.appointment else None,
            "callback": list(state.callback) if state.callback else None,
            "do_not_call": state.do_not_call,
            "transferred": state.transferred,
            "events": state.events,
            "transcript": call.transcript,
        }

    def handover(self, call_id: str) -> str:
        """One or two sentences for the person a call is being transferred to."""
        call = self.session(call_id).call
        answers = call.state.answers
        known = [
            f"goal {answers['goal'].replace('_', ' ')}" if "goal" in answers else "",
            f"property about ${answers['property_value']:,}" if "property_value" in answers else "",
            f"credit {answers['credit']}" if "credit" in answers else "",
        ]
        facts = ", ".join(fact for fact in known if fact) or "no details taken yet"
        why = (call.state.transferred or "the contact asked for a person").rstrip(".")
        return f"{call.lead.first} {call.lead.last}".strip() + f": {facts}. Reason: {why}."

    def prompt(self, mode: Mode = "guarded") -> str:
        return system_prompt(self.playbook, mode)

    # Housekeeping

    def _open_calendar(self, now: datetime) -> None:
        """Make sure there are times to offer. Replace this with a real calendar."""
        if not self.book.free_slots():
            days = business_days(now, 3)
            self.book.open_slots([f"{day:%Y-%m-%d}T{at}" for day in days for at in SLOT_TIMES])

    def _forget_old_sessions(self) -> None:
        cutoff = time.monotonic() - SESSION_TTL_S
        for call_id in [key for key, s in self.sessions.items() if s.started < cutoff]:
            del self.sessions[call_id]
