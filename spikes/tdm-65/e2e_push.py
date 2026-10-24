"""TDM-65 end-to-end push test on desktop Chrome (Playwright, temporary profile, real FCM).

Against a LOCAL stack only (never maincloud):
  1. make fake-stack-style SpacetimeDB on 127.0.0.1:$PORT (db $DB), orchestrator on :8065 with VAPID
     keys, web dev server on :3765 pointed at both (see spikes/tdm-65/README.md).
  2. Joins as a guest, turns on task alerts (notifications permission pre-granted for the origin).
  3. Navigates the page AWAY (no app page open), creates an open reset task with the owner-side
     service token, and waits for the service worker to show the notification.
  4. Opens the notification's deep link and checks the marketplace lands on that task.

  uv run --no-project --with playwright --with httpx python -I spikes/tdm-65/e2e_push.py \
      --token-file $TMPDIR/tdm-28/run-3065/tokens/orchestrator.token

The token is read from the file and never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time

import httpx
from playwright.async_api import async_playwright


def reducer(stdb: str, db: str, token: str, name: str, *args) -> None:
    r = httpx.post(f"{stdb}/v1/database/{db}/call/{name}", json=list(args),
                   headers={"Authorization": f"Bearer {token}"}, timeout=15)
    if r.status_code >= 300:
        raise SystemExit(f"{name} failed: {r.status_code} {r.text[:300]}")


def sql(stdb: str, db: str, token: str, q: str) -> list[dict]:
    r = httpx.post(f"{stdb}/v1/database/{db}/sql", content=q, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    r.raise_for_status()
    out = r.json()[0]
    names = [e["name"].get("some", "") for e in out["schema"]["elements"]]
    return [dict(zip(names, row)) for row in out["rows"]]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", default="http://localhost:3765")
    ap.add_argument("--stdb", default="http://127.0.0.1:3065")
    ap.add_argument("--db", default="tdm65push")
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    token = open(a.token_file).read().strip()

    tickets = [t for t in sql(a.stdb, a.db, token, "SELECT * FROM ticket") if "test.tdm65" in t["task_description"]]
    if not tickets:
        robots = sql(a.stdb, a.db, token, "SELECT * FROM robot")
        reducer(a.stdb, a.db, token, "create_ticket", int(robots[0]["id"]), "test.tdm65 push paging", 0.8, 5000, "")
        tickets = [t for t in sql(a.stdb, a.db, token, "SELECT * FROM ticket") if "test.tdm65" in t["task_description"]]
    ticket_id = int(tickets[0]["id"])

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            tempfile.mkdtemp(prefix="tdm65-chrome-"), channel="chrome", headless=not a.headed,
            args=["--no-first-run", "--no-default-browser-check"])
        await ctx.grant_permissions(["notifications"], origin=a.web)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(f"{a.web}/join")
        await page.get_by_text("Skip, join as a guest").click(timeout=30_000)
        await page.get_by_role("button", name="Turn on task alerts").click(timeout=30_000)
        await page.get_by_text("Task alerts are on for this device").wait_for(timeout=30_000)
        print("1. subscribed from /join: 'Task alerts are on for this device'")
        status = httpx.get("http://127.0.0.1:8065/push/status", timeout=10).json()
        print(f"   server store: {status['subscriptions']} subscription(s), {status['contributors']} contributor(s)")

        # No app page open: only the service worker can show what comes next.
        await page.goto("about:blank")
        sw = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker")
        t0 = time.time()
        reducer(a.stdb, a.db, token, "create_task", ticket_id, "reset", 200,
                json.dumps({"instructions": "test.tdm65: put the cup back on the mark, then mark it done."}))
        print(f"2. created an open reset task on ticket {ticket_id} with no app page open")
        notes: list = []
        for _ in range(100):
            notes = await sw.evaluate("""async () => (await self.registration.getNotifications())
                .map(n => ({title: n.title, body: n.body, tag: n.tag, url: n.data && n.data.url}))""")
            if notes:
                break
            await asyncio.sleep(0.1)
        if not notes:
            raise SystemExit("FAIL: no notification within 10 s")
        n = notes[0]
        print(f"3. notification shown by the service worker after {time.time() - t0:.2f} s: "
              f"{n['title']!r} / {n['body']!r} -> {n['url']}")

        await page.goto(a.web + n["url"])
        task_id = n["url"].split("task=")[1].split("&")[0]
        el = page.locator(f"#task-{task_id}")
        await el.wait_for(timeout=30_000)
        await page.wait_for_timeout(800)
        box = await el.bounding_box()
        vp = page.viewport_size or {"height": 720}
        print(f"4. deep link /marketplace?task={task_id}: task card present, in view={0 <= box['y'] < vp['height']}, "
              f"claim button: {await el.get_by_role('button', name='Claim for').count() == 1}")
        await page.screenshot(path="spikes/tdm-65/tmp/deeplink.png")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
