# Changelog

## 0.2.0 — 2026-11-19

- **Telnyx call webhooks.** `POST /telnyx/events` checks the Ed25519 signature (`telnyx-signature-ed25519` over `{telnyx-timestamp}|` plus the raw body) with `telnyx_public_key`. A timestamp more than 5 minutes off is rejected. A key that is set but does not verify rejects every request. Blank leaves the path off. Lifecycle and recording-saved events are reduced (ids, times, caller, short-lived recording URL) and relayed to CallCatch. The URL and the key are never logged. The caller is logged as the last 4 digits.

## 0.1.2 — 2026-11-17

- **Transcript turns.**  `update_call` accepts `turn` (`speaker` caller or agent, `text` up to 500 characters) and `summary` (up to 1,200, also on `take_message` and `request_callback`).  Turns are limited per call (`turns_per_call_min`, default 20 a minute) on top of the existing tool-call limit.  Turn text is never logged.
- **Caller number + call start as the call key.**  The xAI console's header variables are `caller_number`, `destination_number`, `current_time` and `call_start_time`, with no conversation id.  Line now reads `start_header` (default `X-CallCatch-Start`, filled with `call_start_time`).  With no per-call id header, caller number + call start (both the same on every request of one call) make the call key, ahead of the MCP session id, so a reconnect or a Line restart lands on the same call.  The start value is kept as given (ISO 8601, epoch, or another short printable form, up to 64 characters); an unfilled `{{...}}` template is ignored.  It is passed to CallCatch as `call_start` and never logged.  It joins the call key only when it is a seconds-resolution timestamp (epoch seconds or milliseconds, or ISO 8601 with hours, minutes and seconds) whose instant is in 2020–2100 UTC; a date, a minutes-only time, free text, or an ISO value outside that range (year 0001, 1970, 2019, 9999) could repeat across two calls from one number, so those fall back to the session key (Security Audit L1, RG2-L1).
- **Conversation id passed on.**  The value of the per-call header (`call_id_header`, default `X-CallCatch-Call`) is sent to CallCatch as `conversation_id` when it looks like an id (letters, digits, `_ . : -`, up to 100), so the notice can name the xAI conversation.  `send_conversation_id` turns that off.  The call key is unchanged.
## 0.1.1 — 2026-11-17

- **A restart mid-call no longer breaks the agent's tools.**  Sessions live in memory, so after a Line or Home Assistant restart the agent's next request carried an unknown `Mcp-Session-Id` and got `404 unknown-session`, which the phone platform reports as an error.  Line now re-creates a well-formed session id (the 32-character form Line issues) for the same call: the call key comes from the per-call header, else from that session id, so it maps back to the same call row.  The bearer token is still checked first, and a malformed id still gets 404.  Closing a lost session with `DELETE /mcp` returns 204 and, with `delete_ends_call` on, ends that call.

## 0.1.0 — 2026-11-15

- First release.  MCP server (Streamable HTTP, JSON or single-event SSE replies) at `POST /mcp` with four tools: `update_call`, `take_message`, `request_callback`, `get_free_busy`.
- Bearer auth checked before the body is read.  Bodies over 16 KB are refused.  A browser `Origin` header is refused.  Ten bad tokens in a minute from one client lock that client for five minutes.
- Limits: 60 requests/min, 30 tool calls/min, 6 messages per call, 6 per caller ID per hour, 40 per day.
- The call is recorded on `initialize` when the platform sends a per-call id or caller number, so there is a record before any hangup.
- Optional `POST /hooks/call-ended` (bearer or Standard Webhooks signature) relays a call-ended event and transcript to CallCatch.
- No Supervisor or Core API, no folder maps.  Logs hold tool, code, reference, timing, and the caller's last 4 digits only.
- Security Audit HA-SEC-1 fixes (before first release):
  - The token is checked before the lockout, so a valid token is never refused.  Lockout keys on the TCP peer, and on `CF-Connecting-IP` only from a `trusted_proxies` peer.
  - No host port by default.  New `allowed_peers` option (default Supervisor network + loopback) refuses anything else with 403.
  - Write tools with no session and no per-call id key on caller ID, or are refused, instead of creating a row per request.
  - Bodies are read in a loop, so chunked bodies parse and the size cap holds.
  - The per-number message limit keys on caller ID only.  Calls with no caller ID are limited per call, plus a shared `finals_no_caller_id_hour` ceiling (default 20).
  - `py3-aiohttp>=3.13.5`.
