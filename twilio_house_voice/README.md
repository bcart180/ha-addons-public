# CallCatch Voice

Home Assistant Supervisor add-on for [CallCatch](../voicemail/) (Your Personalized Voicemail). Twilio answers the house number, streams caller audio to xAI Realtime, and on hangup writes a WAV plus a JSON sidecar that can be POSTed into the existing voicemail webhooks.

The store name and sidebar label are **CallCatch Voice**. The inbox add-on is **CallCatch**.

Version **0.1.6**. No Home Assistant Core restart is required to install or update.

## Install from GitHub

1. Settings → Add-ons → Add-on Store → ⋮ → Repositories → add
   `https://github.com/bcart180/ha-addons-public` (skip this if Trading Desk is already installed from that URL).
2. Refresh the store, open **CallCatch Voice**, Install. The image builds on the Home Assistant machine. That takes a few minutes the first time.
3. Configuration → fill the secrets below → Save → Start.
4. Optional: turn on **Show in sidebar** for the status panel (admins only). The panel is behind the Home Assistant login. Twilio does not use it.

## Updates

This add-on is versioned in `config.yaml`. Home Assistant pulls that file from this GitHub repository. After a version bump is merged to `main`:

Supervisor → Add-on Store → ⋮ → Repositories → already using https://github.com/bcart180/ha-addons-public → Check for updates

Then open CallCatch Voice and update it. Do not restart Home Assistant Core.

## Secrets owner fills

Paste these on the add-on **Configuration** tab. They are stored by Supervisor in `/data/options.json` on the Home Assistant host. Do not commit them, and do not put them in a GitHub secret for this repo. Backups of the add-on include them; keep backups private.

| Option | Required | What |
|---|---|---|
| `twilio_account_sid` | yes, to place or inspect calls later | Twilio Console → Account Info. Starts with `AC`. Not printed in logs. |
| `twilio_auth_token` | yes, before the port is safe to publish | Twilio auth token. Used only to check `X-Twilio-Signature`. Until this is set, `/twilio/voice`, `/twilio/status` and `/twilio/stream` answer 503 to everyone. When it is set, `public_base_url` is required too: the add-on refuses to start without it. |
| `twilio_phone_number` | yes | House Twilio number, E.164 (`+12025550101`). Shown masked on the status page. |
| `xai_api_key` | yes, for a speaking agent | xAI API key. Sent only as the Realtime `Authorization` header. Without it the add-on stays in stub mode: it still records. |
| `ha_webhook_token` | if the webhook expects one | Sent as JSON `token` (Home Assistant webhook triggers only see the body) and as `Authorization: Bearer` plus `X-Webhook-Token`. Optional. Not stored in the voicemail JSON. |
| `trust_api_token` | to edit allow / block lists from CallCatch | Long random secret. Put the same value in CallCatch `voice_trust_token`. Blank closes the bearer path; the lists still work and can be read on this add-on's own sidebar API. Redacted from logs. |

Non-secret options you also set:

| Option | Default | What |
|---|---|---|
| `public_base_url` | empty | HTTPS origin Twilio can reach: `https://host[:port]` with no path, query, fragment or credentials. Anything else makes the add-on refuse to start. Example: `https://voice.example.com` |
| `ha_webhook_transcript` | empty | Home Assistant webhook URL for the hangup JSON |
| `ha_webhook_audio` | empty | Second URL, or the same URL. One URL receives the full body once. |
| `xai_voice` | `eve` | Built-ins include `eve`, `ara`, `rex`, `sal`, `leo`. A custom voice id works too. |
| `xai_instructions` | empty | System prompt. `\n` becomes a newline. Empty uses the built-in house-phone prompt. |
| `instructions_file` | empty | Alternative prompt file under `/share` or `/data` only. Relative paths are read from `/share/twilio_house_voice/`. |
| `greeting_mode` | `agent` | `agent` speaks first. `none` waits for the caller. |
| `xai_bridge` | `live` | `stub` never opens xAI, even if a key is set. |
| `record_caller_audio` | true | Keep the caller leg. When this is off, no `{CallSid}.wav` is written. |
| `mix_recording` | true | `{CallSid}.wav` is a mono mix of caller and assistant, time-aligned. Caller-only if the assistant never played audio. CallCatch plays this file. |
| `record_assistant_audio` | false | Also write `{CallSid}.assistant.wav` (assistant timeline only, same alignment, silence where the assistant was not speaking). |
| `include_audio_base64` | false | Attach the WAV as base64 on the webhook. Omitted above 1.5 MB. Off by default so logs and HA traces stay small. |
| `validate_twilio_signature` | true | Ignored since 0.2.3: signatures are always checked when `twilio_auth_token` is set. Kept so saved configs load. |
| `log_level` | `info` | `debug` still redacts secrets. Media payloads are never logged. |

## Point Twilio at the public HTTPS base

