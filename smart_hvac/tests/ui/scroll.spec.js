const { test, expect } = require("@playwright/test");
const http = require("http");
const fs = require("fs");
const path = require("path");

const INDEX = path.resolve(__dirname, "../../rootfs/app/static/index.html");
const html = fs.readFileSync(INDEX);
const TOL = 8;

const ZONES = ["room_a", "room_b", "bath_floor"];
const controls = [];
for (const zone of ZONES) {
  const root = `[data-zone="${zone}"] `;
  const add = (name, selector) => controls.push({ name: zone + " " + name, zone, selector });
  add("schedule pref −", root + 'button[data-pref="-0.5"]');
  add("schedule pref +", root + 'button[data-pref="0.5"]');
  add("override temp −", root + 'button[data-hold="-0.5"]');
  add("override temp +", root + 'button[data-hold="0.5"]');
  add("30m", root + 'button[data-hours="0.5"]');
  add("1h", root + 'button[data-hours="1"]');
  add("2h", root + 'button[data-hours="2"]');
  add("4h", root + 'button[data-hours="4"]');
  add("Hold", root + 'button[data-override="1"]');
}
controls.push(
  { name: "room_a Resume automatic", zone: "room_a", selector: '[data-zone="room_a"] button[data-resume="1"]', leaves: true },
  { name: "room_b Resume automatic", zone: "room_b", selector: '[data-zone="room_b"] button[data-resume="1"]', leaves: true },
  { name: "Resume automatic · both", zone: null, selector: 'button[data-resume-all="1"]', leaves: true, sched: true },
  { name: "Full Auto", zone: null, selector: 'button[data-profile="auto"]', sched: true },
  { name: "Home", zone: null, selector: 'button[data-profile="home"]', sched: true },
  { name: "Sleep", zone: null, selector: 'button[data-profile="sleep"]', sched: true },
  { name: "Away", zone: null, selector: 'button[data-profile="away"]', sched: true },
  { name: "Off · both", zone: null, selector: 'button[data-mode="off"]' },
  { name: "History 24h", zone: null, selector: 'button[data-range="24h"]' },
  { name: "History 7d", zone: null, selector: 'button[data-range="7d"]' },
  { name: "History Room A", zone: null, selector: 'button[data-graph="room_a"]' },
  { name: "History Room B", zone: null, selector: 'button[data-graph="room_b"]' },
  { name: "History Bath floor", zone: null, selector: 'button[data-graph="bath_floor"]' },
);

let server;
let base;

