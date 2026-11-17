import pytest
from conftest import FakeLLM, said
from fastapi.testclient import TestClient

from callagent import voice
from callagent.api import create_app


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(voice, "available", lambda: {"listen": True, "speak": True})
    llm = FakeLLM(
        said("Thanks, Jordan. Is now a good time?"),
        said("Understood. Goodbye.", ("mark_do_not_call", {})),
    )
    app = create_app(
        llm,
        speak=lambda text: b"RIFF" + text.encode(),
        transcribe=lambda audio: "Yes, this is Jordan.",
    )
    return TestClient(app)


def test_page_and_status(client):
    assert "Outbound call agent" in client.get("/").text
    status = client.get("/api/status").json()
    assert status["agent"] == "Ava" and status["voice"] == {"listen": True, "speak": True}


def test_a_call_by_voice_then_text_then_blocked(client):
    start = client.post("/api/calls", json={"first": "Jordan"}).json()
    assert start["allowed"] and "AI assistant" in start["reply"] and not start["ended"]
    call = start["call_id"]
    assert client.get(start["clip"]).content.startswith(b"RIFF")

    heard = client.post(f"/api/calls/{call}/audio", content=b"webm-bytes").json()
    assert heard["heard"] == "Yes, this is Jordan." and heard["reply"].startswith("Thanks, Jordan")

    done = client.post(f"/api/calls/{call}/say", json={"text": "Stop calling me."}).json()
    assert done["ended"] and done["state"]["outcome"] == "do_not_call"
    assert done["state"]["do_not_call"] is True
    assert client.post(f"/api/calls/{call}/say", json={"text": "Hello?"}).status_code == 409

    again = client.post("/api/calls", json={"first": "Jordan"}).json()
    assert again == {"allowed": False, "reason": "on the do-not-call list"}

    client.post("/api/reset")
    assert client.app  # records cleared: the gate allows a call again
    # (no model turn is needed to open a guarded call)
    assert client.post("/api/calls", json={"first": "Jordan"}).json()["allowed"]


def test_bad_requests(client):
    assert client.post("/api/calls/nope/say", json={"text": "hi"}).status_code == 404
    assert client.post("/api/calls", json={"mode": "reckless"}).status_code == 422
    call = client.post("/api/calls", json={}).json()["call_id"]
    assert client.post(f"/api/calls/{call}/say", json={"text": "  "}).status_code == 422
    assert client.get(f"/api/calls/{call}/clips/9").status_code == 404
