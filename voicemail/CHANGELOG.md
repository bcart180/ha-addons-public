# Changelog

## 0.8.9 — 2026-11-26

- **Sender allowlist.**  `agentmail_inbox` may be a dedicated `callcatch…@agentmail.to` inbox, or the single address `callcatch@callcatch.brysoncarter.com`.  No other address at brysoncarter.com is accepted.  `mail_ready` requires that allowlist: a filled-in inbox that is not on it leaves email off.  The Proton recipient pin (`proton.me` and `protonmail.com`) is unchanged.
- **Opt-in sending-domain setup.**  New option `agentmail_domain_setup` (default off).  When on, CallCatch uses its own AgentMail API key in place to register the send-only domain `callcatch.brysoncarter.com` (`inbound_enabled` false, `subdomains_enabled` false), shows the public DNS records AgentMail returns on `/api/diag` (type, name, value, priority, status), asks AgentMail to verify, and creates `callcatch@callcatch.brysoncarter.com` once the domain is verified.  A later run does not create another domain or inbox.  The key is never logged or returned.  CallCatch does not change DNS and does not call Cloudflare.

## 0.8.8 — 2026-11-25

- **Content-free GDIT notice.**  The redacted-transcript work copy is retired.  Full transcripts still go only to the CallCatch inbox and Proton.  When `gdit_work_copy` is on, the pinned work address gets only a missed-call notice: voicemail left yes/no, and the 24-hour ET time.  No name, number, transcript, audio, or link.
- **Presence gate.**  The notice is sent only when `person.bryson` is in the Home Assistant Work zone (`zone.work`).  `home`, `not_home`, and other zones are `not_at_work`.  `unknown`, `unavailable`, a missing entity, or an unreachable Home Assistant read is `work_unknown`.  Both fail closed.  The earlier `binary_sensor.bryson_at_work` gate is no longer used.
- **Send-time re-check.**  The presence gate is checked again at send time, and a pending notice older than about 15 minutes is still dropped.  Dry-run behavior is unchanged.  The option still defaults to off.
- **SA leftovers from 0.8.7.**  Tests derive work-address variants from the code constant (no local-part literals).  The unused Upload .vcf listener is removed from the inbox script.
- **Security Audit PR #77 (L1, L2, I3).**  "Voicemail left" uses the same recording rule as the Proton notice (`recording_file`, `has_audio`, or `verbatim_at`), so phone-agent calls are not always "No".  The presence gate requires `person.bryson` both to have the Work state and to appear in `zone.work`'s `persons` attribute; a second zone named Work does not pass, and a failed zone read is `work_unknown`.  The send-time presence check is required; a missing check fails closed.

## 0.8.7 — 2026-11-24

- **Email last-4 once.**  An unknown-caller heading that is already "Number ending XXXX" no longer repeats that line in the HTML body.  The plain-text path already skipped it.
- **Numbers form stacks on a phone.**  At 400 px and below, the number field is full width, with Block and Add on the next row, so the placeholder is not clipped.
- **Inbox chrome.**  The document title and inbox `h1` say CallCatch.  The shield button is labeled "Allowed, blocked, and contacts".
- **24-hour ET in email.**  The subject and body use 24-hour America/New_York time, matching the inbox.
- **Upload .vcf is hidden.**  Contacts are pulled directly, so the Numbers screen no longer shows an upload control or help text.  `POST /api/contacts/upload` stays available to a signed-in ingress admin session.  `contacts_drop_import` stays off by default.  Names and numbers are never logged.
- **GDIT skip list (N1, N2).**  Additional medical and financial stems are matched, with word-boundary-only matching for short tokens such as ally, tax, and pnc.  Latin small capitals fold into the Latin letters.  The pinned work address is not written in `notify_send.py` or the tests (the code constant is used instead).  `gdit_work_copy` still defaults to off.  Dry-run behavior is unchanged.

## 0.8.6 — 2026-11-24

