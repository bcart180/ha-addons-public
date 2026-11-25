# CallCatch

Your Personalized Voicemail. iPhone-style house inbox (list + detail) behind Supervisor ingress. The Supervisor slug stays `voicemail`.

CallCatch stores the inbox itself. It does not require pyscript. An existing install that still has the pyscript file keeps the old services until you turn that off.

## Sidebar

`ingress_panel` is true, so CallCatch appears in the sidebar behind the Home Assistant login. The label is CallCatch. Set `ingress_panel` to false to hide the panel without stopping the App. No host ports are published.

CallCatch Voice (`twilio_house_voice`) is a separate App. It can post into this inbox over the internal network. This App does not send email and does not change Voice webhooks, signatures, or recording.

## Inbox store

The index is `/data/callcatch/inbox.json`. Writers take an exclusive lock, write a temp file, fsync, and rename it into place. Audio bytes are not stored inside the JSON.

When legacy mode is off, if either of these files exists, CallCatch imports it once:

- `/homeassistant/pyscript/.inbound_voicemails.json`
- `/config/pyscript/.inbound_voicemails.json`

While `legacy_pyscript` is `auto` or `on` and that file is still the live pyscript inbox, CallCatch does not copy it and does not write an import marker. After you set `legacy_pyscript` to `off` and restart, the copy runs. The import opens the file read-only. A marker under `/data/callcatch/legacy_import.json` records that the import finished, so a later start does not duplicate rows. The source file is never written. Audio found next to a known recording directory is copied into the new store; that copy is also read-only on the source.

`legacy_pyscript` controls the live inbox:

| Value | Behavior |
| --- | --- |
| `auto` (default) | On when a legacy file exists, off otherwise |
| `on` | Clear, mark heard, and speaker play keep calling the pyscript services and `script.voicemail_play_audio` |
| `off` | This App owns the inbox |

`auto` is what an existing install gets on update, so that install keeps working. A new Home Assistant with no legacy file uses the built-in store immediately. Restart the App after you change the option.

With the built-in store, this App implements:

| Action | Behavior |
| --- | --- |
| Clear one / Clear all | Deletes the row and its local audio. A Drive copy is queued for deletion. |
| Mark heard | Sets `heard` on that row. |
| Play in the app | `GET /api/audio/{call_id}` streams the bytes. The browser reaches it only through ingress. |
| Speaker | Optional. Uses `media_player.play_media` when `media_player` is set and the file is under `/media/callcatch`. |

`sensor.inbound_voicemails` (or `sensor_entity`) is published through the Core API. The state is the message count. Attributes include `voicemails`, `recent_items` (the same latest 20 rows), `unheard`, and `total`. Dashboards and notifications that already read this entity keep working. The sidebar App reads the full store, not only the recent window. In legacy mode the sensor is left to pyscript and is not overwritten. In legacy mode the App takes each row's numbers and transcript from the pyscript store file (`/homeassistant/pyscript/.inbound_voicemails.json`, mapped read-only), joined by `voicemail_id` (or the sensor's `vm-<short_id>` form for number-derived ids), so a sensor that publishes masked numbers and no transcripts still shows full rows and tap-to-call in the admin-only panel. If that file can't be read, the App shows the sensor rows as they are.

## Ingest from CallCatch Voice

`POST /api/ingest` accepts a transcript and audio. It is not on the public internet (this App publishes no host port). Callers must be on the Supervisor network and send the bearer token.

```
Authorization: Bearer <ingest_token>
Content-Type: application/json
```

The bearer is checked before the body is read. `X-Webhook-Token` and a JSON `token` field are not credentials. Voice still sends `Authorization: Bearer` when `ha_webhook_token` is set. A body that cannot be parsed gets `400` with `{"ok": false, "error": "bad-request"}` and no parser text.

JSON body (the CallCatch Voice hangup shape):

```json
{
  "call_id": "CA5550100",
  "from": "+15555550100",
  "ended_at": "2026-10-29T14:00:00+00:00",
  "duration_s": 12,
  "transcript_text": "Please call 555-555-0101.",
  "audio_base64": "<optional wav>"
}
```

The token is not stored and not logged. Drive file ids and `source` in the body are ignored. CallCatch sets those itself.

Multipart is also accepted: part `metadata` (JSON) and part `audio` (file).

A repeated `call_id` updates the same row and does not mark it unheard again.

If the JSON has no audio bytes, CallCatch copies a file it can already see at:

- `/media/voicemail/{call_id}.wav` (where CallCatch Voice mirrors recordings)
- `/share/twilio_house_voice/voicemail/{call_id}.wav`

### Voice options

Leave Voice's webhook behavior as it is. Point it at this App:

| Voice option | Value |
| --- | --- |
| `ha_webhook_transcript` | `http://local-voicemail:8099/api/ingest` or `http://<repohash>-voicemail:8099/api/ingest` |
| `ha_webhook_audio` | the same URL (Voice then posts once) |
| `ha_webhook_token` | the same secret as `ingest_token` |
| `include_audio_base64` | optional; large recordings are still picked up from `/media/voicemail` when both Apps have the media mount |

Use the internal hostname, not a public name and not a Nabu Casa hook. A local App is `local-voicemail`. An App installed from a repository is `<repohash>-voicemail`, where `<repohash>` is the repository id (8 hex characters) shown on the App page. The port is the port this App listens on, `8099`.

Set `legacy_pyscript` to `off` once Voice posts here, or the sidebar will keep showing the pyscript inbox.

## Storage

`storage_backend` is `local` or `google_drive`.

### local (default)

Audio is written to the first writable directory:

