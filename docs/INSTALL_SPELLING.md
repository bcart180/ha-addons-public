# Spelling add-on: local install

> Shelved 2026-10-11, the owner chose LAN-only; not installed. Files moved to `archive/spelling/`; staging moved to `/share/archive/to-delete-review/spelling_addon_install/`. Steps below are historical.

Staged (non-public): `/share/spelling_addon_install/spelling/` (5 files) + `spelling-0.1.0.zip` + `MANIFEST.sha256`.

Studio Code Server terminal:

    mkdir -p /addons/spelling
    cp -r /share/spelling_addon_install/spelling/. /addons/spelling/
    cd /addons/spelling && sha256sum -c /share/spelling_addon_install/MANIFEST.sha256

Then: Settings > Add-ons > Add-on Store > ⋮ > Check for updates > Local add-ons > Spelling > Install > Start
(Show in sidebar is automatic for ingress panels).

Notes
- Serves `/share/spelling` read-only (map share:ro); nothing is copied into the image.
- `/` returns spelling.html directly (no redirect) so `fetch('spelling-lists.json')`
  resolves under `/api/hassio_ingress/<token>/`.
- localStorage is per origin: progress inside HA is separate from http://spelling.home/.
