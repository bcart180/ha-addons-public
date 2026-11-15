# Changelog

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
