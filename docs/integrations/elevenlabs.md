# ElevenLabs Agents

> Written from the ElevenLabs documentation as it stood on 18 November 2026, and tested
> against requests shaped the way that documentation describes. I have not run it against
> the live service. Points the documentation left open are marked **unconfirmed**.

## This server as the model (recommended)

1. **Run the server** where ElevenLabs can reach it over HTTPS, with `CALLAGENT_API_KEY`
   and `CALLAGENT_TRANSFER_NUMBER` set.
2. **Create an agent with a custom LLM.** In the agent's LLM settings choose Custom LLM.
   - URL: `https://your-host/v1/elevenlabs`. Which path ElevenLabs appends is
     **unconfirmed**, so this server answers on both `/v1/elevenlabs/chat/completions` and
     `/v1/elevenlabs/v1/chat/completions`.
   - API key: store `CALLAGENT_API_KEY` as a workspace secret and select it.
   - Model id: anything. It is ignored.
3. **Allow per-call data.** Enable the "custom LLM extra body" override
   (`platform_settings.overrides.custom_llm_extra_body`) and the first-message override.
   Without the first, this server cannot tell which call a request belongs to.
4. **Enable the system tools** `end_call` and `transfer_to_number`, with your transfer
   number as an allowed destination. Agents created through the API need them listed
   under `prompt.built_in_tools`.
5. **Place a call.** Ask this server to gate the lead and build the per-call part:

   ```bash
   curl -s https://your-host/v1/prepare/elevenlabs -H "Authorization: Bearer $CALLAGENT_API_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"lead_id": "L01", "first": "Jordan", "phone": "+15550142", "timezone": "America/New_York", "consent": true}'
   ```

   If `allowed` is true, send `request`, plus your `agent_id` and
   `agent_phone_number_id`, to `POST https://api.elevenlabs.io/v1/convai/twilio/outbound-call`.
   The `request` sets the first message to the opening line and puts the call id and the
   lead in `custom_llm_extra_body`, which arrives here as `elevenlabs_extra_body`.
6. **Send the post-call webhook** to `https://your-host/v1/elevenlabs/post-call?key=...`.
   The transcript is checked once more for a request to stop calling, and the attempt is
   recorded. A failed dial (`call_initiation_failure`) is recorded as an attempt too.

How the end of a call is sent: the agent's last words go inside the `end_call` tool call
as `message`, which ElevenLabs speaks before hanging up, and are not also sent as speech.
A transfer sends them as `client_message`, with a summary for the loan officer as
`agent_message`.

Not handled: the webhook's `ElevenLabs-Signature` header is not verified, because I could
not confirm its format. The endpoint is protected by the key in the URL. If you add
verification with the ElevenLabs SDK, that would be a welcome contribution.

## This server as the tools

Keep an ElevenLabs model and define six webhook tools that call
`POST https://your-host/v1/calls/{call_id}/tools/{name}`. Pass the call id as a dynamic
variable (`/v1/prepare/elevenlabs` puts `call_id` in `dynamic_variables`) and use it in
the tool's `path_params_schema`. Read [tools.md](tools.md) for what this mode does and
does not enforce. The tool parameter schemas are in `GET /v1/config`.
