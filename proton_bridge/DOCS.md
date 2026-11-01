# Proton Mail Bridge add-on

## Client settings (IMAP)
- Server: the HA host's **IPv4** address, port **1143**. Username/password: from `info` in
  login mode (the Bridge password, not your Proton password).
- Security: **STARTTLS** (default) or **SSL/TLS** if you switched Bridge to SSL (below).
  Accept/pin Bridge's self-signed certificate (CN=127.0.0.1).
- Plain/unencrypted IMAP login is refused: before STARTTLS the server advertises
  `LOGINDISABLED` (no `AUTH=`) and answers `LOGIN`/`AUTHENTICATE` with
  `NO [PRIVACYREQUIRED]`. Set the client to STARTTLS or SSL/TLS, never "None".

## Why there is a STARTTLS guard (`starttls-guard`)
Pi test 2026-10-30 (check b): Bridge v3.27.1 in its default STARTTLS mode advertised
`AUTH=PLAIN` before STARTTLS, without `LOGINDISABLED`, and evaluated a cleartext `LOGIN`.
- Preferred fix was implicit TLS (Bridge `IMAPSSL`). In v3.27.1 that setting lives only in
  the encrypted vault (`vault.enc`, `Settings.IMAPSSL`, default `false`), whose key is in the
  keychain; it can only be changed by a logged-in Bridge via the CLI
  `change imap-security` (interactive yes/no) or the GUI gRPC API. There is no flag, env var
  or config file, so the add-on cannot set it reliably without a login.
- So enforcement is in the add-on: every allowed connection goes through `starttls-guard`
  (between `imap-gate` and Bridge 127.0.0.1:1143). Until STARTTLS succeeds it strips `AUTH=`
  from every CAPABILITY list, adds `LOGINDISABLED`, and answers `LOGIN`/`AUTHENTICATE` itself
  (they never reach Bridge; with a literal attached the connection is closed). Bytes sent
  after `STARTTLS` but before the TLS handshake (command injection) close the connection.
  After TLS it is a transparent pipe; it never logs credentials.
- Before TLS the guard also closes the connection on any literal (`{n}`/`{n+}`) in any
  command, on invalid tags (`*`, `+`, non-atom), and if TLS is not reached within 60 s.
- Limits: at most 32 concurrent IMAP connections; a connection idle for 30 min is closed
  (socat `-T 1800`), so clients re-open IDLE sessions.
- Optional: in login mode you may also type `change imap-security` -> `yes` (SSL/implicit
  TLS). The guard detects TLS and passes it through; clients must then use SSL/TLS.
- Tested by `starttls-guard/main_test.go` (runs in every image build and via
  `starttls-guard/test.sh`).

## Network exposure
- IMAP only (host port 1143 -> container 8143). SMTP stays on container localhost.
- **IPv4 only.** The container listener is `TCP4-LISTEN` and the allow-list is IPv4. On the
  Pi test, `::1` and the IPv6 ULA got no banner and no gate log, i.e. IPv6 is not served.
  Use the host's IPv4 address in clients. If a future test shows IPv6 clients arriving as
  `172.30.32.1`, enable `deny_docker_gateway`.
- Host port 1143 binds on all host interfaces (including WireGuard). The add-on therefore
  filters by source IP (`imap-gate`): allowed = HA internal network `172.30.32.0/23`,
  loopback, and the optional `lan_cidr` option (one IPv4 CIDR, e.g. `192.168.1.0/24`).
  Refused connections are logged and closed. Leave `lan_cidr` empty if only add-ons /
  the Cloudflare tunnel on HA need IMAP.
- `lan_cidr` is validated (IPv4/prefix). `/0` logs a warning: it allows every address,
  including VPN/WireGuard peers.
- Refused connections are logged at most once per 60 s (with a count of the rest).
- `deny_docker_gateway` (default `false`): rejects 172.30.32.1 before the 172.30.32.0/23
  allow. Enable ONLY if a test on the Pi shows LAN/WG clients arriving as 172.30.32.1
  (Docker userland-proxy source rewrite); otherwise it could block legitimate clients that
  reach the add-on via the host/gateway path.
- Never port-forward 1143 on the router.

## Login mode (`login_mode: true`)
- Opens the Bridge CLI in the HA sidebar (ingress, admin-only). ttyd listens on a UNIX
  socket; port 7681 only accepts the Supervisor ingress proxy (172.30.32.2).
- ONE session (`ttyd -o`): when you close the tab, ttyd and the add-on stop.
- Type: `login`, `updates autoupdates disable`, optionally `change imap-security` (SSL),
  `info` (shows the IMAP password, treat it as a secret), `exit`. Then set
  `login_mode: false` and start the add-on.

## Options
- `log_level`: default `info`. `debug` is for short troubleshooting only (verbose logs).
- `lan_cidr`: see above.
- `health_interval`: seconds between probes of Bridge's own IMAP greeting (default 60,
  range 1–3600).
- `health_failures`: consecutive missed greetings before the add-on stops (default 5,
  range 1–30). At the defaults that is about five minutes.

## Failure behaviour
- Fatal errors pause 30 s before exiting (avoids restart loops / Proton rate limits).
- Vault present but keychain empty: Bridge is NOT started; the add-on idles and logs
  what to do (use login mode).
- The Supervisor watchdog probes container port 8143, which is socat, not Bridge.
  Bridge closes 127.0.0.1:1143 when it loses Proton and keeps running, so 8143 would
  stay green while mail is dead. While Bridge is running, the add-on reads one
  cleartext IMAP greeting from 127.0.0.1:1143 (`* OK` only). It sends nothing: no
  LOGIN, no STARTTLS. The greeting text is not logged. After `health_failures`
  consecutive misses, or if the Bridge process exits, it stops socat and exits so
  the watchdog restarts the add-on.
- The greeting check expects Bridge's default STARTTLS mode. Implicit SSL
  (`change imap-security`) does not send a cleartext `* OK` line.
