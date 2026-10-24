# Questions parked for Jaxon (morning)

Work kept going around each of these. Answer in any order.

1. **Jetson access**: add `JETSON_HOST` / `JETSON_USER` to `.env` and make passwordless `ssh` work from the laptop. Blocks: installing the YAM client, the Jetson latency benchmark (TDM-37 leaves a script), and all [HIL] arm work.
2. **GitHub remote**: `brew install gh && gh auth login`, and pick a repo name (needed for Devpost). Until then, commits stay local on `main`.
3. **Fly secrets**: you'll add them in the morning, then we deploy (TDM-5 is held until then).
4. **Easy task choice (TDM-82)**: which task, which objects are on the table.
5. **Phone teleop**: the plan puts gamepad/keyboard browser teleop in E07 and phone-motion (WebXR) control in the E14 stretch epic. The spec's live demo has a judge's phone receiving a takeover task. Should phone clutch control move into E07?
6. **Stripe MCP (optional)**: the Stripe plugin's MCP server wants a browser OAuth. Not needed, since the integration is built on the API keys directly.
7. **E14 mobile app**: still `proposed`. Approve only if you want it.
8. **Stripe Connect loss-liability acknowledgement (blocks real test transfers)**: Stripe refuses to create connected accounts until you accept platform loss liability. That's a legal acknowledgement, so the agents didn't click it. Open https://dashboard.stripe.com/acct_1UTycMPt66MyJKMN/settings/connect/platform-profile and complete it. Then run `services/payments/.venv/bin/python services/payments/smoke_test.py`, finish the test onboarding link (test data is listed in `docs/stripe-plan.md`), and re-run with `--account acct_... --wait`. Everything else in payments (budget guard, dataset math, Checkout, webhook signature check) already passes.
8. **Episode media store (TDM-4)**: media is on a Modal Volume behind a signed-URL endpoint on Modal (see `docs/decisions.md`). Two things to confirm: (a) are you OK leaving it there through the demo, or do you want it moved to Tigris once Fly secrets land (swap is env-only; copy command in decisions.md)? (b) The endpoint scales to zero, so the first upload/play after idle pays a ~2-4 s cold start; say the word and we pin `min_containers=1` for demo day (costs a few cents/hour).
