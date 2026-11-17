"""The web server: the demo page, and the endpoints other voice systems connect to.

The demo page lets you be the contact, by voice or by typing. Calls placed there share one
set of records for as long as the server runs: ask not to be called, start another call,
and the pre-call check refuses it.

The endpoints under ``/v1`` are for third-party voice platforms. They are described in
``docs/integrations``.
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from callagent import voice
from callagent.agent import Reply
from callagent.book import Book, Lead, slot_label
from callagent.llm import LLM
from callagent.personas import NOW
from callagent.playbook import ROOT, Playbook
from callagent.service import CallService, Refused, UnknownCall, load_leads

STATIC = Path(__file__).parent / "static"


class NewCall(BaseModel):
    first: str = "Jordan"
    mode: str = "guarded"


class Said(BaseModel):
    text: str


def demo_lead(first: str) -> Lead:
    return Lead(
        id="demo",
        first=first.strip()[:30] or "Jordan",
        last="Rivera",
        phone="555-0142",
        timezone="America/New_York",
        consent=True,
        inquiry="Asked about refinancing through the website form.",
        inquiry_date="12 November 2026",
    )


def create_app(llm: LLM | None = None, speak=voice.speak, transcribe=voice.transcribe) -> FastAPI:
    app = FastAPI(title="Outbound call agent", docs_url=None, redoc_url=None)
    playbook = Playbook.load()
    made: dict[str, LLM] = {}

    def model_client() -> LLM:
        if "llm" not in made:  # made on first use, so the page loads without an API key
            made["llm"] = llm or LLM(
                ledger_path=ROOT / "results" / "app_spend.json",
                budget_usd=float(os.environ.get("CALLAGENT_APP_BUDGET", "1.0")),
            )
        return made["llm"]

    # The demo always runs at the evaluation's fixed time, so the gate never says "too late".
    demo = CallService(playbook, model_client, clock=lambda: NOW.replace(tzinfo=demo_lead("").tz))
    clips: dict[str, list[bytes]] = {}

    # Third-party platforms get real time, records that can outlive the process, and a lead list.
    leads_file = os.environ.get("CALLAGENT_LEADS")
    live = CallService(
        playbook,
        model_client,
        book=Book(os.environ.get("CALLAGENT_DB", ":memory:")),
        leads=load_leads(Path(leads_file)) if leads_file else {},
        model=os.environ.get("CALLAGENT_MODEL", "claude-haiku-5-5"),
    )
    app.state.service = live
    from callagent.integrations import routers

    for router in routers(live, os.environ.get("CALLAGENT_API_KEY")):
        app.include_router(router)

    def session(call_id: str):
        try:
            return demo.session(call_id)
        except UnknownCall:
            raise HTTPException(404, "No such call") from None

    async def answer(call_id: str, reply: Reply, heard: str | None, stt: float) -> dict[str, Any]:
        clip, tts = None, 0.0
        if voice.available()["speak"]:
            started = time.perf_counter()
            clips.setdefault(call_id, []).append(await asyncio.to_thread(speak, reply.text))
            tts = time.perf_counter() - started
            clip = f"/api/calls/{call_id}/clips/{len(clips[call_id]) - 1}"
        call = demo.session(call_id).call
        if reply.ended:
            demo.finish(call_id)
        seconds = {"listen": stt, "think": reply.latency_s, "speak": tts}
        return {
            "call_id": call_id,
            "heard": heard,
            "reply": reply.text,
            "clip": clip,
            "ended": reply.ended,
            "withheld": reply.withheld,
            "seconds": {key: round(value, 2) for key, value in seconds.items()},
            "state": {
                "outcome": call.state.outcome if reply.ended else None,
                "answers": call.state.answers,
                "appointment": slot_label(call.state.appointment)
                if call.state.appointment
                else None,
                "callback": list(call.state.callback) if call.state.callback else None,
                "do_not_call": call.state.do_not_call,
                "events": call.state.events,
            },
            "spent_usd": round(model_client().ledger.total, 4),
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status() -> dict:
        return {
            "company": playbook.company,
            "agent": playbook.agent_name,
            "voice": voice.available(),
            "now": f"{NOW:%A %d %B %Y, %H:%M}",
        }

    @app.post("/api/calls")
    async def start(body: NewCall) -> dict:
        if body.mode not in ("guarded", "prompt_only"):
            raise HTTPException(422, "mode must be guarded or prompt_only")
        try:
            call_id, reply = await demo.start(demo_lead(body.first), mode=body.mode)
        except Refused as refusal:
            return {"allowed": False, "reason": refusal.reason}
        return {"allowed": True, "reason": "ok", **await answer(call_id, reply, None, 0.0)}

    @app.post("/api/calls/{call_id}/say")
    async def say(call_id: str, body: Said) -> dict:
        if session(call_id).call.ended:
            raise HTTPException(409, "The call has ended")
        if not body.text.strip():
            raise HTTPException(422, "Nothing was said")
        reply = await demo.hear(call_id, body.text.strip())
        return await answer(call_id, reply, body.text.strip(), 0.0)

    @app.post("/api/calls/{call_id}/audio")
    async def audio(call_id: str, request: Request) -> dict:
        if session(call_id).call.ended:
            raise HTTPException(409, "The call has ended")
        if not voice.available()["listen"]:
            raise HTTPException(503, "Speech recognition is not set up. Type instead.")
        started = time.perf_counter()
        heard = await asyncio.to_thread(transcribe, await request.body())
        stt = time.perf_counter() - started
        if not heard:
            return {"call_id": call_id, "heard": "", "reply": None, "ended": False}
        return await answer(call_id, await demo.hear(call_id, heard), heard, stt)

    @app.get("/api/calls/{call_id}/clips/{number}")
    def clip(call_id: str, number: int) -> Response:
        session(call_id)
        if not 0 <= number < len(clips.get(call_id, [])):
            raise HTTPException(404, "No such clip")
        return Response(clips[call_id][number], media_type="audio/wav")

    @app.post("/api/reset")
    def reset() -> dict:
        demo.book, demo.sessions = Book(), {}
        clips.clear()
        return {"ok": True}

    return app


app = create_app()
