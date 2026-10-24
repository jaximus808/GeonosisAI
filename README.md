# Robot Deployment Factory

**Plug in your robot, tell it the job, and the factory gets it deployed.** Built at Cal Hacks 13.0.

You give it a robot and a ticket ("put the cup in the bin, 80% reliable, $50 budget"). It runs a baseline, clusters the failures, decides *which kind of data* fixes them (recovery takeovers vs clean demos), buys exactly that from people on their phones, reviews and pays for it, fine-tunes π0.5, evaluates the new checkpoint and ships a report. SpacetimeDB is the source of truth; Claude does the diagnosis, the data decision and the review; Factory Droids do the engineering and sleep on database predicates until the world changes.

## Quickstart

```sh
cp .env.example .env     # fill in keys; make env-check shows which are set
make install             # pnpm install + uv sync in each Python service
make dev                 # orchestrator :8000, web :3737, mcp :3002 against Maincloud SpacetimeDB
make test-loop           # full loop on a throwaway local SpacetimeDB + fake robot, no arm, no maincloud
```

Every Claude call (ticket parse, diagnose, planner, reviewer, LLM QA) goes through one switchable transport,
`rdf_common.llm`. `RDF_LLM_BACKEND=api` (default; the real demo / E2E, and forced in cloud deploys) uses the
Anthropic API; `droid` uses Factory credits via the local droid CLI; `claude_code` uses your local Claude Code
(`claude -p`, no tools) for heavy local testing. Pick it per command (`make test-loop DRIVER=orchestrator
LLM=claude_code`), in `.env`, or live from `/ops/settings` (no restart). Decisions record which backend made them.

Key pages (web on `http://localhost:3737`):

| Page | What |
|---|---|
| `/tickets/new`, `/tickets/[id]` | Create a ticket; live status, before/after evals with Wilson CI, planner decision trail, tasks, payouts, episodes with video, training runs |
| `/tickets/[id]/report` | Printable deployment report: what was bought, why, cost, before vs after |
| `/agents` | Factory Droid sessions, steps and resume-bridge wake-ups |
| `/events` | Append-only event log with the chain-hash seal |
| `/marketplace`, `/join`, `/earnings` | Paid tasks (atomic claim), contributor join QR + push opt-in, test-mode earnings |
| `/teleop/[taskId]`, `/reset/[taskId]` | Phone pages: drive the arm behind the task lease; do a paid reset |
| `/robots/[id]` | Operator console: state, cameras, command queue, soft e-stop (login at `/ops/login`) |

## Phone access (`make demo-public`)

`make demo-public` makes the local demo reachable from phones over HTTPS, without putting the laptop on the venue
Wi-Fi. It runs `make demo` plus a production web build behind one local edge (`scripts/demo/public_guard.py`) and
one cloudflared tunnel. It prints the `/join` link, a terminal QR code and an **access code**, which phones enter once.
`make demo-public-down` stops it, tunnel first.

- **Quick tunnel (default):** no account needed, but you get a new `*.trycloudflare.com` URL on every run.
- **Named tunnel:** a stable `https://app.<domain>`. One-time setup: `cloudflared tunnel login`, then `cloudflared tunnel create rdf-demo`, then `cloudflared tunnel route dns rdf-demo app.<domain>`. After that, run `RDF_PUBLIC_DOMAIN=<domain> make demo-public`.
- **What's protected:**
  - The access code covers pages, the API and the SpacetimeDB socket. It's on by default; set `RDF_PUBLIC_ACCESS_CODE`, or pass `--no-access-code` to turn it off.
  - Privileged actions still need the operator login.
  - SpacetimeDB is reachable only through an allow-list.
  - The orchestrator isn't exposed. Browser GETs go through `/api/orch`, and `--api` adds an upload path, under a secret, for a remote Jetson.
  - There are per-IP rate limits.

Details: [docs/MORNING.md](docs/MORNING.md) ("Phone access") and the `scripts/demo/public.py` docstring.

## Submission docs

- [docs/submission/devpost-draft.md](docs/submission/devpost-draft.md): Devpost write-up (honest about sim vs real; `[TODO after real run]` placeholders).
- [docs/submission/pitch-script.md](docs/submission/pitch-script.md): 3-minute pitch with stage cues, plus Q&A crib.
- [docs/submission/architecture.md](docs/submission/architecture.md): components, data flow, the resume bridge, what is real vs simulated.
- [docs/submission/demo-runbook-draft.md](docs/submission/demo-runbook-draft.md): Sunday-morning sequence, live A/B, judge's phone, fallbacks.
- Also: [CONTEXT.md](CONTEXT.md) (build spec), [docs/decisions.md](docs/decisions.md), [docs/pre-run-checklist.md](docs/pre-run-checklist.md), [docs/morning-questions.md](docs/morning-questions.md), [services/factory/README.md](services/factory/README.md) (Droid client, wait-for protocol, proofs).

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

`make seed` seeds a **local** SpacetimeDB only (it refuses Maincloud). `make deploy` is a stub for now.

## CI (TDM-6)

`make ci` runs exactly what `.github/workflows/ci.yml` runs (both call `scripts/ci/run.sh`): `uv sync` + pytest per Python service, the pnpm workspace typechecks and tests, `tsc --noEmit` for the SpacetimeDB module, a minimal ruff check and `scripts/verify_event_log.py static`. It uses fake settings only (`scripts/ci/fake.env`); live tests stay opt-in via `RDF_LIVE_*`. One step at a time: `make ci-py SERVICE=orchestrator`, `make ci-ts`, `make ci-lint`. Tests that need the `spacetime` CLI run in the optional `make ci-integration`.
