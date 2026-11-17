from conftest import NOW, FakeLLM, said

from callagent import guard
from callagent.agent import Call, system_prompt


def make(llm, playbook, lead, book, mode="guarded") -> Call:
    return Call(llm, playbook, lead, book, NOW, mode=mode)


def user_text(request: dict) -> str:
    blocks = request["messages"][-1]["content"]
    return " ".join(block["text"] for block in blocks if block["type"] == "text")


async def test_guarded_opening_is_fixed_text_and_costs_no_model_call(playbook, lead, book):
    llm = FakeLLM()
    reply = await make(llm, playbook, lead, book).open()
    assert reply.text == playbook.opening_line("Jordan")
    assert guard.has_disclosures(reply.text) and llm.requests == []


async def test_prompt_only_opening_is_written_by_the_model(playbook, lead, book):
    llm = FakeLLM(said("Hello, this is Ava. Is this Jordan?"))
    reply = await make(llm, playbook, lead, book, "prompt_only").open()
    assert reply.text == "Hello, this is Ava. Is this Jordan?"
    assert "Speak first" in user_text(llm.requests[0])
    assert "Begin every call by saying that you are an AI" in llm.requests[0]["system"]
    assert "Begin every call" not in system_prompt(playbook, "guarded")


async def test_first_request_carries_the_call_context_and_the_opening(playbook, lead, book):
    llm = FakeLLM(said("Thanks, Jordan. Is now a good time?"))
    call = make(llm, playbook, lead, book)
    await call.open()
    await call.hear("Yes, this is Jordan.")
    text = user_text(llm.requests[0])
    assert "Open times with a loan officer" in text and "2026-11-19T10:00" in text
    assert "You opened the call by saying" in text
    assert text.endswith("Contact: Yes, this is Jordan.")
    assert llm.requests[0]["system"] == system_prompt(playbook, "guarded")  # never changes


async def test_words_with_a_note_are_spoken_at_once_and_the_result_follows_later(
    playbook, lead, book
):
    llm = FakeLLM(
        said(
            "Got it. What is the home worth?",
            ("record_answer", {"field": "goal", "value": "refinance"}),
        ),
        said("Thanks."),
    )
    call = make(llm, playbook, lead, book)
    await call.open()
    reply = await call.hear("I want to refinance.")
    assert reply.text == "Got it. What is the home worth?" and len(llm.requests) == 1
    assert call.state.answers == {"goal": "refinance"}
    await call.hear("About 400 thousand.")
    blocks = llm.requests[1]["messages"][-1]["content"]
    assert blocks[0]["type"] == "tool_result" and blocks[0]["content"] == "Saved."
    assert blocks[1]["text"] == "Contact: About 400 thousand."


async def test_a_tool_call_with_no_words_gets_its_result_before_anything_is_said(
    playbook, lead, book
):
    llm = FakeLLM(
        said("", ("record_answer", {"field": "credit", "value": "good"})),
        said("Thanks. Would Thursday at 10 work?"),
    )
    call = make(llm, playbook, lead, book)
    await call.open()
    reply = await call.hear("My credit is good.")
    assert reply.text == "Thanks. Would Thursday at 10 work?" and len(llm.requests) == 2


async def test_words_written_before_a_booking_result_are_not_spoken(playbook, lead, book):
    llm = FakeLLM(
        said("You're booked for Thursday!", ("book_appointment", {"slot": "2026-11-19T10:00"})),
        said("Before I book that, how would you describe your credit?"),
    )
    call = make(llm, playbook, lead, book)
    await call.open()
    reply = await call.hear("Thursday at ten, please.")
    assert reply.text == "Before I book that, how would you describe your credit?"
    assert call.state.appointment is None  # nothing was saved yet, so the booking was refused
    assert llm.requests[1]["messages"][-1]["content"][0]["is_error"] is True
    assert [line["text"] for line in call.transcript if line["speaker"] == "agent"][
        -1
    ] == reply.text


