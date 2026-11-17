import pytest
from conftest import FakeLLM, said
from fastapi import FastAPI
from fastapi.testclient import TestClient

from callagent.integrations import rest
from callagent.service import CallService

AT = "2026-11-18T10:30-05:00"
LEAD = {"first": "Jordan", "phone": "555-0142", "consent": True, "lead_id": "L1"}


def client_for(playbook, *turns, key=None) -> TestClient:
    llm = FakeLLM(*turns)
    app = FastAPI()
    app.include_router(rest.router(CallService(playbook, lambda: llm), key))
    return TestClient(app)


def test_gate_answers_before_anything_is_dialled(playbook):
    client = client_for(playbook)
    assert client.post("/v1/gate", json={**LEAD, "now": AT}).json() == {
        "allowed": True,
        "reason": "ok",
    }
    late = client.post("/v1/gate", json={**LEAD, "now": "2026-11-18T22:00-05:00"}).json()
    assert late["allowed"] is False and "outside calling hours" in late["reason"]
    no_consent = client.post("/v1/gate", json={"first": "Sam", "phone": "555-0100", "now": AT})
    assert no_consent.json()["reason"] == "no consent to automated calls on record"
    assert client.post("/v1/gate", json={"lead_id": "nobody"}).status_code == 422
    assert client.post("/v1/gate", json={**LEAD, "now": "2026-11-18T10:30"}).status_code == 422


def test_this_server_as_the_brain(playbook):
    client = client_for(
        playbook,
        said("Thanks, Jordan. Is now a good time?"),
        said("Connecting you now.", ("transfer_to_officer", {"reason": "asked for a person"})),
    )
    started = client.post("/v1/calls", json={**LEAD, "now": AT, "call_id": "abc"}).json()
    assert started["call_id"] == "abc" and "AI assistant" in started["opening"]
    assert len(started["open_times"]) == 9 and "Open times" in started["call_context"]

    first = client.post("/v1/calls/abc/turn", json={"text": "Yes, speaking."}).json()
    assert first == {
        "reply": "Thanks, Jordan. Is now a good time?",
        "ended": False,
        "withheld": [],
        "outcome": None,
        "transfer": False,
    }
    last = client.post("/v1/calls/abc/turn", json={"text": "Can I talk to a person?"}).json()
    assert last["ended"] and last["transfer"] and last["outcome"] == "transferred"
    assert client.get("/v1/calls/abc").json()["transcript"][-1]["text"] == "Connecting you now."
    assert client.post("/v1/calls/abc/turn", json={"text": ""}).status_code == 422
    assert client.post("/v1/calls/zzz/turn", json={"text": "hi"}).status_code == 404


def test_a_refused_call_is_a_409_with_the_reason(playbook):
    client = client_for(playbook)
    refused = client.post("/v1/calls", json={**LEAD, "now": "2026-11-18T06:00-05:00"})
    assert refused.status_code == 409 and "outside calling hours" in refused.json()["detail"]


def test_a_platform_that_keeps_its_own_model(playbook):
    client = client_for(playbook)
    config = client.get("/v1/config").json()
    assert {tool["function"]["name"] for tool in config["tools"]} >= {
        "book_appointment",
        "end_call",
    }
    assert "{first}" in config["opening_template"] and "Approved answers" in config["system_prompt"]

    call = client.post("/v1/calls", json={**LEAD, "now": AT}).json()["call_id"]
    slot = client.get(f"/v1/calls/{call}").json()
    assert slot["ended"] is False

    refused = client.post(
        f"/v1/calls/{call}/tools/book_appointment", json={"slot": "2026-11-19T10:00"}
    ).json()
    assert refused["ok"] is False and "qualifying questions" in refused["result"]
    for field, value in (("goal", "refinance"), ("credit", "good")):
        saved = client.post(
            f"/v1/calls/{call}/tools/record_answer", json={"field": field, "value": value}
        )
        assert saved.json()["ok"]
    booked = client.post(
        f"/v1/calls/{call}/tools/book_appointment", json={"slot": "2026-11-19T10:00"}
    ).json()
    assert booked["ok"] and "Booked" in booked["result"]

    draft = client.post(
        "/v1/guard/speech", json={"text": "You're guaranteed to be approved!"}
    ).json()
    assert draft["changed"] and draft["withheld"] == ["promises approval"]

    heard = client.post(
        f"/v1/calls/{call}/heard", json={"text": "Actually, stop calling me."}
    ).json()
    assert heard["asked_to_stop"] is True
    done = client.post(f"/v1/calls/{call}/end", json={}).json()
    assert done["outcome"] == "do_not_call" and done["do_not_call"] is True
    assert (
        client.post("/v1/gate", json={**LEAD, "now": AT}).json()["reason"]
        == "on the do-not-call list"
    )


def test_the_transcript_sent_at_the_end_is_checked_for_a_missed_opt_out(playbook):
    client = client_for(playbook)
    call = client.post("/v1/calls", json={**LEAD, "now": AT}).json()["call_id"]
    transcript = [
        {"role": "agent", "text": "Hi, is this Jordan?"},
        {"role": "user", "text": "Yes. Please take me off your list."},
    ]
    done = client.post(f"/v1/calls/{call}/end", json={"transcript": transcript}).json()
    assert done["do_not_call"] is True and done["outcome"] == "do_not_call"


@pytest.mark.parametrize("headers", [{"Authorization": "Bearer s3cret"}, {"X-Api-Key": "s3cret"}])
def test_a_configured_key_is_required(playbook, headers):
    client = client_for(playbook, key="s3cret")
    assert client.get("/v1/config").status_code == 401
    assert client.get("/v1/config", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/v1/config", headers=headers).status_code == 200
