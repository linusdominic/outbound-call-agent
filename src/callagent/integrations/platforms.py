"""Small adapters for the parts of each voice platform that are not plain OpenAI protocol.

Everything here was written from the platforms' public documentation as it stood on
18 November 2026 and is tested against requests shaped the way that documentation
describes. None of it has been run against the live services. Where the documentation was
unclear, the guide for that platform in ``docs/integrations`` says so.

- ``/v1/prepare/{platform}``: runs the gate and builds the per-call part of the request
  that starts an outbound call on that platform.
- ElevenLabs: the post-call webhook.
- Vapi: the server webhook, for tool calls and the end-of-call report.
- Retell: the custom-LLM WebSocket, the custom-function webhook and the call webhook.
"""

import json
import os
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, WebSocket, WebSocketDisconnect

from callagent.integrations.common import (
    LEAD_FIELDS,
    dig,
    gather,
    key_matches,
    lead_from,
    moment,
    require_key,
    require_key_anywhere,
)
from callagent.integrations.openai_compat import REFUSED_LINE
from callagent.service import CallService, Refused, UnknownCall

STILL_THERE = "Are you still there?"
VAPI_PATHS = (
    ("call", "metadata"),
    ("call", "assistantOverrides", "metadata"),
    ("call", "assistantOverrides", "variableValues"),
    ("assistant", "metadata"),
)
RETELL_PATHS = (("call", "metadata"), ("call", "retell_llm_dynamic_variables"))


