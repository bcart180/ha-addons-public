# Spelling add-on

**Shelved 2026-10-11, Bryson chose LAN-only; not installed.** `config.yaml` is kept as `config.yaml.shelved` so the Supervisor store (which finds any `**/config.yaml`) never lists it. The page stays LAN-only at http://spelling.home/ from /share/spelling.

Sidebar panel (HA ingress) for the spelling practice page. Serves `/share/spelling`
read-only; `/` returns `spelling.html`. Nothing is copied into the image, so the page
and `spelling-lists.json` stay the same files NGINX serves at http://spelling.home/.

- `panel_admin: false`: non-admin HA users can open it.
- No host ports; only the Supervisor ingress proxy (172.30.32.2) may connect.
- GET/HEAD only, no directory listings, `.json` served as `application/json`.
- Progress lives in the browser's localStorage. Under ingress the origin is the HA URL,
  so progress there is separate from progress on http://spelling.home/.
