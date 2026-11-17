# Vapi

> Written from the Vapi documentation as it stood on 18 November 2026, and tested against
> requests shaped the way that documentation describes. I have not run it against the live
> service. Points the documentation left open are marked **unconfirmed**.

## This server as the model (recommended)

1. **Run the server** where Vapi can reach it over HTTPS, with `CALLAGENT_API_KEY` and
   `CALLAGENT_TRANSFER_NUMBER` set.
2. **Set the assistant's model** to a custom LLM:

   ```json
   {
     "provider": "custom-llm",
     "url": "https://your-host/v1/vapi",
     "model": "callagent",
     "metadataSendMode": "variable",
     "tools": [
       {"type": "endCall"},
       {"type": "transferCall", "destinations": [{"type": "number", "number": "+15550100"}]}
     ]
   }
   ```

   Vapi's reference says `/chat/completions` is appended to `url`, and its guide shows a
   URL that already ends in it. This server answers on both, so either form works.
   Add a Custom LLM credential holding `CALLAGENT_API_KEY`.
3. **Let this server write the first message.** Set `firstMessageMode` to
   `assistant-speaks-first-with-model-generated-message`. Vapi then asks this server for
   the first line and gets the opening with its disclosures, word for word. Leave
   `endCallMessage` empty: the agent's own last line is the goodbye.
4. **Place a call.** `POST /v1/prepare/vapi` gates the lead and returns `request`: the
   `customer` block and `assistantOverrides.metadata` holding the lead. Add your
   `assistantId` and `phoneNumberId` and send it to `POST https://api.vapi.ai/call`.
   The call is keyed on Vapi's own `call.id`, which it sends with every request.
5. **Send server messages** to `https://your-host/v1/vapi/webhook`, with the key as a
   bearer credential, in an `X-Vapi-Secret` header, or as `?key=`. The end-of-call report
   closes the call here and checks the transcript once more for a request to stop.

**Unconfirmed:** Vapi's custom-LLM example emits the older `function_call` shape for
`transferCall`, and the documentation does not show the `tool_calls` shape for its
built-in tools. This server sends `tool_calls` by default. If hang-ups or transfers do not
trigger, set `CALLAGENT_VAPI_FUNCTION_CALL=1` to send `function_call`.

## This server as the tools

Keep a Vapi model and define the six tools as function tools whose `server.url` is
`https://your-host/v1/vapi/webhook`. Vapi posts `tool-calls` messages there and this
server answers in Vapi's `results` format. The call is opened the first time a tool is
called, from the lead in `assistantOverrides.metadata`. If the gate refuses the lead, every
tool answers that the call is not allowed and should be ended. Read [tools.md](tools.md)
for what this mode does and does not enforce.
