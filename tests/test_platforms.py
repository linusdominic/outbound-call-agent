"""The platform adapters, driven by requests shaped the way each platform's documentation
describes them. These are contract tests against that documentation, not against the live
services."""

import json

from conftest import NOW, FakeLLM, said
from fastapi import FastAPI
from fastapi.testclient import TestClient

from callagent.integrations import openai_compat, platforms, rest
from callagent.service import CallService

AT = "2026-11-18T10:30-05:00"
LEAD = {"lead_id": "L1", "first": "Jordan", "last": "Rivera", "phone": "+15550142", "consent": True}
OPENING = {"role": "assistant", "content": "Hi, this is Ava..."}


def client_for(
    playbook, *turns, key=None, env=None, monkeypatch=None
) -> tuple[TestClient, CallService]:
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    llm = FakeLLM(*turns)
    service = CallService(playbook, lambda: llm, clock=lambda: NOW)
    app = FastAPI()
    profiles = [openai_compat.elevenlabs_profile(), openai_compat.vapi_profile()]
    for router in (
        rest.router(service, key),
        openai_compat.router(service, key, profiles),
        platforms.router(service, key),
    ):
        app.include_router(router)
    return TestClient(app), service


def events(response) -> list[dict]:
    lines = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    return [json.loads(line) for line in lines[:-1]]


def test_prepare_gates_the_lead_and_builds_each_platforms_call_request(playbook):
    client, _ = client_for(playbook)
    eleven = client.post("/v1/prepare/elevenlabs", json={**LEAD, "now": AT, "call_id": "c1"}).json()
    start = eleven["request"]["conversation_initiation_client_data"]
    assert eleven["allowed"] and eleven["request"]["to_number"] == "+15550142"
    assert start["custom_llm_extra_body"]["call_id"] == "c1"
    assert start["custom_llm_extra_body"]["consent"] is True
    assert start["conversation_config_override"]["agent"]["first_message"] == eleven["opening"]
    assert "AI assistant" in eleven["opening"] and "recorded" in eleven["opening"]

    vapi = client.post("/v1/prepare/vapi", json={**LEAD, "now": AT}).json()["request"]
    assert vapi["customer"] == {"number": "+15550142", "name": "Jordan Rivera", "externalId": "L1"}
    assert vapi["assistantOverrides"]["metadata"]["lead_id"] == "L1"

    retell = client.post("/v1/prepare/retell", json={**LEAD, "now": AT}).json()["request"]
    assert retell["metadata"]["first"] == "Jordan"
    assert all(isinstance(v, str) for v in retell["retell_llm_dynamic_variables"].values())

    late = client.post(
        "/v1/prepare/elevenlabs", json={**LEAD, "now": "2026-11-18T23:00-05:00"}
    ).json()
    assert late["allowed"] is False and "request" not in late
    assert client.post("/v1/prepare/carrier-pigeon", json={**LEAD, "now": AT}).status_code == 404


def test_elevenlabs_custom_llm_hangs_up_with_the_farewell_inside_end_call(playbook):
    client, service = client_for(playbook, said("Understood. You're off our list. Goodbye."))
    body = {
        "model": "callagent",
        "stream": True,
        "messages": [OPENING, {"role": "user", "content": "Stop calling me."}],
        "elevenlabs_extra_body": {"call_id": "c1", **LEAD},
    }
    chunks = events(client.post("/v1/elevenlabs/chat/completions", json=body))
    deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
    assert "".join(delta.get("content") or "" for delta in deltas) == ""  # said by the tool, once
    call = next(delta["tool_calls"][0] for delta in deltas if "tool_calls" in delta)
    assert call["function"]["name"] == "end_call"
    assert json.loads(call["function"]["arguments"]) == {
        "reason": "do_not_call",
        "message": "Understood. You're off our list. Goodbye.",
    }
    assert service.book.on_do_not_call("+15550142")
    # The other path the platform might append works too.
    assert client.post("/v1/elevenlabs/v1/chat/completions", json=body).status_code == 200


