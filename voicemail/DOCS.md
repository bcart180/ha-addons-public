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
| Call back | `tel:+1…` |
| Play | In-app audio from `/api/audio/{call_id}` |
| Speaker | Configured `media_player` only, when a Media file exists |
| Trash / Clear all | Built-in store, or pyscript while legacy mode is on |
| Open row | Marks heard |
| Add Contact | Copies the number |
| Report Spam | Confirms, then adds the number to the block list in CallCatch Voice. The voicemail stays. Undo for 10 seconds |
| Always Allow | Adds the number to the allow list (allowed callers skip screening). Undo for 10 seconds |
| Numbers (shield) | Lists blocked and allowed numbers; add or remove, each with Undo |
| Caller ID badge | Twilio STIR/SHAKEN result for that call (verified A/B/C, failed, not verified). Signal only |

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
- **Buttons:** Call back (`tel:`), Mark heard, Block, Open CallCatch (the ingress panel `/47b3c4e5_voicemail`). Withheld or non-numeric callers get no Call back or Block button.
- **Action IDs** are `ACTION_<context.id>_HEARD|BLOCK|UNDO`, so two notifications can never answer each other.
- **Mark heard** fires the HA event `callcatch_command` `{cmd: mark_heard, voicemail_id}`. CallCatch subscribes to that one event on the Supervisor Core websocket with its own Supervisor token and ignores every other `cmd`.
- **Block** runs `hassio.addon_stdin` on CallCatch Voice (`{cmd: block, number}`), then replaces the same notification (same tag, passive) with **Undo** (10 min).
- **Undo only unblocks** (`{cmd: unblock}`). It does not restore an earlier state: if the number was on the allow list before Block, it is not put back (re-add it in Numbers), and if you had already blocked it by hand, Undo removes that block too. The inbox's own Undo restores the previous list exactly.
- **Quiet hours** (default 00:00–06:00, HA local time, which is ET for this house) deliver silently: iOS `interruption-level: passive` with no sound, and the Android `CallCatch quiet` channel at low importance.
- **`dry_run` defaults to ON.** It writes a logbook line ("Would notify…") and stops: no push, no block, no mark heard.
- **Exactly one push per voicemail.** Today `pyscript/inbound_voicemail.py` sends the voicemail push (`notify.mobile_app_<your_phone>`, tag `voicemail_<short_id>`). The blueprint pushes only when `sensor.voicemail_pipeline` reports `push_owner: blueprint`. Any other value, or no value, means it logs "skipped" and stops. It also uses the same tag and `apns-collapse-id`, so an overlap would replace in place rather than stack.
- **Keep the blueprint's dry_run ON until the pyscript `inbound_voicemail.py` push is turned off** (set `PUSH_OWNER = "blueprint"` there, staged separately in ha-automation-tests, which needs its own CoS GO). Do not change pyscript from this add-on.



Home Assistant authenticates the user before Supervisor proxies into this App. This App does not invent a second login.

| Surface | Gate |
| --- | --- |
| Health (`/api/health`) | Open inside the container so the Docker health check can reach it. No host port is published. |
| Inbox, audio, diag (`/api/voicemails`, `/api/audio/{call_id}`, `/api/diag`) | Ingress proxy `172.30.32.2` plus `X-Remote-User-Id` (or legacy `X-Hass-User-ID`), or `Authorization: Bearer` with `ingest_token`. A sibling on `172.30.0.0/16` without one of those is refused. |
| Mutations (`/api/clear_*`, `/api/mark_heard`, `/api/play`, `/api/trust*`) | Peer must be the ingress proxy `172.30.32.2` and the request must carry `X-Remote-User-Id` (or legacy `X-Hass-User-ID`) |
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

`blueprint_default_input` is a hard-coded constant: the blueprint's `dry_run` input default. It does not read or verify any live automation built from the blueprint. `unknown` means the entity could not be read (outside HA, or the entity is missing). The add-on options contain no dry_run value.


## Post-call summary (0.3.7)

Each voicemail gets a short structured summary: **caller name** (only if the caller said it), **reason**, **callback number** and **urgency** (`low`, `normal`, `urgent`). It appears as a card above the transcript, as a pill and reason line in the list, and in the new-voicemail notification.

- When CallCatch Voice includes a `summary` object in its ingest call, that summary is used after validation. Otherwise CallCatch derives one locally from the transcript with fixed rules. Neither path needs a new key or option.
- A callback number is shown only if it is the caller's own number or a number spoken in the call. A model or rule cannot add a number nobody said.
- The panel (admin-only) shows full numbers with tap-to-call. The shared sensor and push only carry masked numbers (`•••1234`).
- Urgent means words such as "urgent", "emergency", "asap", "right away", "leak" or "locked out". Low means phrases such as "no rush" or "just checking in". Everything else is normal. Always check the transcript; the summary is a convenience.

## Moving stores: import_from_share

When this add-on is moved to another add-on store, its `/data` does not move with it. The new install imports allow-listed files once, on its first start, from `/share/.migrate-2026-11-01/<slug>/` (`voicemail` or `twilio_house_voice`). The folder must be owned by root with mode 700 (`chmod 700`). Anything unexpected (a symlink, a file not on the allow-list, a file over 8 MB, invalid JSON) stops the import with nothing written, and the log names only the reason. After a successful import the add-on writes an import marker in `/data` and deletes the staged files. It never imports twice.

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

### .vcf upload (fallback)

Numbers view (sidebar) → **Contacts** → **Upload .vcf**. Export contacts from
iCloud.com or the iPhone Contacts app as a `.vcf`. Limit 5 MB. The file is
parsed in memory, merged the same way and discarded; it is never stored. Only
signed-in Home Assistant ingress sessions can upload (the panel is admin-only).

### Status

The same card shows last sync time, source (iCloud or upload), number and
contact counts, directory size and a status code (`ok`, `disabled`,
`not_configured`, `auth`, `network`, `http`, `parse`, `no_addressbook`,
`empty`, `write`). `/api/diag` carries the same fields under `contacts`.

