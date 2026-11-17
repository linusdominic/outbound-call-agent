"""This server as a language model: an OpenAI-compatible ``/chat/completions`` endpoint.

Most voice platforms can be pointed at a "custom LLM" that speaks the OpenAI Chat
Completions protocol. They handle the phone line, speech recognition and the voice, and
send the conversation so far each time the contact stops talking. This endpoint answers
with the agent's next line.

The platform sends the whole conversation every time, and this server ignores all of it
except the newest thing the contact said. The state of the call (what has been saved, what
is booked, the do-not-call list) lives here, keyed by a call id that the platform must
send with every request. So does the system prompt: whatever prompt is configured on the
platform is not used.

When the call is over, the answer carries a tool call that tells the platform to hang up
or to transfer. The names of those tools differ by platform, so each platform has a
profile below, and the generic one can be set with environment variables.
"""

import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from callagent.integrations.common import LEAD_FIELDS, dig, gather, lead_from, require_key
from callagent.service import CallService, Refused

REFUSED_LINE = "Sorry to have troubled you. Goodbye."
TROUBLE_LINE = "Sorry, could you say that again?"


@dataclass(frozen=True)
class Profile:
    """How one platform names things."""

    name: str
    # Where in the request body the per-call values may be found, most specific first.
    variables: tuple[tuple[str, ...], ...] = ()
    # Where the platform's own id for the call may be found.
    call_id: tuple[tuple[str, ...], ...] = ()
    end_tool: str | None = None  # the tool call that hangs up
    end_args: dict[str, Any] = field(default_factory=dict)
    transfer_tool: str | None = None  # the tool call that hands over to a person
    transfer_args: dict[str, Any] = field(default_factory=dict)
    # Argument values may be {text} (the agent's last words), {reason}, {summary} (for the
    # person taking over) and {transfer_number}. When the last words travel inside the tool
    # call, they are not also sent as speech.
    legacy_function_call: bool = False  # emit "function_call" in place of "tool_calls"
    paths: tuple[str, ...] = ()  # where the endpoint is mounted

    @property
    def words_ride_in_tool(self) -> bool:
        return "{text}" in [*self.end_args.values(), *self.transfer_args.values()]


def generic_profile() -> Profile:
    """For any OpenAI-compatible client. The tool names come from the environment."""
    return Profile(
        name="generic",
        variables=(("callagent",), ("metadata",), ("extra_body",)),
        call_id=(("call_id",), ("user",)),
        end_tool=os.environ.get("CALLAGENT_END_TOOL") or None,
        transfer_tool=os.environ.get("CALLAGENT_TRANSFER_TOOL") or None,
        transfer_args=json.loads(os.environ.get("CALLAGENT_TRANSFER_ARGS", "{}")),
        paths=("/v1/chat/completions",),
    )


def elevenlabs_profile() -> Profile:
    """ElevenLabs Agents with a custom LLM. See docs/integrations/elevenlabs.md."""
    return Profile(
        name="elevenlabs",
        variables=(("elevenlabs_extra_body",),),
        call_id=(("elevenlabs_extra_body", "call_id"),),
        end_tool="end_call",
        end_args={"reason": "{reason}", "message": "{text}"},
        transfer_tool="transfer_to_number",
        transfer_args={
            "transfer_number": "{transfer_number}",
            "client_message": "{text}",
            "agent_message": "{summary}",
            "reason": "{reason}",
        },
        # The docs do not say which path is appended to the configured URL, so both work.
        paths=("/v1/elevenlabs/chat/completions", "/v1/elevenlabs/v1/chat/completions"),
    )


def vapi_profile() -> Profile:
    """Vapi with a custom-llm model. See docs/integrations/vapi.md."""
    return Profile(
        name="vapi",
        variables=(("metadata",), ("call", "metadata"), ("call", "assistantOverrides", "metadata")),
        call_id=(("call", "id"),),
        end_tool="endCall",
        transfer_tool="transferCall",
        transfer_args={"destination": "{transfer_number}"},
        legacy_function_call=os.environ.get("CALLAGENT_VAPI_FUNCTION_CALL") == "1",
        # Vapi's reference says it appends /chat/completions and its guide shows a URL that
        # already ends in it, so both work.
        paths=("/v1/vapi/chat/completions", "/v1/vapi/chat/completions/chat/completions"),
    )