1. `/media/callcatch` (Home Assistant Media → `callcatch`)
2. `/share/callcatch`
3. `/data/callcatch/audio` if neither mount is writable

Nothing new is written under `/local` or `www`. `audio_dir` is only a read-only fallback for files that are already there.

The speaker button can play a file under `/media/callcatch` on the `media_player` you set. Leave `media_player` blank to use only the in-app scrubber.

### google_drive

Each voicemail becomes two files in the folder id you supply: `{call_id}.wav` (or the real extension) and `{call_id}.json` (the transcript). The scope is only:

`https://www.googleapis.com/auth/drive.file`

Requests ask Drive for `fields=id`. CallCatch never calls the permissions API, never sets `anyone`, and never stores a sharing link. Playback downloads the bytes with the same credential and streams them to the ingress session (`Cache-Control: private`).

`keep_local_copy` (default true) also keeps the file on disk. When it is false, the local file is removed after a successful upload. The in-app player still streams from Drive. The speaker button needs the Media copy, so leave `keep_local_copy` on if you use a speaker.

Uploads sit in `/data/callcatch/drive_queue.json` when Drive is offline. The queue retries with backoff (1s, 2s, 4s, … capped at 15 minutes) and stops after repeated permanent failures. A restart tries again. Credentials are held in memory and scrubbed from log lines.

If both a service account JSON and an OAuth refresh token are set, the service account is used.

#### Service account

1. In Google Cloud, enable the Google Drive API.
2. Create a service account and a JSON key.
3. Paste the entire JSON into `google_service_account_json`. It is a password field.
4. Create a folder in Drive (or a shared drive). Copy the folder id from the URL (`.../folders/{id}`).
5. Share that folder with the service account email as a person (Editor or Content manager). Do not use "anyone with the link".
6. Set `google_drive_folder_id` to that id.
7. Restart CallCatch.

The service account must be allowed to add files to that folder. `drive.file` does not grant access to the rest of Drive. Shared drives need the account as a member; uploads send `supportsAllDrives=true`, which is not a public link.

#### OAuth refresh token

Use this when a person owns the folder and you do not want a service account.

1. Create an OAuth client (Desktop) in the same Cloud project, with the Drive API enabled.
2. Mint a refresh token whose only Drive scope is `https://www.googleapis.com/auth/drive.file`.
3. Set `google_oauth_client_id`, `google_oauth_client_secret` (password), and `google_oauth_refresh_token` (password).
4. Set `google_drive_folder_id`. The Google account that minted the token must be able to create files in that folder.
5. Leave the service-account field empty. Restart CallCatch.

A refresh request sends that same `drive.file` scope. Do not mint the token with `drive` or `drive.readonly`.

If Google returns 403, the folder is not visible to this scope. Fix the share or the folder id. CallCatch will not fall back to a broader scope.

## Options a new user sets

| Option | New install |
| --- | --- |
| `ingest_token` | A long random secret. The same value goes in Voice `ha_webhook_token`. |
| `storage_backend` | `local`, or `google_drive` plus the folder id and one credential. |
| `media_player` | Leave blank unless a speaker should play recordings. |
| `sensor_entity` | Leave `sensor.inbound_voicemails` unless dashboards use another id. |
| `legacy_pyscript` | Leave `auto`. |
| `keep_local_copy` | Leave on unless you want Drive-only bytes after upload. |

`mock` is for local preview only. `log_level` defaults to info. `audio_dir` stays empty.

## Actions in the sidebar

| UI | Behavior |
| --- | --- |
| Call back | Queues a pending callback request. Does not dial. See below. |
| Needs callback / Urgent / Spam / Done | Filters the list. The badge counts needs-callback follow-ups, not unheard messages. |
| Prepare draft | Shows a text reply and a call brief from transcript facts. Copy or queue it. Nothing is sent. |
| Done / Snooze | Moves follow-up state. Snooze holds the prior state until the time you pick. |
| Play | In-app audio from `/api/audio/{call_id}` |
| Speaker | Configured `media_player` only, when a Media file exists |
| Trash / Clear all | Built-in store, or pyscript while legacy mode is on |
| Open row | Marks heard |
| Add Contact | Copies the number |
| Report Spam | Confirms, then adds the number to the block list in CallCatch Voice. The voicemail stays. Undo for 10 seconds |
| Always Allow | Adds the number to the allow list (allowed callers skip screening). Undo for 10 seconds |
| Numbers (shield) | Lists blocked and allowed numbers; add or remove, each with Undo |
| Caller ID badge | Twilio STIR/SHAKEN result for that call (verified A/B/C, failed, not verified). Signal only |
| Open on phone | Secondary `tel:` link. Not the Call back action. |

## Callback requests (0.5.7)

Call back does not place a call. It records a **pending callback request** for that voicemail and fires the Home Assistant event `callcatch_callback_requested`. A person approves any real outbound call somewhere else. This add-on never dials a third party: no Twilio `<Dial>`, no outbound SIP, and no `place_phone_call`, including when `dry_run` is on and when `mock` is on. The request is still saved in those cases.

| | |
| --- | --- |
| `POST /api/request_callback` | Body `{"voicemail_id": "…"}`. Sets `callback_status` to `pending` and `callback_requested_at` to an ISO timestamp. Ingress mutation gate (same as clear / mark heard). A second request while pending returns the current pending row and does not fire another event. |
| `POST /api/callback_status` | Body `{"voicemail_id": "…", "status": "returned"\|"failed"\|"cancelled", "note": "optional"}`. Only from `pending`. The same status again is a no-op. Unknown id is `404`. |
| Event | `callcatch_callback_requested` with `voicemail_id`, `from_masked` (`•••1234`), `display_name` (spoken name, if any), `summary` (short, numbers masked), `requested_at`. No full E.164, because the shared sensor does not publish From either. |
| Panel | **Call back** asks for the request. While pending: **Mark returned** and **Cancel request**. After a return: `Returned · {local time}`. **Open on phone** is the optional `tel:` link. |
| Sensor | `callback_status` and `callback_requested_at` only. No callback note and no phone number. Rows also carry `followup_state`, `known_contact`, and `alert_silent` (no note, draft, history, or number). The attribute `needs_callback` counts follow-ups that are not hangups or spam. `quiet` is the policy below, without a live clock. |

