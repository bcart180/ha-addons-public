#!/usr/bin/env python3
"""Read-only guard for the Trading Desk add-on.

Scans every source file shipped in the image and exits non-zero if anything
that could write to a broker appears: HTTP POST/PUT/PATCH/DELETE calls, write
routes, or order submit/replace/cancel/close helpers. Run at image build
(Dockerfile) and again at every start (server.py) and in tests.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent
SELF = pathlib.Path(__file__).resolve()

# Patterns that apply to every file, including vendored libraries.
COMMON = [
    (r"""method\s*[:=]\s*['"](POST|PUT|PATCH|DELETE)['"]""", "HTTP write method"),
    (r"""\b(request|_request)\s*\(\s*['"](POST|PUT|PATCH|DELETE)['"]""", "HTTP write request"),
    (r"""add_route\s*\(\s*['"](POST|PUT|PATCH|DELETE)['"]""", "write route"),
    (r"\badd_(post|put|patch|delete)\s*\(", "write route"),
    (r"\b(submit|replace|cancel|place)_?(all_)?orders?\b", "order write helper"),
    (r"\bclose_(all_)?positions?\b", "position close helper"),
    (r"\bXMLHttpRequest\b", "raw XHR (use GET fetch/EventSource only)"),
]
# Patterns that apply to first-party code only (vendored JS uses Map.delete etc.).
FIRST_PARTY = [
    (r"\.(post|put|patch|delete)\s*\(", "HTTP write call"),
    (r"""['"](POST|PUT|PATCH|DELETE)['"]""", "HTTP write verb literal"),
]
EXTS = {".py", ".js", ".html", ".sh", ".css", ".json"}


def scan(root: pathlib.Path = ROOT):
    problems = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in EXTS or path.resolve() == SELF:
            continue
        text = path.read_text(errors="replace")
        vendored = "vendor" in path.parts
        rules = COMMON + ([] if vendored else FIRST_PARTY)
        for lineno, line in enumerate(text.splitlines(), 1):
            for pat, why in rules:
                if re.search(pat, line, re.IGNORECASE if why.startswith("order") or why.startswith("position") else 0):
                    problems.append(f"{path.relative_to(root)}:{lineno}: {why}: {line.strip()[:120]}")
    return problems


def main():
    root = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT
    problems = scan(root)
    if problems:
        print("READ-ONLY SELF-CHECK FAILED:")
        for p in problems:
            print("  " + p)
        sys.exit(1)
    print(f"read-only self-check passed ({root})")


if __name__ == "__main__":
    main()
