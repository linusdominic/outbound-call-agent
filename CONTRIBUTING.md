# Contributing

Thanks for looking. The project is small on purpose, and changes that keep it that way are
the easiest to accept.

## Set up

```bash
uv sync
make test     # no network, microphone or API key
make lint
```

Everything except `callagent simulate` and the demo page runs without an API key.

## The one rule

**Say only what you ran.** If you add an adapter for a service you could not test against,
say so at the top of its guide, the way the existing guides do. If a claim in the README
depends on a number, the number has to come from a command in the repository.

## Where things go

| You want to | Change |
|---|---|
| Change what the agent may say or do | `config/playbook.yml` |
| Change who may be called and when | `src/callagent/gate.py`, with a test in `tests/test_gate.py` |
| Add a banned phrase or an opt-out wording | `src/callagent/guard.py`, with a test in `tests/test_guard.py` |
| Add an action the agent can take | `src/callagent/tools.py`: a schema, a handler, and the rule it enforces |
| Connect a real calendar | `CallService._open_calendar` and the slot methods in `book.py` |
| Add a voice platform | See below |

## Adding a voice platform

The call engine is `CallService` in `src/callagent/service.py`. It knows nothing about
HTTP or vendors. An adapter turns a platform's requests into four calls: `gate`, `start`,
`hear` and `finish`.

1. **If the platform accepts an OpenAI-compatible custom model**, add a `Profile` in
   `integrations/openai_compat.py` and list it in `integrations/__init__.py`. A profile
   says where the call id and the lead are found in the request body, and what the
   platform's hang-up and transfer tools are called.
2. **If it has its own protocol**, add the adapter to `integrations/platforms.py`. The
   Retell WebSocket handler is the example to copy.
3. **Add its start-a-call request** to `call_request` in `platforms.py`, so that
   `/v1/prepare/{platform}` can gate a lead and build it.
4. **Write contract tests** in `tests/test_platforms.py` with request bodies copied from
   the platform's documentation, and link the documentation page in the guide.
5. **Write a guide** in `docs/integrations/`, starting with what you tested and how.

If you have an account on ElevenLabs, Vapi or Retell and can run the existing adapters
against the live service, a report of what worked and what did not is the most useful
contribution there is right now.

## Changing the agent's behaviour

The prompt, the guard patterns and the scoring were frozen before the held-out evaluation.
If you change any of them, re-run the evaluation (`make simulate`, `make
simulate-baseline`, `make report`, about $1.15) and commit the new records with the change,
or say in the pull request that the published numbers no longer describe the code.
