# Pre-run checklist (before the Saturday-night real run, TDM-72)

Collected overnight from worker reports. Tick these off before starting the real ticket on maincloud.

## Maincloud database
- [x] `abort_eval_run` is live on maincloud. TDM-70's own publish, which the permission system allowed, carried it from HEAD; see morning Q11.
- [ ] **One-time clean event log:** publish with `--delete-data=always`, so the log judges see starts at the real run. `init` re-seeds the owner. Run `scripts/verify_event_log.py --save` right after and keep the output.
- [ ] **Separate service identities:** `POST /v1/identity` for orchestrator, robot_client, factory, web and mcp, then `register_service_identity` for each. Put each token in its own `.env` key. Without this every row shows actor `owner`, and the decision trail can't prove the orchestrator acted on its own (TDM-68).
- [ ] Give the mcp server its `mcp` identity, never the owner token (TDM-80).

## Orchestrator
- [ ] Exactly **one** orchestrator process against maincloud. Check with `ps`, then stop any test orchestrators.
- [ ] `make test-loop DRIVER=orchestrator` passes on the fake stack first.
- [ ] Policy server warm (`min_containers=1`) only for the run window, then scaled down.

## Robot (needs Jaxon at the arm)
- [ ] Jetson reachable (`JETSON_HOST`/`JETSON_USER`), robot client installed, cameras pass the checklist in `services/robot-client/docs/cameras-jetson-checklist.md`.
- [ ] Safety layer verified on the arm: speed caps, workspace limits, soft e-stop, and the physical e-stop within reach.
- [ ] Re-tune the reviewer's jerk threshold (`services/orchestrator/config/review.yaml`) on the first real YAM demos. It's calibrated on synthetic data only.
- [ ] The real robot waits for `resume_policy` after each eval trial (don't reset itself).

## Payments
- [ ] Stripe Connect loss-liability acknowledgement done (morning Q8), and one real test-mode transfer proven.

## Web wiring still open
- [ ] Set `RDF_OPERATOR_PASSCODE` before exposing the site (the tunnel for judges' phones). A generated one is in `.env`; the web server must see it, or every operator route answers 503. Give it only to operators; log in at `/ops/login`.
- [x] Mount `<JoinQr ticketId={id}/>` and `<DecisionTrail ticketId={id} refreshKey={events.length}/>` in `components/ticket/TicketView`. The TDM-75 `DecisionCard` reads a field the planner never writes, so prefer `DecisionTrail`. (TDM-79: done, plus `<TicketSpendPanel>`; DecisionTrail replaces DecisionCard.)
- [ ] Mount `<ContributorPresence page="teleop"/>` on the teleop page.
- [x] Wire `loadEnv(webEnv, "web")` from `@rdf/env` in `apps/web`. (TDM-79: `src/instrumentation.ts`, maps the NEXT_PUBLIC_* names onto webEnv.)
- [x] Remove the stray `.next-tdm75` include lines in `apps/web/tsconfig.json`. (TDM-79: they were never committed; `next dev` re-adds `<distDir>/types` lines whenever it runs with `NEXT_DIST_DIR`, so `git checkout apps/web/tsconfig.json` after such a run.)
- [ ] Set `NEXT_PUBLIC_WEB_PUBLIC_URL` to the public URL, so the join QR works on phones.
- [ ] Set `NEXT_PUBLIC_ORCHESTRATOR_URL` (browser GETs) and `RDF_ORCHESTRATOR_URL` (Next server: `/api/tdm64`, `/api/tdm79` proxies and the `/tickets/[id]/report` page) in `apps/web/.env.local`; port 8000 is taken on the laptop.
- [x] (Fixed in TDM-70, 73be886: planner reads the column first.) Planner gap (TDM-79 found): `planner.py` reads `data_type_override` from `hyperparams_json` or a `planner.data_type_override` event, not the `run_settings.data_type_override` column that TDM-20's PATCH writes, so the settings editor's "Data type" choice is ignored by the planner until that is wired (the editor says so).

## Factory
- [ ] Repo pushed to GitHub and added to the Factory Environment, if cloud droids are wanted; otherwise droids run locally.
- [ ] Delete the overnight test computer `2bfdc311-…` after the morning resume check (Q10).

## Network path Jetson ↔ orchestrator (decide in the morning)
- [ ] The Jetson must reach the orchestrator for episode uploads (`JETSON_ORCHESTRATOR_URL`). If the arm is at the office and the laptop at the venue, use the Fly deploy (TDM-5) or a cloudflared tunnel to the laptop's orchestrator; a LAN address only works if both are on the same network. SpacetimeDB (maincloud), Modal and the media store are already public.
- [ ] Robot-client service mode defaults to `off`. After the SDK bring-up, run `make jetson-deploy JETSON_SERVICE_MODE=yam`. `jetson.sh mint-identity --yes` creates the robot_client SpacetimeDB identity; it writes to maincloud, so it's left for you.

## Claude connector: deploy and connect claude.ai (TDM-81; full guide `docs/connector.md`)
OAuth sign-in, the Fly config and the end-to-end test are done and were tested locally. These steps need you, in this order:
- [ ] **Deploy the orchestrator first** (TDM-5) and note its URL. The connector only forwards to it.
- [ ] **Mint the connector's own SpacetimeDB identity** (`POST /v1/identity`, then `register_service_identity` with role `mcp`; see "Separate service identities" above). Never give the connector the owner token.
- [ ] **Create the Fly app**, from the repo root: `fly apps create rdf-mcp`. If that name is taken, use another and change `app` and `MCP_PUBLIC_URL` in `apps/mcp/fly.toml` to match.
- [ ] **Set the secrets:** `fly secrets set --config apps/mcp/fly.toml MCP_TOKEN=<from .env> RDF_OPERATOR_PASSCODE=<from .env> SPACETIME_TOKEN=<mcp identity token> ORCHESTRATOR_URL=https://<orchestrator app>.fly.dev`
- [ ] **Deploy:** `fly deploy . --config apps/mcp/fly.toml --dockerfile apps/mcp/Dockerfile --ignorefile apps/mcp/Dockerfile.dockerignore`
- [ ] **Check it:** `curl -s https://rdf-mcp.fly.dev/healthz` should show `"oauth":"enabled"`. Then run `MCP_URL=https://rdf-mcp.fly.dev/mcp node apps/mcp/scripts/oauth-e2e.ts --auth-only`, which signs in and lists the 5 tools. It reads the passcode from `.env` and never prints it.
- [ ] **Connect claude.ai.** Go to **Customize > Connectors** (https://claude.ai/customize/connectors) and click **Add custom connector**. On Team/Enterprise, an Owner goes to **Organization settings > Connectors**, clicks **Add**, then **Custom**, then **Web**.
  - Name: `Robot Deployment Factory`.
  - URL: **`https://rdf-mcp.fly.dev/mcp`**
  - Authentication: **Sign in now**. OAuth client: leave the default (**Use Claude's published identity**). If that fails, remove the connector and re-add it with **Register automatically**.
  - Leave the client ID, secret and request headers empty.
  - Click **Add**, then **Connect**. On the "Connect Claude?" page, check that it says you go back to **claude.ai**, type the operator passcode and click **Approve**.
- [ ] **In a chat:** click **+**, then **Connectors**, and turn on Robot Deployment Factory. Then run the four demo prompts in `docs/connector.md`: set up the YAM cup-in-bin deployment, "how is it going?", "approve $20 more", "give me the report". Record the screen capture.
- [ ] **Check the audit trail:** run `SELECT * FROM event_log WHERE actor = 'human:via_mcp'` on maincloud. It should show `human.run_settings_changed` and/or `spend.approved` rows. That is TDM-81's done condition. After it, complete TDM-81 on the board.
- [ ] Optional, Claude Code: `claude mcp add --transport http rdf https://rdf-mcp.fly.dev/mcp`, then `/mcp` to sign in.
- [ ] To sign every connector session out at once: `fly secrets set --config apps/mcp/fly.toml MCP_OAUTH_SECRET=$(openssl rand -hex 32)`.
