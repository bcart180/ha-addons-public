# Changelog

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

