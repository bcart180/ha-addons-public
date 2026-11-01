# Changelog

## 0.2.6

Delivery hardening, part 1 of the HA-ack design (add-on only). No new privileges: no Supervisor or Core API, no new option, no new route. dry_run, push_owner and the pyscript are untouched.

- **HA readiness probe.** Before each post to an HA webhook URL, a token-less `GET` to the same URL tells a loaded webhook (`405`) from one HA has not registered yet (`200`, the Core-restart window that lost the 12:01 call). Not ready: the post is held and re-checked every 30 s without using an attempt. After a hold, a fixed 25 s settle from the first `405` lets pyscript load. Cloudhook and non-HA URLs are not probed.
- **Wedge guard.** If HA still looks not ready after 10 minutes, the call is posted anyway and flagged `posted_unready` (sidecar and index), so a stuck readiness signal can never silently drop calls.
- **Hard attempt cap + `ack_timeout`.** At most 10 delivery attempts per CallSid (was 30), backoff 30 s → 4 h, still within 24 h. Hitting either limit writes a terminal `ack_timeout` (`attempt_cap` or `max_age`) to the sidecar and index once; the outbox never re-posts it. `ack_timeout` is never sent to HA.
- **`public_base_url` hardening.** Startup also rejects a non-ASCII value (zero-width characters, fraction slash, unencoded international names: use the `xn--` form), a trailing dot on the host, and an empty port (`host:`). These already failed closed; now they get a clear `refusing to start` message.
- **aiohttp deprecation fixed.** Mutable runtime state (last prune time, in-flight CallSids, readiness) lives in one holder created before startup, so the watchdog no longer assigns `app["last_prune"]` on a running app.
- **Probe hygiene (review L2/L3).** Only an empty `200` with no or `application/octet-stream` content type counts as HA's unregistered reply; any other `200` means the probe cannot be trusted, so the call is posted without holding. Target webhooks must be POST-only (documented). A URL confirmed ready (registered probe or 2xx post) within 120 s is not re-probed; a failed post or unready probe forces the next probe. Held calls re-probe at 15/30/60/120 s instead of every 30 s, cutting HA's `only supports POST` warnings (which carry the webhook id and IP) from ~20 to a handful per restart.
- Tests: `tests/test_ready.py` (restart hold → settle → one delivery, steady state with no delay, probe carries no token and logs no id, cloudhook not probed, posted_unready deadline, attempt cap, max age), 6 new `public_base_url` cases. CI runs it.

## 0.2.5

- **Fail closed on a half-configured Twilio setup.** With `twilio_auth_token` set and `public_base_url` blank, the add-on logs `refusing to start: public_base_url is required when twilio_auth_token is set ...` and exits. Twilio signs the public https URL, so without it every real webhook would fail validation.
- **No auth token: Twilio routes answer 503.** `/twilio/voice`, `/twilio/status` and `/twilio/stream` return `503 twilio not configured` for every caller, including loopback and the ingress proxy (previously those two were let through unsigned).
- **`public_base_url` must be a bare https origin.** At startup it is checked for `https://`, a host, and no path, query, fragment or credentials; otherwise the add-on logs `refusing to start: public_base_url must ...` and exits with code 2. The value itself is never logged. A trailing `/` is still fine. The local-dev example in the README no longer sets an `http://` URL.
- CI: the Add-on CI workflow also runs the CallCatch Voice Ruff lint and tests.

## 0.2.4

Early-hangup delivery hardening. No Twilio, TwiML, trust, UI or config option changes. dry_run, push_owner and the pyscript are untouched.