def test_elevenlabs_transfer_carries_a_summary_for_the_loan_officer(playbook, monkeypatch):
    client, _ = client_for(
        playbook,
        said("Got it.", ("record_answer", {"field": "goal", "value": "refinance"})),
        said(
            "I'll put you through now.", ("transfer_to_officer", {"reason": "wants a rate quote"})
        ),
        env={"CALLAGENT_TRANSFER_NUMBER": "+15550100"},
        monkeypatch=monkeypatch,
    )
    extra = {"elevenlabs_extra_body": {"call_id": "c2", **LEAD}}
    messages = [OPENING, {"role": "user", "content": "I want to refinance."}]
    client.post("/v1/elevenlabs/chat/completions", json={"messages": messages, **extra})
    messages += [
        {"role": "assistant", "content": "Got it."},
        {"role": "user", "content": "Get me a person."},
    ]
    reply = client.post(
        "/v1/elevenlabs/chat/completions", json={"messages": messages, **extra}
    ).json()
    call = reply["choices"][0]["message"]["tool_calls"][0]["function"]
    assert call["name"] == "transfer_to_number"
    assert json.loads(call["arguments"]) == {
        "transfer_number": "+15550100",
        "client_message": "I'll put you through now.",
        "agent_message": "Jordan Rivera: goal refinance. Reason: wants a rate quote.",
        "reason": "wants a rate quote",
    }


def test_elevenlabs_post_call_webhook_closes_the_call_and_catches_a_missed_opt_out(playbook):
    client, service = client_for(playbook, said("Thanks. Is now a good time?"))
    extra = {"elevenlabs_extra_body": {"call_id": "c3", **LEAD}}
    messages = [OPENING, {"role": "user", "content": "Yes, this is Jordan."}]
    client.post("/v1/elevenlabs/chat/completions", json={"messages": messages, **extra})
    webhook = {
        "type": "post_call_transcription",
        "event_timestamp": 1795000000,
        "data": {
            "agent_id": "agent_1",
            "conversation_id": "conv_1",
            "status": "done",
            "transcript": [
                {"role": "agent", "message": "Hi, this is Ava...", "time_in_call_secs": 0},
                {"role": "user", "message": "Yes, this is Jordan.", "time_in_call_secs": 4},
                {"role": "user", "message": "Actually, lose my number.", "time_in_call_secs": 9},
            ],
            "conversation_initiation_client_data": {
                "dynamic_variables": {"call_id": "c3", "lead_id": "L1"},
            },
        },
    }
    done = client.post("/v1/elevenlabs/post-call", json=webhook).json()
    assert done == {"ok": True, "call_id": "c3", "outcome": "do_not_call"}
    assert service.book.on_do_not_call("+15550142")
    unknown = {**webhook, "data": {**webhook["data"], "conversation_initiation_client_data": {}}}
    assert client.post("/v1/elevenlabs/post-call", json=unknown).json()["ok"] is False


def test_elevenlabs_failed_dial_counts_as_an_attempt(playbook):
    client, service = client_for(playbook)
    webhook = {
        "type": "call_initiation_failure",
        "data": {
            "failure_reason": "no-answer",
            "conversation_initiation_client_data": {"dynamic_variables": {"lead_id": "L1"}},
        },
    }
    assert client.post("/v1/elevenlabs/post-call", json=webhook).json() == {"ok": True}
    assert [outcome for _, outcome in service.book.attempts("L1")] == ["no_answer"]


def test_vapi_custom_llm_keys_the_call_on_vapis_own_id(playbook, monkeypatch):
    client, service = client_for(
        playbook,
        said("Understood. Goodbye."),
        env={"CALLAGENT_TRANSFER_NUMBER": "+15550100"},
        monkeypatch=monkeypatch,
    )
    body = {
        "model": "callagent",
        "messages": [{"role": "system", "content": "ignored"}],
        "call": {"id": "vapi-call-1", "type": "outboundPhoneCall"},
        "customer": {"number": "+15550142"},
        "metadata": LEAD,
    }
    first = client.post(
        "/v1/vapi/chat/completions", json=body
    ).json()  # model-generated first message
    assert first["choices"][0]["message"]["content"] == playbook.opening_line("Jordan")
    body["messages"] += [
        {"role": "assistant", "content": first["choices"][0]["message"]["content"]},
        {"role": "user", "content": "Not interested. Take me off your list."},
    ]
    last = client.post("/v1/vapi/chat/completions/chat/completions", json=body).json()
    message = last["choices"][0]["message"]
    assert message["content"] == "Understood. Goodbye."  # spoken, then the hang-up tool
    assert message["tool_calls"][0]["function"]["name"] == "endCall"
    assert "vapi-call-1" in service.sessions


