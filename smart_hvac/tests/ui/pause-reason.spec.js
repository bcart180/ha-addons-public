const { test, expect } = require("@playwright/test");
const http = require("http");
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "../../rootfs/app/static");
const PAGES = {
  "/": "index.html",
  "/index.html": "index.html",
  "/thermostat": "thermostat.html",
  "/classic": "classic.html",
};

let server;
let base;

test.describe.configure({ timeout: 20000 });

test.beforeAll(async () => {
  server = http.createServer((req, res) => {
    const file = PAGES[new URL(req.url, "http://127.0.0.1").pathname];
    if (!file) {
      res.writeHead(404);
      res.end("not found");
      return;
    }
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
    res.end(fs.readFileSync(path.join(ROOT, file)));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});

test.afterAll(async () => {
  if (server) await new Promise((resolve) => server.close(resolve));
});

for (const [label, url] of [
  ["Climate", "/?mock=1&pause=1"],
  ["thermostat", "/thermostat?mock=1&pause=1"],
  ["classic", "/classic?mock=1&pause=1"],
]) {
  test(label + " shows Paused, window open? on both splits", async ({ page }) => {
    await page.goto(base + url, { waitUntil: "load" });
    const marks = page.locator(".slope-pause");
    await expect(marks).toHaveCount(2);
    await expect(marks.nth(0)).toHaveText("Paused, window open?");
    await expect(marks.nth(1)).toHaveText("Paused, window open?");
  });
}