- **Caller's last words survive a hangup.** On `stop`/disconnect/terminal status, finalize no longer cancels the xAI bridge at once: when a caller line is still being transcribed it commits the input buffer and waits up to 3 s (`FINALIZE_DRAIN_S`) for `transcription.completed`, then tears down. Bounded: a silent xAI never holds the post longer.
- **Finalize can't be cancelled mid-post.** Finalize runs once per CallSid in its own task; every path (stream `stop`, socket close, status callback, watchdog, shutdown) awaits it through `shield()`. Before, a Twilio socket drop could cancel the stream handler while the HA webhook was in flight, leaving a sidecar with no delivery and no index row, so the call was never retried (reproduced: status callback while the socket is open, then an abrupt close → 0 webhooks on 0.2.3, 1 on 0.2.4).
- **Durable outbox.** Each call's sidecar is its outbox entry. The retry loop scans sidecars (not just `index.json`), backs off 30 s → 60 s → … → 15 min, keeps `next_retry_at` on disk, retries for up to 24 h (was 30 min / 8 attempts), and survives restarts. One corrupt entry no longer stops the loop, and the loop itself can't die on an exception.
- **Journal + crash recovery.** A live call is journaled to its sidecar (`state: open`) when the stream opens and after every final transcript line. On startup, and every minute via the watchdog, an open journal with no live session is finalized as `recovered` and delivered with its transcript. A late Twilio status for such a call recovers it instead of posting an empty missed call.
- **Watchdog.** Every 5 s: finalize a session whose Twilio socket is gone (5 s), silent (45 s), that got a terminal status while the socket still looked open (8 s grace), or that passed 1 h.
- **"Caller hung up early" marker.** Records carry `hangup: {by, phase, early, reason}` and `early_hangup`. Phases: `before_stream`, `greeting`, `mid_reply`, `caller_speaking`, `after_reply`, `recovered`. Early hangups append one plain line to `transcript_text` (for example `[Caller hung up early, during the greeting]`); the `transcript` turn list is unchanged.
- **No duplicate delivery on a slow webhook.** An in-memory in-flight set per CallSid makes the outbox retry, orphan recovery and missed-call path skip a call whose post is still running, and `next_retry_at` is written to the sidecar before the first post (reproduced: slow HA + recovery racing retry → 3 posts before, 1 after).
- **Retention.** The watchdog prunes final, acknowledged sidecars and their `/data` WAVs older than 30 days, at most hourly. Pending and open entries are kept. DOCS now say what is stored and that it is in HA backups.
- **One transcript per CallSid.** A finished call is re-posted only if HA has not acknowledged it; a persist failure still delivers a transcript-only record (`persist_error: true`).
- Tests: `tests/test_hangup.py` simulates Twilio webhook sequences with the hangup at each point (before stream, before `start`, right after `start`, mid-media, status before/after `stop`, abrupt close, duplicate status), the xAI drain (late, never, greeting), restart mid-call, HA 404s during a restart, corrupt outbox entries, persist failure, concurrent finalize and a 0-second call. CI runs it.

## 0.2.3

- **Twilio signatures are always required when an auth token is set.** `validate_twilio_signature: false` no longer opens `/twilio/voice` and `/twilio/status` to unsigned requests; the option is kept so saved configurations still load, is ignored, and logs a warning at startup when it is off. With no auth token, only the ingress proxy and loopback are accepted, as before.
- `/api/status` `signature_validation` now reports whether an auth token is set.
- CI: Ruff lint (`twilio_house_voice/ruff.toml`, same pinned rule set as CallCatch) runs on every change; one unused f-string prefix in the tests fixed.

## 0.2.2

- **Plaid status page.** Pure black, white text, one blue accent, no product name in the chrome (title "Voice"). Tap targets are at least 48 px, call rows 76 px. Live calls and recent calls are a list; tapping a recent call opens a call view (masked caller, time, duration, recording, Home Assistant delivery, caller-ID check, screening). On 900 px and wider the list and call view sit side by side.
- **Browser hardening.** The page script moved to `static/app.js` (no inline script or handlers) and builds the DOM with `textContent` only. Every HTTP response now carries a CSP (`default-src 'none'`, self-only script/style/connect, `frame-ancestors 'self'`), nosniff, no-referrer, `X-Frame-Options: SAMEORIGIN`, COOP/CORP same-origin and a Permissions-Policy; `/api/*` is `no-store`, and so is the page. WebSocket upgrades are left untouched.
- An unhandled exception now returns a JSON 500 with those headers; only the exception type is logged.
- No Twilio, stream, trust, stdin or webhook behavior changes.

## 0.2.1

