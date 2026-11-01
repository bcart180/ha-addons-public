#!/usr/bin/env bash
# Proton Mail Bridge add-on entrypoint. Everything stateful lives in /data.
# Never echoes credentials. Never sends mail on its own.
set -euo pipefail

# Fatal exit: pause first so Supervisor/watchdog restarts cannot hammer Proton (rate limits).
die() { echo "[proton-bridge] ERROR: $*"; sleep 30; exit 1; }

# $1 raw, $2 min, $3 max, $4 default, $5 option name. Prints the chosen integer.
# Empty means "use the default". Rejects 0, leading zeros, and non-integers.
read_health_int() {
  local raw=$1 min=$2 max=$3 def=$4 name=$5
  if [ -z "$raw" ]; then
    echo "$def"
    return 0
  fi
  if [[ "$raw" =~ ^[1-9][0-9]{0,3}$ ]] && [ "$raw" -ge "$min" ] && [ "$raw" -le "$max" ]; then
    echo "$raw"
    return 0
  fi
  echo "[proton-bridge] WARN: ${name} must be an integer ${min}-${max}; using ${def}" >&2
  echo "$def"
  return 0
}

OPTS=/data/options.json
LOGIN_MODE=$(jq -r '.login_mode // false' "$OPTS" 2>/dev/null || echo false)
LOG_LEVEL=$(jq -r '.log_level // "info"' "$OPTS" 2>/dev/null || echo info)
if [ "$LOG_LEVEL" = "debug" ]; then
  echo "[proton-bridge] WARN: log_level=debug can log mail metadata/headers; use only briefly, then set back to info."
fi
raw_interval=$(jq -r '.health_interval // empty' "$OPTS" 2>/dev/null || true)
raw_failures=$(jq -r '.health_failures // empty' "$OPTS" 2>/dev/null || true)
HEALTH_INTERVAL=$(read_health_int "$raw_interval" 1 3600 60 health_interval)
HEALTH_FAILURES=$(read_health_int "$raw_failures" 1 30 5 health_failures)

export HOME=/data/home
export XDG_CONFIG_HOME=/data/home/.config
export XDG_DATA_HOME=/data/home/.local/share
export XDG_CACHE_HOME=/data/home/.cache
export GNUPGHOME=/data/home/.gnupg
export PASSWORD_STORE_DIR=/data/home/.password-store
mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME" "$GNUPGHOME"
chmod 700 "$HOME" "$GNUPGHOME"

# `bridge` is the real Bridge app (Makefile v3.27.1: built from cmd/Desktop-Bridge as
# proton-bridge, then `mv proton-bridge bridge`); `proton-bridge` is the LAUNCHER
# (cmd/launcher), which is what applies downloaded updates. The image ships only `bridge`.
BRIDGE=/usr/lib/protonmail/bridge/bridge

# IMAP source allow-list (imap-gate): HA internal network + optional LAN CIDR option.
LAN_CIDR=$(jq -r '.lan_cidr // ""' "$OPTS" 2>/dev/null || true)
case "$LAN_CIDR" in */0) echo "[proton-bridge] WARN: lan_cidr $LAN_CIDR allows EVERY IPv4 address (incl. WireGuard/VPN peers)";; esac
# Optional gateway deny (M3 prep), OFF by default. Only enable if a Pi test shows the
# Docker gateway rewrites LAN/WG source IPs to 172.30.32.1 (userland proxy).
export IMAP_DENY=""
[ "$(jq -r '.deny_docker_gateway // false' "$OPTS" 2>/dev/null)" = "true" ] && IMAP_DENY="172.30.32.1/32"
export IMAP_ALLOW="172.30.32.0/23 127.0.0.0/8 $LAN_CIDR"
echo "[proton-bridge] IMAP allow-list: $IMAP_ALLOW${IMAP_DENY:+ (deny first: $IMAP_DENY)}"
# All 8143 listeners are explicit IPv4 (TCP4-LISTEN): the allow-list is IPv4-only and
# IPv6 is not served (Pi test 2026-10-30: ::1/ULA got no banner). Each allowed connection
# runs through starttls-guard: no LOGIN/AUTHENTICATE before STARTTLS (or implicit SSL).
# max-children=32 caps concurrent IMAP connections; -T 1800 (socat) closes a connection
# idle for 30 min (IMAP IDLE clients re-issue every ~29 min). starttls-guard adds a 60 s
# pre-TLS deadline.
IMAP_LISTEN="TCP4-LISTEN:8143,fork,reuseaddr,max-children=32"

