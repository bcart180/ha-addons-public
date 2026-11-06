"""Usage dashboard tests.

The bundled snapshot is the real Nov 5, 2026 report. Other JSON files in this
module are fixtures for the reader; they are not usage claims.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "rootfs" / "app"
sys.path.insert(0, str(APP))

import server  # noqa: E402
import snapshot  # noqa: E402

BUNDLED = APP / "bundled" / "usage_snapshot.json"
EXPECTED_BOTS = [
    ("VP", 32, [6, "<1", 26]),
    ("Chief of Staff", 24, [15, 4, 5]),
    ("Security Audit", 14, ["<1", "<1", 14]),
    ("Director, Airbnb", 7, ["<1", "<1", 7]),
    ("Director, Inbox", 7, ["<1", 6, 1]),
    ("Side Hustle CEO", 4, [2, 2, "<1"]),
    ("Desk Lead", 3, ["<1", 1, 2]),
    ("Director, Tesla", 2, [1, "<1", 1]),
    ("Director, Hideaway", 2, ["<1", "<1", 2]),
    ("Director, YouTube & X Research", 1, ["<1", "<1", 1]),
    ("Strategist", 1, ["<1", "<1", 1]),
    ("Market Analyst", 1, ["<1", "<1", 1]),
    ("Execution Trader", 1, ["<1", 1, "<1"]),
    ("Savings", "<1", None),
    ("Grok Bot Efficiency Optimizer", "<1", None),
    ("Other bots", 1, ["<1", "<1", 1]),
]


def fixture() -> dict:
    return {
        "schema_version": 1,
        "source": "Grok Bot weekly report",
        "source_tool": "CheckSubscriptionUsage",
        "plan": "Fixture",
        "snapshot_at": "2026-01-01T00:00:00-05:00",
        "snapshot_label": "Fixture label",
        "timezone": "America/New_York",
        "week": {
            "label": "This week",
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
            "range_label": "Fixture range",
        },
        "weekly_limit": {
            "used_percent": 1,
            "shared_across_apps": True,
            "grok_bot_percentage_points": 1,
            "resets_at": "2026-01-08T00:00:00-05:00",
            "resets_label": "Fixture reset",
            "until_reset_at_snapshot": "1d",
        },
        "extra_usage": {"comes_from": "additional usage credits", "dollar_figures_included": False},
        "activity_overall": {
            "bot_to_bot_percent": 0,
            "chats_percent": 100,
            "routines_percent": 0,
            "approximate": False,
            "note": "Fixture note.",
        },
        "activities": ["Chats", "Routines", "Bot-to-Bot"],
        "bots": [
            {
                "name": "Fixture Bot",
                "share_percent": 100,
                "activity": [
                    {"name": "Chats", "percent": 100},
                    {"name": "Routines", "percent": "<1"},
                    {"name": "Bot-to-Bot", "percent": "<1"},
                ],
            }
        ],
    }


class SnapshotTests(unittest.TestCase):
    def test_bundled_report_is_the_nov_5_snapshot(self):
        raw = BUNDLED.read_text(encoding="utf-8")
        self.assertIn('"used_percent": 37.4', raw)
        self.assertIn('"grok_bot_percentage_points": 32.7', raw)
        self.assertNotIn("$", raw)
        data = snapshot.validate(snapshot.load_json(str(BUNDLED)))
        self.assertEqual(data["snapshot_label"], "Nov 5, 2026, 8:02 PM ET")
        self.assertEqual(data["source"], "Grok Bot weekly report")
        self.assertEqual(data["source_tool"], "CheckSubscriptionUsage")
        self.assertEqual(data["plan"], "Ultra")
        self.assertEqual(data["timezone"], "America/New_York")
        self.assertEqual(data["week"]["range_label"], "Nov 3–Nov 6 UTC")
        self.assertEqual(data["weekly_limit"]["until_reset_at_snapshot"], "4d 18h")
        self.assertEqual(data["weekly_limit"]["resets_label"], "Tue, Nov 10, 2026 · 2:44 PM ET")
        self.assertEqual(data["weekly_limit"]["resets_at"], "2026-11-10T14:44:00-05:00")
        self.assertTrue(data["weekly_limit"]["shared_across_apps"])
        self.assertFalse(data["extra_usage"]["dollar_figures_included"])
        self.assertEqual(data["extra_usage"]["comes_from"], "additional usage credits")
        self.assertEqual(
            [data["activity_overall"][key] for key in ("bot_to_bot_percent", "chats_percent", "routines_percent")],
            [60, 26, 14],
        )
        self.assertTrue(data["activity_overall"]["approximate"])
        pairs = []
        for bot in data["bots"]:
            activity = None if bot["activity"] is None else [item["percent"] for item in bot["activity"]]
            pairs.append((bot["name"], bot["share_percent"], activity))
        self.assertEqual(pairs, EXPECTED_BOTS)

    def test_view_wording(self):
        data = snapshot.validate(snapshot.load_json(str(BUNDLED)))
        view = snapshot.build_view(data, "bundled")
        self.assertEqual(
            view["provenance_line"],
            "Bundled snapshot · Nov 5, 2026, 8:02 PM ET · Grok Bot weekly report",
        )
        self.assertEqual(
            [metric["value"] for metric in view["metrics"]],
            ["37.4%", "4d 18h", "32%", "≈60%"],
        )
        self.assertEqual(
            view["metrics"][0]["detail"],
            "Shared across apps on the plan · Grok Bot: 32.7 percentage points",
        )
        self.assertEqual(
            view["metrics"][1]["detail"],
            "Tue, Nov 10, 2026 · 2:44 PM ET · at snapshot",
        )
        self.assertEqual(view["metrics"][2]["detail"], "VP · share of this week's Grok Bot usage")
        self.assertIn("Bot rows are independently rounded.", view["metrics"][3]["detail"])
        self.assertIn("Chats ≈26%", view["metrics"][3]["detail"])
        self.assertIn("Routines ≈14%", view["metrics"][3]["detail"])
        self.assertEqual(
            [(part["label"], part["percent"]) for part in view["donut"]["parts"]],
            [("VP", 32), ("Chief of Staff", 24), ("Security Audit", 14), ("All remaining bots", 30)],
        )
        self.assertEqual(view["donut"]["center"], "100%")
        self.assertEqual(view["whole_share_total"], 100)
        chats = routines = bot_to_bot = 0
        for bot in view["bots"]:
            for segment in bot["segments"]:
                if not segment["percent"]:
                    continue
                if segment["name"] == "Chats":
                    chats += segment["percent"]
                elif segment["name"] == "Routines":
                    routines += segment["percent"]
                else:
                    bot_to_bot += segment["percent"]
        self.assertEqual([chats, routines, bot_to_bot], [24, 14, 62])
        self.assertNotEqual(chats, 26)
        self.assertNotEqual(bot_to_bot, 60)
        below = [line["text"] for line in view["below"]]
        self.assertNotIn("Chief of Staff", "\n".join(below))
        self.assertIn("VP — Routines: <1%.", below)
        self.assertIn("Security Audit — Chats, Routines: <1% each.", below)
        self.assertIn(
            "Savings — share of Grok Bot usage: <1%. No activity split was in the snapshot.",
            below,
        )
        self.assertIn(
            "Grok Bot Efficiency Optimizer — share of Grok Bot usage: <1%. No activity split was in the snapshot.",
            below,
        )
        blob = json.dumps(view)
        self.assertEqual(blob.lower().count("live"), 1)
        self.assertIn("not a live meter", blob)
        self.assertNotIn("refresh", blob.lower())
        self.assertNotIn("$", blob)
        data_view = snapshot.build_view(data, "data")
        self.assertEqual(
            data_view["provenance_line"],
            "Snapshot Nov 5, 2026, 8:02 PM ET · Grok Bot weekly report",
        )

    def test_zero_and_dollars_are_rejected(self):
        data = snapshot.load_json(str(BUNDLED))
        broken = json.loads(json.dumps(data))
        broken["bots"][0]["activity"][1]["percent"] = 0
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.validate(broken)
        broken = json.loads(json.dumps(data))
        broken["extra_usage"]["dollar_figures_included"] = True
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.validate(broken)
        self.assertEqual(snapshot.format_number(37.4), "37.4")
        self.assertEqual(snapshot.format_number(32.7), "32.7")

    def test_version_matches_config(self):
        config = (ROOT / "config.yaml").read_text(encoding="utf-8")
        self.assertIn(f'version: "{snapshot.ADDON_VERSION}"', config)
        self.assertIn("slug: usage_dashboard", config)
        self.assertIn("ingress: true", config)
        self.assertIn("ingress_port: 8098", config)
        self.assertIn('panel_title: Usage', config)
        self.assertIn("panel_icon: mdi:chart-donut", config)
        self.assertIn("homeassistant_api: false", config)
        self.assertNotIn("ports:", config)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.outside = os.path.join(self.tmp, "outside")
        os.makedirs(self.data)
        os.makedirs(self.outside)
        self.options = os.path.join(self.tmp, "options.json")
        self.snapshot_path = os.path.join(self.data, "usage_snapshot.json")
        with open(self.options, "w", encoding="utf-8") as handle:
            json.dump({"snapshot_path": self.snapshot_path, "log_level": "warning"}, handle)
        self.httpd = server.make_httpd(
            server.Config(
                host="127.0.0.1",
                port=0,
                allow={"127.0.0.1"},
                options_path=self.options,
                bundled_path=str(BUNDLED),
                static_dir=str(APP / "static"),
                data_root=self.data,
            )
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()
        shutil.rmtree(self.tmp)

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as response:
            return response.status, response.headers, response.read()

    def test_missing_file_is_labeled_bundled(self):
        status, headers, body = self.get("/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["warning"])
        self.assertEqual(payload["view"]["provenance"], "bundled")
        self.assertEqual(payload["view"]["metrics"][0]["value"], "37.4%")
        page = self.get("/")[2].decode()
        self.assertIn('href="app.css"', page)
        self.assertIn('src="app.js"', page)
        self.assertNotIn('src="/', page)

    def test_missing_static_and_traversal_are_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/bundled/usage_snapshot.json")
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/../server.py")
        self.assertEqual(caught.exception.code, 404)

    def test_data_file_is_reread(self):
        document = fixture()
        with open(self.snapshot_path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        payload = json.loads(self.get("/api/snapshot")[2])
        self.assertEqual(payload["view"]["provenance"], "data")
        self.assertEqual(payload["view"]["provenance_line"], "Snapshot Fixture label · Grok Bot weekly report")
        document["snapshot_label"] = "Fixture label two"
        with open(self.snapshot_path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        again = json.loads(self.get("/api/snapshot")[2])
        self.assertEqual(again["view"]["provenance_line"], "Snapshot Fixture label two · Grok Bot weekly report")
        self.assertNotIn("37.4%", json.dumps(again["view"]["metrics"]))

    def test_invalid_and_outside_files_fall_back(self):
        with open(self.snapshot_path, "w", encoding="utf-8") as handle:
            handle.write("{")
        payload = json.loads(self.get("/api/snapshot")[2])
        self.assertEqual(payload["view"]["provenance"], "bundled")
        self.assertIn("not valid JSON", payload["warning"])
        self.assertEqual(payload["view"]["metrics"][0]["value"], "37.4%")

        outside = os.path.join(self.outside, "usage_snapshot.json")
        with open(outside, "w", encoding="utf-8") as handle:
            json.dump(fixture(), handle)
        with open(self.options, "w", encoding="utf-8") as handle:
            json.dump({"snapshot_path": outside, "log_level": "warning"}, handle)
        payload = json.loads(self.get("/api/snapshot")[2])
        self.assertEqual(payload["view"]["provenance"], "bundled")
        self.assertIn("data directory", payload["warning"])
        self.assertNotIn("Fixture label", payload["view"]["provenance_line"])
        self.assertNotIn("root:", json.dumps(payload))

    def test_post_is_rejected_and_foreign_ip_is_forbidden(self):
        request = urllib.request.Request(self.base + "/api/snapshot", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 405)
        blocked = server.make_httpd(
            server.Config(
                host="127.0.0.1",
                port=0,
                allow={"172.30.32.2"},
                options_path=self.options,
                bundled_path=str(BUNDLED),
                static_dir=str(APP / "static"),
                data_root=self.data,
            )
        )
        thread = threading.Thread(target=blocked.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{blocked.server_address[1]}/"
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(url)
            self.assertEqual(caught.exception.code, 403)
        finally:
            blocked.shutdown()
            thread.join(timeout=5)
            blocked.server_close()

    def test_health(self):
        self.assertEqual(json.loads(self.get("/api/health")[2]), {"ok": True})


class StaticTests(unittest.TestCase):
    def test_page_does_not_embed_the_report(self):
        for name in ("index.html", "app.js", "app.css"):
            text = (APP / "static" / name).read_text(encoding="utf-8")
            self.assertNotIn("37.4", text)
            self.assertNotIn("32.7", text)
            self.assertNotIn("setInterval", text)
        css = (APP / "static" / "app.css").read_text(encoding="utf-8").lower()
        self.assertNotIn("box-shadow", css)
        self.assertNotIn("gradient", css)
        docs = (ROOT / "DOCS.md").read_text(encoding="utf-8")
        self.assertIn("CheckSubscriptionUsage", docs)
        self.assertIn("/data/usage_snapshot.json", docs)
        self.assertIn("a1d3bd4b_usage_dashboard", docs)
        self.assertIn("does not count", docs.lower())


if __name__ == "__main__":
    unittest.main()
