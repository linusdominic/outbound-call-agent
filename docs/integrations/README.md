# Connecting a voice system

The call engine does not care what carries the words. A voice platform handles the phone
line, speech recognition and the voice. This server decides whether a call may be placed,
what the agent says, and what is recorded. There are three ways to connect them.

| Way | Use it when | The platform's own model | Guide |
|---|---|---|---|
| **A. This server is the model** | The platform accepts a custom language model that speaks the OpenAI Chat Completions protocol | Not used | [custom-llm.md](custom-llm.md) |
| **B. This server is the tools** | You want to keep the platform's model and voice latency, and have this server enforce the rules | Runs the conversation | [tools.md](tools.md) |
| **C. Your own pipeline** | You are wiring speech recognition and a voice together yourself | Not used | [tools.md](tools.md#your-own-pipeline) |

Platform notes: [ElevenLabs](elevenlabs.md), [Vapi](vapi.md), [others](other-platforms.md).

Whichever way you choose, three things stay the same.

**Ask the gate before you dial.** `POST /v1/gate` with the lead answers whether they may be
called right now, and why not. A platform will happily dial a number at 11 p.m. This
server will not let the call proceed, and the gate is how you avoid dialling at all.

**Send a call id with every request.** The state of a call lives on this server: what has
been saved, what is booked, who asked not to be called. The id is how a request finds it.

**Tell this server who is being called.** Either a `lead_id` that is in the lead list
(`CALLAGENT_LEADS`, a CSV file with the columns of `data/leads.csv`), or the lead spelled
out: `first`, `phone`, `timezone`, `consent`, and optionally `last`, `inquiry`,
`inquiry_date`. A lead sent without `consent` has none, and the gate refuses it.

## Running the server for an integration

```bash
cp .env.example .env     # set ANTHROPIC_API_KEY and CALLAGENT_API_KEY
uv run callagent serve --host 0.0.0.0 --port 8030
```

| Setting | What it does | Default |
|---|---|---|
| `CALLAGENT_API_KEY` | The shared secret every `/v1` request must send, as `Authorization: Bearer ...` or `X-Api-Key: ...`. Set it before the server is reachable from the internet | none: requests are let through |
| `CALLAGENT_DB` | A SQLite file for the do-not-call list, attempts, calendar and callbacks, so they outlive a restart | in memory |
| `CALLAGENT_LEADS` | A CSV lead list, so requests can name a `lead_id` | none |
| `CALLAGENT_MODEL` | The model that plays the agent | `claude-haiku-5-5` |
| `CALLAGENT_APP_BUDGET` | The most the server may spend on model calls, in dollars | 1.0 |

A voice platform has to reach the server over HTTPS. For a trial, a tunnel to your laptop
is enough. For anything real, put it behind a proper host with TLS.

## What the engine needs from you to be real

The repository ships with stand-ins that you would replace:

- **The calendar.** `CallService._open_calendar` invents three weekdays of open times.
  Replace it with a lookup against the loan officers' real calendar.
- **The lead list and consent.** A CSV file and a yes/no. A real system needs the consent
  record itself and checks against the national and state do-not-call registries.
- **The transfer.** The engine decides that a call should go to a person and why. Where
  it goes (a phone number, a queue) is configured on the voice platform.
- **The playbook.** `config/playbook.yml` is one imaginary lender's script.
