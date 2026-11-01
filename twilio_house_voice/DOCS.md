# CallCatch Voice — architecture

## Why this path

xAI Voice Agent Builder records calls in the console (playback and transcript live there), but the public Voice REST reference has no call-recording download and no `recording_url`. The only documented audio download is custom-voice reference audio, `GET /v1/custom-voices/{voice_id}/audio`, which is the clone source, not a phone call. Checked against the Voice API reference and the Builder announcement on 2026-10-25.

So Home Assistant cannot pull a Builder recording after the fact. The add-on owns the media path:

```
Caller → Twilio number
      → POST /twilio/voice     (TwiML <Connect><Stream>)
      → WSS  /twilio/stream    (Media Streams, μ-law 8 kHz)
      → WSS  wss://api.x.ai/v1/realtime?model=grok-voice-latest
      → on stop: WAV + JSON under /data/voicemail/
      → POST Home Assistant webhook, idempotent by CallSid
```

The xAI phone cookbook (`audio/pcmu` in, `audio/pcmu` out) is the format Twilio already uses. Frames are forwarded as the same base64 payload. There is no resample and no second encode. Assistant deltas are copied straight into Twilio `media` events. `input_audio_buffer.speech_started` sends Twilio `clear` so the caller can interrupt.

That cookbook's sample server is Node. This repository ships add-ons as one Python process (`aiohttp`, `/data/options.json`, a log filter that strips secrets), matching Trading Desk. The wire protocols are JSON WebSockets, so the language does not change the audio path.

## Call recording

`mix_recording` defaults to on. On hangup `{CallSid}.wav` is one PCM16 mono 8 kHz mix of both legs. CallCatch (Your Personalized Voicemail) already requests `api/audio/{call_id}` and serves `{call_id}.wav` from its media directory, so it hears the caller and the assistant without a CallCatch change. The webhook `audio_base64`, when enabled, is that same file.

Alignment, as close as Media Streams and xAI Realtime allow:

- Caller μ-law is placed at Twilio's `media.timestamp` (milliseconds from the start of the stream). A missing timestamp appends in arrival order. A timestamp more than 30 seconds ahead of audio already stored is ignored and the frame is appended, so a bad clock cannot insert a huge silence gap or drop the frame.
- Assistant μ-law is placed after the frame is accepted by the Twilio socket. Playback is an 8 kHz queue: audio still waiting to play pushes the next chunk back, instead of stacking a whole TTS burst on the instant it arrived. A frame that fails to send is left out of the file.
- `input_audio_buffer.speech_started` sends Twilio `clear`. Samples that had not reached that play cursor are dropped. The same cut is applied when a second Media Stream replaces the socket (the old playback buffer is gone). Inbound timestamps on that new stream restart, so they are offset to the end of the caller timeline already kept.
- The timelines are summed sample by sample. μ-law silence (`0xFF`) leaves the other leg's byte unchanged. Where both are non-silent, linear samples are added, clamped to 16-bit, and encoded again.

If the assistant timeline has no samples left (stub bridge, no agent audio, or a clear that removed every assistant frame), `{CallSid}.wav` stays caller-only and `audio_mix` is false. `record_caller_audio: false` writes no main WAV. `record_assistant_audio` still only controls the extra `{CallSid}.assistant.wav`, which is the assistant timeline including silence so it lines up with the mix. `assistant_audio_bytes` counts assistant samples that survived `clear`, not the silence padding in that file.

Limits: Twilio's timestamp and the add-on's monotonic clock are not the same clock (the `start` event arrives a bit after Twilio's stream time zero, and Twilio's outbound jitter buffer is not observable here), so the legs can sit a fraction of a second apart. Overlap can clip. Each WAV is about 16 KB/s and stops growing at 30 minutes. The optional assistant file is a second copy of about that length. There is no echo cancellation; a speakerphone can still trip server VAD.

## What is live, what is stub

**Live** (`xai_bridge: live` and `xai_api_key` set):

- Realtime socket to `wss://api.x.ai/v1/realtime`
- `session.update` with voice, instructions, `server_vad`, and `audio/pcmu` both ways
- Caller frames → `input_audio_buffer.append`
- `response.output_audio.delta` → Twilio playback, unmodified
- Greeting via `response.create` when `greeting_mode` is `agent`
- Transcripts from `conversation.item.input_audio_transcription.updated` / `.completed` and `response.output_audio_transcript.done`. Caller `.updated` events are cumulative. When that text extends the last caller turn, or is a prefix or superset of it, the turn is replaced even after it was marked final. Empty partials are dropped.

**Stub** (`xai_bridge: stub`, or live with no key):

- No socket to xAI
- TwiML plays a short `<Say>` then still opens the Media Stream
- Hangup still writes the WAV, the sidecar, and the webhook (transcript may be empty)

