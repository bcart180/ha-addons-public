"""Run: python3 sports_news/tests/test_sports_news.py  (stdlib only, no network)."""

import datetime as dt
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.dirname(HERE)
APP = os.path.join(ADDON, "rootfs", "app")
PREVIEW = os.path.join(APP, "preview", "starter_2026-11-08T095038Z.json")
import sys
sys.path.insert(0, APP)
import server  # noqa: E402
import validate as v  # noqa: E402

NOW = dt.datetime(2026, 11, 8, 9, 52, tzinfo=dt.timezone.utc)
REPO = os.path.dirname(ADDON)


def starter():
    with open(PREVIEW, encoding="utf-8") as handle:
        return json.load(handle)


def raw_of(doc):
    return json.dumps(doc).encode("utf-8")


def parsed(doc, now=NOW):
    return v.load_bytes(raw_of(doc), now)


class ContractTests(unittest.TestCase):
    def test_starter_snapshot_is_labeled_and_not_a_live_feed(self):
        doc = parsed(starter())
        view = v.view(doc, NOW, origin="bundled_starter")
        self.assertEqual(len(view["items"]), 10)
        self.assertEqual([row["count"] for row in view["teams"]], [4, 3, 3])
        self.assertEqual(view["collected_at_et"], "Nov 8, 2026, 4:50 AM ET")
        self.assertEqual(view["newest_published_at_et"], "Nov 7, 2026, 9:32 PM ET")
        self.assertFalse(view["live_feed"])
        self.assertFalse(view["scheduled"])
        self.assertEqual(view["poll_seconds"], 60)
        self.assertEqual(view["freshness"], "fresh")
        self.assertFalse(view["stale"])
        blob = "\n".join(view["messages"])
        self.assertIn("Starter snapshot collected Nov 8, 2026, 4:50 AM ET", blob)
        self.assertIn("Not a live feed", blob)
        self.assertIn("not a new collection", blob)
        self.assertIn("every 30 minutes from 7:00 AM to 11:00 PM ET", blob)
        self.assertIn("hourly from 11:00 PM to 7:00 AM ET", blob)
        self.assertNotIn("23642374", json.dumps(view))
        self.assertNotIn("evidence_files", json.dumps(view))
        self.assertNotIn("author_id", json.dumps(view))
        self.assertTrue(view["items"][0]["url"].startswith("https://x.com/"))

    def test_future_post_rumor_handle_and_bad_urls_are_rejected(self):
        future = starter()
        future["items"][0]["published_at"] = "2026-11-08T12:00:00Z"
        with self.assertRaises(v.ValidationError):
            parsed(future)
        rumor = starter()
        rumor["items"][0]["source_provenance"] = "rumor"
        with self.assertRaisesRegex(v.ValidationError, "source_provenance"):
            parsed(rumor)
        blocked = starter()
        blocked["items"][0]["author_handle"] = "bcondotta"
        with self.assertRaisesRegex(v.ValidationError, "not allowlisted"):
            parsed(blocked)
        for bad in (
            "http://x.com/Seahawks/status/2119212325949616257",
            "javascript:alert(1)",
            "https://x.com/Seahawks/status/2119212325949616257?track=1",
            "https://evil.example/Seahawks/status/2119212325949616257",
            "https://x.com/NavyFB/status/2119212325949616257",
            "https://www.x.com/Seahawks/status/2119212325949616257",
        ):
            broken = starter()
            broken["items"][0]["canonical_post_url"] = bad
            with self.assertRaises(v.ValidationError):
                parsed(broken)

    def test_pinned_id_html_and_size_limits(self):
        wrong = starter()
        wrong["items"][0]["author_id"] = "1"
        with self.assertRaisesRegex(v.ValidationError, "author_id"):
            parsed(wrong)
        markup = starter()
        markup["items"][0]["summary"] = "<script>nope</script>"
        with self.assertRaises(v.ValidationError):
            parsed(markup)
        huge = raw_of(starter()) + b" " * (v.MAX_BYTES + 1)
        with self.assertRaisesRegex(v.ValidationError, "262144"):
            v.load_bytes(huge, NOW)
        dup = starter()
        dup["items"][1]["id"] = dup["items"][0]["id"]
        dup["items"][1]["canonical_post_url"] = dup["items"][0]["canonical_post_url"]
        dup["items"][1]["author_handle"] = dup["items"][0]["author_handle"]
        dup["items"][1]["author_id"] = dup["items"][0]["author_id"]
        dup["items"][1]["team"] = dup["items"][0]["team"]
        dup["items"][1]["sport"] = dup["items"][0]["sport"]
        dup["items"][1]["source_provenance"] = dup["items"][0]["source_provenance"]
        with self.assertRaisesRegex(v.ValidationError, "duplicate"):
            parsed(dup)

    def test_pinned_profile_ids_reject_a_mismatch(self):
        pins = (("ZagMBB", "Gonzaga Bulldogs", "basketball", "official", "602989093"),
                ("BWagner_CapGaz", "Navy Midshipmen", "football", "reporter", "872205181"))
        for handle, team, sport, role, author_id in pins:
            doc = starter()
            item = json.loads(json.dumps(doc["items"][0]))
            item.update({
                "id": "2119000000000000001",
                "team": team,
                "sport": sport,
                "canonical_post_url": "https://x.com/" + handle + "/status/2119000000000000001",
                "author_handle": handle,
                "author_name": handle,
                "author_id": author_id,
                "source_provenance": role,
                "summary": "Allowlist id check.",
                "source_text": "Allowlist id check.",
            })
            doc["items"] = [item]
            cleaned = parsed(doc)
            self.assertEqual(cleaned["items"][0]["author_id"], author_id)
            item["author_id"] = "999001"
            with self.assertRaisesRegex(v.ValidationError, "author_id"):
                parsed(doc)

    def test_one_team_error_keeps_the_other_posts(self):
        doc = starter()
        doc["status"] = "partial"
        doc["teams"] = {"Gonzaga Bulldogs": {"status": "error", "detail": "public read failed"}}
        view = v.view(parsed(doc), NOW, origin="published")
        gonzaga = [row for row in view["teams"] if row["team"] == "Gonzaga Bulldogs"][0]
        self.assertEqual(gonzaga["status"], "error")
        self.assertEqual(gonzaga["count"], 3)
        self.assertEqual(sum(row["count"] for row in view["teams"]), 10)

    def test_stale_uses_two_cycles_and_older_news_is_separate(self):
        doc = parsed(starter())
        overnight = v.view(doc, NOW + dt.timedelta(minutes=90), origin="published")
        self.assertFalse(overnight["stale"])
        later = v.view(doc, NOW + dt.timedelta(minutes=122), origin="published")
        self.assertTrue(later["stale"])
        self.assertIn("120 minutes", "\n".join(later["messages"]))
        noon = dt.datetime(2026, 11, 8, 16, 0, tzinfo=dt.timezone.utc)
        day_doc = starter()
        day_doc["collected_at"] = "2026-11-08T16:00:00Z"
        for item in day_doc["items"]:
            item["collected_at"] = day_doc["collected_at"]
            item["published_at"] = "2026-11-08T15:00:00Z"
        fresh = v.view(parsed(day_doc, noon + dt.timedelta(minutes=50)), noon + dt.timedelta(minutes=50), origin="published")
        self.assertFalse(fresh["stale"])
        stale = v.view(parsed(day_doc, noon + dt.timedelta(minutes=61)), noon + dt.timedelta(minutes=61), origin="published")
        self.assertTrue(stale["stale"])
        self.assertIn("60 minutes", "\n".join(stale["messages"]))
        aged = v.view(doc, dt.datetime(2026, 11, 9, 22, 30, tzinfo=dt.timezone.utc), origin="published")
        older = [item for item in aged["items"] if item["older"]]
        recent = [item for item in aged["items"] if not item["older"]]
        self.assertTrue(older)
        self.assertTrue(recent)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "sports_news")
        self.data = os.path.join(self.tmp.name, "data")
        os.makedirs(self.root)
        self.inbox = os.path.join(self.root, "inbox.json")
        self.service = server.Service(PREVIEW, self.data, [self.inbox], [self.root])

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_inbox_serves_labeled_starter(self):
        view = self.service.page(NOW)
        self.assertEqual(view["feed_kind"], "bundled_starter")
        self.assertIn("Not a live feed", "\n".join(view["messages"]))
        self.assertFalse(os.path.exists(os.path.join(self.data, "last_good.json")))

    def test_invalid_inbox_keeps_last_good_timestamps(self):
        with open(self.inbox, "wb") as handle:
            handle.write(open(PREVIEW, "rb").read())
        first = self.service.page(NOW)
        self.assertEqual(first["feed_kind"], "published")
        with open(self.inbox, "wb") as handle:
            handle.write(b"{")
        second = self.service.page(NOW + dt.timedelta(minutes=5))
        self.assertEqual(second["feed_kind"], "last_good")
        self.assertEqual(second["collected_at"], first["collected_at"])
        self.assertEqual(len(second["items"]), 10)
        self.assertIn("Collection time was not reset", "\n".join(second["messages"]))
        self.assertNotEqual(second["page_checked_at"], second["collected_at"])

    def test_successful_empty_is_not_an_error(self):
        doc = starter()
        doc["status"] = "empty"
        doc["items"] = []
        doc["warnings"] = ["No posts in the searched window."]
        with open(self.inbox, "wb") as handle:
            handle.write(raw_of(doc))
        view = self.service.page(NOW)
        self.assertEqual(view["freshness"], "empty")
        self.assertEqual(view["items"], [])
        self.assertIn("returned no posts", "\n".join(view["messages"]))

    def test_source_error_without_posts_keeps_last_good(self):
        with open(self.inbox, "wb") as handle:
            handle.write(open(PREVIEW, "rb").read())
        self.service.page(NOW)
        failed = starter()
        failed["status"] = "error"
        failed["items"] = []
        failed["source_error"] = "public read failed"
        failed["warnings"] = []
        failed["last_attempt_at"] = "2026-11-08T10:20:00Z"
        with open(self.inbox, "wb") as handle:
            handle.write(raw_of(failed))
        view = self.service.page(NOW + dt.timedelta(minutes=30))
        self.assertEqual(view["freshness"], "error")
        self.assertEqual(len(view["items"]), 10)
        self.assertEqual(view["source_error"], "public read failed")
        self.assertEqual(view["collected_at_et"], "Nov 8, 2026, 4:50 AM ET")
        self.assertIn("10:20", view["last_attempt_at"])

    def test_symlink_outside_the_publish_root_is_not_read(self):
        outside = os.path.join(self.tmp.name, "outside.json")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write('{"schema_version": 1}')
        os.symlink(outside, self.inbox)
        view = self.service.page(NOW)
        self.assertEqual(view["feed_kind"], "bundled_starter")
        self.assertIn("outside the allowed publish directories", "\n".join(view["messages"]))
        self.assertNotIn("schema_version", json.dumps(view["items"]))

    def test_symlinked_allowed_root_is_not_accepted(self):
        real = os.path.join(self.tmp.name, "real_root")
        os.makedirs(real)
        with open(os.path.join(real, "inbox.json"), "wb") as handle:
            handle.write(open(PREVIEW, "rb").read())
        linked = os.path.join(self.tmp.name, "linked_root")
        os.symlink(real, linked)
        service = server.Service(PREVIEW, self.data, [os.path.join(linked, "inbox.json")], [linked])
        view = service.page(NOW)
        self.assertEqual(view["feed_kind"], "bundled_starter")
        self.assertIn("outside the allowed publish directories", "\n".join(view["messages"]))
        self.assertFalse(os.path.exists(os.path.join(self.data, "last_good.json")))

    def test_missing_inbox_keeps_last_good_after_restart(self):
        with open(self.inbox, "wb") as handle:
            handle.write(open(PREVIEW, "rb").read())
        first = self.service.page(NOW)
        os.remove(self.inbox)
        second = self.service.page(NOW + dt.timedelta(minutes=3))
        self.assertEqual(second["feed_kind"], "last_good")
        self.assertEqual(second["collected_at"], first["collected_at"])
        self.assertEqual(len(second["items"]), 10)
        self.assertIn("Inbox file is missing", "\n".join(second["messages"]))
        self.assertNotIn("Cards are the starter snapshot", "\n".join(second["messages"]))
        restarted = server.Service(PREVIEW, self.data, [self.inbox], [self.root])
        third = restarted.page(NOW + dt.timedelta(minutes=4))
        self.assertEqual(third["feed_kind"], "last_good")
        self.assertEqual(len(third["items"]), 10)

    def test_older_snapshot_does_not_replace_last_good(self):
        with open(self.inbox, "wb") as handle:
            handle.write(open(PREVIEW, "rb").read())
        self.service.page(NOW)
        older = starter()
        older["collected_at"] = "2026-11-08T08:00:00Z"
        older.pop("query_end_time", None)
        for item in older["items"]:
            item["collected_at"] = older["collected_at"]
            item["published_at"] = "2026-11-08T07:00:00Z"
        with open(self.inbox, "wb") as handle:
            handle.write(raw_of(older))
        view = self.service.page(NOW)
        self.assertEqual(view["feed_kind"], "last_good")
        self.assertEqual(view["collected_at_et"], "Nov 8, 2026, 4:50 AM ET")
        self.assertIn("older snapshot rejected", "\n".join(view["messages"]))
        self.assertEqual(len(view["items"]), 10)

    def test_partial_team_failure_keeps_that_teams_posts_and_success_time(self):
        with open(self.inbox, "wb") as handle:
            handle.write(open(PREVIEW, "rb").read())
        first = self.service.page(NOW)
        gonzaga_ids = [item["id"] for item in first["items"] if item["team"] == "Gonzaga Bulldogs"]
        nxt = starter()
        nxt["collected_at"] = "2026-11-08T16:30:00Z"
        nxt["status"] = "partial"
        nxt["teams"] = {"Gonzaga Bulldogs": {"status": "error", "detail": "public read failed"}}
        nxt["items"] = [item for item in nxt["items"] if item["team"] != "Gonzaga Bulldogs"]
        for item in nxt["items"]:
            item["collected_at"] = nxt["collected_at"]
        with open(self.inbox, "wb") as handle:
            handle.write(raw_of(nxt))
        view = self.service.page(dt.datetime(2026, 11, 8, 16, 32, tzinfo=dt.timezone.utc))
        self.assertEqual(view["freshness"], "error")
        self.assertNotIn("inside two proposed cycles", "\n".join(view["messages"]))
        self.assertIn("success times were not reset", "\n".join(view["messages"]))
        kept = [item["id"] for item in view["items"] if item["team"] == "Gonzaga Bulldogs"]
        self.assertEqual(kept, gonzaga_ids)
        gonzaga = [row for row in view["teams"] if row["team"] == "Gonzaga Bulldogs"][0]
        seahawks = [row for row in view["teams"] if row["team"] == "Seattle Seahawks"][0]
        self.assertEqual(gonzaga["status"], "error")
        self.assertEqual(gonzaga["last_success_at_et"], "Nov 8, 2026, 4:50 AM ET")
        self.assertEqual(seahawks["last_success_at_et"], "Nov 8, 2026, 11:30 AM ET")
        self.assertEqual(seahawks["count"], 4)

    def test_unchanged_snapshot_is_not_rewritten(self):
        with open(self.inbox, "wb") as handle:
            handle.write(open(PREVIEW, "rb").read())
        self.service.page(NOW)
        path = os.path.join(self.data, "last_good.json")
        first_mtime = os.stat(path).st_mtime_ns
        self.service.page(NOW + dt.timedelta(seconds=30))
        self.assertEqual(os.stat(path).st_mtime_ns, first_mtime)

    def test_publish_option_is_refused(self):
        with self.assertRaisesRegex(v.ValidationError, "not enabled"):
            server.inbox_list({"inbox_file": "/tmp/sports.json"})
        self.assertEqual(server.inbox_list({}), [])


