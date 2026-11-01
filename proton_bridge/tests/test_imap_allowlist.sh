#!/usr/bin/env bash
# Tests imap-allowlist + imap-gate source filtering (no network, no Bridge).
# Run: proton_bridge/tests/test_imap_allowlist.sh   (needs bash + jq)
set -u
cd "$(dirname "$0")/.."
BIN=rootfs/usr/local/bin
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
# Stub starttls-guard: imap-gate execs it on allow. Copy the gate with the stub path.
printf '#!/bin/sh\necho ALLOWED\n' > "$T/guard"; chmod +x "$T/guard"
sed "s#/usr/local/bin/starttls-guard#$T/guard#" "$BIN/imap-gate" > "$T/gate"; chmod +x "$T/gate"
sed -i "s#/tmp/imap-gate#$T/imap-gate#g" "$T/gate"
fail=0; n=0
lists() { printf '%s' "$1" > "$T/o.json"; L=$("$BIN/imap-allowlist" "$T/o.json" 2>/dev/null)
  ALLOW=$(sed -n 's/^ALLOW=//p' <<<"$L"); DENY=$(sed -n 's/^DENY=//p' <<<"$L"); }
expect() { # $1 opts json, $2 peer, $3 allow|deny
  lists "$1"; n=$((n+1))
  out=$(SOCAT_PEERADDR="$2" IMAP_ALLOW="$ALLOW" IMAP_DENY="$DENY" "$T/gate" 2>/dev/null)
  got=deny; [ "$out" = ALLOWED ] && got=allow
  if [ "$got" != "$3" ]; then echo "FAIL: opts=$1 peer=$2 want=$3 got=$got (allow='$ALLOW' deny='$DENY')"; fail=1; fi
}
MAC=192.0.2.10/32   # RFC 5737 documentation address (placeholder)
D='{"lan_cidr":"","deny_docker_gateway":true,"internal_allow_cidr":""}'
W="{\"lan_cidr\":\"$MAC\",\"deny_docker_gateway\":true,\"internal_allow_cidr\":\"\"}"
# Defaults: loopback only.
expect "$D" 127.0.0.1 allow
expect "$D" 127.0.0.2 deny
expect "$D" 172.30.32.1 deny
expect "$D" 172.30.32.2 deny
expect "$D" 172.30.33.5 deny
expect "$D" 192.0.2.10 deny
# lan_cidr admits the configured /32 and nothing else.
expect "$W" 192.0.2.10 allow
expect "$W" ::ffff:192.0.2.10 allow
expect "$W" 192.0.2.11 deny
expect "$W" 172.30.32.5 deny
expect "$W" 172.30.32.1 deny
expect "$W" 127.0.0.1 allow
# Gateway deny wins even when an allow entry covers it.
expect '{"lan_cidr":"172.30.32.0/23","deny_docker_gateway":true}' 172.30.32.1 deny
expect '{"lan_cidr":"172.30.32.0/23","deny_docker_gateway":true}' 172.30.32.7 allow
expect '{"internal_allow_cidr":"172.30.32.0/23"}' 172.30.32.1 deny
# Missing deny_docker_gateway key = default true.
expect '{"lan_cidr":"0.0.0.0/0"}' 172.30.32.1 deny
# Explicit false turns the deny off (gateway then needs an allow entry).
expect '{"deny_docker_gateway":false}' 172.30.32.1 deny
expect '{"deny_docker_gateway":false,"internal_allow_cidr":"172.30.32.1/32"}' 172.30.32.1 allow
# internal_allow_cidr: explicit, named, single source.
expect '{"internal_allow_cidr":"172.30.33.9/32"}' 172.30.33.9 allow
expect '{"internal_allow_cidr":"172.30.33.9/32"}' 172.30.33.10 deny
# Invalid values are ignored (not widened).
expect '{"lan_cidr":"192.0.2.0/24 0.0.0.0/0"}' 198.51.100.1 deny
expect '{"internal_allow_cidr":"bogus"}' 172.30.32.5 deny
expect '{"lan_cidr":"192.0.2.10"}' 192.0.2.10 deny
# Malformed peers are refused.
expect "$W" "" deny
expect "$W" 999.1.1.1 deny
expect "$W" ::1 deny
# Missing options file: loopback only, gateway denied.
L=$("$BIN/imap-allowlist" "$T/missing.json" 2>/dev/null); n=$((n+1))
[ "$L" = $'ALLOW=127.0.0.1/32\nDENY=172.30.32.1/32' ] || { echo "FAIL: missing opts -> $L"; fail=1; }
# No default allow entry for the HA network anywhere in the shipped sources.
n=$((n+1)); if grep -q '172.30.32.0/23 127' rootfs/run.sh; then echo "FAIL: run.sh still hard-codes 172.30.32.0/23"; fail=1; fi
[ $fail -eq 0 ] && echo "PASS: $n checks" || { echo "FAILED"; exit 1; }
