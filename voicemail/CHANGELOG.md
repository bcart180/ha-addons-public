# Changelog

## 0.3.7 — 2026-11-01

- **Post-call structured summary.** Every call and voicemail now carries `summary`: caller name (if stated), reason, callback number and urgency (`low`, `normal` or `urgent`).
  - Source `voice`: CallCatch Voice may send a `summary` object (`name`/`caller_name`, `reason`, `callback`, `urgency`) with the ingest payload. It is validated: lengths capped, markup stripped, urgency limited to the three values, and a callback kept only if it is the caller's number or is spoken in the transcript.
  - Source `local`: otherwise (and for rows saved before 0.3.7) a deterministic extractor reads the caller's lines of the transcript. No network call, no model and no new keys or options.
- **Panel.** The detail view shows a Summary card above the transcript (urgency pill, caller, reason, tap-to-call callback, and a note to check the transcript). List rows show an Urgent/Low pill and the reason in place of the snippet.
- **Notification.** `sensor.inbound_voicemails` rows add `summary_name`, `summary_reason`, `summary_callback` and `urgency`, with numbers masked as `•••1234` (the same masking the pyscript sensor uses). The blueprint push becomes `Urgent: Name · Reason · Call back •••1234`, falling back to the first transcript line when there is no summary. dry_run handling is unchanged.
- **Masked sensor attributes.** `sensor.inbound_voicemails` rows no longer carry the raw `caller`, `caller_pretty`, `caller_title`, `snippet` or `transcript`; they carry `caller_masked` and `caller_label_masked` (contact name, or `•••1234`), `summary_fallback` (first transcript line, masked) and the masked `summary_*` fields. Any signed-in HA user can read entity attributes; the admin panel keeps reading full rows from the store. The README adds a recommended recorder exclude for the sensor.
- **Push masking.** The push title is `caller_label_masked` and the no-summary fallback body is `summary_fallback`; the push and dry-run logbook never show more than the last 4 digits. Because the sensor no longer carries the raw number, the blueprint's Call back and Block buttons no longer appear (they need the full number); use Open CallCatch, which has tap-to-call and block.
- **Stronger number masking.** Whitespace is collapsed first; digit groups may be split by up to 8 punctuation characters (any dash, minus, bullet, middle dot, comma, quotes, spaces), by a short word or letter (`x`, `and`, `then`) or by markup residue; letter O/o and I/l inside a number read as 0 and 1; invisible format, combining and filler characters are dropped and fullwidth/Arabic-Indic digits are normalized before masking.

## 0.3.6 — 2026-10-30

