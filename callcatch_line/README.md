# CallCatch Line

MCP server for the CallCatch phone agent.  It lets a hosted voice agent (for example Grok Voice) do three things only: take a message, ask for a callback, and see busy times (no event details).  Everything is stored in [CallCatch](../voicemail/).

- No Home Assistant API, no Supervisor API, no folders.  The only credential it holds is the CallCatch agent token.
- `POST /mcp`: Streamable HTTP MCP.  `Authorization: Bearer <mcp_token>` is required.
- `POST /hooks/call-ended`: optional call-ended webhook (bearer `hook_token` or a Standard Webhooks signing secret).
- `GET /healthz`: `{"ok": true}`.  Every other path is 404.

See [DOCS.md](DOCS.md).
