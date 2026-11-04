# Changelog

## 0.5.6 — 2026-11-03

- New option `show_full_numbers` (default `false`). When on, the caller-number fields on the shared sensor and push (`caller_masked`, `caller_label_masked`, `summary_callback`) carry the full formatted number, e.g. `(555) 555-0142`, instead of `•••0142`; a contact title with a masked number gets the full number swapped in. Transcript-derived text (`summary_reason`, `summary_name`, `summary_fallback`, list preview) stays masked. Default behavior is unchanged. No data, API permission, webhook or dry_run change.

## 0.5.5 — 2026-11-03

- Privacy fix: the list preview could show a full phone number when a row had no summary reason (e.g. a snippet that is only a number), because `app.js` fell back to the raw snippet. `/api/voicemails` rows now carry a server-side `preview`: the summary reason, or else `call_summary.fallback_line` (header stripped, numbers masked to last 4). `app.js` uses only that field for the list. The detail view, tap-to-call and the share sheet still show the full number. Display-only; no data, API permission, webhook or dry_run change.

## 0.5.4 — 2026-11-02

- ReDoS fix (SA M2): `mask_phone_runs` look-alike pass (`_LOOKALIKE_TOKEN`, nested stars, O(n²)) rewritten as a linear maximal-run scan with identical output; masking input is capped at 2,000 chars (a number cut at the cap is dropped, never left unmasked; every caller already truncates to ≤160). Name patterns bounded (`{0,30}` per word) and name/reason/urgency read only the first 4,000 chars. `+` + `(-.`×10k: 4.0 s → 0.001 s; push fallback on 90k: 35 s → 0.03 s. New timing tests (`tests/test_redos_mask.py`, <1 s, incl. 100k inputs). Display-only; no data, API, permission or dry_run change.

## 0.5.3 — 2026-11-02

- Follow-up to 0.5.2: the live hangup header starts with a `Caller: <number>` line (and may carry `When:` / `Call ID:`), which the summary extractor read as a Caller speaker turn, so the reason still began with the masked number. Those header-only lines are now stripped too. A `Caller:` line with real speech is kept. Header stripping scans only the first 1,024 characters, uses bounded patterns (no ReDoS), and stops at the TRANSCRIPT marker, so lines after it (e.g. a spoken "When: …") are kept. Display-only.

## 0.5.2 — 2026-11-02

- List rows / push fallback: the hangup header (masked number, "Source: CallCatch", "Duration: N seconds", "TRANSCRIPT") is no longer baked into the caller summary or the first-line push fallback, so the preview starts with what the caller said. Saved summaries are cleaned at read time. Urgency and duration metadata unchanged; display-only, no data, API or dry_run change.

## 0.5.1 — 2026-11-02

- Remove '(new)' name suffix; no functional change.

## 0.5.0 — 2026-11-02

- **iCloud contacts → caller names (Phase 1).** New options `contacts_sync_enabled` (default `false`), `icloud_apple_id`, `icloud_app_password` (password) and `contacts_sync_interval_min` (default 30, minimum 15). When enabled, CallCatch reads ALL iCloud contacts over CardDAV (`contacts.icloud.com`: current-user-principal → addressbook-home-set → every address book), in memory, normalizes every TEL to E.164 (US 10 digits → `+1`, extensions dropped) and writes the flat `{"+1…": "Name"}` map to `/share/callcatch/private/caller_names.json` — the same flat format the pyscript caller-ID directory already reads. Needs the companion ha-automation-tests change so the pyscript reads the `/share` path (it falls back to `/config/private`).
- **Merge.** iCloud names override hand-kept names; hand-kept numbers not in iCloud are kept. Before the first write the existing hand-kept map (`/config/private/caller_names.json`, read-only) is copied once to `caller_names.manual.json`, which is the hand-edited source from then on. Writes are atomic (temp file, fsync, rename) with mode 600. A sync or upload that yields no numbers leaves the directory untouched; an unreadable existing directory is refused, not overwritten.
- **.vcf fallback.** Numbers view → Contacts → Upload .vcf (`POST /api/contacts/upload`): ingress session only, 5 MB limit, same parse/merge/write; the upload is never stored. `Sync now` (`POST /api/contacts/sync`) and `GET /api/contacts/status` are ingress-only too.
- **Status.** The Numbers view and `/api/diag` show last sync time, source, counts and a fixed status code only — never names, numbers, e-mail or the Apple ID.
- **Privacy.** Logs carry only counts and reason codes (`synced N numbers from M contacts`). Errors never echo credentials, URLs or response bodies. Credentials are only ever sent to `https://*.icloud.com`; redirects elsewhere are refused.
- **No new privilege.** `homeassistant_config` stays `ro`. The directory lives in `/share/callcatch/private/` (folder 700, files 600), which Home Assistant Core also sees as `/share`; the pyscript reads it there and falls back to `/config/private/caller_names.json` while it is missing. The old `/config/private` file is only read once, to seed `caller_names.manual.json`.
- **Hardening.** Ingress POSTs (clear, play, mark heard, trust lists, contacts) are refused with 403 when `Sec-Fetch-Site` is present and not `same-origin`. `panel_admin: true` is now explicit.
- Tests: `tests/test_contacts_sync.py` (vCard edge cases, normalization, merge precedence, atomic write + 0600, log redaction, upload auth and size limit, mocked CardDAV, non-iCloud host refusal). Fake 555-01xx numbers and fake names only. dry_run handling unchanged.