An unknown id is rejected. Logs use the existing mask (`•••1234`) and never include a full number or a token.

## Follow-up state (0.6.0)

Stored fields: `followup_state`, `followup_due`, `followup_prior`, `draft_id`, `call_ref`, `outcome_note` (at most 280 characters), `last_actor` (`bryson` or `agent`), and `history`. Ingest cannot set them. A row saved before 0.6.0 keeps working: a pending callback reads as `queued`, and a transcript that asks for a call reads as `needs_callback` until you change it.

| | |
| --- | --- |
| `POST /api/followup` | Body `{"id": "…", "state": "…", "snooze_until": "optional ISO-8601"}`. `voicemail_id` is accepted as `id`. Ingress mutation gate. `actor` is `bryson` (default) or `agent`. Moving to `approved` requires `number` (the caller or a spoken number) and `purpose` (a verbatim transcript phrase). Moving to `in_progress` requires that approved record. An illegal move, including `new` → `in_progress`, is `409`. A URL, script, or other unsafe text is `400`. |
| Snooze | `state` `snoozed` needs `snooze_until`. The previous state is restored by posting that state, or on its own once the time has passed. |
| Draft | The panel builds it from the card. Queue draft only records `draft_ready`. CallCatch does not send mail or place a call. |
| Compatibility | `request_callback` still sets `callback_status` `pending` and fires the same masked event. `callback_status` still sets `returned`, `failed`, or `cancelled` from `pending`. Those writes update `followup_state` to match. |

## Queued callbacks (read-only)

`GET /api/callback_requests` lists rows that are `callback_status` `pending` or follow-up `queued`, except a row that is still snoozed. POST to that path is `405`. `GET /api/callback_status` is not implemented (`404`). The body has no transcript, no callback digits, and no full number.

Same read gate as `GET /api/voicemails`: the ingress proxy plus `X-Remote-User-Id` (or legacy `X-Hass-User-ID`), or `Authorization: Bearer` with `ingest_token`.

```json
{
  "ok": true,
  "v": "0.6.0",
  "count": 1,
  "quiet": {
    "tz": "America/New_York",
    "start": "00:00:00",
    "end": "06:00:00",
    "active": false,
    "exception": "urgent_known_contact"
  },
  "callbacks": [
    {
      "voicemail_id": "vm-1",
      "followup_state": "queued",
      "callback_status": "pending",
      "callback_requested_at": "2026-11-06T15:04:00.000+00:00",
      "from_masked": "•••0100",
      "display_name": "Sam",
      "purpose": "about the gate",
      "urgency": "normal",
      "known_contact": true,
      "medical": false,
      "check_transcript": false,
      "alert_silent": false,
      "quiet_hold": false
    }
  ]
}
```

`quiet.active` is true from midnight until 06:00 America/New_York. `quiet_hold` is true in that window unless the row is urgent and the caller is a known contact (a saved name, or trust allow). A medical row sets `check_transcript` and leaves `purpose` empty. `purpose` is at most 120 characters and numbers in it are last-4 only.

## Quiet hours

The default window is midnight–06:00 ET. The blueprint (`quiet_start` `00:00:00`, `quiet_end` `06:00:00`, Home Assistant local time) delivers silently in that window. An urgent voicemail from a known contact is the exception. Hangup (under 5 seconds) and spam are logbook only (`alert_silent`) and are not in the needs-callback count. This does not change mail routing or the inbound voicemail script.

## Allow and block lists

CallCatch Voice owns the lists (`/data/trust_lists.json` in that App). A blocked caller is rejected with TwiML `<Reject>` before the call is answered; an allowed caller skips the screening questions. STIR/SHAKEN is recorded with each call and shown as a badge, but it is a signal only: it never blocks a call or hides a voicemail.

To edit the lists from this inbox, set both options and restart:

| Option | Value |
| --- | --- |
| `voice_trust_url` | `http://<repohash>-twilio-house-voice:8787` (or `http://local-twilio-house-voice:8787`). Only `http://<hostname>:<port>` with no dots is accepted, so it cannot point at a public host |
| `voice_trust_token` | The same secret as `trust_api_token` in CallCatch Voice. Password field, never logged |

The inbox routes `GET /api/trust`, `POST /api/trust/add`, and `POST /api/trust/remove` take the mutation gate (ingress proxy plus `X-Remote-User-Id`). The ingest bearer is not accepted for them. The App then forwards to Voice with `Authorization: Bearer <voice_trust_token>`. With either option blank, the Numbers page says lists are not configured and Report Spam does nothing.


A blocked number is rejected only when the carrier attests it at STIR level A. Otherwise the call is screened and lands in the inbox tagged `blocked`, in case someone was spoofing a real number.

## Trust model (ingress)
## Phone notifications (blueprint)

`blueprints/callcatch_new_voicemail.yaml` is an automation blueprint. To use it, go to Settings → Automations & Scenes → Blueprints → Import and paste the raw GitHub URL. Then create one automation per phone.

