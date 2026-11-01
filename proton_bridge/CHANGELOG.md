# Changelog

## 3.27.1-22 — 2026-11-01

IMAP allow-list narrowed (security).

- The HA internal network `172.30.32.0/23` is no longer allowed on IMAP by default. Before, every add-on on the HA network (SSH, Terminal, ESPHome and the rest) could reach the IMAP port. Default allow is now `127.0.0.1/32` plus `lan_cidr`.
- New `internal_allow_cidr` option (empty by default) for an HA-internal source that truly needs IMAP.
- `deny_docker_gateway` now defaults to `true` and is applied before every allow entry, so `172.30.32.1` is refused even if `lan_cidr` or `internal_allow_cidr` covers it. A missing key is treated as `true`; only an explicit `false` turns it off. Existing installs keep their stored value; with the new default allow list the gateway is refused either way unless it is named in an allow entry.
- Allow/deny lists are built by the new `/usr/local/bin/imap-allowlist`; invalid CIDRs are ignored instead of passed through.
- New test `tests/test_imap_allowlist.sh` (28 checks), run in CI.
- **Upgrade note:** any add-on that read mail through port 1143 over the HA network will be refused after this update. Name it in `internal_allow_cidr` if it really needs access.

## 3.27.1-21 — 2026-11-01

Carries over the hardening from the source repo (bcart180/ha-addon-proton-bridge 3.27.1-11 to -13, tip 0991bcfa). The store name suffix and URL from -20 are kept.

- Health option floors: `health_interval` minimum 15, `health_failures` minimum 3 (in the schema and enforced in run.sh). An out-of-range value is refused and the default is used.
- New `health_startup_grace` (default 900 s, range 60–7200): missed IMAP greetings during startup are logged but not counted. This replaces the hard ~4-minute startup deadline, which could restart-loop on a slow first sync. If the Bridge process exits, socat is still stopped immediately.
- The grace timer uses a monotonic clock (`/proc/uptime`).
- User-supplied values in log lines go through `sanitize_val` (control characters stripped, capped at 32 characters): refused health options, the `lan_cidr` */0 WARN, and the `IMAP allow-list` startup line. The allow-list value that imap-gate uses is unchanged.
- INFO line at startup when `lan_cidr` is empty (LAN access disabled).
- INFO line explaining that dbus / Secret Service WARNs are expected and that the keychain is `pass`.

## 3.27.1-20 — 2026-11-01

- Public store marker: name suffix (new); no functional change.

