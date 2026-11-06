"""Fail the image build if the panel could fetch usage, hide numbers in the UI, or ship a bad snapshot."""

from __future__ import annotations

import ast
import pathlib
import sys

import snapshot

ROOT = pathlib.Path(__file__).resolve().parent
FORBIDDEN_ROOTS = {"urllib", "requests", "aiohttp", "httpx", "socket", "subprocess", "ftplib"}


def check_python(path: pathlib.Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            root = name.split(".")[0]
            if root in FORBIDDEN_ROOTS or name.startswith("http.client"):
                raise SystemExit(f"{path.name}: forbidden import {name}")


def check_static() -> None:
    static = ROOT / "static"
    for path in static.rglob("*"):
        if not path.is_file() or path.suffix == ".woff2":
            continue
        text = path.read_text(encoding="utf-8")
        # The SVG namespace is a document identifier, not a request.
        scanned = text.replace("http://www.w3.org/2000/svg", "")
        for token in ("http://", "https://", "ws://", "wss://", "setInterval", "WebSocket"):
            if token in scanned:
                raise SystemExit(f"{path.name}: forbidden token {token}")
        for number in ("37.4", "32.7", "8:02"):
            if number in text:
                raise SystemExit(f"{path.name}: usage figures belong only in the snapshot JSON")
    css = (static / "app.css").read_text(encoding="utf-8").lower()
    for token in ("box-shadow", "gradient", "drop-shadow"):
        if token in css:
            raise SystemExit(f"app.css: decorative {token} is not part of this panel")
    for color in ("#0b0d10", "#16191d", "#262b31"):
        if color not in css:
            raise SystemExit(f"app.css: missing surface color {color}")


def check_bundled() -> None:
    path = ROOT / "bundled" / "usage_snapshot.json"
    data = snapshot.validate(snapshot.load_json(str(path)))
    view = snapshot.build_view(data, "bundled")
    line = view["provenance_line"]
    expected = "Bundled snapshot · Nov 5, 2026, 8:02 PM ET · Grok Bot weekly report"
    if line != expected:
        raise SystemExit(f"bundled provenance line changed: {line}")
    raw = path.read_text(encoding="utf-8")
    if "$" in raw or "0.00" in raw:
        raise SystemExit("bundled snapshot must not invent dollar figures")


def main() -> None:
    for path in sorted(ROOT.glob("*.py")):
        check_python(path)
    check_static()
    check_bundled()
    print("selfcheck ok")


if __name__ == "__main__":
    sys.exit(main())
