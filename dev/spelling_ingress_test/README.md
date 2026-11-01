# Spelling ingress test (box only)

`ingress_proxy.py` imitates the Supervisor ingress proxy (strips `/api/hassio_ingress/<token>`).
The server ran with `SPELLING_ROOT=<copy of /share/spelling> SPELLING_ALLOW=127.0.0.1`.
Test files: the current Spelling-Practice.html (as spelling.html) and a real copy of
spelling-lists.json. The archived `archive/to-delete-review/www/spelling.html` is an older
build with no fetch at all, so it needs nothing from the server.

Result (2026-10-11 14:22 ET): `/` 200 text/html; `spelling-lists.json?v=…` fetched as
`/api/hassio_ingress/tok3n_ABC/spelling-lists.json` 200 application/json; no console errors;
traversal 404; POST 405; non-ingress client 403. No `<base>` needed, source unchanged.
