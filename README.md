<div align="center">

# GEONOSISAI

### Plug in your robot, tell it the job, and the factory gets it deployed.

Agents run the whole robot deployment loop: eval, diagnose, buy the right data from people on their phones, fine-tune, re-eval, ship.

[**Live site**](https://geonosis.jaxonp.com) · [Devpost write-up](docs/submission/devpost-paste.md) · [Architecture](docs/submission/architecture.md) · [Pitch](docs/submission/pitch.md)

![Claude](https://img.shields.io/badge/Claude-API-D97757?style=flat-square&logo=anthropic&logoColor=white)
![SpacetimeDB](https://img.shields.io/badge/SpacetimeDB-maincloud-5B3DF5?style=flat-square)
![Factory](https://img.shields.io/badge/Factory-Droids-111111?style=flat-square)
![Modal](https://img.shields.io/badge/Modal-H100-7FEE64?style=flat-square)
![π0.5](https://img.shields.io/badge/%CF%800.5-LoRA-FF6B00?style=flat-square)
![MuJoCo](https://img.shields.io/badge/MuJoCo-sim-2E7D32?style=flat-square)
![Jetson](https://img.shields.io/badge/NVIDIA-Jetson-76B900?style=flat-square&logo=nvidia&logoColor=white)
![Next.js](https://img.shields.io/badge/Next.js-three.js-000000?style=flat-square&logo=nextdotjs&logoColor=white)

<sub>Built at Cal Hacks 13.0</sub>

<br/>

<img src="docs/assets/live.png" alt="The live view: a ticket moving down the line, the robot's cameras, the before/after eval with its Wilson interval, and the planner's decision to buy 5 recovery takeovers" width="100%"/>

<sub>The <code>/live</code> view on the MuJoCo sim YAM: the baseline went 7/10, Claude found one failure cluster ("missed cube grasp"), and the planner decided to buy 5 recovery takeovers.</sub>

</div>

---

## Why

Getting a robot to actually *do* something is painful. You run the policy, watch it fail, guess why, collect more demos, retrain and run the eval again, with a lot of manual steps (and manual mistakes) in between.

Robot data is expensive, and so is the infrastructure to collect it. Data companies aren't set up to collect data for *your* task on *your* robot. But someone with a phone will give you a few good episodes for $5. Scale that out and you have a usable dataset.

So agents should run the loop, and pay people for the one thing agents can't do: touch the physical world.

## How it works

You file a ticket: *"Put the cube in the bin, 80% reliable, $50 budget."* It moves down the line:

| | Station | What happens |
|:-:|---|---|
| 1 | **Baseline** | The robot runs the task on its current policy. Every trial is recorded. |
| 2 | **Diagnose** | Claude groups the failed trials by *why* they failed, from notes, timings and camera frames. |
| 3 | **Plan data** | The planner picks **recovery takeovers** (a person takes over the paused robot mid-failure) or **clean demos**, and how many. It must quote a failure cluster. A human approves the spend. |
| 4 | **Collect** | Paid tasks go out on Discord and by text ("Reply YES"). Contributors claim on their phones, watch the cameras, drive the arm in the browser and hand back. |
| 5 | **Review** | Deterministic checks plus a Claude vision review. Accepted episodes are paid (Stripe test mode). |
| 6 | **Train** | π0.5 LoRA fine-tune on an H100 on Modal, from the accepted episodes in LeRobot format. |
| 7 | **Eval → Ship** | The new checkpoint runs the same eval. It ships with a report if it clears the target. If not, the line buys more data. |

Every rate shows its Wilson 95% interval (at 10/10 the lower bound is only 0.72). Every decision, payout and wake-up lands in an append-only, hash-chained event log.

## Highlights

- **Agents that sleep on the database.** A Factory Droid ends its turn with `WAITING_FOR dataset:accepted_count>=5` and idles for free. A resume bridge re-checks that predicate on every SpacetimeDB row change and wakes the *same* session. In testing, an accepted review woke a parked droid in 597 ms.
- **SpacetimeDB is the bus.** Services, agents, robots and phones all share 23 tables. Reducers keep the risky actions atomic: one arm-control lease per robot, a budget check on every payout, an event log nothing can edit.
- **Real arm and twin sim behind one interface.** The I2RT YAM on an NVIDIA Jetson and a MuJoCo model of the same arm run behind the same safety layer (joint limits, velocity caps, workspace box, watchdog, soft e-stop). Sim and real numbers are never pooled.
- **Remote π0.5 at control rate.** The policy server on Modal hot-swaps checkpoints. The robot runs 50-step action chunks asynchronously and crossfades between them to hide a ~157 ms round trip.
- **Claude everywhere a human used to make a judgment call:** per-ticket 3D scenes, failure clustering, the data plan, episode review and real-arm trial verdicts from the live cameras.
- **Honest labels.** Anything simulated or stubbed says `[SIM]`, `[STUB]` or `[SEEDED]` wherever it shows up.

## Architecture

```mermaid
flowchart LR
  subgraph vercel [Vercel: geonosis.jaxonp.com]
    WEB["Next.js web app<br/>landing, dashboard, /factory, /live,<br/>ticket, report, marketplace, phone pages"]
  end

  subgraph stdb [SpacetimeDB maincloud: the bus]
    DB[("23 tables + reducers<br/>ticket, task, episode, review, payment,<br/>training_run, eval_run, agent_session,<br/>event_log (append-only, chain hash)")]
  end

  subgraph laptop [Laptop backend, behind cloudflared quick tunnels]
    ORCH["Orchestrator (FastAPI)<br/>baseline, diagnose, plan, dispatch,<br/>review, train, eval, ship"]
    LLM["rdf_common.llm<br/>api / droid / claude_code<br/>(production: api only)"]
    BRIDGE["Resume bridge<br/>WAITING_FOR predicate -> wake"]
    DROIDS["Factory Droids<br/>Onboarding, Data, Training"]
    DROPS["Job drops + texts<br/>Discord, Inkbox iMessage"]
    SIM["Robot client<br/>MuJoCo [SIM] YAM + scripted policy"]
  end

  subgraph arm [Office]
    JET["Jetson robot client<br/>real I2RT YAM, cameras, safety layer"]
  end

  subgraph modal [Modal]
    FT["π0.5 LoRA fine-tune (H100)"]
    POL["Policy server<br/>hot-swap checkpoints"]
    VOL[("Volumes: media, datasets,<br/>checkpoints")]
  end

  CLAUDE["Claude API"]
  PHONE["Contributor or judge phone"]
  STRIPE["Stripe Connect (test mode)"]

  WEB <-- "live subscriptions" --> DB
  WEB -- "server routes, HMAC-proven<br/>URL from an event_log row" --> ORCH
  ORCH <-- "subscribe + reducers" --> DB
  ORCH --> LLM --> CLAUDE
  BRIDGE <-- "row changes" --> DB
  BRIDGE -- "resume same session" --> DROIDS
  DROIDS -- "steps, waits" --> DB
  ORCH --> DROPS --> PHONE
  ORCH -- "spawn, poll" --> FT --> VOL
  POL --> VOL
  SIM <-- "commands, trials, scene.deployed" --> DB
  JET <-- "commands, trials, heartbeats" --> DB
  JET -- "action chunks" --> POL
  PHONE -- "claim_task lease" --> DB
  PHONE <-- "cameras + control<br/>(robot tunnel, task token)" --> SIM
  PHONE <-- "cameras + control" --> JET
  ORCH -- "accepted episode -> payout" --> STRIPE
```

- **Discovery.** The backend runs on a laptop behind quick tunnels whose URLs change on every start. `make demo-hybrid` publishes the current URL as an `orchestrator.endpoint` row in SpacetimeDB. Vercel reads it and checks an HMAC proof before trusting it. No backend key is on Vercel.
- **Scenes.** Claude writes a scene spec per ticket. The sim robot hot-swaps to it, and the browser's three.js twin draws the same JSON.

More detail in [docs/submission/architecture.md](docs/submission/architecture.md).

## What's real and what's simulated

| Area | Status |
|---|---|
| SpacetimeDB, orchestrator, Claude diagnosis / planning / review | **Real**, on maincloud with real Claude calls |
| Factory Droids | **Real** Factory sessions, woken by SpacetimeDB writes |
| π0.5 LoRA fine-tune + policy server | **Real** on Modal H100, including a run on public real-YAM data |
| Real I2RT YAM on a Jetson | **Real**: phone teleop, recording, guarded policy rollouts, Claude trial verdicts |
| MuJoCo YAM policy | **[SIM]** scripted IK stand-in, *not* π0.5. Its before/after gap is a designed calibration, not learning. |
| Training in `make demo` | **[STUB]** by default; `--real-modal` runs real H100 training |
| Payments | Stripe Connect **test mode**. No real money moves. |

## Quickstart

Needs Node 20+, pnpm 11, [uv](https://docs.astral.sh/uv/), the `spacetime` CLI and make.

```sh
cp .env.example .env && make install   # fill in keys; `make env-check` shows which are set, never values
make demo                              # the whole factory on this laptop, on the MuJoCo [SIM] YAM
```

Then open **http://localhost:3737**. `make demo` boots a throwaway local SpacetimeDB, the orchestrator, the sim robot, the `[SIM]` policy server and the web app. Nothing touches maincloud. Add `ARGS="--real-modal"` for real H100 training and `LLM=claude_code` to run Claude calls on your local Claude Code login. `make demo-down` stops it.

<details>
<summary><b>Live setup (Vercel + laptop backend + maincloud)</b></summary>

```sh
make demo-maincloud ARGS="--orch-port 8028 --web-port 3737"
make demo-hybrid ARGS="--stack attach --orch-url http://127.0.0.1:8028 --stream-port 8791 \
  --robot-name sim-yam-mujoco --stdb-url https://maincloud.spacetimedb.com --db robo-factory-a706j"
```

Check it with `https://geonosis.jaxonp.com/api/tdm110/backend`, which should return `{"ok": true}`. One-time setup is in [docs/MORNING.md](docs/MORNING.md), "Hybrid deploy".

Other entry points: `make test-loop` (full loop on a fake robot and a throwaway database), `make demo-public` (the whole demo on phones through one tunnel, behind an access code), `make dev` (Docker Compose).

</details>

## Pages

| Page | What |
|---|---|
| `/` | Landing page with live numbers |
| `/factory` | The factory floor: each ticket is a lane of agent stations; droids sleep and wake on database updates |
| `/live` | Projector view: robot, the line, before/after, planner decision, join QR |
| `/tickets/[id]` | Ticket status, 3D scene, evals with Wilson CI, decision trail, tasks, payouts, episodes |
| `/tickets/[id]/report` | Printable report: what was bought, why, cost, before vs after |
| `/marketplace`, `/join`, `/earnings` | Paid tasks, contributor sign-up, test-mode earnings |
| `/teleop/[taskId]`, `/practice` | Drive the arm from a phone; practise on a browser copy first |
| `/agents`, `/events` | Droid sessions and wake-ups; the append-only event log with its chain-hash seal |
| `/dashboard` | Operator dashboard (login at `/ops/login`) |

## Repo layout

| Path | What |
|---|---|
| `spacetime/` | SpacetimeDB module: schema, reducers, subscriptions |
| `apps/web/` | Next.js web app: dashboard, factory floor, phone pages |
| `apps/mcp/` | Claude connector (remote MCP server) |
| `services/orchestrator/` | The line: baseline, diagnose, plan, dispatch, review, train, eval, ship |
| `services/factory/` | Factory Droid sessions, wait-for protocol, resume bridge |
| `services/training/` | LeRobot conversion, π0.5 fine-tune, policy server (Modal) |
| `services/robot-client/` | YAM + MuJoCo adapters, safety layer, cameras, recorder, takeover, evals |
| `services/payments/` | Stripe test-mode payouts |

## Built with

Claude API · Claude Code · Factory Droid SDK · SpacetimeDB · Modal · openpi π0.5 · LeRobot · MuJoCo · I2RT YAM · NVIDIA Jetson · Python · FastAPI · TypeScript · Next.js · React · three.js · Tailwind CSS · Vercel · cloudflared · Stripe Connect · Discord · Inkbox iMessage · MCP

<details>
<summary><b>Developer reference: setup, Docker, CI</b></summary>

### Setup

```sh
cp .env.example .env     # fill in keys; .env is gitignored
make env-check           # shows which keys are set, never their values
make install             # pnpm install + uv sync in each Python service
make dev                 # docker compose up; `make dev-host` runs on the host instead
```

### Docker (`docker-compose.yml`)

| Command | Starts |
|---|---|
| `make dev` | `orchestrator` (:8000), `web` (:3737), `mcp` (:3002), on **Maincloud** via `SPACETIME_HOST` |
| `make dev-local` | the same plus a local `spacetimedb` container (:3300); then `make spacetime-publish` |
| `make dev-minio` | `make dev` plus `minio` (:9000, console :9001) and an `episodes` bucket |
| `make dev-down` | stops everything, all profiles |

Port clashes: set `ORCHESTRATOR_PORT`, `WEB_PORT`, `MCP_PORT`, `SPACETIME_LOCAL_PORT`, `MINIO_PORT` or `MINIO_CONSOLE_PORT`. `make seed` seeds a **local** SpacetimeDB only.

### CI

`make ci` runs what `.github/workflows/ci.yml` runs: pytest per Python service, workspace typechecks and tests, the SpacetimeDB module typecheck, ruff and `scripts/verify_event_log.py static`, with fake settings only. One step at a time: `make ci-py SERVICE=orchestrator`, `make ci-ts`, `make ci-lint`.

### More docs

[CONTEXT.md](CONTEXT.md) (build spec) · [docs/decisions.md](docs/decisions.md) · [docs/MORNING.md](docs/MORNING.md) · [docs/real-arm-safety.md](docs/real-arm-safety.md) · [services/factory/README.md](services/factory/README.md) · [judge Q&A](docs/submission/qa.md)

</details>
