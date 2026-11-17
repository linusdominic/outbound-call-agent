# The JSON interface

Every endpoint is under `/v1`, takes and returns JSON, and needs the API key if one is set.

| Endpoint | What it does |
|---|---|
| `GET /v1/config` | The opening line template, the system prompt, the tools in OpenAI function format, the voicemail and fallback lines |
| `POST /v1/gate` | May this lead be called now? `{"allowed": false, "reason": "outside calling hours (22:10 EST for the contact)"}` |
| `POST /v1/calls` | Runs the gate and opens a call. Returns `call_id`, `opening`, `system_prompt`, `call_context`, `tools`, `open_times`. `409` with the reason if the gate refuses |
| `POST /v1/calls/{id}/turn` | The contact said `text`. Returns `reply`, `ended`, `outcome`, `transfer`, `withheld` |
| `POST /v1/calls/{id}/tools/{name}` | Runs one of the agent's tools with the JSON body as its arguments. Returns `ok`, `result`, `ended` |
| `POST /v1/calls/{id}/heard` | Checks the contact's words. A request to stop calling is acted on. Returns `asked_to_stop`, `asked_if_human` |
| `POST /v1/guard/speech` | Checks words before they are spoken. Returns the `text` that may be said and what was `withheld` |
| `POST /v1/calls/{id}/end` | The call is over. Optionally takes `transcript` (a list of `{"role", "text"}`) for a last do-not-call check. Returns the full record |
| `GET /v1/calls/{id}` | The record so far: answers, appointment, callback, do-not-call, events, transcript |

## Your own pipeline

If you run speech recognition and a voice yourself (LiveKit Agents, Pipecat, or your own
code), this server is three calls:

```bash
KEY="Authorization: Bearer $CALLAGENT_API_KEY"

# 1. Before dialling
curl -s localhost:8030/v1/gate -H "$KEY" -H 'Content-Type: application/json' \
  -d '{"first": "Jordan", "phone": "555-0142", "timezone": "America/New_York", "consent": true}'

# 2. When the call connects: say "opening"
curl -s localhost:8030/v1/calls -H "$KEY" -H 'Content-Type: application/json' \
  -d '{"call_id": "call-123", "first": "Jordan", "phone": "555-0142", "timezone": "America/New_York", "consent": true}'

# 3. Each time the contact finishes speaking: say "reply", hang up when "ended" is true
curl -s localhost:8030/v1/calls/call-123/turn -H "$KEY" -H 'Content-Type: application/json' \
  -d '{"text": "Yes, this is Jordan."}'
```

`transfer` is true when the call ended because the contact should be handed to a person.

## This server as the tools

Here the platform's own model holds the conversation, which keeps the platform's speed and
its handling of interruptions. This server keeps the rules.

1. Before dialling, call `/v1/gate`.
2. When the call starts, call `POST /v1/calls`. Give the platform's model the
   `system_prompt` plus the `call_context`, and have the platform say `opening` as its
   first message, word for word.
3. Define the six tools on the platform from `GET /v1/config`, each one a webhook to
   `POST /v1/calls/{id}/tools/{name}`. The response's `result` is what the model should
   read. `ok: false` means the action was refused and `result` says why.
4. After the call, send the transcript to `POST /v1/calls/{id}/end`.

Be clear about what this mode does and does not enforce:

| Rule | Enforced in this mode? |
|---|---|
| The gate: consent, do-not-call list, calling hours, attempts | Yes, if you call it before dialling |
| A booking must be an open time, after the contact is qualified | Yes, in the tool |
| A saved answer must be a real value | Yes, in the tool |
| The outcome comes from what was done | Yes |
| A request to stop calling | When the model calls `mark_do_not_call`, and again from the transcript you send to `/end`. Not in the moment by code, unless your platform can send each thing the contact says to `/heard` |
| The opening disclosure | Only as far as the platform says the configured first message |
| Never quoting a rate or promising approval | By the prompt only, unless your platform can pass the model's words through `/guard/speech` before speaking them |

If those last three matter to you, use way A, where every word passes through this server.