- **One push per new voicemail.** It triggers when the newest `recent_items` entry on `sensor.inbound_voicemails` changes and is unheard. The push shows the caller, a "caller ID verified" or "failed check" label from STIR/SHAKEN (a label only), and the first transcript line.
- **Buttons:** Call back, Mark heard, Open CallCatch (the ingress panel `/47b3c4e5_voicemail`). Call back is shown for a known caller (sensor `caller_masked` is `•••` plus 4 digits, or `known_contact`). Withheld callers get Mark heard and Open CallCatch only. The push has no `tel:` link.
- **Action IDs** are `ACTION_<context.id>_CALLBACK|HEARD`, so two notifications can never answer each other.
- **Call back** fires `callcatch_command` `{cmd: request_callback, voicemail_id}`. CallCatch queues `callback_status=pending` and fires `callcatch_callback_requested` with a masked number. A second press while pending does not fire another event. This does not dial.
- **Mark heard** fires `callcatch_command` `{cmd: mark_heard, voicemail_id}`. CallCatch subscribes to that event on the Supervisor Core websocket with its own Supervisor token and ignores every other `cmd`.
- **Block** is not on the push. The sensor has no full number, so the push cannot block. Report Spam in the inbox still adds the number to the CallCatch Voice block list.
- **Quiet hours** (default 00:00–06:00, HA local time, which is ET for this house) deliver silently: iOS `interruption-level: passive` with no sound, and the Android `CallCatch quiet` channel at low importance.
- **`dry_run` defaults to ON.** It writes a logbook line ("Would notify…") and stops: no push, no queue, no mark heard.
- **Exactly one push per voicemail.** Today `pyscript/inbound_voicemail.py` sends the voicemail push (`notify.mobile_app_<your_phone>`, tag `voicemail_<short_id>`). The blueprint pushes only when `sensor.voicemail_pipeline` reports `push_owner: blueprint`. Any other value, or no value, means it logs "skipped" and stops. It also uses the same tag and `apns-collapse-id`, so an overlap would replace in place rather than stack.
- **Keep the blueprint's dry_run ON until the pyscript `inbound_voicemail.py` push is turned off** (set `PUSH_OWNER = "blueprint"` there, staged separately in ha-automation-tests, which needs its own CoS GO). Do not change pyscript from this add-on.



Home Assistant authenticates the user before Supervisor proxies into this App. This App does not invent a second login.

| Surface | Gate |
| --- | --- |
| Health (`/api/health`) | Open inside the container so the Docker health check can reach it. No host port is published. |
| Inbox, audio, diag (`/api/voicemails`, `/api/audio/{call_id}`, `/api/diag`) | Ingress proxy `172.30.32.2` plus `X-Remote-User-Id` (or legacy `X-Hass-User-ID`), or `Authorization: Bearer` with `ingest_token`. A sibling on `172.30.0.0/16` without one of those is refused. |
| Mutations (`/api/clear_*`, `/api/mark_heard`, `/api/play`, `/api/trust*`, `/api/request_callback`, `/api/callback_status`, `/api/followup`) | Peer must be the ingress proxy `172.30.32.2` and the request must carry `X-Remote-User-Id` (or legacy `X-Hass-User-ID`) |
| Queued callbacks (`GET /api/callback_requests`) | Same gate as the inbox. GET only. Masked metadata, no transcript. |
| Ingest (`/api/ingest`) | Supervisor network, plus `Authorization: Bearer`. The header is checked before the body is read. Ingress is not required, so Voice can post. |

Implications:

- The sidebar and Apps → Open Web UI can mark heard, clear, and play.
- A sibling App cannot clear or play by calling the container directly.
- `media_player` is options-only. The play API rejects a body override.
- Audio responses are `Cache-Control: private` and are not a Drive link.
- No host ports and no public Cloudflare Access.

## Supervisor token

The App starts under S6 `with-contenv` so `SUPERVISOR_TOKEN` is available. That token publishes the sensor and, when a speaker is set, calls `media_player.play_media`. It is not logged.

## Not in scope

- No Core restart.
- No public sharing link for recordings.
- Does not change CallCatch Voice, dry-run, or email sending.

## dry_run controls

CallCatch itself has no dry_run option. These are the house controls, by name. CallCatch only reads them; it never changes any of them.

| Control | Where | What ON means |
|---|---|---|
| `input_boolean.work_forward_dry_run` | HA helper, read by `pyscript/inbound_work_forward.py` (`DRY_RUN_ENTITY`) and referenced by `pyscript/inbound_voicemail.py` | Voicemail copies are logged only: no outbox, drain or SMTP send. A missing helper is treated as ON. |
| `dry_run` input, default `true` | `blueprints/callcatch_new_voicemail.yaml` | Logbook line ("Would notify…") only: no push, no block, no mark heard. |
| `push_owner` | `sensor.voicemail_pipeline` attribute, set by `PUSH_OWNER` in `pyscript/inbound_voicemail.py` | `pyscript` (today) means pyscript sends the push and the blueprint skips. Flipping it needs its own GO. |
| per-call `dry_run` | `callcatch_command` event data, pyscript service calls (`voicemail_heartbeat_run`, `voicemail_humanize_missed`), CallCatch Voice stdin commands | That one command logs and changes nothing. |

Verify without changing anything: `GET /api/diag` (ingress) returns

```json
"dry_run": {"work_forward_dry_run": "on", "push_owner": "pyscript", "blueprint_default_input": "on"}
```

`input_boolean.callcatch_paused` belongs to the legacy pyscript inbox (`pyscript/inbound_voicemail.py` returns early from its crons and its email-event handler while it is on).  CallCatch does not read it.  The CallCatch send and delete gate is `input_boolean.callcatch_notify_dry_run`, and the recording gate is the `telnyx_connection_ids` allowlist.

