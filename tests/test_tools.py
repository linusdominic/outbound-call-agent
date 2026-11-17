import pytest
from conftest import NOW

from callagent.tools import CallState, Toolbox, parse_dollars


@pytest.fixture
def box(lead, book, playbook) -> Toolbox:
    return Toolbox(CallState(lead, NOW), book, playbook)


@pytest.mark.parametrize(
    ("text", "dollars"),
    [
        ("$400,000", 400_000),
        ("420k", 420_000),
        ("about 350 thousand", 350_000),
        ("1.2 million", 1_200_000),
        ("670000", 670_000),
        ("no idea", None),
    ],
)
def test_dollar_amounts(text, dollars):
    assert parse_dollars(text) == dollars


def test_answers_are_validated_before_they_are_saved(box):
    assert not box.call("record_answer", {"field": "goal", "value": "Cash Out"}).is_error
    assert not box.call("record_answer", {"field": "property_value", "value": "$420,000"}).is_error
    assert box.state.answers == {"goal": "cash_out", "property_value": 420_000}
    assert box.call("record_answer", {"field": "goal", "value": "pending"}).is_error
    assert box.call("record_answer", {"field": "property_value", "value": "unknown"}).is_error
    assert box.call("record_answer", {"field": "ssn", "value": "512-44-7166"}).is_error
    assert box.state.answers == {"goal": "cash_out", "property_value": 420_000}


def test_no_booking_before_the_contact_is_qualified(box):
    result = box.call("book_appointment", {"slot": "2026-11-19T10:00"})
    assert result.is_error and result.wait and box.state.appointment is None
    box.call("record_answer", {"field": "goal", "value": "refinance"})
    box.call("record_answer", {"field": "credit", "value": "good"})
    assert not box.call("book_appointment", {"slot": "2026-11-19T10:00"}).is_error
    assert box.state.appointment == "2026-11-19T10:00"
    assert box.state.outcome == "appointment_booked"


def test_only_an_open_time_can_be_booked(box, book):
    box.call("record_answer", {"field": "goal", "value": "refinance"})
    box.call("record_answer", {"field": "credit", "value": "good"})
    book.take_slot("2026-11-19T13:00", "someone-else")
    taken = box.call("book_appointment", {"slot": "2026-11-19T13:00"})
    invented = box.call("book_appointment", {"slot": "2026-11-21T09:00"})
    assert taken.is_error and invented.is_error
    assert "2026-11-19T10:00" in taken.content  # the open times are offered back
    assert box.state.appointment is None
    box.call("book_appointment", {"slot": "2026-11-20T10:00"})
    assert box.call("book_appointment", {"slot": "2026-11-19T10:00"}).is_error  # one each


def test_callback_must_be_a_coming_weekday_and_a_time_of_day(box, book, lead):
    assert box.call("schedule_callback", {"day": "2026-11-21", "period": "morning"}).is_error
    assert box.call("schedule_callback", {"day": "2026-11-19", "period": "midnight"}).is_error
    assert not box.call("schedule_callback", {"day": "2026-11-23", "period": "evening"}).is_error
    assert book.callback(lead.id) == ("2026-11-23", "evening")
    assert box.state.outcome == "callback_scheduled"


def test_do_not_call_is_written_to_the_list_and_ends_the_call(box, book, lead):
    box.call("mark_do_not_call", {})
    assert book.on_do_not_call(lead.phone) and box.state.finished
    assert box.state.outcome == "do_not_call"


def test_outcome_comes_from_what_was_done_not_what_was_claimed(box):
    assert box.state.outcome == "no_outcome"
    box.call("end_call", {"reason": "completed"})
    assert box.state.outcome == "no_outcome"  # "completed" with nothing done is no outcome
    box.state.ended = "declined"
    assert box.state.outcome == "declined"
    box.state.transferred = "asked for a person"
    assert box.state.outcome == "transferred"
    box.state.do_not_call = True
    assert box.state.outcome == "do_not_call"


def test_unknown_tools_and_bad_arguments_are_errors_not_crashes(box):
    assert box.call("wire_money", {}).is_error
    assert box.call("end_call", {"why": "bored"}).is_error
    assert box.call("end_call", {"reason": "bored"}).is_error
    assert not box.state.finished