Twilio needs a public `https://` webhook and a `wss://` Media Stream. Both are this add-on, on host port **8787** (change the host side under the add-on's Network tab if 8787 is taken; the app itself keeps listening on 8787).

Put TLS in front of that port. Forward only `/twilio/*` publicly; `/api/trust` is for CallCatch on the Supervisor network. Cloudflare Tunnel is the usual fit: public hostname → `http://<home-assistant-host>:8787`, WebSockets enabled (tunnel default). Set `public_base_url` to that `https://` origin.

Then in Twilio Console → Phone Numbers → your number → Voice configuration:

- **A call comes in:** Webhook, HTTP POST, `https://<public_base_url>/twilio/voice`
- **Call status changes:** Webhook, HTTP POST, `https://<public_base_url>/twilio/status`

Save. Place a test call.

Do **not** put the Nabu Casa remote UI (`*.ui.nabu.casa`) in the Twilio Voice URL. That host is the logged-in Home Assistant frontend. Twilio cannot log in, and Media Streams cannot ride the ingress session. Nabu Casa is still how you open the sidebar panel, and a Nabu Casa hook (`https://hooks.nabu.casa/...`) is a valid **Home Assistant webhook** target for `ha_webhook_transcript` / `ha_webhook_audio`. It is not the Voice URL.

Until `twilio_auth_token` is saved, the Twilio routes answer 503, so a scan of port 8787 cannot open a call. After it is saved, Twilio's signature is required on both POSTs. The Media Stream URL has no query string (`<Stream>` does not support one; Twilio error 31920). The voice webhook puts a one-time token in a `<Parameter>`. The WebSocket upgrades, then accepts the stream only if that token arrives on the `start` message (`customParameters`). The token lasts about two minutes, is single use, and is not written to the add-on log.

## Where the recording goes

On hangup the add-on writes:

- `/data/voicemail/{CallSid}.wav` — PCM16 mono 8 kHz. With `mix_recording` (default) this is the caller and the assistant together, so CallCatch (`api/audio/{call_id}`) plays both sides. If the assistant buffer is empty, the file is the caller only.
- `/data/voicemail/{CallSid}.json` — metadata and transcript, no API keys, no base64. `audio_mix` is true when the WAV is a mix.
- `/data/voicemail/{CallSid}.assistant.wav` — only if `record_assistant_audio` is on. Same timeline as the mix, including silence.

Caller frames use Twilio's Media Stream timestamp. Assistant audio is placed after Twilio accepts the frame, queued at 8 kHz (a burst does not stack on one instant). A barge-in `clear` drops assistant audio that had not reached the play cursor. The legs are summed per sample; a silent side is left as-is, and overlap is clamped, so it can clip. The two clocks can disagree by a fraction of a second. See [DOCS.md](DOCS.md).

`/data` is the add-on's private directory (included in add-on backups).

Default maps also copy that WAV when those folders exist:

| Container path | What you see in Home Assistant |
|---|---|
| `/share/twilio_house_voice/voicemail/{CallSid}.wav` | the shared `share` folder, for other add-ons |
| `/media/voicemail/{CallSid}.wav` | Media browser → local media → `voicemail` |

`/local/voicemail` (the `www` folder) is **not** enabled. Mapping `homeassistant_config` would mount all of `/config`, including secrets, into this add-on. If you add `homeassistant_config:rw` yourself later, files are also copied to `/homeassistant/www/voicemail/` and the webhook gains `local_url_path` of `/local/voicemail/{CallSid}.wav`.

## Home Assistant webhook body

POSTed on hangup with `X-Idempotency-Key: {CallSid}` and `call_id` set to the same Twilio CallSid. A repeated status callback does not post again after a 2xx or 409. Failures retry for about 30 minutes.

```json
{
  "call_id": "CA...",
  "event": "hangup",
  "from": "+1555...",
  "to": "+1555...",
  "transcript_text": "Caller: ...\nAssistant: ...",
  "audio_path": "/data/voicemail/CA....wav",
  "audio_mix": true,
  "stir_verstat": "TN-Validation-Passed-A",
  "stir": {"status": "passed", "level": "A", "raw": "TN-Validation-Passed-A"},
  "trust_verdict": "screen",
  "audio_media_path": "/media/voicemail/CA....wav",
  "audio_share_path": "/share/twilio_house_voice/voicemail/CA....wav",
  "token": "<ha_webhook_token when set>"
}
```

`stir_verstat` is Twilio's STIR/SHAKEN result (empty when the carrier sent none) and is a signal only. `trust_verdict` is `screen` or `allow`; blocked callers are rejected and never posted. See DOCS.md, "Trust gate".

`token` is present only when `ha_webhook_token` is set. Home Assistant webhook triggers cannot read headers, so the automation condition uses this field. `audio_base64` is included only when `include_audio_base64` is true and the WAV is under 1.5 MB. If both webhook options are set to different URLs, the transcript URL gets the JSON without base64 and the audio URL gets the full body. The same URL is called once. Both include `token` when it is configured.

Wire `ha_webhook_transcript` to the automation that feeds `sensor.inbound_voicemails`, and `ha_webhook_audio` to the one that feeds `sensor.voicemail_audio`.

## Cutover

Do not forward the Verizon number (*71) until this path has been soaked. Do not buy a Twilio number without an explicit go-ahead. This add-on does not purchase numbers.

## Develop locally

```bash
python3 -m pip install aiohttp
python3 twilio_house_voice/tests/test_voice.py
mkdir -p /tmp/thv
echo '{"xai_bridge":"stub"}' > /tmp/thv-opts.json
THV_OPTIONS=/tmp/thv-opts.json THV_DATA=/tmp/thv THV_PORT=8787 \
  python3 twilio_house_voice/rootfs/app/server.py
```

With no auth token, the Twilio routes answer 503. See [DOCS.md](DOCS.md) for the bridge, why this is not the Voice Agent Builder recording API, and why the service is Python.
