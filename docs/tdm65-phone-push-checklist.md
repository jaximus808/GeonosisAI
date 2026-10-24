# TDM-65 phone push test (morning checklist)

Goal: a phone with the marketplace page **closed or in the background** gets a notification within 10 s of a paid reset or takeover task being created, and a tap lands on that task in `/marketplace`.

How it works: the dispatcher pages on every task it creates, and so does any manual `create_task`. `rdf_orchestrator/paging.py` sends a Web Push (VAPID) to every opted-in contributor, at most one per contributor per 10 s. The service worker `apps/web/public/tdm65-sw.js` shows it, and a tap opens `/marketplace?task=<id>`, which scrolls to and rings that task. Subscriptions are stored only in the orchestrator's private sqlite file (`RDF_PUSH_STORE`, default `~/.local/state/rdf/tdm65_push.sqlite3`). The record of each paging is the `dispatch.paged` event, with counts and no endpoints.

Already verified on the laptop, Oct 24: on desktop Chrome with a temp profile and real FCM, against a local stack, the notification showed **1.4 s** after `create_task`, with no app page open. The deep link scrolled to and highlighted the task. Rerun: `spikes/tdm-65/e2e_push.py --headed`.

## Setup (laptop)
- [ ] `.env` has `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` (`./scripts/check-env.sh`). **Don't regenerate them**: a new key pair silently invalidates every phone's subscription, and phones must then turn alerts off and on again.
- [ ] The orchestrator is the real one, run by the planner session. `GET /push/config` returns `enabled: true`. Optionally set `RDF_WEB_PUBLIC_URL=https://<web tunnel>` so notification links are absolute (relative links also work).
- [ ] The web app runs against maincloud (the default config) with `RDF_ORCHESTRATOR_URL=http://127.0.0.1:<orchestrator port>` and `RDF_DEV_ORIGINS='*.trycloudflare.com'`.
- [ ] HTTPS for the phone: `cloudflared tunnel --no-autoupdate --url http://localhost:3737` gives `https://<random>.trycloudflare.com`. That URL changes on every run, so use it for the join QR (`NEXT_PUBLIC_WEB_PUBLIC_URL`). A plain `http://<LAN IP>` page **cannot** push; the opt-in card says "Alerts need a secure link".

## Android (Chrome), the main demo path
- [ ] On the phone, open `https://<tunnel>/join`, join (as a guest is fine), and tap **Turn on task alerts**, then **Allow**. The card turns green: "Task alerts are on for this device".
- [ ] Tap **Send a test alert**. A notification arrives within a few seconds.
- [ ] Lock the phone, or switch to another app, or close the tab.
- [ ] Create a reset task on a live ticket, either through the flow (eval trial, then the dispatcher's reset) or by hand: `POST /v1/database/robo-factory-a706j/call/create_task` with `[<ticket_id>, "reset", 200, "{\"instructions\":\"test.tdm65 put the cup back\"}"]` as the orchestrator identity.
- [ ] Within 10 s the phone buzzes with "Paid reset · $2" and "Ticket #N needs a reset. Tap to claim it first."
- [ ] Tap it. The marketplace opens with that task scrolled into view and ringed in amber. Then **Claim for $2**.
- [ ] Check the `event_log` row `dispatch.paged`: `result.sent >= 1`. `online_without_push` lists contributors who only get the live highlight.
- [ ] Optional: Add to Home screen (Chrome menu, Install app) and repeat from the installed app.

## iPhone (Safari, iOS 16.4+)
- [ ] Web Push works **only from a home-screen app**. In Safari, open `https://<tunnel>/join`. The card says "On iPhone, alerts need the home-screen app".
- [ ] Tap Share, then Add to Home Screen. Open **Robot tasks** from the home screen (it starts at /marketplace), join there (the home-screen app has its own storage, so this is a new identity), and tap **Turn on task alerts**, then Allow.
- [ ] Repeat the test-alert and locked-phone steps from the Android list.
- [ ] If the push never arrives: Settings → Notifications → Robot tasks must be allowed, and Focus modes must not be silencing it.

## Fallback (say this out loud if push misbehaves)
- [ ] With the marketplace page open, a new task still appears instantly, highlighted with a "New" chip, and the phone vibrates (TDM-61). That needs no permission and no HTTPS. Every non-working state of the opt-in card says this.

## Troubleshooting
- No push but `dispatch.paged.result.sent = 1`: the OS or browser is suppressing it (Do Not Disturb, Chrome site notifications blocked, or battery saver on Android). Check the device settings.
- `result.push = "disabled"`: the orchestrator has no VAPID keys loaded. Restart it after checking `.env`.
- `result.rate_limited` lists the contributor: the phone was paged less than 10 s ago (by design).
- `result.gone >= 1`: the browser dropped that subscription and the store deleted it. Reopen the page and turn alerts on again (the page also re-syncs by itself when opened).
- Headless Chrome doesn't deliver pushes. Use `--headed` for `e2e_push.py`.
