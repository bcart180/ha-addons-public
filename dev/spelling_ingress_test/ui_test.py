import asyncio
from playwright.async_api import async_playwright
B = "http://127.0.0.1:8123/api/hassio_ingress/tok3n_ABC/"
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1280, "height": 800})
        reqs, errs = [], []
        pg.on("response", lambda r: reqs.append((r.status, r.url, r.headers.get("content-type"))))
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        await pg.goto(B); await pg.wait_for_timeout(2500)
        for r in reqs: print(r)
        print("errors:", errs)
        txt = await pg.inner_text("body")
        print("mentions 'Week of October 12':", "Week of October 12" in txt or "October 12" in txt)
        await pg.screenshot(path="/workspace/spelling_addon/spelling-ingress-test.png")
        await b.close()
asyncio.run(main())