- `stdin: true`: HA can send `{"cmd": "block"|"unblock", "number": "..."}` with `hassio.addon_stdin` (used by the CallCatch notification's Block / Undo buttons). Only those two commands are accepted, and `"dry_run": true` is honored. A non-string `number` (for example an int from an HA template) is coerced instead of crashing, and one failing command no longer stops the stdin listener. There is no HTTP surface change.

## 0.2.0

- Review fixes: a block-listed caller is rejected only when STIR is passed-A, and otherwise screened and tagged `blocked`. A full list no longer drops a number from the other list. Non-`+1` numbers keep their full digits. The bearer token is refused from the Docker gateway 172.30.32.1.
- **Trust gate.** Allow and block lists in `/data/trust_lists.json`. A blocked caller gets TwiML `<Reject>` (no stream, no recording, no webhook, no inbox row; a later status callback for that call is ignored). An allowed caller is answered as before, but the agent is told to skip the screening questions and just take the message. Everyone else is unchanged (`screen`).
- **STIR/SHAKEN captured.** `StirVerstat` from the voice webhook is saved per call as `stir_verstat` plus a parsed `stir` (`passed`/`failed`/`none`/`absent`, level A/B/C) in the sidecar, the index, and the hangup webhook body. It is a signal only and never blocks a call by itself.
- **`/api/trust`** (GET), **`/api/trust/add`**, **`/api/trust/remove`** (POST JSON `{list, number}`). Accepted from the ingress proxy with an HA user header, or with `Authorization: Bearer <trust_api_token>` from the Supervisor network or loopback. A blank token closes the bearer path. The token is redacted from logs. Add returns `previous` so CallCatch can undo a move between lists.
- Status page counts rejected calls and list sizes (no full numbers). Slug stays `twilio_house_voice`. No webhook URL, signature, recording, or `xai_bridge` changes.

## 0.1.6

- New transcripts label the assistant side **Assistant**. `caller` still renders as `Caller`. Roles stored on each turn stay `caller` or `assistant` (`house` and `agent` render as Assistant too). Slug stays `twilio_house_voice`. No webhook URL, credential, signature, or mix-recording changes.

## 0.1.5

- Caller transcripts coalesce cumulative xAI Realtime updates. After a caller turn is marked final, a later `input_audio_transcription.updated` (or a completed transcript) that extends that line, or is a prefix or superset of it, replaces the last caller turn instead of appending another Caller line. Empty partials are dropped. Assistant deltas are unchanged.

## 0.1.4

- User-facing name is **CallCatch Voice** (add-on `name` and sidebar `panel_title`). The inbox product name stays **CallCatch**. “Your Personalized Voicemail” is not part of this sidebar label.
- Description still says what the add-on does: Twilio Media Streams to xAI Realtime, with the mixed recording saved for CallCatch.
- Patch bump so Supervisor offers an update and applies the new `config.yaml` name and panel title. Slug stays `twilio_house_voice`. No webhook, credential, signature, or mix-recording behavior changes.

## 0.1.3

- On hangup, `{CallSid}.wav` is a mono mix of the caller and the assistant when `mix_recording` is on (the default) and the assistant actually played audio. Soft Slate keeps playing that same file and hears both sides. Caller-only when the assistant buffer is empty or mixing is off.
- Caller frames follow Twilio's Media Stream timestamp. Assistant audio is queued at 8 kHz from the moment it is sent to Twilio. A barge-in `clear` (and a replacement Media Stream) drops assistant audio that had not started playing.
- `record_assistant_audio` still writes the separate `{CallSid}.assistant.wav`, now on that same timeline. The sidecar gains `audio_mix` and `caller_audio_bytes`.

## 0.1.2

- Media Stream handshake no longer fails with Twilio 31920. `<Stream>` does not accept a query string, so the one-time token was stripped and the WebSocket was refused before it opened. The token and CallSid are now `<Parameter>` nouns. The socket upgrades first, then checks the token on the `start` event `customParameters`. A missing or invalid token, or no `start` within a few seconds, closes the socket. Voice and status webhooks still require `X-Twilio-Signature`.

## 0.1.1
- Home Assistant webhook triggers cannot see HTTP headers. When `ha_webhook_token` is set, the hangup POST includes it as JSON `token` (headers `Authorization` and `X-Webhook-Token` are unchanged). The secret is not written into the voicemail sidecar.

## 0.1.0

- First release. Twilio Voice webhook returns `<Connect><Stream>` TwiML. Media Streams WebSocket bridges caller μ-law to xAI Realtime as `audio/pcmu` without resampling, and plays assistant audio back the same way.
- Hangup writes caller PCM16 WAV and a JSON sidecar under `/data/voicemail/`, copies the WAV to `/share` and `/media` when those mounts are present, and POSTs the hangup to the configured Home Assistant webhooks. Idempotent by Twilio CallSid.
- Stub bridge (or a missing xAI key) still records and posts, with a spoken fallback prompt.
- Health endpoint for the Supervisor watchdog. Admin ingress page shows configuration readiness without secrets.