The process stays up if xAI drops mid-call. The caller recording continues. `/api/health` stays `{"ok": true}` so Supervisor does not restart the add-on just because xAI is down. The sidebar page shows whether the key is set and whether the bridge is live or stub.

## One call, one session

State is keyed by Twilio CallSid. A second Media Stream for the same CallSid replaces the socket and keeps the audio buffer, so a reconnect does not mix two callers and does not throw away the first half of the recording. Unknown CallSids are rejected. The one-time token is a `<Parameter>` on `<Stream>`, not a query string (Twilio error 31920). After the WebSocket upgrade, the `start` event's `customParameters` must carry that token. It is random, single-use, and expires in two minutes. A missing or invalid token, or no `start` within a few seconds, closes the socket. The token is minted only after the voice webhook passes signature checks.

## Ingress and the published port

Ingress (port 8787, admin-only sidebar) is for the status page. Supervisor's ingress proxy is `172.30.32.2`. The status page and `/api/status` accept only that proxy (plus loopback) when `SUPERVISOR_TOKEN` is set.

Twilio cannot use ingress. Host port 8787 publishes the same server. `/twilio/*` is open on that port and is gated by the Twilio signature (POSTs) or the stream token (WebSocket). `/api/health` is the unauthenticated watchdog body and returns only `{"ok": true}`.

`init: false` and a direct `python3` command match Trading Desk: no s6, no extra process supervisor inside the container. Supervisor's own watchdog polls the health URL.

## Secrets

Redacted from every log line: Twilio account SID, auth token, xAI API key, webhook token, both webhook URLs (they are capability links), and `SUPERVISOR_TOKEN` if it is present. The add-on does not request the Supervisor API or the Core API. Options never live in git (`.gitignore` already ignores `options.json` and `.env`).

Caller numbers are stored in the per-call JSON under `/data/voicemail/` because the HA webhook needs them. The status page and the index file only keep the last four digits.

## Trust gate (allow / block lists, STIR/SHAKEN)

The voice webhook reads `From` and `StirVerstat` before it mints a stream token:

| Verdict | When | What happens |
| --- | --- | --- |
| `block` | `From` is on the block list **and** `StirVerstat` is `TN-Validation-Passed-A` | TwiML `<Reject reason="rejected"/>`. No stream, no WAV, no webhook, no inbox row. A later status callback for that CallSid is ignored |
| `allow` | `From` is on the allow list | Normal call. The agent's instructions gain one line telling it not to screen this caller and just take the message |
| `blocked` | `From` is on the block list but STIR is anything other than passed-A (B, C, failed, none, absent) | Screened like any caller, and the message is tagged `trust_verdict: blocked`. Spoofers copy real numbers, so without full attestation the real owner still gets to leave a message |
| `screen` | everything else | Unchanged |

Numbers compare on digits: NANP on the last 10, and any number written with a `+` other than `+1` on its full digit string (country code included), so it never collides with a US number. A number is on at most one list; adding it to one removes it from the other.

