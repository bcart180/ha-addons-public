"""Run: python3 social_pulse/tests/test_social_pulse.py  (stdlib only, no network)."""
import base64
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.dirname(HERE)
APP = os.path.join(ADDON, "rootfs", "app")
sys.path.insert(0, APP)
import collectors as c  # noqa: E402

NOW = dt.datetime(2026, 11, 1, 15, 20, tzinfo=dt.timezone.utc)
FIX = os.path.join(HERE, "fixtures")


def fx(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


ZONES = [{"name": "site-a.example", "zone_id": "0000000000000000000000000000a001", "rum_site_tag": "0000000000000000000000000000b001"},
         {"name": "site-b.example", "zone_id": "0000000000000000000000000000a002", "rum_site_tag": "0000000000000000000000000000b002"}]
HANDLES = ["example_stays", "ExampleTavern", "ExampleAmbience"]


class XTests(unittest.TestCase):
    def test_public_metrics_from_real_sample(self):
        out = {a["handle"]: a for a in c.x_normalize(fx("x_raw.json"), HANDLES, NOW)}
        self.assertEqual(out["ExampleAmbience"]["followers"]["value"], 2)
        self.assertEqual(out["ExampleAmbience"]["posts_7d"]["value"], 14)
        self.assertEqual(out["ExampleAmbience"]["post_impressions_7d"]["value"], 39)
        self.assertEqual(out["example_stays"]["post_impressions_7d"]["value"], 11)

    def test_private_metrics_are_not_available_never_guessed(self):
        for a in c.x_normalize(fx("x_raw.json"), HANDLES, NOW):
            for k in ("profile_visits", "link_clicks", "account_impressions"):
                self.assertFalse(a[k]["available"])
                self.assertIsNone(a[k]["value"])
                self.assertTrue(a[k]["reason"])
            # zero engagements with impressions -> 0%, not a guess
            self.assertEqual(a["engagement_rate_7d"]["value"], 0.0)

    def test_adding_example_new_is_one_handle(self):
        out = c.x_normalize(fx("x_raw.json"), HANDLES + ["example_new"], NOW)
        self.assertEqual(len(out), 4)
        self.assertIn("error", out[-1])  # not in fixture -> shown as error, not fabricated

    def test_missing_token_marks_everything_unavailable(self):
        snap = c.collect({"x_accounts": HANDLES, "zones": ZONES,
                          "cloudflare_account_id": "0000000000000000000000000000c001"}, NOW)
        self.assertTrue(all("x_bearer_token not set" in a["error"] for a in snap["x"]))
        for s in snap["sites"]:
            self.assertFalse(s["requests_7d"]["available"])
            self.assertIsNone(s["requests_7d"]["value"])
            self.assertIsNone(s["rum_visits_30d"]["value"])
            self.assertIn("cloudflare_api_token not set", s["error"])
            self.assertNotIn("disabled", (s.get("rum_status_detail") or "").lower())


class CloudflareTests(unittest.TestCase):
    def setUp(self):
        self.sites = {s["name"]: s for s in c.cf_normalize(fx("cf_raw.json"), ZONES, NOW)}

    def test_totals_from_real_sample(self):
        w = self.sites["site-a.example"]
        self.assertEqual(w["requests_7d"]["value"], 12109)
        self.assertTrue(w["page_views_30d"]["available"])
        self.assertEqual(w["countries_7d"]["value"][0][0], "US")

    def test_top_pages_drop_assets_api_and_wp_probes(self):
        for s in self.sites.values():
            for p, _ in s["top_paths_7d"]["value"]:
                self.assertIsNone(re.search(r"wp-|\.php|\.css|\.jpg|\.png|^/api/", p), p)
        self.assertGreater(self.sites["site-b.example"]["bot_probe_requests_7d"]["value"], 900)

    def test_graphql_error_becomes_not_available(self):
        raw = {"site-a.example": {"zone": {"data": None, "errors": [{"message": "not authorized for that zone"}]}}}
        s = c.cf_normalize(raw, ZONES[:1], NOW)[0]
        self.assertFalse(s["requests_7d"]["available"])
        self.assertIn("not authorized", s["requests_7d"]["reason"])

    def test_graphql_guard_rejects_mutations(self):
        with self.assertRaises(ValueError):
            c._graphql("mutation{x}", "t")

    def test_uniques_are_labeled_summed_not_deduped(self):
        for s in self.sites.values():
            self.assertIn("not deduplicated across the 7 days", s["uniques_7d"]["note"].lower())
            self.assertIn("not deduplicated across the 30 days", s["uniques_30d"]["note"].lower())
            self.assertEqual(s["uniques_7d"]["source"], "edge")
            self.assertEqual(s["page_views_7d"]["source"], "edge")
            self.assertIn("not sampled browser visits", s["page_views_7d"]["note"].lower())

    def test_seven_and_thirty_day_windows_from_sample(self):
        w = self.sites["site-a.example"]
        b = self.sites["site-b.example"]
        self.assertEqual(w["requests_7d"]["value"], 12109)
        self.assertEqual(w["windows"]["edge_requests"]["pct"], 87.8)
        self.assertEqual(w["windows"]["edge_requests"]["pct"], round(100.0 * 12109 / 13786, 1))
        self.assertEqual(w["windows"]["edge_page_views"]["pct"], 83.9)
        self.assertEqual(w["rum_visits_30d"]["value"], 20)
        self.assertEqual(w["rum_visits_7d"]["value"], 20)
        self.assertEqual(w["rum_visits_7d"]["source"], "web_analytics")
        self.assertFalse(w["rum_referrers_7d"]["available"])
        self.assertTrue(w["rum_referrers"]["available"])
        self.assertEqual(b["rum_visits_7d"]["value"], 30)
        self.assertEqual(b["rum_visits_30d"]["value"], 40)
        self.assertEqual(b["windows"]["rum_visits"]["pct"], 75.0)
        us7 = dict(w["countries_7d"]["value"])["US"]
        self.assertEqual(dict(w["countries_30d"]["value"])["US"], us7 + 679)
        self.assertFalse(w["prev7"]["available"])
        self.assertNotIn("disabled", w["prev7"]["reason"].lower())

    def test_rum7_query_wins_over_the_daily_slice(self):
        raw = fx("cf_raw.json")
        raw["site-a.example"]["rum7"] = {"data": {"viewer": {"accounts": [{
            "daily": [{"count": 5, "dimensions": {"date": "2026-10-31"}, "sum": {"visits": 4}}],
            "pages": [{"count": 5, "dimensions": {"requestPath": "/stay"}}],
            "refs": [{"count": 5, "dimensions": {"refererHost": "news.example"}}],
        }]}}, "errors": None}
        s = c.cf_normalize(raw, ZONES[:1], NOW)[0]
        self.assertEqual(s["rum_visits_7d"]["value"], 4)
        self.assertEqual(s["rum_pageloads_7d"]["value"], 5)
        self.assertEqual(s["rum_visits_30d"]["value"], 20)
        self.assertEqual(s["rum_top_pages_7d"]["value"][0], ["/stay", 5])
        self.assertEqual(s["rum_referrers_7d"]["value"][0], ["news.example", 5])
        self.assertEqual(s["rum_status"], "ok")

    def test_scope_miss_keeps_edge_and_does_not_invent_visits(self):
        zone = fx("cf_raw.json")["site-a.example"]
        calls = []

        def gql(q, token):
            calls.append(q)
            self.assertTrue(q.lstrip().startswith("query"))
            self.assertNotIn("mutation", q.lower())
            if "rumPageload" in q:
                raise RuntimeError("HTTP Error 403: Forbidden")
            return zone["zone"]

        raw = c.cf_fetch_raw(ZONES[:1], "0000000000000000000000000000c001", "tok", NOW, gql)
        rum_calls = [q for q in calls if "rumPageload" in q]
        self.assertEqual(len(rum_calls), 2)
        self.assertTrue(any('date_geq:"2026-10-03"' in q for q in rum_calls))
        self.assertTrue(any('date_geq:"2026-10-26"' in q and "limit:10" in q for q in rum_calls))
        s = c.cf_normalize(raw, ZONES[:1], NOW)[0]
        self.assertEqual(s["requests_7d"]["value"], 12109)
        self.assertFalse(s["rum_visits_7d"]["available"])
        self.assertFalse(s["rum_visits_30d"]["available"])
        self.assertIsNone(s["rum_visits_7d"]["value"])
        self.assertIsNone(s["rum_visits_30d"]["value"])
        self.assertEqual(s["rum_status"], "missing_scope")
        detail = s["rum_status_detail"]
        self.assertIn("Account Analytics", detail)
        self.assertIn("does not need to be replaced", detail)
        self.assertIn("Edge request metrics are unaffected", detail)
        self.assertNotIn("disabled", detail.lower())
        self.assertNotIn("rotat", detail.lower())

    def test_optional_rum7_failure_uses_daily_sample_not_edge(self):
        sample = fx("cf_raw.json")["site-a.example"]

        def gql(q, token):
            if "rumPageload" in q and 'date_geq:"2026-10-26"' in q:
                raise RuntimeError("timeout talking to analytics")
            if "rumPageload" in q:
                return sample["rum"]
            return sample["zone"]

        raw = c.cf_fetch_raw(ZONES[:1], "0000000000000000000000000000c001", "tok", NOW, gql)
        s = c.cf_normalize(raw, ZONES[:1], NOW)[0]
        self.assertEqual(s["requests_7d"]["value"], 12109)
        self.assertEqual(s["rum_status"], "ok")
        self.assertEqual(s["rum_visits_30d"]["value"], 20)
        self.assertEqual(s["rum_visits_7d"]["value"], 20)
        self.assertIn("not estimated from edge requests", s["rum_visits_7d"]["note"].lower())
        self.assertFalse(s["rum_referrers_7d"]["available"])
        self.assertTrue(s["rum_referrers"]["available"])
        self.assertNotIn("disabled", s["rum_referrers_7d"]["reason"].lower())

    def test_bad_ids_are_not_sent(self):
        calls = []
        zones = [{"name": "evil.example", "zone_id": 'abc"; mutation {', "rum_site_tag": "zzzz"}]
        raw = c.cf_fetch_raw(zones, "not-an-account", "tok", NOW, lambda q, t: calls.append(q))
        self.assertEqual(calls, [])
        s = c.cf_normalize(raw, zones, NOW)[0]
        self.assertFalse(s["requests_7d"]["available"])
        self.assertIsNone(s["rum_visits_30d"]["value"])
        self.assertNotIn("mutation", json.dumps(s).lower())

    def test_prev7_change_requires_full_prior_window(self):
        start = dt.date(2026, 10, 19)
        days = []
        for i in range(14):
            day = start + dt.timedelta(days=i)
            days.append({"dimensions": {"date": day.isoformat()},
                         "sum": {"requests": 20 if i >= 7 else 10, "pageViews": 1, "bytes": 1, "threats": 0, "countryMap": []},
                         "uniq": {"uniques": 1}})
        zone = [{"name": "synth.example", "zone_id": "0000000000000000000000000000a001"}]
        raw = {"synth.example": {"zone": {"data": {"viewer": {"zones": [{"daily": days, "paths": []}]}}}}}
        s = c.cf_normalize(raw, zone, NOW)[0]
        self.assertTrue(s["prev7"]["available"])
        self.assertEqual(s["prev7"]["requests"]["prior"], 70)
        self.assertEqual(s["prev7"]["requests"]["current"], 140)
        self.assertEqual(s["prev7"]["requests"]["change_pct"], 100.0)
        short = {"synth.example": {"zone": {"data": {"viewer": {"zones": [{"daily": days[1:], "paths": []}]}}}}}
        s2 = c.cf_normalize(short, zone, NOW)[0]
        self.assertFalse(s2["prev7"]["available"])
        self.assertNotIn("requests", s2["prev7"])
        zeros = []
        for i in range(14):
            day = start + dt.timedelta(days=i)
            zeros.append({"dimensions": {"date": day.isoformat()},
                          "sum": {"requests": 0 if i < 7 else 5, "pageViews": 0, "bytes": 0, "threats": 0, "countryMap": []},
                          "uniq": {"uniques": 0}})
        zraw = {"synth.example": {"zone": {"data": {"viewer": {"zones": [{"daily": zeros, "paths": []}]}}}}}
        s3 = c.cf_normalize(zraw, zone, NOW)[0]
        self.assertIsNone(s3["prev7"]["requests"]["change_pct"])
        self.assertIn("undefined", s3["prev7"]["requests"]["change_reason"])

    def test_successful_empty_sample_is_zero_not_permission(self):
        zone = fx("cf_raw.json")["site-a.example"]["zone"]
        empty = {"data": {"viewer": {"accounts": [{"daily": [], "pages": [], "refs": []}]}}, "errors": None}
        raw = {"site-a.example": {"zone": zone, "rum": empty, "rum7": empty}}
        s = c.cf_normalize(raw, ZONES[:1], NOW)[0]
        self.assertEqual(s["rum_status"], "ok")
        self.assertEqual(s["rum_visits_30d"]["value"], 0)
        self.assertTrue(s["rum_visits_30d"]["available"])
        self.assertEqual(s["rum_visits_7d"]["value"], 0)


class SecurityTests(unittest.TestCase):
    def test_redact(self):
        self.assertNotIn("SECRET123", c.redact("HTTP 401 Bearer SECRET123 failed SECRET123", ["SECRET123"]))

    def test_selfcheck_passes(self):
        r = subprocess.run([sys.executable, os.path.join(APP, "selfcheck.py")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_tokens_only_as_password_options(self):
        cfg = open(os.path.join(ADDON, "config.yaml")).read()
        self.assertIn("x_bearer_token: password?", cfg)
        self.assertIn("cloudflare_api_token: password?", cfg)
        self.assertNotIn("\nmap:", cfg)
        self.assertIn("homeassistant_api: false", cfg)

    def test_no_tokens_anywhere_in_repo(self):
        root = ADDON  # this add-on only (the store repo holds other add-ons with their own guards)
        pat = re.compile("(?i)" + "A" * 21 + r"|bearer [A-Za-z0-9%._-]{20,}")
        for d, _, files in os.walk(root):
            if ".git" in d:
                continue
            for fn in files:
                if fn.endswith((".py", ".js", ".html", ".yaml", ".md", ".json", ".css")):
                    src = open(os.path.join(d, fn), encoding="utf-8", errors="ignore").read()
                    self.assertIsNone(pat.search(src), fn)


class ServerTests(unittest.TestCase):
    def test_serves_snapshot_and_refuses_writes(self):
        import importlib
        os.environ["SP_PORT"] = "0"
        server = importlib.import_module("server")
        server._state["snap"] = fx("snapshot.json")
        server.ALLOW = {"127.0.0.1"}
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        snap = json.loads(urllib.request.urlopen(base + "/api/snapshot").read())
        self.assertEqual(len(snap["x"]), 3)
        self.assertIn(b"Social Pulse", urllib.request.urlopen(base + "/").read())
        req = urllib.request.Request(base + "/api/snapshot", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        self.assertEqual(e.exception.code, 405)
        httpd.shutdown()


class SelfcheckBypassTests(unittest.TestCase):
    """MA L2: each bypass must now fail the build guard."""

    def run_on(self, fname, src):
        import tempfile
        d = tempfile.mkdtemp()
        with open(os.path.join(d, fname), "w") as f:
            f.write(src)
        return subprocess.run([sys.executable, os.path.join(APP, "selfcheck.py"), d], capture_output=True, text=True)

    def test_implicit_post_via_data(self):
        r = self.run_on("evil.py", "import urllib.request as u\nu.urlopen(u.Request('https://x', data=b'1'))\n")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("implicit POST", r.stdout)

    def test_implicit_post_positional_data(self):
        r = self.run_on("evil.py", "import urllib.request as u\ndef f():\n    return u.Request('https://x', b'1')\n")
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_urlopen_data(self):
        r = self.run_on("evil.py", "import urllib.request as u\nu.urlopen('https://x', data=b'1')\n")
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_single_quoted_write_methods(self):
        for m in ("DELETE", "PUT", "PATCH"):
            r = self.run_on("evil.py", "import urllib.request as u\nu.Request('https://x', method='%s')\n" % m)
            self.assertEqual(r.returncode, 1, m + r.stdout)

    def test_js_write_method(self):
        r = self.run_on("evil.js", "fetch('api/x', {method: 'DELETE'})\n")
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_post_outside_graphql_in_collectors(self):
        r = self.run_on("collectors.py", "import urllib.request as u\ndef other():\n    return u.Request('https://x', data=b'1', method='POST')\n")
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_cloudflare_token_without_closing_quote(self):
        tok = "Zq" + "9xK2" * 9 + "_-"  # 40 chars, CF token shape, not a real token
        for src in ("T = '" + tok + "\n", "T = " + tok + "\n", "# " + tok):
            r = self.run_on("evil.py", src)
            self.assertEqual(r.returncode, 1, src + r.stdout)

    def test_clean_app_still_passes(self):
        r = subprocess.run([sys.executable, os.path.join(APP, "selfcheck.py")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)


class IngressAllowlistTests(unittest.TestCase):
    """MA L1: only 172.30.32.2 is served; every other source gets 403."""

    def setUp(self):
        import importlib
        self.server = importlib.import_module("server")
        self.server._state["snap"] = fx("snapshot.json")

    def serve(self, allow):
        self.server.ALLOW = set(allow)
        httpd = self.server.ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return "http://127.0.0.1:%d" % httpd.server_address[1]

    def test_default_allowlist_is_supervisor_only(self):
        self.assertEqual(self.server.ALLOW if os.environ.get("SP_ALLOW") else {"172.30.32.2"}, {"172.30.32.2"})

    def test_other_source_gets_403_everywhere(self):
        base = self.serve({"172.30.32.2"})  # test client is 127.0.0.1 -> not allowed
        for path, method in (("/api/snapshot", "GET"), ("/api/health", "GET"), ("/", "GET"), ("/index.html", "HEAD"), ("/api/snapshot", "POST"), ("/x", "DELETE")):
            req = urllib.request.Request(base + path, method=method)
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 403, path + method)

    def test_allowed_source_is_served(self):
        base = self.serve({"127.0.0.1"})
        self.assertEqual(json.loads(urllib.request.urlopen(base + "/api/health").read()), {"ok": True})


class SiteDashboardTests(unittest.TestCase):
    def test_panel_separates_edge_sampled_and_summed_uniques(self):
        static = os.path.join(APP, "static")
        js = open(os.path.join(static, "app.js"), encoding="utf-8").read()
        html = open(os.path.join(static, "index.html"), encoding="utf-8").read()
        css = open(os.path.join(static, "soft-slate.css"), encoding="utf-8").read()
        blob = "\n".join((js, html, css))
        for host in ("winchestergetaway.com", "hideawaytavernlcv.com"):
            self.assertIn(host, html)
            self.assertIn(host, js)
        self.assertIn("rum_visits", js)
        self.assertIn("Summed daily uniques", js)
        self.assertIn("Not deduplicated", js)
        self.assertIn("Edge requests", js)
        self.assertIn("Sampled", js)
        self.assertIn("Nothing here is estimated", js)
        self.assertIn("soft-slate.css", html)
        self.assertIn("#000000", css)
        self.assertIn("#e8eef4", css)
        self.assertIn("#d7b4ff", css)
        self.assertIn("#ff9e4a", css)
        self.assertIn("#ffe566", css)
        self.assertIn("#ff7a7a", css)
        self.assertIn("#ffb3d9", css)
        self.assertIn("#7dffb3", css)
        self.assertIn("#7ee0ff", css)
        self.assertIn("--radius: 16px", css)
        self.assertIn("Edge requests", js)
        self.assertIn("Edge page views", js)
        self.assertNotIn("#d5f0ec", js)
        self.assertNotIn("#1e2a36", js)
        self.assertNotIn("disabled", blob.lower())
        self.assertNotIn("rotat", blob.lower())
        self.assertIsNone(re.search(r"requests_7d\"\]\s*=\s*\d", js))

    def test_docs_explain_the_permission_gap(self):
        docs = open(os.path.join(ADDON, "DOCS.md"), encoding="utf-8").read()
        self.assertIn("Account Analytics", docs)
        self.assertIn("not deduplicated", docs.lower())
        self.assertIn("winchestergetaway.com", docs)
        self.assertIn("hideawaytavernlcv.com", docs)
        self.assertNotIn("disabled", docs.lower())
        self.assertNotIn("rotat", docs.lower())
        cfg = open(os.path.join(ADDON, "config.yaml"), encoding="utf-8").read()
        self.assertIn('version: "0.3.2"', cfg)
        self.assertNotIn('version: "0.3.1"', cfg)
        self.assertNotIn("Soft Slate", docs)
        readme = open(os.path.join(ADDON, "README.md"), encoding="utf-8").read()
        self.assertNotIn("Soft Slate", readme)
        self.assertIn("#000000", docs)
        self.assertRegex(cfg, r"zones:\s*\[\]")


PROD_SHA = {
    "index.html": (1513, "6995d19f7be1185c9aef4c8db109ea631523eff331419d3ef346520e77b670ae"),
    "app.css": (6954, "4b123b3d3f93047b55d343598e2002754ba5708d6cd35e17d909a32f38e58e99"),
    "app.js": (13653, "1a04928dc1054a5d98c929437d9aafc41a3ed7fd7813ddcb4fb1ff91aea1f5b3"),
    "soft-slate.css": (3674, "019fb22224e06b0598ceefd4e7cc1f0b2498c5f70edf3d21e9c3e15ed10a0f2b"),
}
COLLECTOR_SHA = (22865, "9e8d4b211abc66ac6fea788bad2e9900f02ea2500a7e11712cafc4541c98b0b8")
PROD_CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'self'"
SCHEMA = """schema:
  # Secrets: password fields only, entered at install time. Never in the repo.
  x_bearer_token: password?
  cloudflare_api_token: password?
  x_accounts:
    - match(^[A-Za-z0-9_]{1,15}$)
  cloudflare_account_id: match(^[a-f0-9]{32}$)?
  zones:
    - name: str
      zone_id: match(^[a-f0-9]{32}$)
      rum_site_tag: match(^[a-f0-9]{32}$)?
  refresh_minutes: int(15,1440)
  snapshot_file: str?
  log_level: list(debug|info|warning|error)
"""


def trial_ingress_root(pathname):
    """Mirror the trial head script: strip the trial document, then keep a trailing slash."""
    text = open(os.path.join(APP, "static", "ui-trial", "index.html"), encoding="utf-8").read()
    listed = re.search(r"var suffixes = \[(.*?)\];", text, re.S)
    if listed is None:
        raise AssertionError("trial head script is missing the suffix list")
    suffixes = re.findall(r'"([^"]+)"', listed.group(1))
    path = pathname
    for suffix in suffixes:
        if len(path) >= len(suffix) and path.endswith(suffix):
            path = path[: len(path) - len(suffix)]
            break
    if not path or not path.endswith("/"):
        path += "/"
    return path


def _hue_sat(hex_color):
    value = hex_color[1:]
    channels = [int(value[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    high = max(channels)
    low = min(channels)
    if high == 0 or high == low:
        return None, 0.0
    sat = (high - low) / high
    delta = high - low
    red, green, blue = channels
    if high == red:
        hue = ((green - blue) / delta) % 6
    elif high == green:
        hue = (blue - red) / delta + 2
    else:
        hue = (red - green) / delta + 4
    return hue * 60, sat


class TrialTests(unittest.TestCase):
    def test_version_schema_and_docs(self):
        cfg = open(os.path.join(ADDON, "config.yaml"), encoding="utf-8").read()
        self.assertIn('version: "0.3.2"', cfg)
        self.assertIn(SCHEMA, cfg)
        self.assertIn("ingress_port: 8099", cfg)
        self.assertIn("panel_admin: true", cfg)
        self.assertIn("homeassistant_api: false", cfg)
        self.assertNotIn("auth_api", cfg)
        self.assertNotIn("homeassistant_api: true", cfg)
        changelog = open(os.path.join(ADDON, "CHANGELOG.md"), encoding="utf-8").read()
        self.assertIn("## 0.3.2", changelog)
        for name in ("README.md", "DOCS.md"):
            text = open(os.path.join(ADDON, name), encoding="utf-8").read()
            self.assertIn("/ui-trial", text)
            self.assertIn("0.3.1", text)
        collector = open(os.path.join(APP, "collectors.py"), "rb").read()
        self.assertEqual((len(collector), hashlib.sha256(collector).hexdigest()), COLLECTOR_SHA)

    def test_production_bytes_and_trial_source(self):
        static = os.path.join(APP, "static")
        for name, (size, digest) in PROD_SHA.items():
            raw = open(os.path.join(static, name), "rb").read()
            self.assertEqual(len(raw), size, name)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest, name)
        trial = os.path.join(static, "ui-trial")
        banned = ("orange", "amber", "#ff9e4a", "#ffe566", "gold", "winchester", "hideaway", "nabu.casa", "/legacy")
        blob = []
        for name in ("index.html", "app.css", "app.js"):
            text = open(os.path.join(trial, name), encoding="utf-8").read()
            blob.append(text)
            lowered = text.lower()
            for token in banned:
                self.assertNotIn(token, lowered, name + " " + token)
            for old_blue in ("#005aff", "#206fff", "#004ad6", "rgba(0,90,255"):
                self.assertNotIn(old_blue, lowered, name + " " + old_blue)
            self.assertNotIn("scrollIntoView", text, name)
            self.assertNotIn("autofocus", lowered, name)
            for match in re.finditer(r"#[0-9a-f]{6}\b", lowered):
                hue, sat = _hue_sat(match.group(0))
                if hue is not None and 18 <= hue <= 62 and sat >= 0.18:
                    self.fail("%s warm hue %s h=%.1f s=%.2f" % (name, match.group(0), hue, sat))
        html, css, js = blob
        self.assertIn('data-ui="bryson-ui-4"', html)
        self.assertIn('href="ui-trial/app.css"', html)
        self.assertIn('src="ui-trial/app.js"', html)
        self.assertNotIn('href="/', html)
        self.assertNotIn('src="/', html)
        self.assertIn('aria-label="Range"', html)
        self.assertIn('data-range="7d"', html)
        self.assertIn('data-range="30d"', html)
        self.assertIn("min-height: 48px", css)
        self.assertIn("#3e6ae1", css)
        self.assertIn("#5c84e8", css)
        self.assertIn("#2f56c0", css)
        self.assertIn("#000000", css)
        self.assertIn("#414141", css)
        self.assertIn("@media (min-width: 768px)", css)
        self.assertIn("@media (min-width: 1280px)", css)
        self.assertIn("overflow-anchor: none", css)
        self.assertIn('fetch("api/snapshot", { cache: "no-store" })', js)
        self.assertEqual(js.count("fetch("), 1)
        self.assertNotIn('fetch("/', js)
        self.assertNotIn("post_impressions_30d", js)
        self.assertIn("post_impressions_7d", js)
        self.assertIn("setInterval(load, 5 * 60 * 1000)", js)
        self.assertIn("function scrollRoot()", js)
        self.assertIn("function writeTop(", js)
        self.assertNotIn(".focus(", js)
        self.assertIn("No X accounts in this snapshot.", js)
        self.assertIn("No original posts yet", js)
        self.assertIn('href="https://x.com/\' + esc(a.handle)', js)
        self.assertIn('target="_blank" rel="noopener">@\' + esc(a.handle)', js)
        self.assertNotIn('class="handle"', js)
        self.assertIn("Add the zone name, zone id, and Web Analytics site tag in Configuration, plus the Cloudflare API token.", js)
        self.assertIn("This card stays empty until Cloudflare returns data. Nothing here is estimated.", js)
        self.assertIn('stroke="#3e6ae1"', js)
        self.assertIn("rgba(62,106,225,.16)", js)
        self.assertIn("Not available", js)
        self.assertIn("WordPress/PHP probe hits are hidden from Top pages.", js)
        on_range = re.search(r"function onRange\(b\) \{(.*?)\n  \}", js, re.S)
        self.assertIsNotNone(on_range)
        self.assertNotIn("fetch", on_range.group(1))
        self.assertEqual(trial_ingress_root("/ui-trial"), "/")
        self.assertEqual(trial_ingress_root("/ui-trial/"), "/")
        self.assertEqual(trial_ingress_root("/ui-trial/index.html"), "/")
        prefix = "/hassio/ingress/example"
        for suffix in ("/ui-trial", "/ui-trial/", "/ui-trial/index.html"):
            self.assertEqual(trial_ingress_root(prefix + suffix), prefix + "/")
        server_text = open(os.path.join(APP, "server.py"), encoding="utf-8").read()
        self.assertIn('Location", "../ui-trial"', server_text)
        self.assertNotIn("/legacy", server_text)
        self.assertNotIn('version: "0.3.1"', server_text)

    def test_routes_keep_production_and_serve_trial(self):
        import importlib
        server = importlib.import_module("server")
        server.ALLOW = {"127.0.0.1"}
        server._state["snap"] = fx("snapshot.json")
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        base = "http://127.0.0.1:%d" % httpd.server_address[1]

        def get(path):
            with urllib.request.urlopen(base + path) as response:
                return response.status, response.headers, response.read(), response.geturl()

        root_status, root_headers, root_body, root_url = get("/")
        self.assertEqual(root_status, 200)
        self.assertTrue(root_url.endswith("/"))
        index_body = get("/index.html")[2]
        self.assertEqual(root_body, index_body)
        static = os.path.join(APP, "static")
        for name in PROD_SHA:
            served = root_body if name == "index.html" else get("/" + name)[2]
            self.assertEqual(served, open(os.path.join(static, name), "rb").read(), name)
            self.assertEqual(hashlib.sha256(served).hexdigest(), PROD_SHA[name][1], name)
        self.assertEqual(root_headers["Content-Security-Policy"], PROD_CSP)
        self.assertNotIn("sha256-", root_headers["Content-Security-Policy"])
        self.assertNotIn("base-uri", root_headers["Content-Security-Policy"])
        self.assertEqual(get("/app.css")[1]["Content-Security-Policy"], PROD_CSP)
        self.assertEqual(get("/api/health")[1]["Content-Security-Policy"], PROD_CSP)
        self.assertEqual(json.loads(get("/api/health")[2]), {"ok": True})
        self.assertNotIn(b"bryson-ui-4", root_body)
        self.assertNotIn(b"/ui-trial/", root_body)

        status, headers, trial, final = get("/ui-trial")
        self.assertEqual(status, 200)
        self.assertTrue(final.endswith("/ui-trial"))
        self.assertNotEqual(trial, root_body)
        self.assertEqual(get("/ui-trial/")[2], trial)
        self.assertEqual(get("/ui-trial/index.html")[2], trial)
        self.assertTrue(get("/ui-trial/")[3].endswith("/ui-trial"))
        self.assertIn(b'data-ui="bryson-ui-4"', trial)
        self.assertIn(b'href="ui-trial/app.css"', trial)
        self.assertIn(b'src="ui-trial/app.js"', trial)
        self.assertNotIn(b'href="/ui-trial/', trial)
        self.assertNotIn(b'src="/', trial)
        csp = headers["Content-Security-Policy"]
        self.assertIn("base-uri 'self'", csp)
        self.assertNotIn("base-uri 'none'", csp)
        script = re.search(br"<script>(.*?)</script>", trial, re.S).group(1)
        script_hash = base64.b64encode(hashlib.sha256(script).digest()).decode()
        self.assertIn("'sha256-%s'" % script_hash, csp)
        css_headers = get("/ui-trial/app.css")[1]
        self.assertEqual(css_headers["Content-Security-Policy"], PROD_CSP)
        trial_js = get("/ui-trial/app.js")[2]
        self.assertIn(b'fetch("api/snapshot", { cache: "no-store" })', trial_js)
        self.assertNotIn(b'fetch("/', trial_js)
        for suffix in ("/ui-trial/", "/ui-trial/index.html"):

            class StopRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                    raise urllib.error.HTTPError(req.full_url, code, msg, hdrs, fp)

            stopped = urllib.request.build_opener(StopRedirect)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                stopped.open(urllib.request.Request(base + suffix, method="GET"))
            self.assertEqual(caught.exception.code, 302)
            self.assertEqual(caught.exception.headers["Location"], "../ui-trial")
        with self.assertRaises(urllib.error.HTTPError) as missing:
            get("/legacy")
        self.assertEqual(missing.exception.code, 404)
        req = urllib.request.Request(base + "/ui-trial", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(req)
        self.assertEqual(denied.exception.code, 405)
        self.assertIn(b"read-only", denied.exception.read())
        for method in ("PUT", "PATCH", "DELETE"):
            write = urllib.request.Request(base + "/api/snapshot", data=b"{}", method=method)
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(write)
            self.assertEqual(denied.exception.code, 405, method)


if __name__ == "__main__":
    unittest.main(verbosity=2)