# First start: create a local, passphrase-less GPG key and init pass with it.
# (Key never leaves /data; protection at rest = encrypted HA backups.)
if [ ! -f "$PASSWORD_STORE_DIR/.gpg-id" ]; then
  echo "[proton-bridge] first start: creating GPG key + pass store in /data"
  gpg --batch --pinentry-mode loopback --passphrase '' \
      --quick-gen-key 'Proton Bridge HA add-on <bridge@localhost>' default default never
  FPR=$(gpg --list-keys --with-colons | awk -F: '/^fpr:/{print $10; exit}')
  pass init "$FPR" >/dev/null
fi

# Keychain sanity: Bridge needs pass+gpg (secret-service/dbus warnings are expected).
GPG_ID=$(cat "$PASSWORD_STORE_DIR/.gpg-id" 2>/dev/null || true)
if [ -z "$GPG_ID" ] || ! gpg --list-secret-keys "$GPG_ID" >/dev/null 2>&1; then
  die "pass store has no usable GPG secret key ($PASSWORD_STORE_DIR). /data keychain is broken; restore a backup or wipe /data/home and log in again."
fi
NENTRIES=$(find "$PASSWORD_STORE_DIR" -name '*.gpg' -type f 2>/dev/null | wc -l)
if [ -f "$XDG_CONFIG_HOME/protonmail/bridge-v3/vault.enc" ] && [ "$NENTRIES" -eq 0 ]; then
  echo "[proton-bridge] ERROR: vault exists but pass store is EMPTY -> Bridge would fail with 'no keychain'."
  if [ "$LOGIN_MODE" != "true" ]; then
    echo "[proton-bridge] NOT starting Bridge. Set login_mode: true and log in again. Idling (no restart loop)."
    # Dummy listener on 8143 (accept+close) so the HA watchdog TCP check passes and does
    # not restart-loop; idle in the background so PID1 (this shell) honours TERM.
    socat "$IMAP_LISTEN" SYSTEM:'exit 0' &
    IDLE_SOCAT=$!
    trap 'kill "$IDLE_SOCAT" 2>/dev/null || true; exit 0' TERM INT
    sleep infinity &
    wait $!
  fi
elif [ "$NENTRIES" -eq 0 ]; then
  echo "[proton-bridge] keychain: pass store initialised, empty (not logged in yet -> use login_mode)"
else
  echo "[proton-bridge] keychain: pass store OK ($NENTRIES entries)"
fi

# Strict mode split: only ONE Bridge process ever runs in this container (login CLI OR
# noninteractive), so any lock file at start is stale (crash/kill) and safe to remove.
if pgrep -f "^${BRIDGE}( |$)" >/dev/null 2>&1; then
  die "another Bridge process is already running; refusing to start a second"