test.beforeAll(async () => {
  server = http.createServer((req, res) => {
    const pathname = new URL(req.url, "http://127.0.0.1").pathname;
    if (pathname !== "/" && pathname !== "/index.html") {
      res.writeHead(404);
      res.end("not found");
      return;
    }
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
    res.end(html);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${server.address().port}/index.html?mock`;
});

test.afterAll(async () => {
  if (server) await new Promise((resolve) => server.close(resolve));
});

async function ready(page) {
  await page.goto(base, { waitUntil: "load" });
  await page.waitForFunction(() => document.querySelectorAll("svg.g").length >= 4);
}

async function centerControl(page, selector) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const height = el.getBoundingClientRect().height;
    const midTop = (window.innerHeight - height) / 2;
    window.scrollBy(0, el.getBoundingClientRect().top - midTop);
    const box = el.getBoundingClientRect();
    const card = el.closest("[data-zone]");
    return {
      top: box.top,
      height,
      scrollY: window.scrollY,
      cardTop: card ? card.getBoundingClientRect().top : null,
      vh: window.innerHeight,
    };
  }, selector);
}

async function clickControl(page, selector) {
  await page.evaluate(async (sel) => {
    const el = document.querySelector(sel);
    el.click();
    for (let i = 0; i < 30; i++) await Promise.resolve();
  }, selector);
  await page.waitForTimeout(80);
}

async function measure(page, selector, zone) {
  return page.evaluate(({ sel, zoneName }) => {
    const el = document.querySelector(sel);
    let card = null;
    if (zoneName) {
      const cards = document.querySelectorAll("[data-zone]");
      for (let i = 0; i < cards.length; i++) {
        if (cards[i].getAttribute("data-zone") === zoneName) card = cards[i];
      }
    }
    return {
      present: !!el,
      top: el ? el.getBoundingClientRect().top : null,
      cardTop: card ? card.getBoundingClientRect().top : null,
      scrollY: window.scrollY,
    };
  }, { sel: selector, zoneName: zone });
}

function num(n) {
  return n == null ? "gone" : n.toFixed(1);
}

test("columns follow the 932px breakpoint", async ({ page }) => {
  await ready(page);
  for (const width of [390, 931, 932, 1100, 1280]) {
    await page.setViewportSize({ width, height: 800 });
    const info = await page.evaluate(() => {
      const tracks = (sel) => getComputedStyle(document.querySelector(sel)).gridTemplateColumns.split(" ").filter(Boolean).length;
      const wides = [...document.querySelectorAll(".card.wide")].map((el) => {
        const s = getComputedStyle(el);
        return s.gridColumnStart + " / " + s.gridColumnEnd;
      });
      return { cols: tracks(".grid"), metrics: tracks(".metrics"), wides };
    });
    if (width < 932) {
      expect(info.cols, "columns at " + width).toBe(1);
      expect(info.metrics, "metrics at " + width).toBe(2);
      expect(info.wides, "wide cards at " + width).toEqual(["auto / auto", "auto / auto"]);
    } else {
      expect(info.cols, "columns at " + width).toBe(2);
      expect(info.metrics, "metrics at " + width).toBe(4);
      expect(info.wides, "wide cards at " + width).toEqual(["1 / -1", "1 / -1"]);
    }
  }
});

for (const viewport of [
  { width: 1280, height: 800 },
  { width: 390, height: 844 },
]) {
  test.describe(viewport.width + "x" + viewport.height, () => {
    test.use({ viewport });

    test("taps keep the control in place", async ({ page }) => {
      const failures = [];
      for (const control of controls) {
        try {
          await ready(page);
          const before = await centerControl(page, control.selector);
          if (!before) throw new Error("missing " + control.selector);
          if (!control.sched) {
            const target = (before.vh - before.height) / 2;
            if (Math.abs(before.top - target) > TOL) {
              throw new Error("not mid-screen top=" + num(before.top) + " target=" + num(target) + " y=" + num(before.scrollY));
            }
            if (before.scrollY < 1) throw new Error("page did not scroll");
          }
          await clickControl(page, control.selector);
          const after = await measure(page, control.selector, control.zone);
          if (before.scrollY !== 0 && after.scrollY === 0) {
            throw new Error("jumped to scrollY 0 from " + num(before.scrollY));
          }
          if (after.present) {
            const d = Math.abs(after.top - before.top);
            if (d > TOL) throw new Error("top moved " + num(d) + " (" + num(before.top) + " -> " + num(after.top) + ") y " + num(before.scrollY) + " -> " + num(after.scrollY));
          } else if (control.zone) {
            if (after.cardTop == null || before.cardTop == null) throw new Error("room card missing");
            const d = Math.abs(after.cardTop - before.cardTop);
            if (d > TOL) throw new Error("room card moved " + num(d) + " (" + num(before.cardTop) + " -> " + num(after.cardTop) + ") y " + num(before.scrollY) + " -> " + num(after.scrollY));
          } else {
            const d = Math.abs(after.scrollY - before.scrollY);
            if (d > TOL) throw new Error("scrollY moved " + num(d) + " (" + num(before.scrollY) + " -> " + num(after.scrollY) + ")");
          }
        } catch (err) {
          failures.push(control.name + ": " + err.message);
        }
      }
      expect(failures, failures.join("\n")).toEqual([]);
    });

    test("poll and history load keep scrollY", async ({ page }) => {
      await page.clock.install();
      await ready(page);
      const before = await centerControl(page, '[data-zone="bath_floor"]');
      expect(before.scrollY).toBeGreaterThan(8);
      await page.clock.fastForward(30000);
      const afterPoll = await page.evaluate(() => window.scrollY);
      expect(Math.abs(afterPoll - before.scrollY), "30s poll").toBeLessThanOrEqual(TOL);
      await page.evaluate(async () => { await loadHist("room_a", "7d"); });
      const afterHist = await page.evaluate(() => window.scrollY);
      expect(Math.abs(afterHist - afterPoll), "loadHist").toBeLessThanOrEqual(TOL);
      expect(afterHist).not.toBe(0);
    });
  });
}