- **Optional GDIT work-hours copy.**  New option `gdit_work_copy` (default `false`).  When on, a redacted copy is considered only after the Proton notice has been sent, or would have been sent while `input_boolean.callcatch_notify_dry_run` is on (would-send is logged, nothing is sent).  The recipient is a single exact-address pin in code; the existing Proton pin is unchanged.  The work address is not published in this file or in the documentation.
- **At-work gate, fail closed.**  The copy is sent only when `binary_sensor.bryson_at_work` is explicitly `on`.  There is no clock fallback.  If the sensor is `off`, `unknown`, `unavailable`, or Home Assistant is unreachable, no GDIT copy is sent.
- **Redacted template.**  The copy names the contact (or "A caller" for CNAM, spoken, or unusable names), the time in America/New_York, and "See Proton for the full message."  It has no transcript, summary, number or last-4, audio, or link.  The contact name is taken from `fields.caller_name_source` after identity is applied and written back onto the stored call row.
- **Sensitive-caller skip.**  VA, medical, or financial callers are skipped entirely.  Matching uses the resolved contact name and organization, plus a documented keyword list in the summary or transcript, after NFKC, casefold, look-alike folding, and punctuation/spacing collapse (fail toward skipping).  Reason codes only are logged (`disabled`, `no_proton`, `work_unknown`, `not_at_work`, `sensitive`, `dry_run`, `stale`, plus send success).  Names and numbers are never logged.
- **Stale GDIT jobs.**  A pending work copy older than about 15 minutes is refused and not retried.  When `gdit_work_copy` is off at startup, pending GDIT jobs are cleared.

## 0.8.5 — 2026-11-24

- **Post-call email format.**  The Proton notice subject is `{caller} called at {time} ET`.  The body is black-friendly (`#000` background, `#3E6AE1` accent) and shows the caller name (or a last-4 fallback), last-4, time, duration, and early hangup yes/no, then a short summary, the verbatim transcript with 0.8.4 `Assistant` / `Caller` labels, a note that the recording lives in CallCatch (it is not attached), and a home-network link to that inbox row when `panel_base_url` is set.  Two spaces after periods are preserved in HTML.  Caller-supplied sections still follow the untrusted-text notice.  Test mode (`dry_run`) and Telnyx delete settings are unchanged.
- **Caller names on notices and the inbox.**  A number is normalized to E.164 and looked up in the private directory (`/share/callcatch/private/caller_names.json`, mode 0600) first, then Telnyx CNAM if one was provided, then a usable spoken name.  When the name is from CNAM, the subject, header, and push title append ` (caller ID)`.  Logs still print last-4 only.  Contact names, numbers, and CNAM are never logged.
- **Optional drop-folder re-import.**  Off by default (`contacts_drop_import: false`).  When on, a `.vcf` or `.vcard` placed in `/share/callcatch/import/` (always mode 0700) is parsed in memory, merged into the private directory without wiping iCloud-only entries, and deleted on success.  Symlinks are skipped.  A failed file is renamed `.rejected` and logged once.  Filenames are never logged.  The authenticated admin **Upload .vcf** button remains the normal import route; optional iCloud CardDAV sync still works.
- **Security Audit PR #74 (M1, M2, L1–L4, I1).**  TEL normalization caps the input at 64 characters before the extension-split regex (M1).  The share drop folder is gated behind `contacts_drop_import` defaulting off; drop and admin upload merge instead of replacing (M2).  Drop import skips symlinks (L1), rejects a bad file after the first failure (L2), and always chmods the folder to 0700 (L3).  CNAM names are labeled `(caller ID)` on notices (L4).  `server.py` `VERSION` is 0.8.5 (I1).

## 0.8.4 — 2026-11-21

