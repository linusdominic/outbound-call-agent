# This server as the language model

Most voice platforms can use a "custom LLM": a server that speaks the OpenAI Chat
Completions protocol. The platform handles the phone line, speech recognition, the voice
and interruptions. Each time the contact stops talking it sends the conversation so far,
and this server answers with the agent's next line.

```
POST /v1/chat/completions                 any OpenAI-compatible client
POST /v1/elevenlabs/chat/completions      ElevenLabs Agents
POST /v1/vapi/chat/completions            Vapi
```

Streaming (`"stream": true`, server-sent events ending in `data: [DONE]`) and plain JSON
responses are both supported.

## What is different from an ordinary model

- **Only the newest thing the contact said is used.** The platform sends the whole
  conversation every time. This server keeps its own record of the call and ignores the
  rest, including whatever system prompt is configured on the platform.
- **A call id is required on every request.** It is how a request finds its call.
- **The first request starts the call.** It must carry the lead (see below). The gate runs
  then. If the lead may not be called, the answer is a short goodbye and a hang-up.
- **A request with nothing said by the contact yet returns the opening line.** If the
  platform speaks its own first message, configure that message to be the opening line
  word for word (`/v1/prepare/{platform}` returns it).
- **The same request sent twice is answered once.** Platforms retry, and regenerate after
  an interruption. The second answer is the stored first one.
- **When the call is over, the answer carries a tool call** that tells the platform to
  hang up or to transfer. The tool names differ by platform.

## Where the call id and the lead are read from

| Endpoint | Call id | Lead fields |
|---|---|---|
| generic | `X-Call-Id` header, or `call_id` or `user` in the body | `X-Lead-Id`, `X-First`, `X-Phone`, `X-Timezone`, `X-Consent` headers, or an object named `callagent`, `metadata` or `extra_body` in the body |
| ElevenLabs | `elevenlabs_extra_body.call_id` | `elevenlabs_extra_body` |
| Vapi | `call.id`, which Vapi sends | `metadata`, `call.metadata` or `call.assistantOverrides.metadata` |

The lead is either `lead_id` (looked up in `CALLAGENT_LEADS`) or `first`, `phone`,
`timezone` and `consent` spelled out.

## Hanging up and transferring

| Endpoint | Hang up | Transfer |
|---|---|---|
| generic | the tool named in `CALLAGENT_END_TOOL`, no arguments | the tool named in `CALLAGENT_TRANSFER_TOOL`, with the JSON in `CALLAGENT_TRANSFER_ARGS` |
| ElevenLabs | `end_call` with `reason` and `message` | `transfer_to_number` with `transfer_number`, `client_message`, `agent_message`, `reason` |
| Vapi | `endCall` | `transferCall` with `destination` |

`CALLAGENT_TRANSFER_NUMBER` is the number calls are transferred to. With no tool
configured, the last line is still spoken and `finish_reason` is `stop`: the platform then
needs another way to end the call, such as a silence timeout.

## Try it with the OpenAI client

```python
from openai import OpenAI

client = OpenAI(api_key="your CALLAGENT_API_KEY", base_url="http://localhost:8030/v1")
lead = {"first": "Jordan", "phone": "555-0142", "timezone": "America/New_York", "consent": True}
extra = {"call_id": "trial-1", "callagent": lead}

opening = client.chat.completions.create(model="callagent", messages=[], extra_body=extra)
print(opening.choices[0].message.content)

reply = client.chat.completions.create(
    model="callagent",
    messages=[
        {"role": "assistant", "content": opening.choices[0].message.content},
        {"role": "user", "content": "Yes, this is Jordan."},
    ],
    extra_body=extra,
)
print(reply.choices[0].message.content)
```

This is how the endpoint is tested: `tests/test_openai_compat.py` drives it with the
OpenAI client library, and I ran a full call through it against the real model.

## Limits

- **Speed.** The reply is not streamed from the model word by word, because every
  sentence is checked before it is spoken. The first words arrive after the model has
  finished, about 1.4 seconds on average. The first chunk of the stream is sent at once so
  the connection is not idle.
- **Interruptions.** If the contact talks over the agent, the platform may cut the
  agent's line short. This server still believes the whole line was said.
- **One process.** Calls in progress are held in memory. The do-not-call list, attempts,
  calendar and callbacks go to `CALLAGENT_DB` and survive a restart. A call in progress
  does not, and two server processes do not share calls.
