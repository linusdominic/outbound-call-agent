from datetime import timedelta

import pytest
from conftest import NOW, FakeLLM, said

from callagent.book import Book
from callagent.playbook import ROOT
from callagent.service import CallService, Refused, UnknownCall, UnknownLead, load_leads


def make(playbook, *turns, **kwargs) -> CallService:
    llm = FakeLLM(*turns)
    return CallService(playbook, lambda: llm, clock=lambda: NOW, **kwargs)


def test_no_model_client_is_made_until_a_model_is_needed(playbook, lead):
    def explode():
        raise AssertionError("an API key would be needed here")

    service = CallService(playbook, explode, clock=lambda: NOW)
    assert service.gate(lead).allowed
    assert service.check_speech("Hello there.")["changed"] is False
    assert "Approved answers" in service.prompt()


async def test_a_call_from_start_to_finish(playbook, lead):
    service = make(
        playbook,
        said("Thanks, Jordan. Is now a good time?"),
        said("Understood. Goodbye.", ("end_call", {"reason": "declined"})),
    )
    call_id, opening = await service.start(lead)
    assert opening.text == playbook.opening_line("Jordan")
    assert (await service.hear(call_id, "Yes, this is Jordan.")).ended is False
    last = await service.hear(call_id, "I'm not interested, thanks.")
    assert last.ended and service.snapshot(call_id)["outcome"] == "declined"
    assert [outcome for _, outcome in service.book.attempts(lead.id)] == ["declined"]
    assert (await service.hear(call_id, "Hello?")).ended  # nothing more is said or recorded
    assert len(service.book.attempts(lead.id)) == 1


async def test_the_gate_runs_before_every_call(playbook, lead):
    service = make(playbook)
    with pytest.raises(Refused) as refusal:
        await service.start(lead, now=NOW.replace(hour=22))
    assert "outside calling hours" in refusal.value.reason
    assert service.sessions == {}


async def test_an_opt_out_on_one_call_refuses_the_next(playbook, lead):
    service = make(playbook, said("Thanks. Is now a good time?"), said("Understood. Goodbye."))
    call_id, _ = await service.start(lead)
    await service.hear(call_id, "Yes?")
    assert (await service.hear(call_id, "Take me off your list.")).ended
    with pytest.raises(Refused) as refusal:
        await service.start(lead, now=NOW + timedelta(days=30))
    assert refusal.value.reason == "on the do-not-call list"


async def test_records_in_a_file_outlive_the_process(playbook, lead, tmp_path):
    path = tmp_path / "records.db"
    first = make(playbook, said("Understood. Goodbye."), book=Book(path))
    call_id, _ = await first.start(lead)
    await first.hear(call_id, "Stop calling me.")
    second = make(playbook, book=Book(path))  # a new process, the same file
    assert not second.gate(lead).allowed


async def test_tools_can_be_driven_by_a_model_that_lives_elsewhere(playbook, lead):
    service = make(playbook)
    call_id, _ = await service.start(lead)
    assert not service.tool(
        call_id, "record_answer", {"field": "goal", "value": "purchase"}
    ).is_error
    slot = service.book.free_slots()[0]
    assert service.tool(call_id, "book_appointment", {"slot": slot}).is_error  # one answer so far
    service.tool(call_id, "record_answer", {"field": "credit", "value": "good"})
    assert not service.tool(call_id, "book_appointment", {"slot": slot}).is_error
    service.tool(call_id, "end_call", {"reason": "completed"})
    assert service.snapshot(call_id)["outcome"] == "appointment_booked"
    assert service.book.attempts(lead.id)[0][1] == "appointment_booked"


async def test_the_guard_can_be_used_on_its_own(playbook, lead):
    service = make(playbook)
    call_id, _ = await service.start(lead)
    assert service.heard(call_id, "Is this a real person?") == {
        "asked_to_stop": False,
        "asked_if_human": True,
    }
    assert service.heard(call_id, "Lose my number.")["asked_to_stop"]
    assert service.book.on_do_not_call(lead.phone)
    checked = service.check_speech("You'd get 6.25% today. Shall I book a call?")
    assert checked["withheld"] == ["quotes a rate"] and "6.25" not in checked["text"]


def test_leads_come_from_the_list_or_from_the_request(playbook):
    leads = load_leads(ROOT / "data" / "leads.csv")
    service = make(playbook, leads=leads)
    assert service.lead({"lead_id": "L01"}).first == "Jordan"
    spelled = service.lead({"first": "Sam", "phone": "555-0199", "consent": "yes"})
    assert spelled.consent and spelled.id == "555-0199" and spelled.timezone == "America/New_York"
    assert not service.lead(
        {"first": "Sam", "phone": "555-0199"}
    ).consent  # none stated, none assumed
    with pytest.raises(UnknownLead):
        service.lead({"lead_id": "nobody"})
    with pytest.raises(UnknownLead):
        service.lead({})


async def test_unknown_calls_and_a_calendar_that_fills_itself(playbook, lead):
    service = make(playbook)
    with pytest.raises(UnknownCall):
        service.snapshot("nope")
    await service.start(lead)
    assert len(service.book.free_slots()) == 9  # three weekdays, three times each