- **Recording transcripts no longer label the assistant as the caller.**  Telnyx dual-channel audio puts the first call leg on channel A (index 0) and the rest on channel B, but on a bridged SIP call that first leg is not always the inbound caller.  CallCatch no longer assumes channel 0 is the caller.  When exactly one channel looks like the assistant opening, that channel is labeled Assistant and the other is Caller, including when the opening is on channel A.  If the roles cannot be told apart, or the STT result has no per-channel split, the transcript is left unlabeled rather than given a wrong speaker.
- **Security Audit M-1, I-1, and I-2.**  Role assignment now requires exactly two channels with explicit, distinct integer indexes, an English assistant-opening match in that channel's first ~15 words, that channel speaking first when word timings exist, and no match anywhere on the other channel.  `recorded line` is word-bounded.  Detection uses the same tokens as the labels (words when present).  Otherwise the transcript stays unlabeled.  A leading `Assistant:` or `Caller:` (or house/agent) on an unlabeled line is neutralized so it is not treated as a speaker label.  The rule is documented in DOCS.md; the phrase list is English-only.
- **Security Audit R-1, R-2, and I-3.**  An empty other channel no longer counts as a non-match, so a lone caller cue is left unlabeled and stays in `caller_text`.  Roles are assigned only when both channels have word timings or neither does, so a text-only channel is not dropped from a mixed result.  Unlabeled neutralization splits on `\r` as well as `\n`, and newlines inside a labeled text-only channel are flattened to spaces.

## 0.8.3 — 2026-11-21

- **Follow-up to 0.8.2: Security Audit M-1, L-1, L-2, and I-1.**  Hard-reject types now also include any `text/*` type, any `+json` or `+xml` suffix, `application/xhtml+xml`, and any comma-joined Content-Type, in addition to `text/html`, `application/json`, and `application/xml` (M-1).  A hard-reject type is discarded at the headers, without reading the body past the usual discard cap (L-1).  `bad-type` uses its own try counter, so earlier download failures do not spend its budget: three backoffs of about 5 s, then 15 s, then 45 s, and the failure after the 45 s wait is terminal (L-2).  Unused `ALLOWED_TYPES` and `content_type_ok` are removed (I-1).

## 0.8.2 — 2026-11-21

- **Recording download: accept audio by magic, retry `bad-type`.**  A 200 body whose bytes are a valid RIFF/WAVE, ID3, or MPEG frame is kept even when the Content-Type is unexpected.  `text/html`, `application/json`, and `application/xml` are still rejected (`check=header`), even if the bytes look like audio.  A wrong type whose bytes are also not audio still fails (`check=magic`).  `bad-type` is no longer terminal on the first try: three attempts, waiting about 5 s then 15 s between them; the third failure is terminal.  The log records the Content-Type, which check failed, the HTTP status, and the byte count.  It never logs the body or the URL.

## 0.8.1 — 2026-11-20

- **Connection allowlist for carrier recordings (fail closed).**  New option `telnyx_connection_ids`: the Telnyx connection ids (comma or space separated, digits only, up to 10) whose recordings CallCatch may read.  Blank, too many, or any bad token means capture stays off: no poll, no job, no download, no transcript, no email, and no remote delete.  Capture now needs both API keys **and** a valid allowlist; with the keys set and the list blank, CallCatch logs one warning and does not hold email for a recording.
- **Other connections are never touched.**  A recordings-list item or a relayed webhook event whose `connection_id` is missing or not on the list is dropped before any job is written.  A stored job whose connection is not on the list (a job from 0.8.0, or a connection later removed) closes as `skipped` (`connection-not-allowed`) with no download, transcript, call row, email, or Telnyx delete, and the delete step re-checks the list right before `DELETE /v2/recordings/{id}`.  This keeps another app on the same Telnyx account (for example an outbound-only TeXML number) out of CallCatch.
- `/api/diag` shows `telnyx_connections` (count only) and `recordings_skipped` (inputs dropped since start; a recording seen on several polls counts each time).  No ids, numbers, or URLs are logged.

## 0.8.0 — 2026-11-19

