# Retell, LiveKit, Pipecat and anything else

> Written from each project's documentation as it stood on 18 November 2026. The Retell
> adapter is tested against messages shaped the way that documentation describes, and has
> not been run against the live service. The LiveKit and Pipecat notes are sketches.

## Retell AI

Retell's custom LLM is a WebSocket protocol of its own, and this server implements it.

1. Set the agent's response engine to a custom LLM with
   `llm_websocket_url: wss://your-host/v1/retell/<CALLAGENT_API_KEY>/llm-websocket`.
   Retell adds the call id to the end of that URL. It cannot send headers, which is why
   the key is part of the path.
2. Place calls with `POST /v1/prepare/retell`, which gates the lead and returns the
   `metadata` and `retell_llm_dynamic_variables` to send to Retell's `create-phone-call`.
3. Point Retell's webhook at `https://your-host/v1/retell/webhook?key=...`. The
   `call_ended` event closes the call here.

On this protocol the server speaks first, so the opening line is always this server's.
Hang-ups use Retell's `end_call` field and transfers its `transfer_number`
(`CALLAGENT_TRANSFER_NUMBER`), sent with the agent's last words.

To keep Retell's own model, define custom functions that post to
`https://your-host/v1/retell/tools/{name}` with the API key in the function's headers.

Not handled: the `X-Retell-Signature` header is not verified.

## LiveKit Agents and Pipecat

These are frameworks: you run the worker that joins the call. Two ways to connect one.

**As an OpenAI-compatible model.** Point the framework's OpenAI model class at
`https://your-host/v1` (Pipecat's `OpenAILLMService` takes a `base_url`). The catch is the
call id: this server needs it on every request, and I could not confirm that either
framework lets you add a header or body field per call. If yours does, send `X-Call-Id`
and the lead headers.

**As three HTTP calls from your own code**, which always works. In the node or processor
where the framework would call a model, call this server:

```python
import httpx

server = httpx.AsyncClient(base_url="https://your-host", headers={"X-Api-Key": API_KEY})


async def on_call_connected(call_id: str, lead: dict) -> str:
    started = await server.post("/v1/calls", json={"call_id": call_id, **lead})
    if started.status_code == 409:  # the gate refused: hang up without speaking
        return ""
    return started.json()["opening"]  # say this first


async def on_contact_finished_speaking(call_id: str, text: str) -> tuple[str, bool, bool]:
    turn = (await server.post(f"/v1/calls/{call_id}/turn", json={"text": text})).json()
    return turn["reply"], turn["ended"], turn["transfer"]  # say it, then hang up or transfer
```

Call `POST /v1/gate` before you dial, so that a refused lead is never rung at all.

## Adding another platform

If it accepts an OpenAI-compatible custom model, add a `Profile` in
`src/callagent/integrations/openai_compat.py`: where the call id and lead are found in the
request, and what its hang-up and transfer tools are called. If it has its own protocol,
add an adapter in `platforms.py` that calls `CallService.start` and `CallService.hear`.
See [CONTRIBUTING.md](../../CONTRIBUTING.md).
