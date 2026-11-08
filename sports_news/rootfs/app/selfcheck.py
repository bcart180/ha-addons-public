"""Fail the image build if Sports News could call X or ship an invalid starter snapshot."""

import ast
import datetime as dt
import json
import os
import sys

import validate

ROOT = os.path.dirname(os.path.abspath(__file__))
FORBIDDEN_ROOTS = {"urllib", "requests", "aiohttp", "httpx", "socket", "subprocess", "ftplib"}
PREVIEW = os.path.join(ROOT, "preview", "starter_2026-11-08T095038Z.json")


def check_python(path):
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            root = name.split(".")[0]
            if root in FORBIDDEN_ROOTS or name.startswith("http.client"):
                raise SystemExit(f"{os.path.basename(path)}: forbidden import {name}")


def check_static():
    static = os.path.join(ROOT, "static")
    for name in os.listdir(static):
        text = open(os.path.join(static, name), encoding="utf-8").read()
        for token in ("innerHTML", "iframe", "googletagmanager", "google-analytics", "platform.twitter", "api.x.com", "api.twitter.com"):
            if token in text:
                raise SystemExit(f"{name}: forbidden token {token}")
        if name.endswith(".js"):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                if f"method: '{method}'" in text or f'method: "{method}"' in text:
                    raise SystemExit(f"{name}: write method {method}")


def check_preview():
    raw = open(PREVIEW, "rb").read()
    text = raw.decode("utf-8")
    for token in ("api.x.com", "api.twitter.com", "bearer ", "x_bearer"):
        if token in text.lower():
            raise SystemExit("starter snapshot contains a forbidden source marker")
    collected = validate.parse_time(json.loads(text)["collected_at"], "collected_at")
    doc = validate.load_bytes(raw, collected + dt.timedelta(minutes=1))
    if doc["status"] != "starter_snapshot_not_live" or len(doc["items"]) != 10:
        raise SystemExit("starter snapshot did not validate as the labeled Nov 8 collection")
    if any(item["author_handle"].casefold() == "bcondotta" for item in doc["items"]):
        raise SystemExit("starter snapshot includes a handle that is not allowlisted")


def main():
    for name in os.listdir(ROOT):
        if name.endswith(".py") and name != "selfcheck.py":
            check_python(os.path.join(ROOT, name))
    check_static()
    check_preview()
    print("selfcheck ok")


if __name__ == "__main__":
    sys.exit(main())