`blueprint_default_input` is a hard-coded constant: the blueprint's `dry_run` input default. It does not read or verify any live automation built from the blueprint. `unknown` means the entity could not be read (outside HA, or the entity is missing). The add-on options contain no dry_run value.


## Post-call summary (0.3.7)

Each voicemail gets a short structured summary: **caller name** (only if the caller said it), **reason**, **callback number** and **urgency** (`low`, `normal`, `urgent`). It appears as a card above the transcript, as a pill and reason line in the list, and in the new-voicemail notification.

- When CallCatch Voice includes a `summary` object in its ingest call, that summary is used after validation. Otherwise CallCatch derives one locally from the transcript with fixed rules. Neither path needs a new key or option.
- A callback number is shown only if it is the caller's own number or a number spoken in the call. A model or rule cannot add a number nobody said.
- The panel (admin-only) shows full numbers with tap-to-call. The shared sensor and push only carry masked numbers (`•••1234`).
- Urgent means words such as "urgent", "emergency", "asap", "right away", "leak" or "locked out". Low means phrases such as "no rush" or "just checking in". Everything else is normal. Always check the transcript; the summary is a convenience.

## Moving stores: import_from_share

When this add-on is moved to another add-on store, its `/data` does not move with it. The new install imports allow-listed files once, on its first start, from `/share/.migrate-2026-11-01/<slug>/` (`voicemail` or `twilio_house_voice`). The folder must be owned by root with mode 700 (`chmod 700`). Anything unexpected (a symlink, a file not on the allow-list, a file over 8 MB, invalid JSON) stops the import with nothing written, and the log names only the reason. After a successful import the add-on writes an import marker in `/data` and deletes the staged files. It never imports twice.

## Post-call email (0.8.5, 0.8.7)

When a call is finalized, CallCatch queues one email (still gated by the notify
dry-run switch).  The subject is the caller name, or a last-4 fallback, plus
the time in America/New_York: `Jordan Hale called at 17:15 ET`.  The HTML body
is black-friendly for Proton on iPhone (`#000` background, `#3E6AE1` links) and
includes:

- header: name, last-4, time, duration, early hangup yes/no
- short summary, when one exists
- verbatim transcript with 0.8.4 `Assistant` / `Caller` labels (unlabeled when
  the roles cannot be told apart)
- a note that the recording is in CallCatch, not attached to the email
- a home-network link to that inbox row when `panel_base_url` is set
  (`http://ha.home.arpa:8123` plus the ingress path and `#` plus the call
  reference)

`email_full_number` still adds a call-back line with the full number when it is
on.  The last-4 is shown once: as the heading for an unknown caller, or as a
subline under a name.  Times in the subject and body are 24-hour ET, like the
inbox.  Prose uses two spaces after periods.

## GDIT work-hours copy (0.8.6)

Off by default (`gdit_work_copy: false`).  Do not turn this on until a later
explicit go-live.  When the option is on, CallCatch may send a redacted copy of
the post-call Proton notice to a pinned work address, only after the Proton
notice, and only while Bryson is at work.

### Gates

- The option must be on.  Default is off (`reason=disabled`).
- The Proton notice must already have been sent, or would have been sent while
  `input_boolean.callcatch_notify_dry_run` is explicitly on.  If no Proton
  notice goes out (no recipient, sender missing, or the job never sends), no
  GDIT copy is sent (`reason=no_proton`).
- `binary_sensor.bryson_at_work` must be explicitly `on`.  There is no clock
  fallback.  `off` is `reason=not_at_work`.  `unknown`, `unavailable`, a
  missing entity, or an unreachable Home Assistant read is `reason=work_unknown`.
  All of those fail closed: no copy is sent.
- VA, medical, or financial callers are skipped entirely (`reason=sensitive`).
  Matching uses the contact name or label, and keywords in the summary or
  transcript.  Notes are not scanned.  The keyword list is conservative: short
  or ambiguous tokens such as bare `va`, `dr`, `bank`, or `doctor` are omitted
  so everyday callers are not dropped by accident.
- The same notify dry-run switch still applies.  With dry-run on, a copy that
  would have been sent is logged as `reason=dry_run` and is not enqueued, so
  turning the switch off later does not release a stored GDIT job.

### Recipient pin

The work copy goes only to a single exact-address pin kept in code (one
constant).  The address is not listed here.  The pin is a separate check from
the Proton pin.  `mail_to` still accepts only `proton.me` and
`protonmail.com`.  A distinct AgentMail category and Idempotency-Key
(`callcatch-gdit-<ref>`) keep the two messages from crossing.

### Redacted template

The body carries only:

- the contact name, when the name came from the private directory and is
  usable; otherwise the wording is `A caller` (CNAM and spoken names included)
- the time in America/New_York
- the sentence `See Proton for the full message.`

It has no transcript, summary, number or last-4, audio, or link.

### Sensitive keyword list

Matching runs on the resolved contact name and organization, and on the
summary or transcript.  The haystack is normalized with NFKC, a look-alike
fold, casefold, control-character strip, and punctuation/spacing collapse.
Adjacent single-letter tokens are joined, so spaced or punctuated spellings
(`V A`, `D.r.`) match.  The list fails toward skipping.

