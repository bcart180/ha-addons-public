"""Usage dashboard tests.

The bundled snapshot is the real Nov 5, 2026 report. Other JSON files in this
module are fixtures for the reader; they are not usage claims.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
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
        self.assertEqual(snapshot.ADDON_VERSION, "0.1.1")
        self.assertIn(f'version: "{snapshot.ADDON_VERSION}"', config)
        self.assertIn("slug: usage_dashboard", config)
        self.assertIn("ingress: true", config)
        self.assertIn("ingress_port: 8098", config)
        self.assertIn('panel_title: Usage', config)
        self.assertIn("panel_icon: mdi:chart-donut", config)
        self.assertIn("panel_admin: true", config)
        self.assertIn("homeassistant_api: false", config)
        self.assertNotIn("ports:", config)
        self.assertIn("## 0.1.1", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertIn("/ui-trial", (ROOT / "README.md").read_text(encoding="utf-8"))
        data = snapshot.validate(snapshot.load_json(str(BUNDLED)))
        view = snapshot.build_view(data, "bundled")
        self.assertIn("Add-on 0.1.1 · schema version 1.", view["context"])


def trial_ingress_root(pathname: str) -> str:
    """Mirror the trial head script: strip the trial document, then keep a trailing slash."""
    text = (APP / "static" / "ui-trial" / "index.html").read_text(encoding="utf-8")
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

    def test_root_stays_010_and_trial_serves_ui4(self):
        root_body = self.get("/")[2]
        index_body = self.get("/index.html")[2]
        css_body = self.get("/app.css")[2]
        js_body = self.get("/app.js")[2]
        self.assertEqual(root_body, index_body)
        self.assertEqual(len(root_body), 2192)
        self.assertEqual(hashlib.sha256(root_body).hexdigest(), "dc92887525dff53769f3d4518f955c8de30568c74ddf36e411924fa62a755786")
        self.assertEqual(len(css_body), 6286)
        self.assertEqual(hashlib.sha256(css_body).hexdigest(), "c7decfc47eed68c746fb9d2fea87e1161c4a7ddb8c654a57a1186eacee78e5f8")
        self.assertEqual(len(js_body), 16672)
        self.assertEqual(hashlib.sha256(js_body).hexdigest(), "bef447f8c4872abd83994804b98a1178c74b7bcdde00e9b9348de51e52aab0f5")
        self.assertIn(b'href="app.css"', root_body)
        self.assertIn(b'src="app.js"', root_body)
        self.assertNotIn(b"bryson-ui-4", root_body)
        self.assertNotIn(b"/ui-trial/", root_body)
        self.assertNotIn(b"/legacy", root_body)

        status, headers, trial = self.get("/ui-trial")
        self.assertEqual(status, 200)
        self.assertEqual(self.get("/ui-trial/")[2], trial)
        self.assertEqual(self.get("/ui-trial/index.html")[2], trial)
        for suffix in ("/ui-trial/", "/ui-trial/index.html"):

            class StopRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                    raise urllib.error.HTTPError(req.full_url, code, msg, hdrs, fp)

            stopped = urllib.request.build_opener(StopRedirect)
            request = urllib.request.Request(self.base + suffix, method="GET")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                stopped.open(request)
            self.assertEqual(caught.exception.code, 302)
            self.assertEqual(caught.exception.headers["Location"], "../ui-trial")
        self.assertNotEqual(trial, root_body)
        self.assertIn(b'data-ui="bryson-ui-4"', trial)
        self.assertIn(b'href="ui-trial/app.css"', trial)
        self.assertIn(b'src="ui-trial/app.js"', trial)
        self.assertNotIn(b'href="/ui-trial/', trial)
        self.assertNotIn(b'src="/ui-trial/', trial)
        self.assertIn(b'id="reread"', trial)
        self.assertIn(b'id="focus"', trial)
        self.assertIn(b'id="kicker"', trial)
        self.assertIn(b'id="provenance"', trial)
        self.assertNotIn(b"/legacy", trial)
        self.assertEqual(trial_ingress_root("/ui-trial"), "/")
        self.assertEqual(trial_ingress_root("/ui-trial/"), "/")
        self.assertEqual(trial_ingress_root("/ui-trial/index.html"), "/")
        prefix = "/hassio/ingress/example"
        for suffix in ("/ui-trial", "/ui-trial/", "/ui-trial/index.html"):
            self.assertEqual(trial_ingress_root(prefix + suffix), prefix + "/")
        csp = headers["Content-Security-Policy"]
        self.assertIn("base-uri 'self'", csp)
        self.assertNotIn("base-uri 'none'", csp)
        script = re.search(br"<script>(.*?)</script>", trial, re.S).group(1)
        script_hash = base64.b64encode(hashlib.sha256(script).digest()).decode()
        self.assertIn(f"'sha256-{script_hash}'", csp)
        trial_js = self.get("/ui-trial/app.js")[2]
        self.assertIn(b"return name;", trial_js)
        self.assertNotIn(b'return "/" + name', trial_js)
        self.assertNotIn(b'fetch("/', trial_js)
        self.assertIn(b'cache: "no-store"', trial_js)
        self.assertIn(b'endpoint("api/snapshot")', trial_js)
        self.assertNotIn(b"setInterval", trial_js)
        trial_css = self.get("/ui-trial/app.css")[2]
        self.assertNotIn(b"#d9c7a2", trial_css.lower())
        self.assertIn(b"#d9c7a2", css_body)

        request = urllib.request.Request(self.base + "/ui-trial", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 405)
        self.assertIn(b"read-only", caught.exception.read())


def _hue_sat(hex_color: str) -> tuple[float | None, float]:
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
        bundled = (APP / "bundled" / "usage_snapshot.json").read_bytes()
        self.assertEqual(len(bundled), 4537)
        self.assertEqual(hashlib.sha256(bundled).hexdigest(), "0283c3dd60c5183c870e6b60e91b6515611a9c823640585dae25b0173b784bff")

    def test_trial_shell_has_no_warm_hues_or_usage_api(self):
        trial = APP / "static" / "ui-trial"
        banned = ("#d9c7a2", "#f59e0b", "#ff9f1a", "#ffe14a", "orange", "amber")
        for name in ("index.html", "app.css", "app.js"):
            text = (trial / name).read_text(encoding="utf-8")
            lowered = text.lower()
            for token in banned:
                self.assertNotIn(token, lowered)
            scanned = text.replace("http://www.w3.org/2000/svg", "")
            for token in ("http://", "https://", "ws://", "wss://", "setInterval", "WebSocket"):
                self.assertNotIn(token, scanned)
            for number in ("37.4", "32.7", "8:02"):
                self.assertNotIn(number, text)
            for match in re.finditer(r"#[0-9a-f]{6}\b", lowered):
                hue, sat = _hue_sat(match.group(0))
                if hue is not None and 18 <= hue <= 62 and sat >= 0.18:
                    self.fail(f"{name} warm hue {match.group(0)} h={hue:.1f} s={sat:.2f}")
        css = (trial / "app.css").read_text(encoding="utf-8")
        self.assertIn("min-height: 48px", css)
        self.assertIn("#005aff", css)
        self.assertIn("#101010", css)
        self.assertIn("#414141", css)
        server_text = (APP / "server.py").read_text(encoding="utf-8")
        self.assertIn('files["/ui-trial"]', server_text)
        self.assertNotIn("/legacy", server_text)


if __name__ == "__main__":
    unittest.main()