def text_of(content: Any) -> str:
    """A message's text, whether it is a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def per_call_values(body: dict[str, Any], headers: Any, profile: Profile) -> dict[str, Any]:
    """Lead fields and the call id, from the request body and from ``X-`` headers."""
    values = gather(body, profile.variables)
    for key in (*LEAD_FIELDS, "call_id"):
        header = headers.get(f"x-{key.replace('_', '-')}")
        if header:
            values[key] = header
    if "call_id" not in values:
        for path in profile.call_id:
            found = dig(body, path)
            if isinstance(found, str | int) and str(found):
                values["call_id"] = str(found)
                break
    return values


@dataclass
class Answer:
    text: str
    action: str | None = None  # "end" or "transfer"
    reason: str = ""
    summary: str = ""  # for the person taking over a transferred call


class Conversations:
    """Maps a platform's requests onto calls, once each."""

    def __init__(self, service: CallService) -> None:
        self.service = service
        self.seen: dict[str, tuple[int, Answer]] = {}  # call id -> (contact turns handled, answer)

    async def answer(self, body: dict[str, Any], headers: Any, profile: Profile) -> Answer:
        values = per_call_values(body, headers, profile)
        call_id = values.get("call_id")
        if not call_id:
            raise HTTPException(
                422,
                "No call id. Send one with every request: an X-Call-Id header, or one of "
                f"{['.'.join(path) for path in profile.call_id]} in the body.",
            )
        said = [
            text_of(message.get("content")).strip()
            for message in body.get("messages", [])
            if message.get("role") == "user"
        ]
        said = [line for line in said if line]

        if call_id not in self.service.sessions:
            try:
                # With nothing said yet, the platform is asking for the opening line.
                _, opening = await self.service.start(
                    lead_from(self.service, values), call_id=call_id
                )
            except Refused as refusal:
                return Answer(REFUSED_LINE, "end", f"gate: {refusal.reason}")
            self.seen[call_id] = (0, Answer(opening.text))

        handled, last = self.seen.get(call_id, (0, Answer("")))
        if len(said) <= handled:
            return last  # a retry, or the opening: say the same thing again
        reply = await self.service.hear(call_id, " ".join(said[handled:]))
        state = self.service.session(call_id).call.state
        action = None
        if reply.ended:
            action = "transfer" if state.transferred else "end"
        answer = Answer(reply.text, action, state.transferred or state.outcome if action else "")
        if action == "transfer":
            answer.summary = self.service.handover(call_id)
        self.seen[call_id] = (len(said), answer)
        return answer


def tool_call(answer: Answer, profile: Profile) -> dict[str, Any] | None:
    """The tool call that makes the platform hang up or transfer, if it has one."""
    if answer.action == "transfer" and profile.transfer_tool:
        name, args = profile.transfer_tool, profile.transfer_args
    elif answer.action and profile.end_tool:
        name, args = profile.end_tool, profile.end_args
    else:
        return None
    filled = {
        "{text}": answer.text,
        "{reason}": answer.reason,
        "{summary}": answer.summary,
        "{transfer_number}": os.environ.get("CALLAGENT_TRANSFER_NUMBER", ""),
    }
    args = {key: filled.get(value, value) for key, value in args.items()}
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def spoken(answer: Answer, profile: Profile) -> str:
    """The words sent as speech. Empty when they are carried inside the tool call instead."""
    carried = tool_call(answer, profile) is not None and profile.words_ride_in_tool
    return "" if carried else answer.text


def completion(answer: Answer, profile: Profile, model: str) -> dict[str, Any]:
    call = tool_call(answer, profile)
    message: dict[str, Any] = {"role": "assistant", "content": spoken(answer, profile)}
    if call and profile.legacy_function_call:
        message["function_call"] = call["function"]
    elif call:
        message["tool_calls"] = [call]
    finish = (
        "stop" if not call else "function_call" if profile.legacy_function_call else "tool_calls"
    )
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def pieces(text: str, size: int = 6) -> list[str]:
    """The reply cut into a few words at a time, so a voice can start before it has all of it."""
    if not text:
        return []
    words = text.split(" ")
    return [
        " ".join(words[i : i + size]) + (" " if i + size < len(words) else "")
        for i in range(0, len(words), size)
    ]


async def stream(pending, profile: Profile, model: str) -> AsyncIterator[str]:
    """The same answer as server-sent events, in the shape of ``chat.completion.chunk``."""
    base = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }

    def event(delta: dict[str, Any], finish: str | None = None) -> str:
        chunk = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(chunk)}\n\n"

    yield event({"role": "assistant", "content": ""})  # at once, before the model has answered
    try:
        answer = await pending
    except HTTPException as error:
        answer = Answer(TROUBLE_LINE, reason=str(error.detail))
    for piece in pieces(spoken(answer, profile)):
        yield event({"content": piece})
    call = tool_call(answer, profile)
    finish = "stop"
    if call and profile.legacy_function_call:
        yield event({"function_call": call["function"]})
        finish = "function_call"
    elif call:
        yield event({"tool_calls": [{"index": 0, **call}]})
        finish = "tool_calls"
    yield event({}, finish)
    yield "data: [DONE]\n\n"


def router(service: CallService, api_key: str | None, profiles: list[Profile]) -> APIRouter:
    api = APIRouter(dependencies=[Depends(require_key(api_key))])
    conversations = Conversations(service)

    def mount(profile: Profile) -> None:
        async def chat(request: Request, body: dict[str, Any] = Body(...)):
            model = str(body.get("model") or service.model)
            if body.get("stream"):
                pending = conversations.answer(body, request.headers, profile)
                return StreamingResponse(
                    stream(pending, profile, model), media_type="text/event-stream"
                )
            answer = await conversations.answer(body, request.headers, profile)
            return JSONResponse(completion(answer, profile, model))

        for number, path in enumerate(profile.paths):
            api.add_api_route(path, chat, methods=["POST"], name=f"chat_{profile.name}_{number}")

    for profile in profiles:
        mount(profile)
    return api
