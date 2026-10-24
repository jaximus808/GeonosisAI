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
make dev                 # docker compose up (see below); `make dev-host` runs on the host instead
```

## Local dev with Docker (`docker-compose.yml`)

| Command | Starts |
|---|---|
| `make dev` | `orchestrator` (:8000), `web` (:3737), `mcp` (:3002), all on **Maincloud** SpacetimeDB via `SPACETIME_HOST` in `.env` |
| `make dev-local` | the same plus a local `spacetimedb` container (:3300), with the apps repointed at it. Then `make spacetime-publish` to load the module |
| `make dev-minio` | the same as `make dev` plus `minio` (:9000, console :9001) and an init job that creates the `episodes` bucket |
| `make dev-down` | stops everything, all profiles |

- Each container runs the app's own dev command (`make dev-<svc>`, `pnpm --filter @rdf/<app> run dev`), so changing an app's dev script changes what the container runs. Placeholders that exit 0 are kept idle so the stack stays up.
- Source is bind-mounted. `node_modules` and the Python venv live in Docker volumes, so the host's macOS installs are never touched. If you add a workspace package, add its `node_modules` volume in `docker-compose.yml`. After dependency changes that don't show up, reset them with `docker volume ls -q | grep '^rdf_node-modules' | xargs docker volume rm`.
- Host port clashes (for example when the same app is already running on the host): set `ORCHESTRATOR_PORT`, `WEB_PORT`, `MCP_PORT`, `SPACETIME_LOCAL_PORT`, `MINIO_PORT` or `MINIO_CONSOLE_PORT` in your shell.
- MinIO is optional. Nothing in the apps depends on it. Credentials default to dev-only values. You can override them with `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` and `EPISODES_BUCKET`. The image is the `pgsty/minio` community build, because `minio/minio` is no longer on Docker Hub.
- In `make dev-local`, server-side code uses `http://spacetimedb:3000`, and the browser uses `NEXT_PUBLIC_SPACETIME_HOST=http://localhost:3300`. `SPACETIME_TOKEN` is blank, so services connect anonymously.

`make seed` and `make deploy` are stubs for now.

## CI (TDM-6)

`make ci` runs exactly what `.github/workflows/ci.yml` runs (both call `scripts/ci/run.sh`): `uv sync` + pytest per Python service, the pnpm workspace typechecks and tests, `tsc --noEmit` for the SpacetimeDB module, a minimal ruff check and `scripts/verify_event_log.py static`. It uses fake settings only (`scripts/ci/fake.env`); live tests stay opt-in via `RDF_LIVE_*`. One step at a time: `make ci-py SERVICE=orchestrator`, `make ci-ts`, `make ci-lint`. Tests that need the `spacetime` CLI run in the optional `make ci-integration`.