- **Every inbound call is captured.** Telnyx records the bridged call from answer (dual channel) while the SIP voice agent stays on the call. CallCatch Line verifies the Telnyx webhook (Ed25519, 5-minute replay window, fail closed when a public key is set) and relays a reduced event. A poller lists `GET /v2/recordings` so a missed webhook or a restart mid-call still finds the audio. The same recording id, call session, leg, or call-control id is one job.
- **Local file, then transcript, then delete.** Audio is written under `/data/callcatch/recordings` (mode 0700/0600, excluded from backups). Download URLs must be https on Telnyx or its recording storage; size and content type are capped. Speech-to-text is the xAI `/v1/stt` endpoint (`xai_api_key`, never logged). The Telnyx copy is deleted only after the file and the transcript are re-read from disk, and only when `telnyx_delete_remote` is on (the default). A failure retries, then sits as `dead` in `/api/diag`, and still becomes a call row.
- **One row, one email, including a hang-up with no agent tool call.** The recording is matched to the agent call by Telnyx id, else caller and start time. The email transcript is the carrier recording. A silent or empty recording still notifies: "caller hung up after N seconds, no speech detected". The notify dry-run switch is unchanged and stays the gate. Sensor attributes and logs stay masked (last 4 only). The panel shows the call and the verbatim transcript.
- New options: `telnyx_api_key`, `telnyx_delete_remote`, `telnyx_poll_s`, `xai_api_key`, `xai_stt_model`, `recording_grace_s`. Line 0.2.0 adds `telnyx_public_key` and `POST /telnyx/events`.
- **Review fixes.** Telnyx delete follows the notify dry-run gate (explicit off, and the delete option on). A second recording of one call is its own download and delete; the transcripts are combined in time order on the one row and the one email. A failed capture sets `capture_failed` and an error code and does not store the failure as the transcript. Capture waits for a recording only when both API keys are set. Error responses are not read in full. The job cap drops the oldest finished index row and leaves its audio for the retention sweep. The recordings folder is mode 0700 and the job files are 0600 before any audio is saved. The retention sweep only removes audio filenames. A failure after the email does not send a "details added" push. Job state lives in the backup-excluded recordings folder, and deleting the inbox row blanks the transcript, caller, and download URL there. Download hosts are Telnyx names or the documented `telephony-recorder-prod` bucket, and a path with `..` (including percent-encoded) is rejected. Non-public addresses, including CGNAT `100.64.0.0/10`, are rejected. The recordings poll does not move its watermark past unread pages, and a partial poll re-reads its boundary page so a shifted item is not skipped. Line's public key is the raw 32-byte key in base64. `py3-cryptography` has a minimum version, not an exact package revision.

## 0.7.2 — 2026-11-17

- **Agent-written transcript.**  `update_call` takes one transcript turn at a time (`speaker` caller or agent, up to 500 characters), appended to the call and never rewritten.  A retried identical turn is kept once.  Up to 400 turns or 60,000 characters per call; anything past that is counted, not stored.  `update_call`, `take_message` and `request_callback` also take a `summary` (up to 1,200 characters, latest wins).
- **The notice carries it.**  The one email per call now has a Summary section, the turn log as the transcript (a call-ended webhook transcript still wins when one arrives), and the xAI conversation id when the agent passed one.  The email says the transcript was written by the agent and may paraphrase.  All of it is caller text: HTML-escaped, and phone-number runs masked to the last 4 whenever full numbers are off in email, including the structured reason, agent notes and best time (Security Audit RG2-L2).  Turns arriving after the email only update the stored call and at most one masked "details added" push.
- **Match by conversation id, then caller + call start.**  When CallCatch Line passes the platform's conversation id, a call already holding that id is used first; else a call from the last 6 hours with the same caller ID and platform call start (`call_start`); else the Line call key.  Caller + start matching needs a seconds-resolution start (epoch seconds or milliseconds, or ISO 8601 with seconds) whose instant is in 2020–2100 UTC; a coarser value or an ISO start outside that range never matches (Security Audit L1, RG2-L1).
- **Agent text stays off Home Assistant.**  Turn and summary text never reaches HA sensor attributes or events: phone-agent rows get a blank `summary_fallback`, their sensor summary comes from the agent's structured fields only (never derived from the turn log), and the callback event uses the same rule (Security Audit L2).
- **Repeated turns.**  A turn identical to the one just before it (same speaker and text) is taken as a retried tool call and kept once; the tool has no retry id, so a caller who really repeats a sentence back to back is logged once (Security Audit I3, documented).
- Tests: `test_voice_link.py` skips when `twilio_house_voice/` is not in the checkout.
- **Find the call in the xAI console.**  The email shows "Call started" in Eastern when the platform's value is epoch or ISO 8601 with a zone (otherwise the value as sent), and a line naming the caller's last 4 and the start time, for searching the console's voice logs.
## 0.7.1 — 2026-11-17

