"""The custom-LLM endpoint, exercised with the real OpenAI client library.

Voice platforms that accept a custom language model talk to it the way this client does,
so if the client is satisfied the protocol is right.
"""

import json

import httpx
import pytest
from conftest import FakeLLM, said
from fastapi import FastAPI
from openai import AsyncOpenAI

from callagent.book import Book
from callagent.integrations import openai_compat
from callagent.integrations.openai_compat import Profile
from callagent.service import CallService

LEAD = {"first": "Jordan", "phone": "555-0142", "consent": True, "lead_id": "L1"}
OPENING = [{"role": "assistant", "content": "Hi, this is Ava..."}]
WITH_TOOLS = Profile(
    name="generic",
    variables=(("callagent",),),
    call_id=(("call_id",), ("user",)),
    end_tool="end_call",
    transfer_tool="transfer_to_number",
    transfer_args={"reason": "{reason}", "transfer_number": "+15550100"},
    paths=("/v1/chat/completions",),
)


class Morning(CallService):
    """A service whose clock always says a weekday morning in New York."""

    def __init__(self, playbook, llm):
        from conftest import NOW

        super().__init__(playbook, lambda: llm, book=Book(), clock=lambda: NOW)


def client_for(playbook, *turns, profile=WITH_TOOLS, key=None) -> tuple[AsyncOpenAI, CallService]:
    service = Morning(playbook, FakeLLM(*turns))
    app = FastAPI()
    app.include_router(openai_compat.router(service, key, [profile]))
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://server")
    return AsyncOpenAI(
        api_key=key or "unused", base_url="http://server/v1", http_client=http
    ), service


def ask(client, messages, call_id="c1", stream=False, **extra):
    return client.chat.completions.create(
        model="callagent",
        messages=messages,
        stream=stream,
        extra_body={"call_id": call_id, "callagent": LEAD, **extra},
    )


async def test_with_nothing_said_yet_it_gives_the_opening_line(playbook):
    client, service = client_for(playbook)
    reply = await ask(client, [{"role": "system", "content": "ignored"}])
    assert reply.choices[0].message.content == playbook.opening_line("Jordan")
    assert reply.choices[0].finish_reason == "stop" and "c1" in service.sessions


async def test_a_conversation_turn_by_turn(playbook):
    client, service = client_for(
        playbook,
        said("Thanks, Jordan. Is now a good time?"),
        said(
            "Got it. What is the home worth?",
            ("record_answer", {"field": "goal", "value": "refinance"}),
        ),
    )
    history = [*OPENING, {"role": "user", "content": "Yes, this is Jordan."}]
    first = await ask(client, history)
    assert first.choices[0].message.content == "Thanks, Jordan. Is now a good time?"
    history += [
        {"role": "assistant", "content": first.choices[0].message.content},
        {"role": "user", "content": [{"type": "text", "text": "Sure. I want to refinance."}]},
    ]
    second = await ask(client, history)
    assert second.choices[0].message.content == "Got it. What is the home worth?"
    assert service.snapshot("c1")["answers"] == {"goal": "refinance"}


async def test_the_same_request_twice_is_answered_once(playbook):
    client, service = client_for(playbook, said("Thanks, Jordan. Is now a good time?"))
    history = [*OPENING, {"role": "user", "content": "Yes, this is Jordan."}]
    first = await ask(client, history)
    again = await ask(client, history)  # a platform retrying, or regenerating after an interruption
    assert again.choices[0].message.content == first.choices[0].message.content
    assert service.session("c1").call.agent_turns == 2  # the opening and one reply


async def test_streaming_ends_with_the_hang_up_tool_call(playbook):
    client, service = client_for(
        playbook, said("Understood. You're on our do-not-call list. Goodbye.")
    )
    history = [*OPENING, {"role": "user", "content": "Stop calling me."}]
    words, calls, finish = "", [], None
    async for chunk in await ask(client, history, stream=True):
        delta = chunk.choices[0].delta
        words += delta.content or ""
        calls += delta.tool_calls or []
        finish = chunk.choices[0].finish_reason or finish
    assert words == "Understood. You're on our do-not-call list. Goodbye."
    assert [call.function.name for call in calls] == ["end_call"] and finish == "tool_calls"
    assert service.book.on_do_not_call("555-0142")


async def test_a_transfer_names_the_transfer_tool_and_carries_the_reason(playbook):
    client, _ = client_for(
        playbook,
        said("Connecting you now.", ("transfer_to_officer", {"reason": "asked for a person"})),
    )
    history = [*OPENING, {"role": "user", "content": "Can I speak to a person?"}]
    message = (await ask(client, history)).choices[0].message
    assert message.content == "Connecting you now."
    call = message.tool_calls[0].function
    assert call.name == "transfer_to_number"
    assert json.loads(call.arguments) == {
        "reason": "asked for a person",
        "transfer_number": "+15550100",
    }


async def test_without_tool_names_the_words_are_still_right(playbook):
    plain = Profile(
        name="generic",
        variables=(("callagent",),),
        call_id=(("call_id",),),
        paths=("/v1/chat/completions",),
    )
    client, _ = client_for(playbook, said("Understood. Goodbye."), profile=plain)
    reply = await ask(client, [*OPENING, {"role": "user", "content": "Take me off your list."}])
    assert reply.choices[0].message.content == "Understood. Goodbye."
    assert reply.choices[0].message.tool_calls is None and reply.choices[0].finish_reason == "stop"


async def test_a_lead_the_gate_refuses_gets_a_goodbye_and_a_hang_up(playbook):
    client, service = client_for(playbook)
    reply = await client.chat.completions.create(
        model="callagent",
        messages=[*OPENING, {"role": "user", "content": "Hello?"}],
        extra_body={"call_id": "c9", "callagent": {"first": "Sam", "phone": "555-0199"}},
    )
    assert reply.choices[0].message.content == openai_compat.REFUSED_LINE
    assert reply.choices[0].message.tool_calls[0].function.name == "end_call"
    assert "c9" not in service.sessions


async def test_the_call_id_can_come_from_a_header_or_the_user_field(playbook):
    client, service = client_for(playbook)
    await client.chat.completions.create(
        model="callagent", messages=[], user="from-user", extra_body={"callagent": LEAD}
    )
    await client.chat.completions.create(
        model="callagent",
        messages=[],
        extra_headers={"X-Call-Id": "from-header", "X-Lead-Id": "L2", "X-First": "Avery",
                       "X-Phone": "555-0143", "X-Consent": "yes"},
    )  # fmt: skip
    assert set(service.sessions) == {"from-user", "from-header"}
    assert service.session("from-header").call.lead.first == "Avery"


async def test_requests_without_a_call_id_or_a_key_are_rejected(playbook):
    from openai import AuthenticationError, UnprocessableEntityError

    client, _ = client_for(playbook)
    with pytest.raises(UnprocessableEntityError):
        await client.chat.completions.create(model="callagent", messages=[])
    locked, _ = client_for(playbook, key="s3cret")
    assert (await ask(locked, [])).choices[0].message.content
    wrong = AsyncOpenAI(api_key="nope", base_url="http://server/v1", http_client=locked._client)
    with pytest.raises(AuthenticationError):
        await ask(wrong, [])


def test_reply_is_cut_into_pieces_that_join_back_exactly():
    text = "I can't quote a rate on this call. Would Thursday at 10 work for you?"
    assert "".join(openai_compat.pieces(text)) == text
    assert len(openai_compat.pieces(text)) == 3