Phrases (substring after that normalize): `veterans affairs`,
`veteran's affairs`, `veterans administration`, `veterans health`, `the va`,
`va hospital`, `va clinic`, `va medical`, `va appointment`, `va health`,
`va benefits`, `va loan`, `va claim`, `va office`, `urgent care`,
`emergency room`, `medical center`, `medical centre`, `medical group`,
`health system`, `nursing home`, `lab results`, `internal revenue`,
`credit union`, `credit card`, `navy federal`, `walter reed`,
`bank of america`, `wells fargo`, `chase bank`, `capital one`,
`american express`, `charles schwab`, `h&r block`, `hr block`,
`loan servicer`, `collections agency`, `payday loan`, `roth ira`, `401(k)`,
`401k`, `social security`, `quest diagnostics`.

Word-bounded tokens (and compact forms of four or more letters): `va`,
`vamc`, `claim`, `benefits`, `dr`, `doctor`, `md`, `dds`, `therapist`,
`therapy`, `hospital`, `clinic`, `pharmacy`, `pharmacist`, `physician`,
`dentist`, `dental`, `dermatology`, `oncology`, `pediatric`, `pediatrics`,
`cardiology`, `radiology`, `hospice`, `medstar`, `medicaid`, `medicare`,
`prescription`, `insurance`, `tricare`, `usaa`, `bank`, `cvs`, `walgreens`,
`kaiser`, `citi`, `discover`, `paypal`, `brokerage`, `mortgage`, `loan`,
`newrez`, `fidelity`, `vanguard`, `turbotax`, `irs`, `labcorp`.

A pending GDIT job older than about 15 minutes is dropped and not retried.
When `gdit_work_copy` is off at startup, pending GDIT jobs are cleared.

Logs print a reason code only.  Names and numbers are never logged.


## Contacts → caller names (0.5.0)

Missed-call and voicemail alerts show a contact's name when the caller's
number is in the caller directory (flat `{"+15555550100": "Name"}`
map, E.164 keys). From 0.5.0 CallCatch can fill
`/share/callcatch/private/caller_names.json` from your iPhone contacts. The
pyscript reads that file and falls back to `/config/private/caller_names.json`
while it does not exist. CallCatch keeps `/config` read-only.

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| `contacts_sync_enabled` | `false` | Turn the iCloud sync on. |
| `icloud_apple_id` | blank | Apple ID email that owns the contacts. Never logged. |
| `icloud_app_password` | blank | App-specific password (password field). Never logged. |
| `contacts_sync_interval_min` | `30` | Minutes between syncs. Minimum 15. |

### Create an app-specific password

1. Go to https://appleid.apple.com and sign in.
2. Open **Sign-In and Security → App-Specific Passwords**.
3. Click **+**, name it `CallCatch`, and copy the password Apple shows.
4. In Home Assistant: **Settings → Add-ons → CallCatch → Configuration**, type
   your Apple ID email into `icloud_apple_id`, paste the password into
   `icloud_app_password`, turn on `contacts_sync_enabled`, save and restart.

Enter these yourself in the add-on options; never paste them into chat, a
ticket or the repo. To revoke access, delete the app-specific password on
appleid.apple.com.

### How it works

- ALL contacts in every iCloud address book are read over CardDAV
  (`contacts.icloud.com`), in memory. Credentials are only sent to
  `https://*.icloud.com`.
- Every phone number is normalized to E.164 (US 10-digit numbers get `+1`;
  extensions are dropped). Name: full name, else given + family, else company.
- **Merge:** iCloud names win over hand-kept names. Hand-kept numbers that are
  not in iCloud stay. Before the first write, the existing
  `/config/private/caller_names.json` (read-only) is copied once to `caller_names.manual.json`. **Edit
  `/share/callcatch/private/caller_names.manual.json` from then on**; `caller_names.json` is rewritten
  on every sync.
- Writes are atomic with mode 600. A sync that finds no numbers leaves the file
  alone. The pyscript picks up changes within seconds (mtime check); no restart.
- Logs show only counts and a status, for example
  `contacts: synced 412 numbers from 380 contacts`.

### .vcf upload (admin endpoint only)

The Numbers screen no longer shows an Upload .vcf control.  Contacts are
pulled directly.  `POST /api/contacts/upload` remains for a signed-in Home
Assistant ingress session only (5 MB limit).  A file sent there is parsed in
memory, merged into the private directory (existing iCloud-only entries are
kept), and discarded; it is never stored.  Names, numbers, and filenames are
never logged.

### .vcf drop folder (0.8.5, off by default)

A second, optional fallback.  Turn on `contacts_drop_import` in the add-on
options (it defaults to off), then copy a `.vcf` or `.vcard` export into
`/share/callcatch/import/` on the Home Assistant host.  When the option is off,
that folder is not read at all.  When it is on, the folder is created or
tightened to mode 0700, checked about every 30 seconds, and each file is read
in memory, merged into the private directory (iCloud-only entries are kept;
new numbers are also stored in the manual map so a later iCloud sync does not
drop them), and deleted on success.  Symlinks are skipped.  A file that fails
to parse is renamed with a `.rejected` suffix after the first attempt and
logged once (reason code only).  Filenames, names and numbers are never logged.

### Status

The same card shows last sync time, source (iCloud, upload, or drop), number and
contact counts, directory size and a status code (`ok`, `disabled`,
`not_configured`, `auth`, `network`, `http`, `parse`, `no_addressbook`,
`empty`, `write`). `/api/diag` carries the same fields under `contacts`.



## Carrier recordings (0.8.0)

Telnyx records the inbound call from the moment it is answered, dual channel, while the call stays bridged to the existing SIP voice agent. CallCatch does not answer, bridge, or start a recording itself: a command sent after the webhook would miss a hang-up of a couple of seconds. The portal (or the Call Control answer, if that is what bridges the call) starts the recording. CallCatch only receives it.

Two paths write the same job, deduped by recording id, then call session id, then call leg id, then call control id:

1. CallCatch Line `POST /telnyx/events` checks the Telnyx Ed25519 signature and relays a reduced event to `POST /api/agent/telnyx`.
2. A poller calls `GET /v2/recordings` (`filter[created_at][gte]`) so a missed webhook or a restart mid-call is still captured.

Audio is saved under `/data/callcatch/recordings` (directory 0700, files 0600, excluded from add-on backups). The job index (`jobs.json`) is in that same folder, so a backup does not keep transcripts, caller numbers, or download URLs. Deleting the inbox row blanks those three fields on the job. The download URL must be https, port 443, with no userinfo. The host must be a Telnyx name, or the recording bucket Telnyx documents (`telephony-recorder-prod` on `s3.amazonaws.com`, path-style or virtual-hosted). Resolved addresses in CGNAT (`100.64.0.0/10`) or any other non-public range are rejected. Bodies over 32 MB are rejected.  A 200 body with valid RIFF/WAVE, ID3, or MPEG magic is accepted even if the Content-Type is unexpected, except `text/html`, `application/json`, `application/xml`, `application/xhtml+xml`, any `text/*` type, any `+json` or `+xml` suffix, or a comma-joined type (always rejected, and the body is discarded without a full read).  A `bad-type` failure uses its own try counter and is retried three times (about 5 s, then 15 s, then 45 s); the failure after the 45 s wait is terminal.  The log records the Content-Type, which check failed, the HTTP status, and the byte count, never the body or the URL. Speech-to-text is `POST https://api.x.ai/v1/stt` (model `grok-voice-transcribe-2.0` unless `xai_stt_model` is set). Capture stays off unless `telnyx_api_key`, `xai_api_key`, and a valid `telnyx_connection_ids` allowlist are all set (0.8.1).  A recording or event from a connection that is not on the list is dropped before a job is written, and a stored job from such a connection closes as `skipped` without a download, transcript, row, email, or remote delete.  With the keys set but no valid list, capture is off; with only one of them, CallCatch logs a warning and does not hold email for a recording. The key is the option, or the matching environment variable when the option is blank. It is never logged.

Telnyx's copy is deleted (`DELETE /v2/recordings/{id}`) only after the local file and the transcript have been read back and match what was written, only when `telnyx_delete_remote` is on (the default), and only when `input_boolean.callcatch_notify_dry_run` is explicitly `off` (the same gate as the email). An explicit `on` does not delete. Missing, unavailable, or unknown does not delete. A failed download, a failed transcript, or a failed persist does not delete the remote copy. Failures retry with backoff, then stay `dead` on `/api/diag` with a failed flag and an error code. That failure is not stored as the transcript. A second recording of the same call is downloaded, transcribed, and deleted on its own. The call is still one row and one email, and the transcripts are combined in time order.

The recording is the same call as the agent turns when the caller and the start time match (or the Telnyx ids were already stored). That is one inbox row and one email. A call that ends before the agent runs a tool still produces that row and that email. A short or silent recording says `caller hung up after N seconds, no speech detected`. The panel shows the call, the verbatim transcript, and the in-app player. The shared sensor does not carry the transcript or a full number.

Speaker labels on a recording transcript are assigned only from the dual-channel STT result, and only when it is safe to do so. Telnyx puts the first call leg on channel A (index 0) and the rest on channel B; on a bridged SIP call the first leg is not always the inbound caller, so channel 0 is not assumed to be the caller. A channel is labeled Assistant only when there are exactly two channels with explicit, distinct integer indexes, both channels have speech tokens (an empty other channel is not treated as a non-match), both have word timings or neither does, an English assistant-opening cue (such as “you have reached,” “recorded line,” or “how may I”) appears in that channel’s first ~15 words, that channel speaks first when word timings exist, and the other channel does not match those cues anywhere. The phrase list is English-only. If any of those checks fail, or STT returns no per-channel split, the transcript is stored unlabeled rather than with a guessed speaker. The polish step only normalizes existing prefixes (house/agent → Assistant, caller → Caller); it does not invent labels.

`input_boolean.callcatch_notify_dry_run` is unchanged. Only an explicit `off` sends the email, and only an explicit `off` deletes the Telnyx recording. Leave it on until go-live.

### Telnyx settings at go-live

- On the SIP connection that bridges to the voice agent: inbound call recording on, channels dual, format wav, starting at answer. If a Call Control application answers the leg instead, answer with `record` = `record-from-answer`, `record_channels` = `dual`, `record_format` = `wav`. Do not take the call off the existing bridge.
- Webhook API version 2. Webhook URL is the tunnel path `/telnyx/events` on CallCatch Line.
- Copy the account public key (portal: Keys & Credentials → Public Key) into Line `telnyx_public_key`.
- In CallCatch, set `telnyx_api_key` and `xai_api_key`. Leave `telnyx_delete_remote` on.
- In CallCatch and in CallCatch Line, set `telnyx_connection_ids` to the id of the connection the CallCatch number uses (Telnyx portal: Voice → SIP Connections, or `GET /v2/connections`).  Only recordings and events from these connections are read, transcribed, emailed, or deleted.  Blank keeps capture off.

### New options

| Option | Default | Meaning |
| --- | --- | --- |
| `telnyx_api_key` | blank | List and delete recordings. Password. Never logged. |
| `telnyx_connection_ids` | blank | Allowlist of Telnyx connection ids (digits, comma or space separated, up to 10).  Blank or invalid: capture off.  Recordings from any other connection on the account are never downloaded, transcribed, emailed, or deleted. |
| `telnyx_delete_remote` | on | Delete the Telnyx copy only after a verified local save. |
| `telnyx_poll_s` | 30 | Seconds between recordings-list polls (15–300). |
| `xai_api_key` | blank | Speech-to-text key. Password. Never logged. |
| `xai_stt_model` | `grok-voice-transcribe-2.0` | Model on `POST /v1/stt`. |
| `recording_grace_s` | 180 | How long the one email waits for the verbatim transcript when a key is set. |