fi
rm -fv "$XDG_CACHE_HOME"/protonmail/bridge-v3/*.lock

# Bridge binds IMAP/SMTP to 127.0.0.1:1143/1025 only. socat publishes IMAP ONLY
# on container port 8143 (mapped to host 1143, LAN only). SMTP is never published.

if [ "$LOGIN_MODE" = "true" ]; then
  echo "[proton-bridge] LOGIN MODE: open the add-on Web UI (ingress) for the Bridge CLI."
  echo "[proton-bridge] Type: login  -> updates autoupdates disable  -> (optional) change imap-security -> info  -> copy IMAP creds -> exit. Then set login_mode false and restart."
  # Version is pinned by the image: we run `bridge` directly and the launcher is not in the
  # image, so downloaded updates can never run; disabling autoupdates stops the downloads.
  socat -T 1800 "$IMAP_LISTEN" SYSTEM:"/usr/local/bin/imap-gate" &
  # ttyd ONLY in login mode. It listens on a UNIX socket (never a TCP port); the ingress
  # port 7681 is a socat forwarder that accepts ONLY the Supervisor ingress proxy
  # (172.30.32.2), so access requires an HA admin session. The CLI shows the IMAP password.
  # -o (--once): ONE client session; ttyd exits when it disconnects, ending login mode
  # (the add-on then stops; set login_mode false and start it again).
  TTYD_SOCK=/run/ttyd.sock; rm -f "$TTYD_SOCK"
  socat TCP-LISTEN:7681,fork,reuseaddr,range=172.30.32.2/32 UNIX-CONNECT:"$TTYD_SOCK" &
  exec ttyd --interface "$TTYD_SOCK" --writable -o "$BRIDGE" --cli --log-level "$LOG_LEVEL"
fi

# Auto-update backstop: vault AutoUpdate defaults to true. No launcher in the image, so
# downloads never run, but any file in updates/ means autoupdate is still on -> purge.
UPD_DIR="$XDG_DATA_HOME/protonmail/bridge-v3/updates"
if [ -d "$UPD_DIR" ] && [ -n "$(ls -A "$UPD_DIR" 2>/dev/null)" ]; then
  echo "[proton-bridge] WARN: downloaded updates found in $UPD_DIR -> autoupdate is ON."
  echo "[proton-bridge] WARN: run login_mode once and type: updates autoupdates disable. Removing ignored downloads."
  rm -rf "${UPD_DIR:?}"/*
else
  echo "[proton-bridge] autoupdate check: no downloaded updates (version pinned by image)"
fi

# Clean stop (HA stop/restart) armed BEFORE any child starts: kill what exists, exit 0.
BRIDGE_PID=""; SOCAT_PID=""; SLEEP_PID=""
stop_all() {
  echo "[proton-bridge] stopping"
  [ -n "$SLEEP_PID" ] && kill "$SLEEP_PID" 2>/dev/null || true
  [ -n "$BRIDGE_PID" ] && kill "$BRIDGE_PID" 2>/dev/null || true
  [ -n "$SOCAT_PID" ] && kill "$SOCAT_PID" 2>/dev/null || true
  [ -n "$BRIDGE_PID" ] && wait "$BRIDGE_PID" 2>/dev/null || true
  exit 0
}
trap stop_all TERM INT

# Close 8143 first, then exit non-zero. The Supervisor watchdog probes socat's
# published port, not Bridge. A 30s pause after the port is closed avoids a
# tight restart loop against Proton. The pause is interruptible (HA stop).
health_fail() {
  echo "[proton-bridge] ERROR: $*"
  [ -n "$SLEEP_PID" ] && kill "$SLEEP_PID" 2>/dev/null || true
  [ -n "$SOCAT_PID" ] && kill "$SOCAT_PID" 2>/dev/null || true
  [ -n "$BRIDGE_PID" ] && kill "$BRIDGE_PID" 2>/dev/null || true
  [ -n "$SOCAT_PID" ] && wait "$SOCAT_PID" 2>/dev/null || true
  [ -n "$BRIDGE_PID" ] && wait "$BRIDGE_PID" 2>/dev/null || true
  SOCAT_PID=""; BRIDGE_PID=""
  sleep 30 &
  SLEEP_PID=$!
  wait "$SLEEP_PID" || true
  SLEEP_PID=""
  exit 1
}

echo "[proton-bridge] starting Bridge (noninteractive) v$("$BRIDGE" --version 2>/dev/null | awk '{print $NF}')"
"$BRIDGE" --noninteractive --log-level "$LOG_LEVEL" &
BRIDGE_PID=$!

# socat is the only listener on 8143 (the port the Supervisor watchdog probes).
# It is started with Bridge so the watchdog does not fail during Bridge startup.
# It must not outlive Bridge's own IMAP: Bridge closes 127.0.0.1:1143 when it
# loses Proton, and a TCP accept on 8143 would stay green while mail is dead.
socat -T 1800 "$IMAP_LISTEN" SYSTEM:"/usr/local/bin/imap-gate" &
SOCAT_PID=$!
ready=0
for _ in $(seq 1 120); do
  if /usr/local/bin/imap-greeting-ok; then ready=1; break; fi
  if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then break; fi
  sleep 2
done
if [ "$ready" != 1 ]; then
  kill "$BRIDGE_PID" "$SOCAT_PID" 2>/dev/null || true
  die "Bridge did not send an IMAP greeting on 127.0.0.1:1143 after start"
fi

echo "[proton-bridge] IMAP health: reading 127.0.0.1:1143 greeting every ${HEALTH_INTERVAL}s; stopping 8143 after ${HEALTH_FAILURES} consecutive misses"
fails=0
while true; do
  if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
    health_fail "Bridge process exited. Stopping the socat listener on 8143 and exiting so the Supervisor watchdog restarts the add-on."
  fi
  if ! kill -0 "$SOCAT_PID" 2>/dev/null; then
    health_fail "socat on 8143 exited. Exiting so the Supervisor watchdog restarts the add-on."
  fi
  if /usr/local/bin/imap-greeting-ok; then
    fails=0
  else
    fails=$((fails + 1))
    echo "[proton-bridge] WARN: no IMAP greeting on 127.0.0.1:1143 (${fails}/${HEALTH_FAILURES})"
    if [ "$fails" -ge "$HEALTH_FAILURES" ]; then
      health_fail "no IMAP greeting on 127.0.0.1:1143 for ${fails} consecutive probes (${HEALTH_INTERVAL}s apart). Bridge shuts down its IMAP server when it loses the connection to Proton, while socat on 8143 would keep the watchdog green. Stopping socat and exiting so Supervisor restarts the add-on."
    fi
  fi
  # Background sleep + wait so SIGTERM (HA stop) runs the trap immediately.
  sleep "$HEALTH_INTERVAL" &
  SLEEP_PID=$!
  wait "$SLEEP_PID" || true
  SLEEP_PID=""
done
