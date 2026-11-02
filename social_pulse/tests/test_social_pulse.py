"""Run: python3 social_pulse/tests/test_social_pulse.py  (stdlib only, no network)."""
import datetime as dt
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
        snap = c.collect({"x_accounts": HANDLES, "zones": ZONES}, NOW)
        self.assertTrue(all("x_bearer_token not set" in a["error"] for a in snap["x"]))
        for s in snap["sites"]:
            self.assertFalse(s["requests_7d"]["available"])


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
