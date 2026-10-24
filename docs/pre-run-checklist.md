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
