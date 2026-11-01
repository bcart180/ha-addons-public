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
  `172.30.32.1`, keep `deny_docker_gateway` on (default).
- Host port 1143 binds on all host interfaces (including WireGuard). The add-on therefore
  filters by source IP (`imap-gate`, lists built by `imap-allowlist`). Allowed by default:
  loopback `127.0.0.1/32` only, plus the optional `lan_cidr` (one IPv4 CIDR, e.g. your Mac
  as `a.b.c.d/32`). Since 3.27.1-22 the HA internal network `172.30.32.0/23` is **not**
  allowed by default (before, every add-on on the HA network could reach IMAP).
  Refused connections are logged and closed.
- `internal_allow_cidr` (default empty): one extra IPv4 CIDR, for an HA-internal source that
  truly needs IMAP (e.g. one add-on as `172.30.33.x/32`). Keep it a single /32.
- `lan_cidr` / `internal_allow_cidr` are validated (IPv4/prefix); invalid values are ignored
  (never widened). `/0` logs a warning: it allows every address, including VPN peers.
- Refused connections are logged at most once per 60 s (with a count of the rest).
- `deny_docker_gateway` (default `true`): `172.30.32.1/32` (Docker/HA gateway) is checked
  **before** the allow list and refused, even if `lan_cidr` or `internal_allow_cidr` covers
  it. This stops LAN/WG clients that arrive source-rewritten as the gateway. Set it to
  `false` only if a Pi test shows a legitimate client that can only arrive via the gateway,
  and then also name it in `internal_allow_cidr`.
- The Supervisor watchdog only needs a TCP accept on 8143, which still happens before the
  gate refuses, so the watchdog is unaffected (it may show up in the refusal log).
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
- `lan_cidr`, `internal_allow_cidr`, `deny_docker_gateway`: see above.
- `health_interval`: seconds between probes of Bridge's own IMAP greeting (default 60,
  range 15–3600).
- `health_failures`: consecutive missed greetings, after startup grace, before the
  add-on stops (default 5, range 3–30). At the defaults that is about five minutes.
- `health_startup_grace`: seconds after Bridge starts during which a missed greeting
  is logged and not counted (default 900, range 60–7200). A slow first sync/unlock
  after login can take longer than a few minutes before 1143 answers. The Bridge
  process exiting still stops the add-on immediately.
- A value outside those ranges is refused (logged) and the default is used.

## Failure behaviour
- Fatal errors pause 30 s before exiting (avoids restart loops / Proton rate limits).
- Vault present but keychain empty: Bridge is NOT started; the add-on idles and logs
  what to do (use login mode).
- The Supervisor watchdog probes container port 8143, which is socat, not Bridge.
  Bridge closes 127.0.0.1:1143 when it loses Proton and keeps running, so 8143 would
  stay green while mail is dead. While Bridge is running, the add-on reads one
  cleartext IMAP greeting from 127.0.0.1:1143 (`* OK` only). It sends nothing: no
  LOGIN, no STARTTLS. The greeting text is not logged. For `health_startup_grace`
  seconds after Bridge starts, misses are logged and not counted. After that,
  `health_failures` consecutive misses stop socat and exit so the watchdog
  restarts the add-on. If the Bridge process exits, socat is stopped immediately,
  including during the grace period.
- The greeting check expects Bridge's default STARTTLS mode. Implicit SSL
  (`change imap-security`) does not send a cleartext `* OK` line.