def lead_values(lead_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """The lead as plain values, to hand to a platform and get back with each request."""
    values = {key: body[key] for key in LEAD_FIELDS if key in body}
    return {**values, "lead_id": lead_id}


def strings(values: dict[str, Any]) -> dict[str, str]:
    """For platforms whose per-call variables must all be text."""
    return {
        key: "yes" if value is True else "no" if value is False else str(value)
        for key, value in values.items()
    }


def call_request(platform: str, call_id: str, opening: str, lead, values: dict[str, Any]) -> dict:
    """The per-call part of ``platform``'s start-an-outbound-call request."""
    name = f"{lead.first} {lead.last}".strip()
    if platform == "elevenlabs":  # POST /v1/convai/twilio/outbound-call
        return {
            "to_number": lead.phone,
            "conversation_initiation_client_data": {
                "dynamic_variables": {"call_id": call_id, "lead_id": lead.id, "opening": opening},
                "custom_llm_extra_body": {"call_id": call_id, **values},
                "conversation_config_override": {"agent": {"first_message": opening}},
            },
        }
    if platform == "vapi":  # POST https://api.vapi.ai/call
        return {
            "customer": {"number": lead.phone, "name": name, "externalId": lead.id},
            "assistantOverrides": {
                "metadata": values,
                "variableValues": {"first": lead.first, "opening": opening},
            },
        }
    if platform == "retell":  # POST https://api.retellai.com/v2/create-phone-call
        return {
            "to_number": lead.phone,
            "metadata": values,
            "retell_llm_dynamic_variables": strings({"first": lead.first, "opening": opening}),
        }
    raise HTTPException(404, "platform must be elevenlabs, vapi or retell")


def router(service: CallService, api_key: str | None) -> APIRouter:
    api = APIRouter(prefix="/v1")
    secured = [Depends(require_key(api_key))]

    webhook = [Depends(require_key_anywhere(api_key))]

    async def ensure(call_id: str, values: dict[str, Any]) -> str | None:
        """Open the call if this is the first time it is seen. Returns a refusal, if any."""
        if call_id in service.sessions:
            return None
        try:
            await service.start(lead_from(service, values), call_id=call_id)
        except Refused as refusal:
            return refusal.reason
        return None

    def close(call_id: str, lines: list[tuple[str, str]]) -> dict[str, Any]:
        """End a call from a platform's transcript, checking it for a missed opt-out."""
        try:
            for role, text in lines:
                if role in ("user", "customer", "contact"):
                    service.heard(call_id, text)
            outcome = service.finish(call_id)
        except UnknownCall:
            return {"ok": False, "reason": "unknown call", "call_id": call_id}
        return {"ok": True, "call_id": call_id, "outcome": outcome}

    @api.post("/prepare/{platform}", dependencies=secured)
    def prepare(platform: str, body: dict[str, Any] = Body(...)) -> dict:
        """Gate the lead, then build what the platform needs to place the call."""
        lead = lead_from(service, body)
        decision = service.gate(lead, moment(body.get("now")))
        if not decision.allowed:
            return {"allowed": False, "reason": decision.reason}
        call_id = str(body.get("call_id") or uuid.uuid4().hex[:12])
        opening = service.playbook.opening_line(lead.first)
        values = lead_values(lead.id, body)
        return {
            "allowed": True,
            "reason": decision.reason,
            "call_id": call_id,
            "opening": opening,
            "request": call_request(platform, call_id, opening, lead, values),
        }

    # ElevenLabs

    @api.post("/elevenlabs/post-call", dependencies=webhook)
    def elevenlabs_post_call(body: dict[str, Any] = Body(...)) -> dict:
        data = body.get("data", {})
        started_with = data.get("conversation_initiation_client_data", {})
        values = gather(started_with, (("custom_llm_extra_body",), ("dynamic_variables",)))
        if body.get("type") == "call_initiation_failure":
            if values.get("lead_id"):  # nobody answered: it still counts as an attempt
                reason = str(data.get("failure_reason", "unknown")).replace("-", "_")
                service.book.record_attempt(str(values["lead_id"]), service.clock(), reason)
            return {"ok": True}
        if body.get("type") != "post_call_transcription":
            return {"ok": True}
        lines = [
            (str(t.get("role")), str(t.get("message") or "")) for t in data.get("transcript", [])
        ]
        return close(str(values.get("call_id", "")), lines)

    # Vapi

    @api.post("/vapi/webhook", dependencies=webhook)
    async def vapi_webhook(body: dict[str, Any] = Body(...)) -> dict:
        message = body.get("message", {})
        call_id = str(dig(message, ("call", "id")) or "")
        if message.get("type") == "tool-calls":
            refusal = (
                await ensure(call_id, gather(message, VAPI_PATHS)) if call_id else "no call id"
            )
            results = []
            for request in message.get("toolCallList", []):
                args = dig(request, ("function", "arguments")) or {}
                args = json.loads(args) if isinstance(args, str) else args
                if refusal:
                    text = f"This call is not allowed: {refusal}. Say goodbye and end the call."
                else:
                    text = service.tool(
                        call_id, str(dig(request, ("function", "name"))), args
                    ).content
                results.append({"toolCallId": request.get("id"), "result": text})
            return {"results": results}
        if message.get("type") == "end-of-call-report":
            spoken = dig(message, ("artifact", "messages")) or []
            return close(
                call_id, [(str(m.get("role")), str(m.get("message") or "")) for m in spoken]
            )
        return {"ok": True}

    # Retell

    @api.post("/retell/tools/{name}", dependencies=secured)
    async def retell_tool(name: str, body: dict[str, Any] = Body(...)) -> dict:
        call_id = str(dig(body, ("call", "call_id")) or "")
        refusal = await ensure(call_id, gather(body, RETELL_PATHS)) if call_id else "no call id"
        if refusal:
            return {"ok": False, "result": f"This call is not allowed: {refusal}. End the call."}
        result = service.tool(call_id, name, body.get("args") or {})
        return {"ok": not result.is_error, "result": result.content}

    @api.post("/retell/webhook", dependencies=webhook)
    def retell_webhook(body: dict[str, Any] = Body(...)) -> dict:
        if body.get("event") != "call_ended":
            return {"ok": True}
        call = body.get("call", {})
        lines = [
            (str(t.get("role")), str(t.get("content") or ""))
            for t in call.get("transcript_object", [])
        ]
        return close(str(call.get("call_id", "")), lines)

    @api.websocket("/retell/{key}/llm-websocket/{call_id}")
    async def retell_llm(socket: WebSocket, key: str, call_id: str) -> None:
        """Retell's custom-LLM protocol. Retell cannot send headers, so the key is in the path."""
        if not key_matches(api_key, key):
            await socket.close(code=1008)
            return
        await socket.accept()
        await socket.send_json(
            {"response_type": "config", "config": {"auto_reconnect": True, "call_details": True}}
        )

        def response(number: int, content: str, **extra: Any) -> dict[str, Any]:
            return {
                "response_type": "response",
                "response_id": number,
                "content": content,
                "content_complete": True,
                **extra,
            }

        try:
            while True:
                event = await socket.receive_json()
                kind = event.get("interaction_type")
                if kind == "ping_pong":
                    await socket.send_json(
                        {"response_type": "ping_pong", "timestamp": event.get("timestamp")}
                    )
                elif kind == "call_details":
                    if call_id in service.sessions:  # a reconnect: the call is already open
                        continue
                    try:
                        values = gather(event, RETELL_PATHS)
                        _, opening = await service.start(
                            lead_from(service, values), call_id=call_id
                        )
                        await socket.send_json(response(0, opening.text))
                    except (Refused, HTTPException):
                        await socket.send_json(response(0, REFUSED_LINE, end_call=True))
                elif kind == "reminder_required":
                    await socket.send_json(response(event["response_id"], STILL_THERE))
                elif kind == "response_required":
                    said: list[str] = []
                    for utterance in event.get(
                        "transcript", []
                    ):  # the words since the agent last spoke
                        if utterance.get("role") == "user":
                            said.append(str(utterance.get("content", "")))
                        else:
                            said = []
                    if call_id not in service.sessions or not said:
                        await socket.send_json(response(event["response_id"], ""))
                        continue
                    reply = await service.hear(call_id, " ".join(said))
                    extra: dict[str, Any] = {}
                    number = os.environ.get("CALLAGENT_TRANSFER_NUMBER")
                    if reply.ended and service.session(call_id).call.state.transferred and number:
                        extra["transfer_number"] = number
                    elif reply.ended:
                        extra["end_call"] = True
                    await socket.send_json(response(event["response_id"], reply.text, **extra))
        except WebSocketDisconnect:
            return

    return api