## 0.4.4 — 2026-11-02

- List rows (iPhone): the full caller name or number now wraps instead of being cut off with "…"; the date (and duration, if known) sits on its own small muted line; the preview shows up to 3 lines of the summary or transcript. The hangup header (masked number, "Source: CallCatch", "Duration_s") is stripped from the list preview only. Urgent tag kept. Display-only: no data, API or dry_run change.

## 0.4.3 — 2026-11-02

- Least privilege: removed `hassio_api: true`. The add-on only talks to Home Assistant Core through the Supervisor proxy (`http://supervisor/core/api/...` and `ws://supervisor/core/websocket`), which `homeassistant_api: true` covers. `tools/wire_voice_link.py` (Supervisor add-on options) is not shipped in the image and runs from an SSH/other add-on. `share:rw` / `media:rw` stay: new recordings are written under `/media/callcatch` or `/share/callcatch`.
- Tests: removed the hashed word and phone deny-lists; the fixture guards are now generic (only fictional 555 numbers, no email addresses or private IPs in the summarizer). No behavior change; dry_run handling unchanged.

## 0.4.2 — 2026-11-01

- Quieter logs: successful `GET /api/health` access lines (watchdog / health checks) are no longer logged. The filter is anchored on the exact request line and a 2xx status, so health errors, other methods, query strings and paths like `/api/healthz` still log (new tests). No other change; dry_run handling unchanged.

## 0.4.1 — 2026-11-01

- An empty share-import staging folder is now logged at info and reported as status `staging_empty` (was an error "refused: staging_empty"). Nothing is imported or deleted; other refusals are unchanged. dry_run handling unchanged.

## 0.4.0 — 2026-11-01

- Public store marker: name suffix (new); no functional change.

## 0.3.8 — 2026-11-01

- **One-shot `import_from_share` for moving stores.** On start, before the inbox opens, CallCatch imports allow-listed `/data` files that the owner staged in `/share/.migrate-2026-11-01/voicemail/`: `inbox.json`, `legacy_import.json` and `drive_queue.json` (into `/data/callcatch/`). No new option, privilege or mapping (`share:rw` was already mapped).
  - The staging root and folder must be real directories owned by root with mode 700. Symlinks, any file not on the allow-list (including `options.json`), files over 8 MB and invalid JSON are refused, and a refusal imports nothing.
  - Files are copied byte-for-byte (temp file, fsync, rename). Contents are never logged; logs and `/api/diag` carry only file names, sizes, entry counts and reason codes.
  - Success writes `/data/callcatch/.import_from_share.json` and then deletes the staged files and folder. With the marker present it never imports again; a staging folder that reappears is flagged, not imported.
  - `/api/diag` adds `import_from_share` (status) and `import_marker` (true/false).
- Tests: `tests/test_share_import.py` (allow-list, symlink, size, JSON, permission and owner refusals, one-shot, staging deleted, no unmasked number in import output). Fixtures use 555-01xx numbers only.

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

- **Soft Slate Voicemail sidebar ON (tip):** `ingress_panel: true` after CoS GO (the owner chose option A ~7:35 AM ET America/New_York). Soft Slate Recents returns as the HA sidebar app.
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
