from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import NOW

from callagent.gate import may_call


def test_a_consenting_lead_in_calling_hours_may_be_called(lead, book, playbook):
    assert may_call(lead, NOW, book, playbook).allowed


def test_no_consent_no_call(lead, book, playbook):
    decision = may_call(replace(lead, consent=False), NOW, book, playbook)
    assert not decision.allowed and "consent" in decision.reason


def test_do_not_call_list_wins_over_everything(lead, book, playbook):
    book.add_do_not_call(lead.phone, NOW, "test")
    decision = may_call(lead, NOW, book, playbook)
    assert not decision.allowed and decision.reason == "on the do-not-call list"


@pytest.mark.parametrize(
    ("zone", "allowed"),
    [
        ("America/New_York", True),  # 10:30
        ("America/Chicago", True),  # 09:30
        ("America/Denver", True),  # 08:30
        ("America/Los_Angeles", False),  # 07:30
    ],
)
def test_calling_hours_are_in_the_contacts_time_zone(lead, book, playbook, zone, allowed):
    assert may_call(replace(lead, timezone=zone), NOW, book, playbook).allowed is allowed


@pytest.mark.parametrize(
    ("clock", "allowed"), [("07:59", False), ("08:00", True), ("20:59", True), ("21:00", False)]
)
def test_calling_hours_edges(lead, book, playbook, clock, allowed):
    hour, minute = map(int, clock.split(":"))
    assert may_call(lead, NOW.replace(hour=hour, minute=minute), book, playbook).allowed is allowed


def test_time_is_converted_not_assumed(lead, book, playbook):
    """10:30 in Los Angeles is 13:30 for a contact in New York."""
    pacific = datetime(2026, 11, 18, 19, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    decision = may_call(lead, pacific, book, playbook)  # 22:00 in New York
    assert not decision.allowed and "22:00" in decision.reason


def test_a_naive_time_is_an_error(lead, book, playbook):
    with pytest.raises(ValueError):
        may_call(lead, datetime(2026, 11, 18, 10, 30), book, playbook)


def test_attempt_limit(lead, book, playbook):
    for days in (6, 4, 2):
        book.record_attempt(lead.id, NOW - timedelta(days=days), "voicemail")
    decision = may_call(lead, NOW, book, playbook)
    assert not decision.allowed and "attempt limit" in decision.reason


def test_not_twice_within_a_day(lead, book, playbook):
    book.record_attempt(lead.id, NOW - timedelta(hours=23), "voicemail")
    assert may_call(lead, NOW, book, playbook).reason == "called too recently"
    assert may_call(lead, NOW + timedelta(hours=2), book, playbook).allowed


@pytest.mark.parametrize("outcome", ["appointment_booked", "transferred", "wrong_number"])
def test_a_finished_lead_is_not_called_again(lead, book, playbook, outcome):
    book.record_attempt(lead.id, NOW - timedelta(days=3), outcome)
    assert not may_call(lead, NOW, book, playbook).allowed


def test_an_opt_out_on_one_call_blocks_the_next(lead, book, playbook):
    from callagent.tools import CallState, Toolbox

    Toolbox(CallState(lead, NOW), book, playbook).do_not_call("the guard")
    assert (
        may_call(lead, NOW + timedelta(days=7), book, playbook).reason == "on the do-not-call list"
    )
