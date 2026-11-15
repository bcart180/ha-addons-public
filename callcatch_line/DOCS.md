# CallCatch Line

## What it exposes

| Tool | Does | Never |
|---|---|---|
| `update_call` | Saves what the agent has learned so far on this call | Read anything back |
| `take_message` | Saves the final message.  CallCatch sends one email + one push | Send twice, dial |
| `request_callback` | Same, plus a pending callback in CallCatch | Dial, book a time |
| `get_free_busy` | Busy blocks from the calendars CallCatch is set to read | Return titles, places, notes |

There is no tool to list or read messages, transcripts, contacts, or any entity.

## Setup (CallCatch 0.7.0 or later)

1. In CallCatch, set `agent_token` (32+ random characters, different from `ingest_token`).
2. Here, set `callcatch_agent_token` to the same value, and `mcp_token` to a different 32+ character secret.
3. Leave the host port blank.  Point the tunnel at `http://a1d3bd4b-callcatch-line:8791` from a connector on the Supervisor network, and route only `/mcp` (and `/hooks/call-ended` if used).  Setting a host port publishes the add-on on every host interface, including the LAN.
4. In the phone agent's custom MCP dialog: URL `https://<your-host>/mcp`, header `Authorization` = `Bearer <mcp_token>` marked Secret.  Add header `X-CallCatch-Call` with the platform's per-call conversation id variable, and `X-CallCatch-From` with the caller number variable, if the platform offers them.

## How a call is tracked

The call key is a SHA-256 of the per-call header, else of the MCP session id.  With neither, write tools key on the caller ID header (a new call after 3 quiet minutes from that number), and with no caller ID either they are refused with "reconnect", so a sessionless client cannot create a row per request.  CallCatch keeps one row per key and finalizes it once: when the agent takes the message, when a call-ended webhook arrives, when the session closes (`delete_ends_call`, off by default), or after `agent_inactivity_s` of quiet (CallCatch option, default 180 s).  A call that ends without a message is still sent to Bryson, marked "Caller hung up early".

## Limits

Request bodies 16 KB (webhook 64 KB).  60 requests/min, 30 tool calls/min, 6 messages per call, 6 per caller ID per hour, calls with no caller ID limited per call plus a shared 20 per hour (`finals_no_caller_id_hour`), 40 per day.  The token is checked first.  A valid token always passes.  Ten bad tokens in a minute from one client make that client's further bad tries get 429 for 5 minutes.  The client is the TCP peer, or `CF-Connecting-IP` only when the peer is listed in `trusted_proxies`.  Connections from outside `allowed_peers` (default: Supervisor network and loopback) get 403.  Put the real throttle in a Cloudflare rate-limit rule.

## Logs

Tool name, result code, reference, milliseconds, caller last 4.  No headers, bodies, tokens, names, reasons, or transcripts.
