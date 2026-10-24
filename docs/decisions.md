# Decisions made without Jaxon (with revert plans)

Format: decision · why · who decided · how to revert quickly.

- **SpacetimeDB on Maincloud (`robo-factory-a706j`), TypeScript module**: user-provided DB, TS is the recommended module language. Decided by: plan (Fable) + user. Revert: `spacetime start` locally, set `SPACETIME_HOST=http://localhost:3000` in `.env`, republish `spacetime/`.
- **Stripe: built on API keys + skills/docs, not the Stripe MCP planner**: MCP needed browser OAuth and the user was away. Revert: after OAuth, run `stripe_implementation_planner` and diff it against `docs/stripe-plan.md`.
- **Fly deploy deferred to morning**: no Fly secrets yet. Everything runs locally.
- **Eval ship rule compares the point estimate by default (`require_ci_low=false`); the Wilson 95% CI is always shown** (TDM-66): with 10 trials, the Wilson lower bound is at most 0.72, even at 10/10, so a CI-low rule can never clear a 0.8 target. Stats live in `services/orchestrator/src/rdf_orchestrator/stats.py` (stdlib only), not `services/common/`, because `services/common` (rdf-common) was not committed yet. The file is stdlib only, so it can move into `rdf_common` once that lands. Decided by: TDM-66 worker. Revert: set `require_ci_low=true` in run settings (to clear 0.8 you need at least 16/16, or about 63/70 at a 90% rate), or move the file to a shared package.