def test_vapi_can_be_switched_to_the_older_function_call_shape(playbook, monkeypatch):
    client, _ = client_for(
        playbook,
        said("Connecting you.", ("transfer_to_officer", {"reason": "asked"})),
        env={"CALLAGENT_VAPI_FUNCTION_CALL": "1", "CALLAGENT_TRANSFER_NUMBER": "+15550100"},
        monkeypatch=monkeypatch,
    )
    body = {
        "messages": [OPENING, {"role": "user", "content": "A person, please."}],
        "call": {"id": "v2"},
        "metadata": LEAD,
    }
    message = client.post("/v1/vapi/chat/completions", json=body).json()["choices"][0]["message"]
    assert message["function_call"]["name"] == "transferCall"
    assert json.loads(message["function_call"]["arguments"]) == {"destination": "+15550100"}


def test_vapi_webhook_runs_tools_and_closes_the_call(playbook):
    client, service = client_for(playbook)
    call = {"id": "v3", "assistantOverrides": {"metadata": LEAD}}

    def tools(*requests):
        listed = [
            {"id": f"tc{i}", "type": "function", "function": {"name": name, "arguments": args}}
            for i, (name, args) in enumerate(requests)
        ]
        message = {"type": "tool-calls", "call": call, "toolCallList": listed}
        return client.post("/v1/vapi/webhook", json={"message": message}).json()["results"]

    early = tools(("book_appointment", {"slot": "2026-11-19T10:00"}))
    assert early[0]["toolCallId"] == "tc0" and "qualifying questions" in early[0]["result"]
    saved = tools(
        ("record_answer", {"field": "goal", "value": "purchase"}),
        (
            "record_answer",
            json.dumps({"field": "credit", "value": "good"}),
        ),  # arguments as a string
        ("book_appointment", {"slot": "2026-11-19T10:00"}),
    )
    assert [r["result"] for r in saved][:2] == ["Saved.", "Saved."] and "Booked" in saved[2][
        "result"
    ]

    report = {
        "type": "end-of-call-report",
        "endedReason": "customer-ended-call",
        "call": call,
        "artifact": {
            "messages": [
                {"role": "bot", "message": "Hi"},
                {"role": "user", "message": "Thanks, bye."},
            ]
        },
    }
    done = client.post("/v1/vapi/webhook", json={"message": report}).json()
    assert done == {"ok": True, "call_id": "v3", "outcome": "appointment_booked"}
    assert client.post("/v1/vapi/webhook", json={"message": {"type": "status-update"}}).json() == {
        "ok": True
    }


def test_vapi_tool_for_a_lead_the_gate_refuses_tells_the_model_to_stop(playbook):
    client, _ = client_for(playbook)
    message = {
        "type": "tool-calls",
        "call": {"id": "v4", "metadata": {"first": "Sam", "phone": "+15550199"}},  # no consent
        "toolCallList": [{"id": "a", "function": {"name": "record_answer", "arguments": {}}}],
    }
    result = client.post("/v1/vapi/webhook", json={"message": message}).json()["results"][0][
        "result"
    ]
    assert "no consent" in result and "end the call" in result


