# CallCatch Line

## What it exposes

| Tool | Does | Never |
|---|---|---|
| `update_call` | Saves what the agent has learned so far on this call, plus one transcript `turn` and a `summary` | Read anything back, rewrite earlier turns |
| `take_message` | Saves the final message.  CallCatch sends one email + one push | Send twice, dial |
| `request_callback` | Same, plus a pending callback in CallCatch | Dial, book a time |
| `get_free_busy` | Busy blocks from the calendars CallCatch is set to read | Return titles, places, notes |

There is no tool to list or read messages, transcripts, contacts, or any entity.

## Setup (CallCatch 0.7.0 or later)

1. In CallCatch, set `agent_token` (32+ random characters, different from `ingest_token`).
2. Here, set `callcatch_agent_token` to the same value, and `mcp_token` to a different 32+ character secret.
3. Leave the host port blank.  Point the tunnel at `http://a1d3bd4b-callcatch-line:8791` from a connector on the Supervisor network, and route only `/mcp`, `/telnyx/events` (when Telnyx recording webhooks are on), and `/hooks/call-ended` if used.  Setting a host port publishes the add-on on every host interface, including the LAN.
4. In the phone agent's custom MCP dialog: URL `https://<your-host>/mcp`, header `Authorization` = `Bearer <mcp_token>` marked Secret.  Add header `X-CallCatch-Call` with the platform's per-call conversation id variable, and `X-CallCatch-From` with the caller number variable, if the platform offers them.

## How a call is tracked

The call key is a SHA-256 of the per-call header, else of the MCP session id.  With neither, write tools key on the caller ID header (a new call after 3 quiet minutes from that number), and with no caller ID either they are refused with "reconnect", so a sessionless client cannot create a row per request.  CallCatch keeps one row per key and finalizes it once: when the agent takes the message, when a call-ended webhook arrives, when the session closes (`delete_ends_call`, off by default), or after `agent_inactivity_s` of quiet (CallCatch option, default 180 s).  A call that ends without a message is still sent to Bryson, marked "Caller hung up early".

## Limits

Request bodies 16 KB (webhook 64 KB).  60 requests/min, 30 tool calls/min, 6 messages per call, 6 per caller ID per hour, calls with no caller ID limited per call plus a shared 20 per hour (`finals_no_caller_id_hour`), 40 per day.  The token is checked first.  A valid token always passes.  Ten bad tokens in a minute from one client make that client's further bad tries get 429 for 5 minutes.  The client is the TCP peer, or `CF-Connecting-IP` only when the peer is listed in `trusted_proxies`.  Connections from outside `allowed_peers` (default: Supervisor network and loopback) get 403.  Put the real throttle in a Cloudflare rate-limit rule.

## Logs

Tool name, result code, reference, milliseconds, caller last 4.  No headers, bodies, tokens, names, reasons, or transcripts.

## Telnyx webhooks (0.2.0, CallCatch 0.8.0)

`POST /telnyx/events` takes the call-lifecycle and recording-saved webhooks for the Telnyx number. Telnyx signs the raw body: header `telnyx-timestamp`, header `telnyx-signature-ed25519`, signed string `{timestamp}|{raw body}`, Ed25519, the account public key in `telnyx_public_key`. A timestamp more than 5 minutes from the clock is rejected. If the option is set, a bad or missing signature is rejected (the path does not accept unsigned events). If the option is blank, the path is off.

`telnyx_connection_ids` (0.2.1) lists the Telnyx connection ids whose events are relayed (digits, comma or space separated, up to 10; use the same value as in CallCatch).  The path stays off (503) until this list and the public key are both set.  A signed event from any other connection, or with no connection id, gets 200 and is not relayed.

`telnyx_public_key` is the raw 32-byte Ed25519 public key, standard base64 (44 characters, no PEM headers and no whitespace). That is the value shown in the Telnyx portal under Keys & Credentials → Public Key. A value that is set but is not 32 bytes after decoding rejects every webhook.

Line relays ids, times, the caller, channel count, and the short-lived recording URL to CallCatch. It does not log the URL, the key, or the body. The log line is the event name, the upstream status, and the caller's last 4 digits. CallCatch downloads, transcribes, and deletes the remote copy. See CallCatch's docs for the go-live recording settings.

## Agent-written transcript (0.1.2, CallCatch 0.7.2)

The console agent's own recording and transcript stay in the xAI console; xAI has no API to fetch them.  So the agent writes the transcript as it goes: after each caller turn it calls `update_call` with `turn: {speaker: "caller", text: ...}` (and may log its own replies as `agent`).  Before ending it sends a `summary`.  This is the model's account, not a word-for-word recording, and a caller who hangs up suddenly loses at most the last turn.  The 180 s finalizer still sends the one email.

Per-call header variables (xAI console, agent > Tools > CallCatch MCP > Headers, type Variable):

| Header | Variable | Line option |
|---|---|---|
| `X-CallCatch-From` | `caller_number` | `caller_header` |
| `X-CallCatch-Start` | `call_start_time` | `start_header` |
| `X-CallCatch-Call` (optional) | a per-call conversation id, if the console ever offers one | `call_id_header` |

The call key is the per-call id when present, else caller number + call start when the start is a seconds-resolution timestamp (epoch seconds or milliseconds, or ISO 8601 with seconds), else the MCP session id.  A date-only or minutes-only start never joins two calls.  Header names are options here.  Line ignores a value that still contains `{` (an unfilled template).  Header values are never logged; the log shows the caller's last 4 only.