class HttpTests(unittest.TestCase):
    def serve(self, allow):
        service = server.Service(PREVIEW, tempfile.mkdtemp(), [], ())
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(service, set(allow), server.STATIC_DIR))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return "http://127.0.0.1:%d" % httpd.server_address[1]

    def test_ingress_allowlist_methods_and_headers(self):
        blocked = self.serve({"172.30.32.2"})
        for path, method in (("/api/snapshot", "GET"), ("/", "GET"), ("/api/snapshot", "POST")):
            req = urllib.request.Request(blocked + path, method=method)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req)
            self.assertEqual(caught.exception.code, 403)
        base = self.serve({"127.0.0.1"})
        health = json.loads(urllib.request.urlopen(base + "/api/health").read())
        self.assertEqual(health, {"ok": True})
        response = urllib.request.urlopen(base + "/api/snapshot")
        body = json.loads(response.read())
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", response.headers["Content-Security-Policy"])
        self.assertFalse(body["live_feed"])
        self.assertIn("Not a live feed", "\n".join(body["messages"]))
        self.assertNotIn("23642374", json.dumps(body))
        page = urllib.request.urlopen(base + "/").read().decode("utf-8")
        script = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()
        self.assertIn("noopener noreferrer", script)
        self.assertIn("no-referrer", script)
        self.assertNotIn("<iframe", page.lower())
        denied = urllib.request.Request(base + "/api/snapshot", method="POST", data=b"{}")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(denied)
        self.assertEqual(caught.exception.code, 405)