def test_retell_websocket_runs_a_whole_call(playbook, monkeypatch):
    client, service = client_for(
        playbook,
        said("Thanks, Jordan. Is now a good time?"),
        said("Understood. Goodbye.", ("end_call", {"reason": "declined"})),
        key="s3cret",
    )
    with client.websocket_connect("/v1/retell/s3cret/llm-websocket/retell-1") as socket:
        assert socket.receive_json() == {
            "response_type": "config",
            "config": {"auto_reconnect": True, "call_details": True},
        }
        socket.send_json({"interaction_type": "ping_pong", "timestamp": 123})
        assert socket.receive_json() == {"response_type": "ping_pong", "timestamp": 123}

        details = {"call_id": "retell-1", "direction": "outbound", "metadata": LEAD}
        socket.send_json({"interaction_type": "call_details", "call": details})
        begin = socket.receive_json()
        assert begin["response_id"] == 0 and begin["content"] == playbook.opening_line("Jordan")

        transcript = [
            {"role": "agent", "content": begin["content"]},
            {"role": "user", "content": "Yes."},
        ]
        socket.send_json({"interaction_type": "update_only", "transcript": transcript})
        socket.send_json(
            {"interaction_type": "response_required", "response_id": 1, "transcript": transcript}
        )
        first = socket.receive_json()
        assert (
            first["response_id"] == 1 and first["content"] == "Thanks, Jordan. Is now a good time?"
        )
        assert first["content_complete"] is True and "end_call" not in first

        socket.send_json({"interaction_type": "reminder_required", "response_id": 2})
        assert socket.receive_json()["content"] == platforms.STILL_THERE

        transcript += [
            {"role": "agent", "content": first["content"]},
            {"role": "user", "content": "No thanks."},
            {"role": "user", "content": "I'm not interested."},
        ]
        socket.send_json(
            {"interaction_type": "response_required", "response_id": 3, "transcript": transcript}
        )
        last = socket.receive_json()
        assert last["content"] == "Understood. Goodbye." and last["end_call"] is True
    assert service.snapshot("retell-1")["outcome"] == "declined"
    heard = [
        line["text"]
        for line in service.snapshot("retell-1")["transcript"]
        if line["speaker"] == "contact"
    ]
    assert heard == ["Yes.", "No thanks. I'm not interested."]


def test_retell_websocket_refuses_a_wrong_key_and_a_lead_without_consent(playbook):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    client, _ = client_for(playbook, key="s3cret")
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/v1/retell/wrong/llm-websocket/x"),
    ):
        pass
    with client.websocket_connect("/v1/retell/s3cret/llm-websocket/r2") as socket:
        socket.receive_json()
        call = {"call_id": "r2", "metadata": {"first": "Sam", "phone": "+15550199"}}
        socket.send_json({"interaction_type": "call_details", "call": call})
        refused = socket.receive_json()
        assert refused["end_call"] is True and refused["content"] == openai_compat.REFUSED_LINE


def test_retell_custom_function_and_call_ended_webhook(playbook):
    client, _ = client_for(playbook)
    call = {"call_id": "r3", "metadata": LEAD}
    body = {"name": "record_answer", "call": call, "args": {"field": "goal", "value": "cash out"}}
    assert client.post("/v1/retell/tools/record_answer", json=body).json() == {
        "ok": True,
        "result": "Saved.",
    }
    ended = {
        "event": "call_ended",
        "call": {
            **call,
            "transcript_object": [{"role": "user", "content": "Please stop calling me."}],
        },
    }
    assert client.post("/v1/retell/webhook", json=ended).json()["outcome"] == "do_not_call"
    assert client.post(
        "/v1/retell/webhook", json={"event": "call_started", "call": call}
    ).json() == {"ok": True}


def test_webhooks_take_the_key_in_a_header_or_in_the_url(playbook):
    client, _ = client_for(playbook, key="s3cret")
    body = {"message": {"type": "status-update"}}
    assert client.post("/v1/vapi/webhook", json=body).status_code == 401
    assert client.post("/v1/vapi/webhook?key=s3cret", json=body).status_code == 200
    assert (
        client.post("/v1/vapi/webhook", json=body, headers={"X-Vapi-Secret": "s3cret"}).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/vapi/webhook", json=body, headers={"Authorization": "Bearer s3cret"}
        ).status_code
        == 200
    )
    assert (
        client.post("/v1/prepare/vapi?key=s3cret", json=LEAD).status_code == 401
    )  # headers only here
