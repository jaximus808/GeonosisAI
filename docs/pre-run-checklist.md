# Pre-run checklist (before the Saturday-night real run, TDM-72)

Collected overnight from worker reports. Tick these off before starting the real ticket on maincloud.

## Maincloud database
- [ ] Publish anything parked in `docs/morning-questions.md` #11 (`abort_eval_run`), once Jaxon OKs it.
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
- [ ] Mount `<JoinQr ticketId={id}/>` and `<DecisionTrail ticketId={id} refreshKey={events.length}/>` in `components/ticket/TicketView`. The TDM-75 `DecisionCard` reads a field the planner never writes, so prefer `DecisionTrail`.
- [ ] Mount `<ContributorPresence page="teleop"/>` on the teleop page.
- [ ] Wire `loadEnv(webEnv, "web")` from `@rdf/env` in `apps/web`.
- [ ] Remove the stray `.next-tdm75` include lines in `apps/web/tsconfig.json`.
- [ ] Set `NEXT_PUBLIC_WEB_PUBLIC_URL` to the public URL, so the join QR works on phones.

## Factory
- [ ] Repo pushed to GitHub and added to the Factory Environment, if cloud droids are wanted; otherwise droids run locally.
- [ ] Delete the overnight test computer `2bfdc311-…` after the morning resume check (Q10).
