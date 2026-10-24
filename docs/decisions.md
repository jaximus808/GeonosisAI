# Decisions made without Jaxon (with revert plans)

Format: decision · why · who decided · how to revert quickly.

- **SpacetimeDB on Maincloud (`robo-factory-a706j`), TypeScript module**: user-provided DB, TS is the recommended module language. Decided by: plan (Fable) + user. Revert: `spacetime start` locally, set `SPACETIME_HOST=http://localhost:3000` in `.env`, republish `spacetime/`.
- **Stripe: built on API keys + skills/docs, not the Stripe MCP planner**: MCP needed browser OAuth and the user was away. Revert: after OAuth, run `stripe_implementation_planner` and diff it against `docs/stripe-plan.md`.
- **Fly deploy deferred to morning**: no Fly secrets yet. Everything runs locally.