class PageTests(unittest.TestCase):
    def test_static_page_is_readable_spectrum_and_keyboard_ready(self):
        css = open(os.path.join(APP, "static", "app.css"), encoding="utf-8").read()
        js = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()
        html = open(os.path.join(APP, "static", "index.html"), encoding="utf-8").read()
        for color in ("#000000", "#d7b4ff", "#ff9e4a", "#ffe566", "#ff7a7a", "#ffb3d9", "#7dffb3", "#7ee0ff"):
            self.assertIn(color, css)
        self.assertIn("min-height: 44px", css)
        self.assertIn(":focus-visible", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertIn("overflow-x: clip", css)
        self.assertIn("max-width: 720px", css)
        self.assertIn("max-width: 600px", css)
        self.assertIn("Skip to posts", html)
        self.assertIn('type="button"', html)
        self.assertIn("60000", js)
        self.assertIn("textContent", js)
        self.assertIn("ArrowRight", js)
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("iframe", js + html)
        cfg = open(os.path.join(ADDON, "config.yaml"), encoding="utf-8").read()
        self.assertIn('version: "0.1.1"', cfg)
        self.assertNotRegex(cfg, r"(?m)^ports:")
        self.assertIn("homeassistant_api: false", cfg)
        self.assertNotIn("homeassistant_config:", cfg)
        self.assertNotIn("share:", cfg)
        self.assertNotRegex(cfg, r"(?m)^map:")
        self.assertIn("ingress: true", cfg)
        self.assertIn("Last success", js)
        self.assertIn("panel_admin: true", cfg)

    def test_trading_desk_charts_and_canvas_are_unchanged(self):
        desk = os.path.join(REPO, "trading_desk")
        style = open(os.path.join(desk, "rootfs", "app", "static", "style.css"), encoding="utf-8").read()
        main = open(os.path.join(desk, "rootfs", "app", "static", "main.js"), encoding="utf-8").read()
        cfg = open(os.path.join(desk, "config.yaml"), encoding="utf-8").read()
        self.assertIn("--bg:#0b0d10", style)
        self.assertIn("#c9892b", main)
        self.assertIn('version: "0.3.2"', cfg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
