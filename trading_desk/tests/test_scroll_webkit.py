"""Detail scroll stability in Playwright WebKit, mobile 390x844.

Placeholder preview only. S9 (a real iPhone) is not run here.
Run: python3 tests/test_scroll_webkit.py
"""
import pathlib
import re
import threading

from playwright.sync_api import sync_playwright

import preview_ui_trial as preview

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = ROOT / "rootfs" / "app" / "static"
TRIAL = STATIC / "ui-trial"
PHONE = {"width": 390, "height": 844}


def _server():
    httpd = preview.ThreadingHTTPServer(("127.0.0.1", 0), preview.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def test_static_guards():
    css = (TRIAL / "trial.css").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    enh = (TRIAL / "enhance.js").read_text(encoding="utf-8")
    html = (TRIAL / "index.html").read_text(encoding="utf-8")
    assert "table.stack" in css and ":has(td[data-l])" not in css
    assert 'class="stack"' in html
    assert 'data-l="' in app
    assert "setAttribute" not in enh
    assert "min-height: 220px" in css and "min-height: 240px" in css
    first_party = [STATIC / "app.js", STATIC / "assets.js", TRIAL / "main.js", TRIAL / "bubbles.js",
                   TRIAL / "enhance.js", TRIAL / "trial.css", TRIAL / "index.html"]
    for path in first_party:
        text = path.read_text(encoding="utf-8")
        for needle in ("scrollIntoView", "scroll-snap", ".focus(", "scrollTop ="):
            assert needle not in text, (path.name, needle)
        if "scrollTo(" in text:
            assert "restoreScrollY" in text, path.name
        assert not re.search(r"https?://", text), path.name
        assert '"/api' not in text and "'/api" not in text
    # Orange / amber hues are rejected by the same check as the UI-4 test.
    assert "f59e0b" not in css.lower() and "fbbf24" not in css.lower() and "ffa500" not in css.lower()


def _phone(browser):
    return browser.new_context(
        viewport=PHONE,
        is_mobile=True,
        has_touch=True,
        device_scale_factor=2,
        user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"),
    )


def _settle_detail(page):
    page.wait_for_selector("#positions tbody td[data-l]", timeout=10000)


def test_null_safe_dom(page):
    page.evaluate("window.TDStop && window.TDStop()")
    page.evaluate("""() => {
      const sw = {ok:true, error:null, as_of:"2026-01-02T14:00:00Z", feed:"iex", prices_note:"No live prices: stream down",
        market:{is_open:false, next_open:"2026-01-03T14:30:00Z", next_close:"2026-01-03T21:00:00Z", as_of:"2026-01-02T21:00:00Z"},
        accounts:[{id:"cashonly", name:"Cash", mask:null, cash:25, positions:[], total:25, positions_value:0,
          day_pl:null, day_plpc:null, unrealized_pl:null, unrealized_plpc:null, missing:[], day_excluded:[], basis_excluded:[]},
          {id:"individual", name:"Brokerage", mask:"0000", cash:0, total:0, positions_value:0,
           day_pl:null, day_plpc:null, unrealized_pl:null, unrealized_plpc:null,
           missing:["AAA"], day_excluded:[], basis_excluded:["AAA"],
           positions:[{symbol:"AAA", name:"Example A Corp", qty:10, price:null, value:null, day_pl:null, day_plpc:null,
             unrealized_pl:null, unrealized_plpc:null, cost_basis:1000, label:"no quote", price_source:"none"}]}],
        combined:{total:25, cash:25, positions_value:0, day_pl:null, day_plpc:null, unrealized_pl:null, unrealized_plpc:null,
          missing:["AAA"], day_excluded:[], basis_excluded:["AAA"]}};
      window.TDApplyState({v:"0.3.5", ts_ms:Date.now(), mock:true, env:"paper", keys_configured:false,
        health:{trade_stream:{state:"no keys", last_msg_ms:null, error:"no keys"},
          data_stream:{state:"no keys", last_msg_ms:null, error:"no keys", feed:"iex"},
          rest:{last_ok_ms:null, error:"no API keys configured"}, status_feed:{source:"", error:null}},
        account:{}, positions:[], orders:[], fills:[], curve:{t:[], equity:[], base:null},
        risk:{caps:{temp_open_risk_pct:{value:null, source:""}, single_name_cap_pct:{value:null, source:""}},
          open_risk_pct:null, open_risk_usd:null, largest:null, unprotected:[]},
        desk:{bots:[], pipeline:[], proposals:[], feed:"missing", example:false, holdings:null, portfolio:null},
        totals:{schwab_total:25, alpaca_equity:null, combined_total:25, day:{}, week:{}, month:{}, year:{}, unrealized:{},
          schwab_source:"holdings file + Alpaca live"},
        crypto:{}, ha_prices:{}, ranges:{}, gauges:[], schwab:sw});
    }""")
    page.click("#tab-assets")
    page.wait_for_selector(".sw-total", timeout=5000)
    text = page.inner_text("#assetsview")
    for bad in ("NaN", "undefined", "Infinity"):
        assert bad not in text, bad
    assert "AAA" in text and "10.0000" in text
    assert "No live prices" in text
    assert "No price, left out of the total: AAA" in text


def test_identical_states_do_not_touch_detail(page):
    page.goto(page.url.split("#")[0] + "#detail")
    _settle_detail(page)
    page.wait_for_timeout(800)
    mutations = page.evaluate("""() => {
      window.TDStop();
      const raw = JSON.stringify(window.TDShared);
      window.TDApplyState(raw);
      return new Promise((resolve) => {
        setTimeout(() => {
          let n = 0;
          const obs = new MutationObserver((recs) => { n += recs.length; });
          obs.observe(document.getElementById("deskview"), {subtree:true, childList:true, attributes:true, characterData:true});
          for (let i = 0; i < 20; i++) window.TDApplyState(raw);
          setTimeout(() => { obs.disconnect(); resolve(n); }, 1600);
        }, 1200);
      });
    }""")
    assert mutations == 0, mutations


def test_scroll_holds(page, seconds=30):
    _settle_detail(page)
    page.wait_for_timeout(500)
    jumps = []
    for frac in (0.25, 0.50, 0.75, 0.90):
        page.evaluate("""(frac) => {
          const max = Math.max(0, document.scrollingElement.scrollHeight - window.innerHeight);
          window.scrollTo(0, Math.round(max * frac));
        }""", frac)
        page.wait_for_timeout(250)
        target = page.evaluate("window.scrollY")
        for _ in range(seconds * 2):
            y = page.evaluate("window.scrollY")
            if abs(y - target) >= 1:
                jumps.append((frac, target, y))
                break
            page.wait_for_timeout(500)
        assert not jumps, jumps


def test_tab_scroll_memory(page):
    page.evaluate("""() => {
      window.__hashes = [];
      const orig = history.replaceState;
      history.replaceState = function (s, t, url) {
        window.__hashes.push(String(url));
        return orig.apply(this, arguments);
      };
    }""")
    page.evaluate("""() => {
      const max = Math.max(0, document.scrollingElement.scrollHeight - window.innerHeight);
      window.scrollTo(0, Math.round(max * 0.4));
    }""")
    page.wait_for_timeout(200)
    detail_y = page.evaluate("window.scrollY")
    assert detail_y > 20, detail_y
    page.click("#tab-assets")
    page.wait_for_selector(".sw-total")
    page.wait_for_timeout(250)
    first = page.evaluate("window.scrollY")
    assert first <= 2, first
    page.evaluate("window.scrollTo(0, 180)")
    page.wait_for_timeout(150)
    schwab_y = page.evaluate("window.scrollY")
    page.click("#tab-desk")
    page.wait_for_timeout(300)
    back = page.evaluate("window.scrollY")
    assert abs(back - detail_y) <= 2, (back, detail_y)
    page.click("#tab-assets")
    page.wait_for_timeout(300)
    again = page.evaluate("window.scrollY")
    assert abs(again - schwab_y) <= 2, (again, schwab_y)
    hashes = page.evaluate("window.__hashes")
    assert hashes, hashes
    for url in hashes:
        assert "http" not in url and "://" not in url
        assert url.endswith("#detail") or url.endswith("#assets") or url.endswith("#")


def test_height_and_charts(page):
    page.evaluate("""() => {
      const el = document.getElementById("curve");
      window.__chartH = el ? el.getBoundingClientRect().height : 0;
    }""")
    page.wait_for_timeout(1500)
    base = page.evaluate("document.scrollingElement.scrollHeight")
    chart_h = page.evaluate("document.getElementById('curve').getBoundingClientRect().height")
    assert chart_h >= 200, chart_h
    samples = []
    for _ in range(60):
        samples.append(page.evaluate("document.scrollingElement.scrollHeight"))
        cleared = page.evaluate("""() => {
          const el = document.getElementById('curve');
          return !el || el.innerHTML === '';
        }""")
        assert not cleared
        page.wait_for_timeout(1000)
    print("document height min", min(samples), "max", max(samples), "base", base)
    assert min(samples) >= base - 1, (base, min(samples), max(samples))


def test_no_horizontal_scroll(browser, port):
    for width in (390, 768, 1280):
        context = browser.new_context(viewport={"width": width, "height": 900})
        page = context.new_page()
        page.goto("http://127.0.0.1:%d/#assets" % port, wait_until="domcontentloaded")
        page.wait_for_selector(".sw-total")
        box = page.evaluate("""() => ({sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth})""")
        assert box["sw"] <= box["cw"] + 1, (width, box)
        if width == 390:
            labeled = page.evaluate("""() => {
              const cell = document.querySelector('.sw-pos td[data-l]');
              return cell ? {l: cell.getAttribute('data-l'), h: cell.getBoundingClientRect().height} : null;
            }""")
            assert labeled and labeled["l"] == "Symbol", labeled
            assert labeled["h"] >= 44, labeled
            tab = page.evaluate("document.querySelector('#tab-assets').getBoundingClientRect().height")
            assert tab >= 44, tab
        context.close()


def main():
    test_static_guards()
    print("PASS static guards")
    httpd, port = _server()
    base = "http://127.0.0.1:%d/" % port
    try:
        with sync_playwright() as p:
            browser = p.webkit.launch()
            try:
                test_no_horizontal_scroll(browser, port)
                print("PASS widths 390/768/1280")
                context = _phone(browser)
                page = context.new_page()
                page.goto(base + "#detail", wait_until="domcontentloaded")
                _settle_detail(page)
                test_tab_scroll_memory(page)
                print("PASS tab scroll memory")
                page.click("#tab-desk")
                _settle_detail(page)
                test_scroll_holds(page, 30)
                print("PASS page scroll 30s")
                test_height_and_charts(page)
                print("PASS height 60s")
                quiet = context.new_page()
                quiet.goto(base + "#detail", wait_until="domcontentloaded")
                test_identical_states_do_not_touch_detail(quiet)
                print("PASS identical SSE mutations")
                quiet.click("#tab-assets")
                test_null_safe_dom(quiet)
                print("PASS null-safe DOM")
                frame_page = context.new_page()
                frame_page.goto(base + "iframe-harness", wait_until="domcontentloaded")
                child = [f for f in frame_page.frames if f != frame_page.main_frame][0]
                child.wait_for_selector("#positions tbody td[data-l]", timeout=10000)
                for frac in (0.25, 0.50, 0.75, 0.90):
                    child.evaluate("""(frac) => {
                      const max = Math.max(0, document.scrollingElement.scrollHeight - window.innerHeight);
                      window.scrollTo(0, Math.round(max * frac));
                    }""", frac)
                    child.wait_for_timeout(250)
                    target = child.evaluate("window.scrollY")
                    for _ in range(60):
                        y = child.evaluate("window.scrollY")
                        assert abs(y - target) < 1, (frac, target, y)
                        child.wait_for_timeout(500)
                print("PASS iframe scroll 30s")
            finally:
                browser.close()
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