`StirVerstat` is only present when the carrier sent a SHAKEN PASSporT ([Twilio docs](https://www.twilio.com/docs/voice/trusted-calling-with-shakenstir)). It is stored as `stir_verstat` and parsed into `stir` (`passed` / `failed` / `none` / `absent`, level `A` / `B` / `C`) in the sidecar, the index, and the webhook body. It is never an input to the list lookup, so a failed or missing attestation cannot block a call. It can only soften a block: anything short of passed-A screens instead of rejecting.

Lists are `/data/trust_lists.json` (temp file + fsync + rename; a corrupt file is renamed `.corrupt` and the lists start empty, which means everyone is screened). The API:

| Route | Body | Notes |
| --- | --- | --- |
| `GET /api/trust` | | `{allow: [...], block: [...], blocked_calls}` |
| `POST /api/trust/add` | `{"list": "block"\|"allow", "number": "...", "note": "..."}` | Returns `entry` and `previous` (the list it moved from, for undo) |
| `POST /api/trust/remove` | `{"list": ..., "number": ...}` | Returns `removed` |

Accepted from the ingress proxy with an HA user header, or with `Authorization: Bearer <trust_api_token>` from the Supervisor network (`172.30.x.x`, except the Docker gateway `172.30.32.1`, where published-port traffic appears) or loopback. A blank `trust_api_token` closes the bearer path. The token is redacted from logs. If a reverse proxy publishes port 8787, forward only `/twilio/*`.

## Supervisor stdin (0.2.1)

`stdin: true` lets HA send `hassio.addon_stdin` with `{"cmd": "block"|"unblock", "number": "+1…"}`. This is how the CallCatch notification's Block and Undo buttons reach the block list without any add-on-to-add-on call. Nothing else is accepted, `"dry_run": true` logs without changing the lists, and STIR/SHAKEN is never consulted. Set `THV_STDIN=0` to disable it.

## Out of scope

- Verizon *71 cutover
- Buying Twilio numbers
- Echo cancellation (a speakerphone can trip server VAD)
- Voice Agent Builder as the recorder of record
- Paid Twilio Lookup (line type / CNAM) and third-party spam scores

## Early hangups and delivery guarantees (0.2.4)

The assistant never ends a call; the caller (or the network) always does. Every way a call can end — Media Streams `stop`, the socket closing, Twilio's status callback, the watchdog, add-on shutdown — runs the same single finalize for that CallSid, so Home Assistant gets exactly one transcript per call.

- If the caller was mid-sentence, CallCatch Voice gives xAI up to 3 seconds to finish that transcript before closing the bridge.
- The record says how the call ended: `hangup.phase` is `before_stream`, `greeting`, `mid_reply`, `caller_speaking`, `after_reply` or `recovered`, and `early_hangup` is true unless the call ended after a finished exchange. Early hangups add one line such as `[Caller hung up early, during the greeting]` to `transcript_text`.
- Webhook delivery is durable: a failed post is retried from disk with backoff (30 s, 1, 2, 4, 8, 15, 30 min, 1, 2, 4 h), at most 10 attempts and at most 24 hours, including across restarts. When either limit is hit the sidecar is marked `ack_timeout` (reason `attempt_cap` or `max_age`), the index row shows `ack_timeout: true`, and the call is never re-posted.
- Home Assistant readiness (0.2.6): before posting to a Home Assistant webhook URL (`.../api/webhook/<id>`), the add-on may send a plain `GET` to the same URL with no body and no token. HA answers `405` when the webhook is registered and an empty `200` when the id is not registered yet, which is what a restarting Core does before automations load. On that empty `200`, an error or a timeout (5 s) the post is held and re-checked after 15 s, 30 s, 60 s, then every 120 s; a hold is not a delivery attempt. When the webhook reappears after a hold, the add-on waits a fixed 25 s (from the first `405`) so pyscript can finish loading. If HA is still not ready after 10 minutes the call is posted anyway and flagged `posted_unready`. Nabu Casa cloudhook URLs and non-HA targets are not probed.
  - **The webhook target must be POST-only** (the default for HA webhook triggers). Do not add `GET` to the trigger's `allowed_methods`: a GET-enabled hook runs its automation on every probe and answers `200`, which looks like "not registered". A `200` that is not HA's unregistered reply (it has a body, or a content type other than none/`application/octet-stream`) is treated as `probe_unsupported` and the call is posted at once without holding.
  - **Probe frequency and the HA log.** A probe of a registered hook makes HA log one WARNING `Webhook <id> only supports POST` with the webhook id and the add-on's IP (admin-only log, but it lands in backups). To keep that rare, a URL confirmed ready by a probe or a successful post in the last 120 s is posted without probing; after a failed post or an unready probe the next delivery always probes first. Remaining warnings: about one per call when calls are more than 2 minutes apart, plus a few (about 4 in the first 4 minutes, then one per 2 minutes) while a call is held during an HA restart. The add-on itself never logs the URL or id.
- A call in progress is journaled to disk, so if the add-on restarts mid-call the transcript so far is still delivered, marked `recovered`. Audio recorded in memory before a crash is not recoverable.
- One delivery at a time per CallSid: while a post is waiting on Home Assistant, the retry loop, crash recovery and a late status callback skip that call, and the next retry time is written to disk before the first post. A slow or restarting HA gets one post per call, not several. HA should still treat `X-Idempotency-Key` (the CallSid) as the dedupe key.

## Data retention and backups

Each call leaves a sidecar `{CallSid}.json` (full caller and callee numbers, STIR result, the full transcript, delivery status) and, when recording is on, `{CallSid}.wav` / `{CallSid}.assistant.wav` in the add-on's `/data/voicemail`. A call in progress is journaled there too. `index.json` keeps a short list with masked numbers.

- `/data` is part of every Home Assistant backup that includes this add-on, so those numbers, transcripts and recordings are in your backups. Treat backups as sensitive, and keep them encrypted.
- Retention: about once an hour the watchdog deletes sidecars (and their WAVs in `/data`) that are final, acknowledged by HA (or had no webhook configured) and older than 30 days. Calls still waiting on delivery and open journals are never deleted. `index.json` is kept.
- Copies the add-on mirrors to `/share`, `/media` or `/config` for playback, and anything HA itself stores (CallCatch, notifications, history), are not pruned by this add-on.

## Moving stores: import_from_share

When this add-on is moved to another add-on store, its `/data` does not move with it. The new install imports allow-listed files once, on its first start, from `/share/.migrate-2026-11-01/<slug>/` (`voicemail` or `twilio_house_voice`). The folder must be owned by root with mode 700 (`chmod 700`). Anything unexpected (a symlink, a file not on the allow-list, a file over 8 MB, invalid JSON) stops the import with nothing written, and the log names only the reason. After a successful import the add-on writes an import marker in `/data` and deletes the staged files. It never imports twice.
