# Sports News security review — corrected head

Status: pre-install packet for this branch. Nothing here is installed, restarted, scheduled, or merged. Social Pulse behavior is unchanged. Trading Desk is unchanged.

This packet does not say the secret scan is clean until the gitleaks job on this head passes. The earlier job failed while downloading the scanner from the releases CDN (HTTP 504) and did not scan. The workflow now downloads official gitleaks 8.30.1 through the GitHub release API and checks the same checksum.

## Publish bridge

Version 0.1.2 still has no `map:` entries, no host port, `homeassistant_api: false`, and `hassio_api: false`. It does not mount the Home Assistant config directory or the shared folder. Those mounts would expose private files, and read-only access would not keep them confidential.

The publish value is the public option `snapshot_json`. Schema `str(0,262144)` matches the Supervisor parser in `supervisor/apps/options.py`: a string length, not a silent trim. The Supervisor API body limit in `supervisor/api/__init__.py` is 16 MiB. The starter file is 8791 bytes. A snapshot around 15KB fits. This process also rejects more than 262144 UTF-8 bytes.

Supervisor writes `/data/options.json` only when the app starts (https://contributors.home-assistant.io/apps/guides/options/). This process validates that string once with the same snapshot rules, then serves the result. The 60-second page poll does not read the option again. There is no runtime propagation.

The parent tool is the existing `ha_manage_app`: one `options` update that sets only `snapshot_json`, a readback, then `action: restart` for this slug only. This repository does not call that tool and does not install the app. A routine should wait until that install, write, readback, and restart have been shown on the running app.

A merged candidate is serialized and validated again, including the 75-post total, 75 posts per team, and 262144-byte cap, before anything is written. Two valid inputs can still merge into an invalid document. That result is rejected, the previous file bytes stay, and a later start still serves that file. A rejected option does not replace `/data/last_good.json` and does not stop the process. `/api/health` stays ok, so bad news data does not make the watchdog restart the app again. Empty `snapshot_json` leaves any saved snapshot in place and, when none exists, shows the starter.

## What the code still enforces

These rules are covered by `sports_news/tests/test_sports_news.py`. The installed app applies them to `snapshot_json` at startup. A file inbox remains a test seam and is not mounted.

- ZagMBB must be author id `602989093`. BWagner_CapGaz must be `872205181`. A mismatch, including `999001`, is rejected. `bcondotta` is rejected.
- After a save, a missing inbox keeps `/data/last_good.json`, says the inbox file is missing, and does not show the starter. A new process with the same data directory does the same.
- A document whose `collected_at` is older than the saved snapshot is rejected. The saved posts and time stay.
- `status: error` does not replace a saved snapshot, with or without posts. It does not advance success times.
- `status: partial` keeps the failed team's previous posts and that team's content time. The page does not treat a schedule, a page re-read, or `last_attempt_at` as a new collector check.
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
