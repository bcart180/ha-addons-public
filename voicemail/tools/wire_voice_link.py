#!/usr/bin/env python3
"""Wire CallCatch <-> CallCatch Voice through Supervisor add-on options.

Pairs (copied, never generated, never printed):
  Voice ha_webhook_token  -> CallCatch ingest_token      (call ingest bearer)
  Voice trust_api_token   -> CallCatch voice_trust_token (allow/block-list API bearer)
  Voice hostname          -> CallCatch voice_trust_url   (http://<hostname>:8787)

Voice is the source of truth. If a Voice secret is blank, this exits non-zero and
asks for it to be set in the Voice Configuration tab first. It never mints a value.

Default is --dry-run: reports only "set"/"blank"/"match"/"differs" per pair.
--apply writes CallCatch options (one POST) and does NOT restart anything.
Run where SUPERVISOR_TOKEN is present (an add-on or the SSH add-on).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

SUPERVISOR = "http://supervisor"
PORT = 8787
PAIRS = (
    ("ha_webhook_token", "ingest_token"),
    ("trust_api_token", "voice_trust_token"),
)


def _req(method: str, path: str, body: dict | None = None) -> dict:
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise SystemExit("SUPERVISOR_TOKEN not set; run inside an add-on")
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(SUPERVISOR + path, data=data, method=method)
    r.add_header("Authorization", "Bearer " + token)
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=20) as resp:  # noqa: S310 (fixed host)
        return json.loads(resp.read().decode() or "{}").get("data") or {}


def find_slug(addons: list[dict], suffix: str) -> str:
    hits = [a["slug"] for a in addons if a.get("slug", "").split("_", 1)[-1] == suffix or a.get("slug") == suffix]
    if len(hits) != 1:
        raise SystemExit(f"expected one add-on ending in {suffix!r}, found {len(hits)}")
    return hits[0]


def trust_url(hostname: str) -> str:
    host = (hostname or "").strip()
    if not host or "." in host or "/" in host or ":" in host:
        raise SystemExit("Voice hostname unusable for voice_trust_url")
    return f"http://{host}:{PORT}"


def plan(voice_opts: dict, cc_opts: dict, url: str) -> tuple[dict, list[str]]:
    """Return (new CallCatch options, redacted report lines). Pure; no I/O."""
    new = dict(cc_opts)
    report: list[str] = []
    missing = []
    for src, dst in PAIRS:
        v = str(voice_opts.get(src) or "")
        c = str(cc_opts.get(dst) or "")
        if not v:
            missing.append(src)
            report.append(f"{src}->{dst}: voice blank")
            continue
        report.append(f"{src}->{dst}: {'match' if v == c else 'differs'}")
        new[dst] = v
    report.append(f"voice_trust_url: {'match' if cc_opts.get('voice_trust_url') == url else 'differs'} ({url})")
    new["voice_trust_url"] = url
    if missing:
        raise SystemExit("set in CallCatch Voice first: " + ", ".join(missing) + "\n" + "\n".join(report))
    return new, report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write CallCatch options (no restart)")
    a = ap.parse_args(argv)
    addons = _req("GET", "/addons").get("addons", [])
    voice = find_slug(addons, "twilio_house_voice")
    cc = find_slug(addons, "voicemail")
    vinfo = _req("GET", f"/addons/{voice}/info")
    cinfo = _req("GET", f"/addons/{cc}/info")
    new, report = plan(vinfo.get("options") or {}, cinfo.get("options") or {}, trust_url(vinfo.get("hostname", "")))
    print("\n".join(report))
    if not a.apply:
        print("dry-run: nothing written")
        return 0
    _req("POST", f"/addons/{cc}/options", {"options": new})
    print("CallCatch options written; restart CallCatch to load them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
