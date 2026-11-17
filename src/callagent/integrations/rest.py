"""A plain JSON interface to the call engine.

Two ways to use it.

*This server is the brain.* Start a call, then send each thing the contact says to
``/turn`` and speak what comes back. Any speech pipeline can do that.

*Your platform keeps its own model.* Start a call to get the prompt, the tools and the
opening line. Point the platform's tools at ``/tools/{name}``, send the contact's words to
``/heard`` and the agent's draft words to ``/guard/speech``. The rules are then enforced
even though the conversation is run elsewhere.
"""

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from callagent.integrations.common import lead_from, moment, not_found, require_key
from callagent.service import CallService, Refused, UnknownCall
from callagent.tools import END_REASONS


def openai_tools(service: CallService) -> list[dict[str, Any]]:
    """The agent's tools in the function format most platforms accept."""
    from callagent.tools import schemas

    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in schemas(service.playbook, ["YYYY-MM-DD"])
    ]


def router(service: CallService, api_key: str | None) -> APIRouter:
    api = APIRouter(prefix="/v1", dependencies=[Depends(require_key(api_key))])

    def call(call_id: str):
        try:
            return service.session(call_id).call
        except UnknownCall as error:
            raise not_found(error) from None

    @api.get("/config")
    def config() -> dict:
        playbook = service.playbook
        return {
            "company": playbook.company,
            "agent_name": playbook.agent_name,
            "opening_template": playbook.opening,
            "voicemail": playbook.voicemail_line(),
            "fallback": playbook.fallback,
            "system_prompt": service.prompt("guarded"),
            "tools": openai_tools(service),
            "end_reasons": list(END_REASONS),
        }

    @api.post("/gate")
    def gate(body: dict[str, Any] = Body(...)) -> dict:
        """May this lead be called right now? Ask before dialling."""
        decision = service.gate(lead_from(service, body), moment(body.get("now")))
        return {"allowed": decision.allowed, "reason": decision.reason}

    @api.post("/calls")
    async def start(body: dict[str, Any] = Body(...)) -> dict:
        """Open a call. Refused with 409 and a reason if the gate says no."""
        lead = lead_from(service, body)
        try:
            call_id, reply = await service.start(
                lead, call_id=body.get("call_id"), now=moment(body.get("now"))
            )
        except Refused as refusal:
            raise HTTPException(409, f"Not called: {refusal.reason}") from None
        opened = service.session(call_id).call
        return {
            "call_id": call_id,
            "opening": reply.text,
            "system_prompt": service.prompt("guarded"),
            "call_context": opened.context,
            "tools": opened.toolbox.schemas,
            "open_times": service.book.free_slots(),
        }

    @api.post("/calls/{call_id}/turn")
    async def turn(call_id: str, body: dict[str, Any] = Body(...)) -> dict:
        """The contact said ``text``. Returns what to say, and whether to hang up."""
        said = str(body.get("text", "")).strip()
        if not said:
            raise HTTPException(422, "text is required")
        try:
            reply = await service.hear(call_id, said)
            state = service.snapshot(call_id)
        except UnknownCall as error:
            raise not_found(error) from None
        return {
            "reply": reply.text,
            "ended": reply.ended,
            "withheld": reply.withheld,
            "outcome": state["outcome"],
            "transfer": state["transferred"] is not None,
        }

    @api.post("/calls/{call_id}/tools/{name}")
    def tool(call_id: str, name: str, body: dict[str, Any] = Body(default={})) -> dict:
        """Run one of the agent's tools for a model that lives somewhere else."""
        call(call_id)
        result = service.tool(call_id, name, body)
        return {
            "ok": not result.is_error,
            "result": result.content,
            "ended": service.session(call_id).call.ended,
        }

    @api.post("/calls/{call_id}/heard")
    def heard(call_id: str, body: dict[str, Any] = Body(...)) -> dict:
        """Check the contact's words. A request to stop calling is acted on here."""
        call(call_id)
        return service.heard(call_id, str(body.get("text", "")))

    @api.post("/guard/speech")
    def speech(body: dict[str, Any] = Body(...)) -> dict:
        """Check words before they are spoken. Returns the words that may be said."""
        return service.check_speech(str(body.get("text", "")))

    @api.post("/calls/{call_id}/end")
    def end(call_id: str, body: dict[str, Any] = Body(default={})) -> dict:
        """The call is over. Optionally send the transcript for a last do-not-call check."""
        call(call_id)
        for line in body.get("transcript", []):
            if line.get("role") in ("user", "contact", "customer"):
                service.heard(call_id, str(line.get("text") or line.get("message") or ""))
        outcome = service.finish(call_id, body.get("outcome"))
        return {**service.snapshot(call_id), "ended": True, "outcome": outcome}

    @api.get("/calls/{call_id}")
    def show(call_id: str) -> dict:
        call(call_id)
        return service.snapshot(call_id)

    return api
