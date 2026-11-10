# Phonic as a second voice provider (local testing)

Cartesia stays the default; Phonic is picked at the top of the Voice page ("Voice provider"), which then shows that
provider's own settings. Meant for local testing so far.

## How it fits

- `app/providers/`: one interface for both (`base.py`): prepare, start call, get call (one common shape), end call,
  recording. `cartesia_provider.py` wraps the existing Cartesia code unchanged; `phonic_provider.py` builds the
  Phonic config and maps a Phonic conversation onto the same call shape, so `calls.py` (poll, hang-up rules, outcomes,
  supervisor, emails, texts, STOP, results) works the same for both.
- `app/phonic.py`: the Phonic HTTP client and its guards.
- The choice + Phonic options: `<data>/voice_provider.json`. Each call stores its provider (`Call.provider`); History
  shows a Phonic tag and an All / Cartesia / Phonic filter once a Phonic call exists.
- Same agent: the instructions come from `agent_config.build_instructions` (one prompt), the lead details are the
  same per-call variables (Phonic `template_variables`). Only the tool names differ: Phonic hangs up with
  `natural_conversation_ending`, and has no voicemail tool (see below).

## Phonic API facts (docs.phonic.co, read 2026-11-10)

- Outbound: `POST /v1/conversations/sip/outbound_call`, headers `X-Sip-Address` (bare host:port, no `;transport=`),
  `X-Sip-Auth-Username`, `X-Sip-Auth-Password`; body `from_phone_number`, `to_phone_number`, `sip.transport`,
  `sip.media_encryption`, `config`, `dry_run`. Carrier refusals come back as HTTP 409 with `sip_status_code`.
  We use the Cartesia trunk's settings: `sip.telnyx.com:5061`, TLS, SRTP required, TELNYX_SIP_* credentials.
- Voicemail detection: only on bridged transfers (`detect_voicemail`), not for the called person.
- Speed: `audio_speed` (default 1; no documented range, we allow 0.5-2).
- Background: `background_noise` = office / call-center / coffee-shop or null (off), `background_noise_level`.
- Transfer: `built_in_transfer_to_phone_number`. We use hand-off (`keep_listening: false`, SIP REFER, Phonic leaves
  the call), like Cartesia's cold transfer. One supervisor with a phone = a fixed number; several = the agent dials
  the `transfer_number` that request_supervisor returned.
- After a call: `GET /v1/conversations/{id}` (items with transcripts and tool calls, `ended_by`, `audio_url`).
  Webhooks (`conversation.ended`, `.analysis`, `.transferred`) are set in Phonic's dashboard; we don't need them
  (we poll). No summary field.
- Voices: `GET /v1/voices` (the dry run saw 42).
- Webhook tools are named objects in the Phonic account (`/v1/tools`). Ours (record_outcome, collect_email,
  request_supervisor, transfer_to_specialist) are created / re-pointed by the backend before a Phonic call; they
  call the same `/api/cartesia/tools/*` endpoints with the same Bearer secret (`call_info.conversation_id` = call id).

## Safety

- `PHONIC_TEST_ONLY` (on unless `0`): Phonic may only call `TEST_CALL_NUMBER`; anything else is blocked before the request
  and logged (`BLOCKED: ...` in logs/backend.log). The dashboard's TEST MODE guard runs as well.
- `PHONIC_CALLS_DISABLED` / `CARTESIA_CALLS_DISABLED`: no real Phonic call; a dry run still validates.
- `DIALER_STANDBY`: Cartesia stays read-only as before; Phonic writes are allowed only while `PHONIC_TEST_ONLY` is on.
- `python dashboard/backend/phonic_dry_run.py`: validates the setup without a call, with its own data folder
  (`.cache/phonic_test`), one fake lead on the test phone, no Google Sheets, no Phonic tools created.

## Not supported by Phonic (not faked)

- Voicemail detection of the called person. The agent is told to hang up on a recording; our transcript check
  (`outcomes.voicemail_phrase`) still closes the line and marks the call voicemail.
- A call summary (`ai_summary` stays empty on Phonic calls).
- A ringing timeout (the Telnyx connection's own timeout applies).
- Our own background recordings (Voice page): Phonic only has its three built-in sounds.
- The 4-second "wait, then Hello?" start is done with Phonic's `no_input_poke` (the callee speaks first; after
  `call_start.wait_seconds` of silence it says the silent greeting). Phonic has one poke setting, so the later
  silence check-ins also say that greeting, not `silence.check_message`.