Local audio is removed 30 days after the job closes, or sooner when the inbox row is cleared. The speaker button does not play these files; they stay in the private folder and play in the panel.

## Phone agent (0.7.0, CallCatch Line)

CallCatch Line (separate add-on) is the internet-facing MCP server for a hosted phone agent.  It talks to CallCatch over the Supervisor network with `agent_token`.  CallCatch keeps one row per call in `/data/callcatch/agent_calls.json` and finalizes it once (agent message, call-ended webhook, closed session, or `agent_inactivity_s` of quiet).  The first finalization stores an inbox row (`source: phone_agent`), queues a callback if asked (no dialing), and enqueues one email and one masked push in `/data/callcatch/notify_outbox.json`.  Failed sends retry with backoff for about four hours, then park as `dead` (counts in `/api/diag`).

- Email: `mail_backend` `agentmail` (or `off`, the default).  CallCatch POSTs directly to `https://api.agentmail.to/v0/inboxes/<agentmail_inbox>/messages/send` with the `agentmail_api_key` bearer.  No agent, routine, or LLM sits in the path.  The rendered subject, text, and HTML go out verbatim, labels `callcatch` and `callcatch_phone_message`.  `mail_to` is one address on `proton.me` or `protonmail.com` (pinned in code); a list, another domain, or Gmail turns email off.  `agentmail_inbox` must be a dedicated CallCatch-only AgentMail inbox whose name starts with `callcatch` (`callcatch…@agentmail.to`); any other value, including a shared bot inbox, turns email off and logs one warning (the value is not logged).  See "Dedicated sender inbox" below.  A 4xx answer (for example a recipient suppression) parks the job as `dead`, shown in `/api/diag`.
- Push: `push_service` (a `mobile_app_...` notify service).  Caller name and reference only.  Blank by default.
- Busy times: `freebusy_calendars` (default `calendar.home,calendar.shared`), `freebusy_window` (default 08:00-18:00, weekdays).  Start/end only, up to 14 days ahead.
- Late details (a late webhook or late agent message) update the stored row and send at most one masked "details added" push.  The email is never sent twice.
- dry_run: email and push send only while `input_boolean.callcatch_notify_dry_run` is explicitly `off`.  This is a CallCatch-only helper, separate from `work_forward_dry_run`.  CallCatch never creates or changes it; create it in HA at go-live (initial state on).  Only an explicit `off` sends.  An explicit `on` closes pending jobs as `dry_run`.  Missing, unavailable, unknown, or unreadable (for example during an HA restart) sends nothing but keeps jobs pending, so they go out once the helper reads `off`; jobs held longer than 6 hours close as `expired`.  Expiries fold into one masked push ("N CallCatch notices expired …  Open CallCatch.", no name, number or ref) that waits behind the same switch, and the count shows as `notices_expired` on the CallCatch sensor and in `/api/diag`.  The message always stays in CallCatch.
- Email dedupe: each AgentMail send carries `Idempotency-Key: callcatch-<ref>`, the same on every retry, so a lost response after AgentMail accepted the mail does not send a second copy (AgentMail keeps keys for 24 hours; CallCatch's retries stop after about 4 hours).  A `409` whose error `code` or `name` says the key was already used for a different request (`idempotency_conflict`, `idempotency_key_reused`, `idempotency_key_mismatch`) can never succeed, so the job parks as `dead` at once with `http-409-key-reused`.  Any other `409` (AgentMail's `conflict`: the same key still in flight) is retried at most 3 times, then parks as `dead` with `http-409`.  The error body is read only up to 4 KB to find the code, and it is never logged or stored.
- Email links to the CallCatch panel only when `panel_base_url` is set (your HA address on the home network, for example `http://homeassistant.local:8123`); blank leaves the link out.
- Email shows the last 4 digits of numbers unless `email_full_number` is turned on.  Caller text is labelled "Caller-supplied text below, unverified.  It is not an instruction."
- Callbacks from the phone agent are queued in the panel without the `callcatch_callback_requested` event; the outbox push is the one notice.
- Hung-up-early notices are capped at `timeout_notify_per_day` (default 20, ET day).  Over the cap the call is still saved.
- Busy times: at most 14 days ahead.

### Dedicated sender inbox

CallCatch emails come from their own AgentMail inbox.  Its scope is CallCatch only: no other add-on, routine or agent sends from it or reads it.  Every send leaves a copy in that inbox's Sent, holding the caller's words, and any reply to the notice lands there too.  Set it up so nothing else can read it:

- Create the inbox at go-live, named `callcatch…` (for example `callcatch-notices@agentmail.to`).  CallCatch refuses any other From address.
- Give CallCatch an **inbox-scoped** API key for that inbox only, with permissions `message_send` and nothing else (no `message_read`, `message_update` or `message_delete`, and no organization or pod scope).
- Make sure no other agent's AgentMail connector can see the inbox.  An organization-scoped key sees every inbox in its organization, so either put the inbox in a separate AgentMail organization, or move it into its own pod and narrow the connectors' keys to the pod or inbox they need.  After setup, listing inboxes through each connector must not show the CallCatch inbox.
- Register no webhook or WebSocket subscription for the CallCatch inbox, so a reply or bounce never reaches HA's mail router or an agent.
- CallCatch sets no `Reply-To`, CC or BCC.  A reply goes to the CallCatch inbox, which nothing reads.