- **Times show Eastern.**  List and detail times were shown in UTC (a 12:28 PM call read "17:28", and an evening call showed the next day's date).  They now show America/New_York, standard and daylight time.  Stored times stay UTC.

## 0.7.0 — 2026-11-15

- **Panel link is an option.**  New `panel_base_url` (default blank).  Set it to your Home Assistant address on the home network, for example `http://homeassistant.local:8123`, and the email links to the CallCatch panel there.  Blank leaves the link out and says to open CallCatch from the sidebar.  No hostname is built in.
- **HA-SEC-1 R4-I1: expiry count kept.**  An expiry notice that lands in the same second as an older closed notice now takes the next free second instead of dropping its count.
- **HA-SEC-1 R4-L1: 409 handling.**  A `409` whose AgentMail error code or name says the Idempotency-Key was already used for a different request parks the email as `dead` at once (`http-409-key-reused`); any other `409` is retried at most 3 times.  The error body is read up to 4 KB and never logged.
- **Dedicated CallCatch-only sender inbox (before first release).**  `agentmail_inbox` must now be a dedicated AgentMail inbox named `callcatch…@agentmail.to`, used by CallCatch only; any other value, including a shared bot inbox, turns email off with one warning that doesn't echo the value.  The sent payload never carries `Reply-To`, CC or BCC.  DOCS "Dedicated sender inbox" covers the inbox-scoped send-only key and keeping the inbox away from other agents' connectors.
- **Phone agent routes for CallCatch Line.** `POST /api/agent/call` (touch, final, ended), `POST /api/agent/hook` (call-ended webhook relay) and `GET /api/agent/freebusy`. They need a Supervisor-network peer and the new `agent_token` bearer (32+ characters, different from `ingest_token`). Ingress sessions and the ingest token are not accepted. Responses carry only a reference and a state, never stored text.
- **A record before any hangup.** CallCatch Line opens a call row on the first MCP request. `update_call` adds details as they are learned. A row is finalized once by the agent's message, a call-ended webhook, a closed MCP session (optional), or the quiet-time finalizer (`agent_inactivity_s`, default 180). A call that ends without a message is marked "Caller hung up early" and still notifies.
- **Email + push exactly once.** The first finalization stores a CallCatch row (`source: phone_agent`), queues a callback when asked (the event stays masked, nothing dials), and enqueues one email and one masked push. Jobs are keyed by reference, kept in `/data`, and retried with backoff (30 s doubling to 30 min, 12 tries). A late webhook or late details update the stored row and send at most one masked "details added" push.
- **Mail is deterministic and never uses Gmail.** One route: `agentmail`, a direct HTTPS POST to the AgentMail API (no agent, routine, or LLM in the path).  One recipient, pinned in code to `proton.me` / `protonmail.com`.  Rendered content is sent verbatim with fixed labels.  Any gmail.com or googlemail.com address is refused. `mail_backend` defaults to `off`, and `push_service` defaults to blank.
- **Busy times only.** `calendar.home` and `calendar.shared` by default, weekdays 08:00–18:00 America/New_York, merged and rounded to 30 minutes, all-day events ignored, 14 days max, never in the past. Titles, places, notes and calendar names are never returned.

- **Security Audit HA-SEC-1 fixes (before first release).** `email_full_number` now defaults to off (last 4 digits only).  Email and push honor a CallCatch-only switch, `input_boolean.callcatch_notify_dry_run` (separate from `work_forward_dry_run`): only an explicit "off" sends.  An explicit "on" closes jobs as `dry_run`.  Missing or unreadable holds them pending (nothing sent) and closes them as `expired` after 6 hours, then sends one masked "N CallCatch notices expired" push behind the same switch and shows `notices_expired` on the sensor.  AgentMail sends carry `Idempotency-Key: callcatch-<ref>`.  Agent bodies are read whole (chunked) under the 96 KB cap.  Phone-agent callbacks no longer fire `callcatch_callback_requested`; the outbox push is the one notice.  A name that is a number is masked in the email.  The email labels caller text as unverified.  Hung-up-early notices are capped per day (`timeout_notify_per_day`, default 20).  Webhook fallback matches caller ID only.  Fields are frozen after the email; late changes become notes.  Busy times are limited to 14 days ahead.

## 0.6.0 — 2026-11-06

- **Follow-up inbox.** Each voicemail has a follow-up state: `new`, `seen`, `needs_callback` (default when the caller asked for a callback), `queued`, `draft_ready`, `approved`, `in_progress`, `returned`, `no_answer`, `failed`, `cancelled`, `fyi`, `spam`, `done`, and `snoozed`. `POST /api/followup` is admin-only and allowlists those moves. `new` to `in_progress` is rejected. `in_progress` requires an approved record that names a number already on the call and a purpose quoted from the transcript. `no_answer` can return to the queue at most twice. Nothing on this path dials or sends.
- **0.5.7 callbacks still queue only.** `POST /api/request_callback` and `POST /api/callback_status` keep their events and stored fields. A new request sets follow-up `queued` and still fires `callcatch_callback_requested` with a masked number. `GET /api/callback_status` is not a route.
- **Read-only queue.** `GET /api/callback_requests` lists queued and pending callbacks: masked caller, short purpose, urgency, and quiet-hours hold. No transcript and no full number. Same read gate as the inbox.
- **Who / wants / callback.** The detail card quotes the transcript. A field with no span is blank. Medical messages stay verbatim and say "Check transcript". Prepare draft builds a queue-only note from those facts and does not send it.
- **Quieter counts.** The badge counts `needs_callback`, not unheard. Hangups under 5 seconds and spam stay out of that count. Quiet hours default to midnight–06:00 America/New_York. The notification blueprint holds alerts in that window except an urgent voicemail from a known contact. Mail routing is unchanged.
- **Unused Soft Slate file removed.** `soft-slate.css` was not loaded. The Plaid theme stays.
- No new options, secrets, or dry_run change. This add-on still does not dial.
- **Push Call back queues.** The notification Call back button no longer builds a `tel:` link from `newest.caller` (the sensor does not publish that field, so the button could not render). For a known caller — masked last-4 or a saved contact — it fires `callcatch_command` `{cmd: request_callback, voicemail_id}`, the same pending queue as the inbox. It does not dial. Withheld callers get no Call back button. Blocking stays in the inbox.

## 0.5.7 — 2026-11-05

- **Callback request, not a dial.** Call back in the list and on the detail screen queues a pending request (`POST /api/request_callback`). It sets `callback_status=pending` and `callback_requested_at`, and fires the Home Assistant event `callcatch_callback_requested` (voicemail id, masked from, display name, short summary, requested_at). A second request while pending is a no-op. `POST /api/callback_status` sets `returned`, `failed`, or `cancelled` (optional note) after a human-approved outbound call elsewhere, or from **Mark returned** / **Cancel request** in the panel. Returned shows `Returned · {local time}`.
- The add-on never dials: no Twilio Dial, no outbound SIP, no `place_phone_call`. Call back is no longer a `tel:` link. **Open on phone** is an optional secondary `tel:` link (transcript numbers stay tappable). Mock and dry_run still only record the request. The event does not include a full number (the shared sensor does not publish From either). Logs stay masked. No new options, secrets, or dry_run change.

## 0.5.6 — 2026-11-03

- Privacy contract locked by tests (no behavior change): full caller numbers appear only in the admin-only ingress panel (`panel_admin: true`; list title and detail). Every shared output (`sensor.inbound_voicemails` attributes `caller_masked`, `caller_label_masked`, `summary_callback`, `summary_*`, `summary_fallback`, and the blueprint push built from them) stays last-4 only (`•••1234`). There is deliberately no option to unmask shared outputs. New `tests/test_full_numbers_admin_only.py`. No data, API permission, webhook or dry_run change.

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
