#!/usr/bin/env python3
"""Vendor shared/design/bryson-ui-4.css into each add-on listed in consumers.json.

Only destinations whose parent add-on folder exists are written. --check exits non-zero on drift.
"""
import json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
HERE = ROOT / "shared" / "design"
SRC = (HERE / "bryson-ui-4.css").read_text()
check = "--check" in sys.argv
bad = []
for c in json.loads((HERE / "consumers.json").read_text())["vendored"]:
    dest = ROOT / c["dest"]
    addon = ROOT / c["dest"].split("/")[0]
    if not addon.is_dir() or (c["status"].startswith("todo") or c["status"].startswith("held")) and not dest.exists():
        continue
    if check:
        if not dest.exists() or dest.read_text() != SRC:
            bad.append(c["dest"])
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(SRC)
        print("synced", c["dest"])
if bad:
    sys.exit("design drift: " + ", ".join(bad) + "  (run python3 shared/design/sync.py)")
if check:
    print("ok")