async def test_a_request_to_stop_is_acted_on_even_if_the_model_does_nothing(playbook, lead, book):
    llm = FakeLLM(said("Are you sure? We have great options."))
    call = make(llm, playbook, lead, book)
    await call.open()
    reply = await call.hear("Stop calling me.")
    assert book.on_do_not_call(lead.phone)
    assert reply.ended and call.state.outcome == "do_not_call"
    assert "do-not-call list" in user_text(llm.requests[0])
    assert {"type": "do_not_call", "source": "the guard"} in call.state.events


async def test_without_the_guard_the_same_request_depends_on_the_model(playbook, lead, book):
    llm = FakeLLM(said("Hi, is this Jordan?"), said("Are you sure? We have great options."))
    call = make(llm, playbook, lead, book, "prompt_only")
    await call.open()
    reply = await call.hear("Stop calling me.")
    assert not book.on_do_not_call(lead.phone) and not reply.ended


async def test_a_rate_is_withheld_and_the_models_memory_is_corrected(playbook, lead, book):
    llm = FakeLLM(said("Sure. You'd get about 6.25% today. What is the home worth?"), said("Ok."))
    call = make(llm, playbook, lead, book)
    await call.open()
    reply = await call.hear("What rate can I get?")
    assert "6.25" not in reply.text and reply.text.endswith(playbook.fallback)
    assert reply.withheld == ["quotes a rate"]
    assert call.state.events[-1] == {"type": "guard", "rule": "quotes a rate"}
    await call.hear("Fine.")
    remembered = llm.requests[1]["messages"][-2]["content"]
    assert remembered == [{"type": "text", "text": reply.text}]


async def test_prompt_only_speaks_whatever_the_model_wrote(playbook, lead, book):
    llm = FakeLLM(said("Hi, is this Jordan?"), said("You'd get about 6.25% today."))
    call = make(llm, playbook, lead, book, "prompt_only")
    await call.open()
    assert (await call.hear("What rate can I get?")).text == "You'd get about 6.25% today."


async def test_asked_if_it_is_a_person_it_always_says_ai(playbook, lead, book):
    llm = FakeLLM(said("I'm Ava with Harbor Home Lending. Is now a good time?"))
    call = make(llm, playbook, lead, book)
    await call.open()
    reply = await call.hear("Wait, are you a real person?")
    assert reply.text.startswith("I'm an AI assistant.")
    assert "Say you are an AI assistant" in user_text(llm.requests[0])


async def test_voicemail_gets_the_approved_message_word_for_word(playbook, lead, book):
    llm = FakeLLM(
        said("Hey Jordan, call me about your refinance!", ("end_call", {"reason": "voicemail"}))
    )
    call = make(llm, playbook, lead, book)
    await call.open()
    reply = await call.hear("You've reached Jordan. Leave a message after the tone.")
    assert reply.text == playbook.voicemail_line() and reply.ended
    assert call.state.outcome == "voicemail"


async def test_ending_tools_end_the_call(playbook, lead, book):
    llm = FakeLLM(
        said("Connecting you now.", ("transfer_to_officer", {"reason": "asked for a person"}))
    )
    call = make(llm, playbook, lead, book)
    await call.open()
    reply = await call.hear("Can I talk to a person?")
    assert reply.ended and call.state.outcome == "transferred"


async def test_reasoning_is_dropped_from_history_between_turns(playbook, lead, book):
    llm = FakeLLM(said("Thanks. Is now a good time?"), said("Great."))
    call = make(llm, playbook, lead, book)
    await call.open()
    await call.hear("Yes, this is Jordan.")
    await call.hear("Sure.")
    earlier = llm.requests[1]["messages"][1]["content"]
    assert all(block["type"] != "thinking" for block in earlier)


async def test_cost_and_time_are_added_up(playbook, lead, book):
    llm = FakeLLM(said("", ("record_answer", {"field": "goal", "value": "purchase"})), said("Ok."))
    call = make(llm, playbook, lead, book)
    await call.open()
    reply = await call.hear("Buying a home.")
    assert round(call.cost_usd, 3) == 0.002 and reply.latency_s == 1.0
