"""Build-time guard: fail if the app could write to X/Cloudflare or carries a secret.

Usage: python3 selfcheck.py [app_dir]   (default: this file's directory)
"""
import ast
import os
import re
import sys

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
WRITE_ENDPOINT = re.compile(r"/2/tweets[\"'/]|/likes|/retweets\b|dns_records|/settings")
TOKEN_RES = [
    re.compile(r"A{21}[A-Za-z0-9%]{20,}"),                      # X app bearer shape
    re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{40}(?![A-Za-z0-9_-])"),  # Cloudflare API token shape (40 chars), any quoting
    re.compile(r"(?i)bearer\s+[A-Za-z0-9%._-]{20,}"),
]
HEX_OK = re.compile(r"^[a-f0-9]{32,64}$")  # ids/hashes are not tokens


def _const(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def scan_python(fn, src):
    bad = []
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"{fn}: unparsable ({e})"]
    owner = {}
    for f in ast.walk(tree):
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(f):
                owner.setdefault(id(n), f.name) if n is not f else None
    # innermost wins: re-walk so nested functions overwrite
    for f in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for n in ast.walk(f):
            if n is not f:
                owner[id(n)] = f.name
    graphql_posts = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        method = (_const(kw.get("method")) or "").upper()
        if method in WRITE_METHODS - {"POST"}:
            bad.append(f"{fn}:{node.lineno}: write method {method}")
        if name == "Request" and ("data" in kw or len(node.args) >= 2 or method == "POST"):
            if fn == "collectors.py" and owner.get(id(node)) == "_graphql":
                graphql_posts += 1
            else:
                bad.append(f"{fn}:{node.lineno}: Request with body/POST outside collectors._graphql (implicit POST)")
        if name in ("request", "urlopen") and ("data" in kw or len(node.args) >= 2):
            bad.append(f"{fn}:{node.lineno}: {name}(data=...) implicit POST")
    if graphql_posts > 1:
        bad.append("collectors.py: more than one POST in _graphql")
    return bad


def scan_text(fn, src):
    bad = []
    for m in re.finditer(r"""method\s*[:=]\s*['"](PUT|PATCH|DELETE|POST)['"]""", src, re.I):
        if fn.endswith(".js") or fn.endswith(".html"):
            bad.append(f"{fn}: write method {m.group(1)}")
    if WRITE_ENDPOINT.search(src):
        bad.append(f"{fn}: write-capable endpoint")
    for rx in TOKEN_RES:
        for m in rx.finditer(src):
            tok = m.group(0)
            if HEX_OK.match(tok) or tok.isalpha() or "_" in tok and tok.replace("_", "").isalpha():
                continue
            bad.append(f"{fn}: looks like an embedded token")
            break
    return bad


def check(app_dir):
    bad = []
    for root, _, files in os.walk(app_dir):
        for fn in files:
            if not fn.endswith((".py", ".js", ".html", ".json", ".yaml")) or fn == "selfcheck.py":
                continue
            src = open(os.path.join(root, fn), encoding="utf-8", errors="ignore").read()
            if fn.endswith(".py"):
                bad += scan_python(fn, src)
            bad += scan_text(fn, src)
    return bad


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    problems = check(target)
    if problems:
        print("SELFCHECK FAILED:\n  " + "\n  ".join(problems))
        sys.exit(1)
    print("selfcheck ok: read-only, no embedded tokens")