- **Legacy mode reads full rows from the pyscript store file.** `/api/voicemails` now joins each `sensor.inbound_voicemails` row by `voicemail_id` to the full row in `/homeassistant/pyscript/.inbound_voicemails.json` (already mapped read-only; opened read-only, 8 MB cap). Numbers, tap-to-call, snippet and transcript come from the store; order and heard state come from the sensor. A caller title the sensor masked is dropped so the full number shows. This keeps the panel working once the pyscript publishes a redacted sensor (masked numbers, no transcripts).
- A sensor `voicemail_id` of the form `vm-<short_id>` (the redacted sensor's stand-in for ids that embed a caller number) is matched to its store row by the pyscript's short id, and the panel then uses the full id for clear and mark-heard.
- If the store file is missing or unreadable, the panel falls back to the sensor rows unchanged. The response reports `rows_from: store|sensor`.
- The panel stays admin-only. No write paths, trust or notification changes.

## 0.3.5 — 2026-10-30

- **Transcript: hangup `Source:` line removed.** `Source: twilio_house_voice` (and an already-polished `Source: CallCatch`) is internal plumbing and showed the old product name; the detail view now drops that line instead of renaming it. Other `Source:` values and mid-sentence mentions are untouched.
- **`/api/diag`: `blueprint_default` renamed `blueprint_default_input`** and documented as a hard-coded constant (the blueprint's `dry_run` input default), not a reading of any live automation.

## 0.3.4 — 2026-10-30

- **Admin-only panel.** Dropped the explicit non-admin `panel_admin` setting. Supervisor's default (`true`) now applies, so the CallCatch sidebar panel (and so transcripts) is visible to Home Assistant admins only.
- **Read-only dry_run status in `/api/diag`.** New `dry_run` object: `work_forward_dry_run` (state of `input_boolean.work_forward_dry_run`), `push_owner` (from `sensor.voicemail_pipeline`), `blueprint_default` (`on`). Read only; CallCatch never changes any dry_run value. See DOCS.md, "dry_run controls".
- **Crash responses keep security headers.** An unhandled exception now returns a JSON 500 (`{"ok": false, "error": "internal"}`) through the security middleware, so it carries the CSP, nosniff and the other headers, plus `no-store` on `/api/*`. Only the exception type is logged.

## 0.3.3 — 2026-10-30

- **Plaid theme.** Pure `#000` canvas, white text, grey secondary text, one blue accent (`#3E6AE1`) and red only for destructive actions. The page no longer loads the Soft Slate stylesheet.
- **No branding in the chrome.** The header and tab title read "Voicemail"; the player hint and share title no longer name the add-on.
- **Touch-first.** Every tap target is at least 48 px; list rows are at least 76 px; the play button is 72 px; the scrubber's hit area is 32 px tall.
- **Wide screens (900 px and up: Mac, Echo Show)** show the list and the open voicemail side by side, with the open row highlighted. Nothing opens on its own (opening marks a voicemail heard).
- Phone header no longer wraps the Clear-all button onto a second line at 390 px.
- No server, API, trust or notification changes.

## 0.3.2 — 2026-10-30

- **Browser hardening.** Every response (page, scripts, JSON, audio, errors) now carries a strict Content-Security-Policy (`default-src 'none'`, scripts/styles/media/fetch from self only, `frame-ancestors 'self'`, no inline script), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: SAMEORIGIN`, COOP/CORP same-origin and a Permissions-Policy that turns off camera, microphone and location. API responses default to `Cache-Control: private, no-store`.
- The list's call-back button no longer uses an inline `onclick`, so the page works under that policy.
- `/api/diag` no longer reports the Supervisor token's length or where it was read from.

## 0.3.1 — 2026-10-30

- **Actionable new-voicemail notification (blueprint).** `blueprints/callcatch_new_voicemail.yaml` sends one Companion-app push per new voicemail: caller (with a caller-ID-verified label from STIR/SHAKEN), the first transcript line, and **Call back** (`tel:`), **Mark heard**, **Block** (with Undo), **Open CallCatch**. Action IDs are unique per notification (`ACTION_<context.id>_…`). Quiet hours (default 12–6 AM HA local time) deliver silently. `dry_run` defaults to ON: logbook line only, no push, no action.
- One push per voicemail: the blueprint pushes only when `sensor.voicemail_pipeline` has `push_owner: blueprint` (otherwise pyscript `inbound_voicemail.py` owns the push and the blueprint logs a skip). It shares the pyscript tag `voicemail_<short_id>` so iOS collapses any overlap. Blueprint Undo only unblocks (documented).
- CallCatch now subscribes to the HA event `callcatch_command` over the Supervisor Core websocket and applies `{cmd: mark_heard, voicemail_id}` (built-in store, or the legacy pyscript service). Any other command is ignored. No new port, and no add-on-to-add-on call.

## 0.3.0 — 2026-10-30

- **Report Spam now blocks the number** instead of deleting the voicemail. Future calls from it are rejected by CallCatch Voice before they ring. A 10-second **Undo** restores the previous state (including a number that was on the allow list). Trash still deletes the voicemail.
- **Always Allow** on a voicemail puts the caller on the allow list; allowed callers skip the screening questions.
- **Numbers** page (shield icon) lists blocked and allowed numbers, with add and remove (each with Undo).
- The lists live in CallCatch Voice's `/data`. The inbox reaches them through its own ingress-gated routes (`/api/trust`, `/api/trust/add`, `/api/trust/remove`: HA ingress session required; the ingest bearer is not accepted), which forward to Voice with `voice_trust_token`. `voice_trust_url` accepts only `http://<addon-hostname>:<port>`.
- Each voicemail stores `stir_verstat` (Twilio STIR/SHAKEN) and `trust_verdict`. The detail view shows a carrier caller-ID badge. It is a signal only and never blocks or hides a message.
- Slug stays `voicemail`. New options default blank, so an update changes nothing until both are set.

## 0.2.0 — 2026-10-29

- CallCatch keeps its own inbox under `/data` (JSON, exclusive lock, atomic replace). It no longer needs a private pyscript to clear, mark heard, play, or count messages.
- `POST /api/ingest` accepts a transcript and audio from CallCatch Voice on the internal network. The `Authorization: Bearer` secret (`ingest_token`) is checked before the body is read. It is never logged.
- Publishes `sensor.inbound_voicemails` (count, plus `voicemails` and `recent_items`) through the Core API so existing dashboards and notifications keep working.
- The pyscript JSON is copied once, read-only, only when legacy mode is off. While `auto` still sees that file, pyscript keeps handling calls and no import marker is written.
- `legacy_pyscript` defaults to `auto`: when that legacy file exists, clear / mark heard / play keep using the pyscript services so a live install is unchanged. Set `off` to use the built-in store. Slug stays `voicemail`.
- `storage_backend`: `local` (default) writes audio to `/media/callcatch` or `/share/callcatch`, not `/local`. `google_drive` uploads audio plus transcript JSON with the `drive.file` scope only, retries with backoff, and does not create public sharing links. Playback streams through the App.
- The inbox, audio, and diag APIs require an ingress session or the bearer token. `media_player` is blank unless you set one.
- CallCatch Voice is not modified. Point its webhook at `http://local-voicemail:8099/api/ingest` or `http://<repohash>-voicemail:8099/api/ingest`.

## 0.1.9 — 2026-10-29

- The assistant side of a transcript renders as **Assistant**. A leading `House`, `house`, `assistant`, or `agent` label displays as `Assistant`. A leading `caller` label displays as `Caller`. This applies to the detail transcript and the list preview, and to the same mapping when the inbox API normalizes a message for display.
- Display only. Saved voicemails are not rewritten, so older messages that still say `House:` keep working. Words inside a line, including “his assistant”, stay as spoken. The share sheet still copies the transcript without the Source / Duration header polish. Slug stays `voicemail`.

## 0.1.8 — 2026-10-29

- Packaging only. Slug stays `voicemail`. Option defaults, pipeline, webhooks, and auth are unchanged. `ingress_panel: true` stays set.
- No `image` field, so Supervisor builds this add-on on the device. `build.yaml` pins the Home Assistant 3.23 base images for aarch64 and amd64. armv7 is omitted because `ghcr.io/home-assistant/armv7-base:3.23` is not published and that architecture is no longer supported. A `v*` tag still publishes `ghcr.io/bcart180/{arch}-addon-voicemail`.
- Icon, logo, and English names for every configuration option.
- Docker `HEALTHCHECK` on `/api/health` replaces the obsolete config `watchdog` URL for this add-on.
- Mock inbox uses fictional callers on 555-01xx numbers only.

## 0.1.7 — 2026-10-28

- Detail transcript display polish for messages already in the inbox. A hangup line `Source: twilio_house_voice` renders as `Source: CallCatch`. `Duration_s: 10.77` renders as formal English (`Duration: 10 seconds`, `1 minute`, `1 minute 5 seconds`; fractional seconds round down). A bare `TRANSCRIPT` header renders as `Transcript`.
- Display only, on the detail transcript. Stored text, the share sheet, the list snippet, the pipeline, webhooks, options, and auth are unchanged. Slug stays `voicemail`. Soft Slate CSS is unchanged.

## 0.1.6 — 2026-10-27

- User-facing name is **CallCatch**. Add-on `name` and sidebar `panel_title` are CallCatch only. “Your Personalized Voicemail” lives in the description, README, and DOCS.
- In-app title and share sheet use CallCatch. Soft Slate CSS (`soft-slate.css` and theme tokens) is unchanged. Slug stays `voicemail`.
- Patch bump so Supervisor offers an update and applies the new `config.yaml` name and panel title. No pipeline, webhook, option, or credential changes.

## Docs / ops — 2026-10-27 (option A)

- **Soft Slate Voicemail sidebar ON (tip):** `ingress_panel: true` after CoS GO (owner chose option A ~7:35 AM ET America/New_York). Soft Slate Recents returns as the HA sidebar app.
- Companion track: remove only the legacy Lovelace Recents block on `home-dashboard` view `path: voicemail` (storage dashboard; not this add-on). House Voice stays `ingress_panel: false`. Pipeline/bridge unchanged.
- No version bump (0.1.5). Runtime Supervisor `ingress_panel` flip applies after Sec Audit YES; tip is source of truth for reinstall.

## Docs / ops — 2026-10-26

- **Recents sidebar hidden (live):** Supervisor options set `ingress_panel: false` on add-on `47b3c4e5_voicemail` (Voicemail 0.1.5). Add-on left started. Soft Slate Recents (“19 voicemails · audio when ready”) is no longer in the house sidebar.
- Twilio House Voice and the voicemail store/pipeline are unchanged (hangup/audio webhooks, pyscript inbound store, `sensor.inbound_voicemails`, `sensor.voicemail_pipeline`).
- Tip `config.yaml` already had `ingress_panel: false` (sidebar stays OFF until an explicit CoS/owner GO). Live had drifted true after the 0.1.5 ship; the runtime flip restored tip intent. No version bump.

## 0.1.5 — 2026-10-26

- **Ingress path fix (Companion sidebar 404):** `app.js` API fetches use relative paths (`api/voicemails`, `api/mark_heard`, `api/clear_all`, `api/clear_one`, `api/play`) so under `/api/hassio_ingress/<token>/` they hit the add-on instead of Core `/api/...`. Relative `api/audio/...` unchanged. `ingress_panel` left as in repo (live sidebar already ON — not flipped).

## 0.1.4

- **M1:** Mutation APIs (`clear_one` / `clear_all` / `mark_heard` / `play`) require Supervisor ingress proxy (`172.30.32.2`) **and** `X-Remote-User-Id` (HA ingress session). Sibling add-ons on `172.30.0.0/16` can no longer mutate by calling the container directly. Reads stay on the LAN allowlist.
- **M2:** `api_play` pins `media_player` to add-on options; body override is rejected (`media_player-not-overridable`).
- Document ingress-app trust model in DOCS. `ingress_panel: false` set explicitly (sidebar stays OFF).
- Soft Slate list/detail UI unchanged.

## 0.1.3 — 2026-10-26
- Fix `in_ha=False` / mutation 503: start via S6 `/etc/services.d/voicemail/run` with `#!/command/with-contenv bash` so `SUPERVISOR_TOKEN` is loaded from the container environment (bare Dockerfile `CMD` ran under S6 legacy-services with a stripped env).
- Token reader also falls back to `/run/s6/container_environment/SUPERVISOR_TOKEN` (and `/var/run/...`) if the process env is still empty.
- `/api/diag` reports `in_ha` / token presence without leaking the token.
- Keep file-store fallback for list reads; Lovelace `voicemail-log/inbox` remains available. `ingress_panel` stays off.

## 0.1.2 — 2026-10-26
- File-store fallback: if `SUPERVISOR_TOKEN` is missing, read `/homeassistant/pyscript/.inbound_voicemails.json` (mapped `homeassistant_config:ro`) so the Soft Slate list still shows live inbox data.
- Clear/play/mark_heard still need Supervisor token (documented); Lovelace `voicemail-log/inbox` remains the mutation fallback.

## 0.1.1 — 2026-10-26
- Enable `hassio_api` + clearer Supervisor token detection (fixes `in_ha=False` / sensor 502).
- Allow Supervisor/add-on LAN `172.30.0.0/16` (ingress + ha-mcp proxies), not only `172.30.32.2`.

## 0.1.0 — 2026-10-26
- Initial Soft Slate iPhone list + detail UI (ingress App).
- HA sensor poll, clear one/all, mark heard, play via script, audio file proxy, tel: call-back.
- Mock mode for local preview. No public ports.
