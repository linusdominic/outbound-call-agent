"""What the lender keeps between calls: the do-not-call list, attempts, the calendar.

SQLite, because these are small facts that must not be lost: a number added to the
do-not-call list during one call has to block the next one. Each simulated call gets its
own in-memory database, so the evaluation uses the same code as a real campaign would.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS do_not_call (phone TEXT PRIMARY KEY, added_at TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS attempts (lead_id TEXT, at TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS slots (slot TEXT PRIMARY KEY, lead_id TEXT);
CREATE TABLE IF NOT EXISTS callbacks (lead_id TEXT, day TEXT, period TEXT, created_at TEXT);
"""

PERIODS = {"morning": "09:00", "afternoon": "13:00", "evening": "18:00"}


@dataclass(frozen=True)
class Lead:
    """Someone who sent an inquiry. ``consent`` is their agreement to automated calls."""

    id: str
    first: str
    last: str
    phone: str
    timezone: str
    consent: bool
    inquiry: str  # what they asked about, in their own form's words
    inquiry_date: str

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def slot_label(slot: str) -> str:
    """'2026-11-19T10:00' as 'Thursday 19 November at 10:00 AM'."""
    at = datetime.fromisoformat(slot)
    return f"{at:%A} {at.day} {at:%B} at {at:%I:%M %p}".replace(" at 0", " at ")


def business_days(start: datetime, count: int) -> list[datetime]:
    """The next ``count`` weekdays after ``start``'s date."""
    days, day = [], start
    while len(days) < count:
        day += timedelta(days=1)
        if day.weekday() < 5:
            days.append(day)
    return days


class Book:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)

    # Do-not-call list
    def add_do_not_call(self, phone: str, at: datetime, source: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO do_not_call VALUES (?, ?, ?)", (phone, at.isoformat(), source)
        )
        self.db.commit()

    def on_do_not_call(self, phone: str) -> bool:
        row = self.db.execute("SELECT 1 FROM do_not_call WHERE phone = ?", (phone,)).fetchone()
        return row is not None

    # Attempts
    def record_attempt(self, lead_id: str, at: datetime, outcome: str) -> None:
        self.db.execute("INSERT INTO attempts VALUES (?, ?, ?)", (lead_id, at.isoformat(), outcome))
        self.db.commit()

    def attempts(self, lead_id: str) -> list[tuple[datetime, str]]:
        rows = self.db.execute(
            "SELECT at, outcome FROM attempts WHERE lead_id = ? ORDER BY at", (lead_id,)
        )
        return [(datetime.fromisoformat(at), outcome) for at, outcome in rows]

    # Loan officers' calendar
    def open_slots(self, slots: list[str]) -> None:
        self.db.executemany("INSERT OR IGNORE INTO slots VALUES (?, NULL)", [(s,) for s in slots])
        self.db.commit()

    def free_slots(self) -> list[str]:
        rows = self.db.execute("SELECT slot FROM slots WHERE lead_id IS NULL ORDER BY slot")
        return [slot for (slot,) in rows]

    def take_slot(self, slot: str, lead_id: str) -> bool:
        """Book ``slot`` for the lead. False if it does not exist or is already taken."""
        done = self.db.execute(
            "UPDATE slots SET lead_id = ? WHERE slot = ? AND lead_id IS NULL", (lead_id, slot)
        )
        self.db.commit()
        return done.rowcount == 1

    def appointment(self, lead_id: str) -> str | None:
        row = self.db.execute("SELECT slot FROM slots WHERE lead_id = ?", (lead_id,)).fetchone()
        return row[0] if row else None

    # Callbacks
    def add_callback(self, lead_id: str, day: str, period: str, at: datetime) -> None:
        self.db.execute(
            "INSERT INTO callbacks VALUES (?, ?, ?, ?)", (lead_id, day, period, at.isoformat())
        )
        self.db.commit()

    def callback(self, lead_id: str) -> tuple[str, str] | None:
        row = self.db.execute(
            "SELECT day, period FROM callbacks WHERE lead_id = ? ORDER BY created_at DESC",
            (lead_id,),
        ).fetchone()
        return (row[0], row[1]) if row else None
