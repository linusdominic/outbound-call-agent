"""The demo page: be the contact, by voice or by typing, and watch what the agent records.

Calls placed here share one set of records for as long as the server runs. Ask not to be
called, start another call, and the pre-call check refuses it.
"""

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from callagent import voice
from callagent.agent import Call, Reply
from callagent.book import Book, Lead, business_days, slot_label
from callagent.gate import may_call
from callagent.llm import LLM
from callagent.personas import NOW, SLOT_TIMES
from callagent.playbook import ROOT, Playbook

STATIC = Path(__file__).parent / "static"


class NewCall(BaseModel):
    first: str = "Jordan"
    mode: str = "guarded"


class Said(BaseModel):
    text: str


@dataclass
class Session:
    call: Call
    clips: list[bytes] = field(default_factory=list)


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
    state: dict[str, Any] = {"book": None, "sessions": {}, "llm": llm}

    def model_client() -> LLM:
        if state["llm"] is None:  # made on first use, so the page loads without an API key
            state["llm"] = LLM(
                ledger_path=ROOT / "results" / "app_spend.json",
                budget_usd=float(os.environ.get("CALLAGENT_APP_BUDGET", "1.0")),
            )
        return state["llm"]

    def book() -> Book:
        if state["book"] is None:
            state["book"] = Book()
            days = business_days(NOW, 3)
            state["book"].open_slots([f"{d:%Y-%m-%d}T{t}" for d in days for t in SLOT_TIMES])
        return state["book"]

    def session(call_id: str) -> Session:
        if call_id not in state["sessions"]:
            raise HTTPException(404, "No such call")
        return state["sessions"][call_id]

    async def answer(call_id: str, found: Session, reply: Reply, heard: str | None, stt: float):
        clip, tts = None, 0.0
        if voice.available()["speak"]:
            started = time.perf_counter()
            found.clips.append(await asyncio.to_thread(speak, reply.text))
            tts = time.perf_counter() - started
            clip = f"/api/calls/{call_id}/clips/{len(found.clips) - 1}"
        call = found.call
        if reply.ended:
            book().record_attempt(call.lead.id, call.state.now, call.state.outcome)
        return {
            "call_id": call_id,
            "heard": heard,
            "reply": reply.text,
            "clip": clip,
            "ended": reply.ended,
            "withheld": reply.withheld,
            "seconds": {
                "listen": round(stt, 2),
                "think": round(reply.latency_s, 2),
                "speak": round(tts, 2),
            },
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
        lead = demo_lead(body.first)
        now = NOW.replace(tzinfo=lead.tz)
        decision = may_call(lead, now, book(), playbook)
        if not decision.allowed:
            return {"allowed": False, "reason": decision.reason}
        call = Call(model_client(), playbook, lead, book(), now, mode=body.mode)
        call_id = uuid.uuid4().hex[:12]
        found = state["sessions"][call_id] = Session(call)
        reply = await call.open()
        return {
            "allowed": True,
            "reason": decision.reason,
            **await answer(call_id, found, reply, None, 0.0),
        }

    @app.post("/api/calls/{call_id}/say")
    async def say(call_id: str, body: Said) -> dict:
        found = session(call_id)
        if found.call.ended:
            raise HTTPException(409, "The call has ended")
        if not body.text.strip():
            raise HTTPException(422, "Nothing was said")
        reply = await found.call.hear(body.text.strip())
        return await answer(call_id, found, reply, body.text.strip(), 0.0)

    @app.post("/api/calls/{call_id}/audio")
    async def audio(call_id: str, request: Request) -> dict:
        found = session(call_id)
        if found.call.ended:
            raise HTTPException(409, "The call has ended")
        if not voice.available()["listen"]:
            raise HTTPException(503, "Speech recognition is not set up. Type instead.")
        started = time.perf_counter()
        heard = await asyncio.to_thread(transcribe, await request.body())
        stt = time.perf_counter() - started
        if not heard:
            return {"call_id": call_id, "heard": "", "reply": None, "ended": False}
        reply = await found.call.hear(heard)
        return await answer(call_id, found, reply, heard, stt)

    @app.get("/api/calls/{call_id}/clips/{number}")
    def clip(call_id: str, number: int) -> Response:
        clips = session(call_id).clips
        if not 0 <= number < len(clips):
            raise HTTPException(404, "No such clip")
        return Response(clips[number], media_type="audio/wav")

    @app.post("/api/reset")
    def reset() -> dict:
        state["book"], state["sessions"] = None, {}
        return {"ok": True}

    return app


app = create_app()
