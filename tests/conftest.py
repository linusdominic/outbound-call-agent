"""Shared helpers. Nothing here needs the network, a microphone or an API key."""

import copy
import itertools
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from callagent.book import Book, Lead
from callagent.llm import Turn
from callagent.playbook import Playbook

_ids = itertools.count(1)
NOW = datetime(2026, 11, 18, 10, 30, tzinfo=ZoneInfo("America/New_York"))
SLOTS = ["2026-11-19T10:00", "2026-11-19T13:00", "2026-11-20T10:00"]


def said(text: str = "", *tools: tuple[str, dict]) -> Turn:
    """A model response: some words and, optionally, tool calls."""
    content = [{"type": "thinking", "thinking": "...", "signature": "s"}]
    if text:
        content.append({"type": "text", "text": text})
    for name, args in tools:
        content.append({"type": "tool_use", "id": f"t{next(_ids)}", "name": name, "input": args})
    return Turn(content, "tool_use" if tools else "end_turn", 100, 20, 0.001, 0.5)


class FakeLLM:
    """Plays back scripted model responses and keeps every request it was sent."""

    def __init__(self, *turns: Turn) -> None:
        self.turns = list(turns)
        self.requests: list[dict] = []
        self.ledger = SimpleNamespace(total=0.0)

    async def respond(self, **request) -> Turn:
        self.requests.append(copy.deepcopy(request))
        return self.turns.pop(0)


@pytest.fixture(scope="session")
def playbook() -> Playbook:
    return Playbook.load()


@pytest.fixture
def lead() -> Lead:
    return Lead(
        id="L1",
        first="Jordan",
        last="Rivera",
        phone="555-0142",
        timezone="America/New_York",
        consent=True,
        inquiry="Asked about refinancing through the website form.",
        inquiry_date="12 November 2026",
    )


@pytest.fixture
def book() -> Book:
    book = Book()
    book.open_slots(SLOTS)
    return book
