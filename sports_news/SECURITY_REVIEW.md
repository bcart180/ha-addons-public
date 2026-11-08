# Sports News security review — corrected head

Status: pre-install packet for this branch. Nothing here is installed, restarted, scheduled, or merged. Social Pulse behavior is unchanged. Trading Desk is unchanged.

This packet does not say the secret scan is clean until the gitleaks job on this head passes. The earlier job failed while downloading the scanner from the releases CDN (HTTP 504) and did not scan. The workflow now downloads official gitleaks 8.30.1 through the GitHub release API and checks the same checksum.

## Publishing block

`ha_write_file` can write `custom_templates/sports_news/inbox.json` under the Home Assistant config directory. A later overwrite needs `overwrite: true`. The add-on can read that file only by mounting the whole config directory (`homeassistant_config` → `/homeassistant`). That tree includes secrets and auth files. Read-only stops mutation. It does not stop disclosure if the container is compromised.

`/share` is also a broad tree. That writer path was not granted, and this change does not ask for it again. `/config` is not a mount in this manifest. The add-on's own config directory would be a narrow mount, but the file tool's allowed directories are `www/`, `themes/`, `custom_templates/`, `dashboards/`, plus sibling volumes only when that tool is separately permitted. The add-on config directory is not one of those. No other sanctioned writer was found that can place the snapshot where this process can read it without one of the broad mounts.

Decision: version 0.1.1 has no `map:` entries. It does not mount the Home Assistant config directory or the shared folder. `homeassistant_api` is false. There is no host `ports:` entry and no write route. The installed page serves the labeled starter snapshot from inside the image. The tested inbox rules are not attached to a host path. An `inbox_file` option is refused if one is injected. Unattended publishing stays blocked until a narrow sanctioned path exists.

## What the code still enforces

These rules are covered by `sports_news/tests/test_sports_news.py`. They apply when a test, or a future bridge, supplies an inbox directory. Production does not supply one.

- ZagMBB must be author id `602989093`. BWagner_CapGaz must be `872205181`. A mismatch, including `999001`, is rejected. `bcondotta` is rejected.
- After a save, a missing inbox keeps `/data/last_good.json`, says the inbox file is missing, and does not show the starter. A new process with the same data directory does the same.
- A document whose `collected_at` is older than the saved snapshot is rejected. The saved posts and time stay.
- `status: error` does not replace a saved snapshot, with or without posts. It does not advance success times.
- `status: partial` keeps the failed team's previous posts and that team's success time. Updated teams use the new time. The page freshness is error, not "inside two proposed cycles".
- Opening the inbox does not follow symlinks. If the allowed directory itself is a symlink, the open fails. `realpath` is not used to redefine the allowed root.
- The save writes the full byte string, syncs the file, renames it, and syncs the directory. A page check that produces the same bytes does not rewrite the file.
- Schema checks do not prove a post is true, that the display name matches the account, or that the URL still exists.

## Ingress

Every request is refused unless the source address is in the allowlist. The default is `172.30.32.2`, the Supervisor ingress proxy. Other addresses get 403, including POST. Allowed clients get 405 for POST, PUT, PATCH, DELETE, and OPTIONS. `SN_ALLOW` is a process environment used by tests. It is not an add-on option and it is not set in `config.yaml`.

`panel_admin: true` controls whether Home Assistant shows the sidebar entry to non-admins. This process does not read the Home Assistant user on each request and does not enforce an admin role itself. Authorization in front of the proxy is the Supervisor ingress session. Content-Security-Policy limits the browser. It does not stop the container from opening its own network connections. This image does not call X or any other publisher.

## Social Pulse and Trading Desk

Social Pulse 0.3.1 remains presentation-only: pure black page and category colors. Its collector, server, options, and ingress allowlist are not part of this correction. Trading Desk 0.3.2, including its chart colors and the Bitcoin orange allocation swatch, is not modified.

## Rollback

Do not install. The sports panel does not exist until this branch is merged and the app is installed. Reverting the branch removes the add-on files. There is no host mount to revoke because none is declared.

## Tests

`python3 sports_news/rootfs/app/selfcheck.py`

`python3 sports_news/tests/test_sports_news.py`
