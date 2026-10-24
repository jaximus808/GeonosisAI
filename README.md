# Robot Deployment Factory

"Plug in your robot, tell it the job, and the factory gets it deployed." Built at Cal Hacks 13.0.

- **Build spec:** [CONTEXT.md](CONTEXT.md). Read it before writing code.
- **Task board:** Tandem canvas "Robot Deployment Factory — Cal Hacks 13" (code `HL4RZKEQ`). Architecture overview is on its Architecture tab.

## Layout

| Path | What |
|---|---|
| `spacetime/` | SpacetimeDB module: schema, reducers, subscriptions |
| `apps/web/` | Dashboard and contributor phone task pages |
| `apps/mcp/` | Claude connector (remote MCP server) |
| `apps/mobile/` | Placeholder for a native phone app |
| `services/orchestrator/` | Planner, reviewer and dispatcher loops |
| `services/factory/` | Factory Droid session management |
| `services/training/` | LeRobot conversion, π0.5 fine-tune, policy serving (GPU box) |
| `services/robot-client/` | Robot PC bridge: YAM control, cameras, recording, takeover, evals |
| `services/payments/` | Stripe test-mode payouts |
| `scripts/` | Repo scripts (`check-env.sh`) |

## Setup

Requires Node 20+, pnpm 11, [uv](https://docs.astral.sh/uv/) (it fetches Python 3.11 itself), make.

```sh
cp .env.example .env     # fill in keys; .env is gitignored
make env-check           # shows which keys are set, never their values
make install             # pnpm install + uv sync in each Python service
make dev                 # starts everything (placeholders for now)
```

`make seed` and `make deploy` are stubs for now.
