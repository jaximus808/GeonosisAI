# Robot Deployment Factory: top-level dev commands.
# TS apps live under apps/ (pnpm workspace); Python services under services/
# (one uv project each, Python 3.11).

PY_SERVICES := orchestrator factory training robot-client

.PHONY: help install dev dev-host dev-local dev-minio dev-down dev-ts dev-py \
        dev-spacetime spacetime-publish seed deploy env-check \
        $(addprefix dev-,$(PY_SERVICES))

# Local SpacetimeDB (docker compose profile local-spacetime). Same DB name as
# Maincloud so apps need no config change beyond the host.
# Host port 3300, not 3000: 3000 is the usual Node dev port and often taken.
SPACETIME_LOCAL_PORT ?= 3300
export SPACETIME_LOCAL_PORT
SPACETIME_LOCAL_DB ?= robo-factory-a706j
COMPOSE ?= docker compose

# LLM backend for every target (rdf_common.llm): make test-loop LLM=claude_code | LLM=droid | LLM=api.
# Unset = RDF_LLM_BACKEND from the env / .env (default api). Production/cloud deploys are locked to api.
ifdef LLM
export RDF_LLM_BACKEND := $(LLM)
endif

help:
	@echo "make install    pnpm install + uv sync in each Python service"
	@echo "make dev        docker compose up: orchestrator, web, mcp against Maincloud SpacetimeDB"
	@echo "make dev-local  same, plus a local SpacetimeDB container (offline dev)"
	@echo "make dev-minio  same as dev, plus MinIO + episodes bucket"
	@echo "make dev-down   stop every compose service (all profiles)"
	@echo "make dev-host   run every app and service directly on the host (no Docker)"
	@echo "make spacetime-publish  publish spacetime/ to the local SpacetimeDB container"
	@echo "make seed       seed a LOCAL SpacetimeDB with [SEED] demo robot/contributor/ticket (TDM-17; SEED_HOST, SEED_DB)"
	@echo "make deploy     deploy the backend (stub)"
	@echo "make env-check  report which .env keys are set (never prints values)"
	@echo "make deploy-modal  modal deploy services/training/modal_app.py (app rdf-training, TDM-38)"
	@echo "LLM=api|droid|claude_code  pick the Claude transport for any target, e.g. make test-loop DRIVER=orchestrator LLM=claude_code"
	@echo "make modal-secrets create/refresh Modal secret rdf-training from .env (names only printed)"
	@echo "make checkpoints [TICKET=42]  checkpoint registry on rdf-checkpoints (TDM-42); make checkpoint-cleanup = dry run (APPLY=1 deletes)"
	@echo "make download-checkpoint TICKET=42 [WHICH=latest|deployed] [URI=modal-vol://...] [DEST=dir]  local-serving backup (TDM-42)"
	@echo "make policy-up / policy-down / policy-status  warm, cool, inspect the policy server (TDM-41)"
	@echo "make test-loop  full-loop integration tests: local SpacetimeDB + fake robot, no arm (TDM-28)"
	@echo "make fake-stack boot local SpacetimeDB + fake robot and keep them running (TDM-28)"
	@echo "make jetson-bootstrap JETSON_HOST=<ip>  set up the Jetson over ssh (TDM-44; DRY_RUN=1 to preview)"
	@echo "make jetson-deploy / jetson-env / jetson-check / jetson-status / jetson-logs  Jetson code update, env, checks, logs"

install:
	pnpm install
	@for s in $(PY_SERVICES); do echo "uv sync: services/$$s"; (cd services/$$s && uv sync) || exit 1; done

# Docker dev stack (TDM-3, docker-compose.yml). Containers run the per-app
# targets below, so these stay the single definition of "how an app runs".
dev:
	$(COMPOSE) up --build

dev-local:
	SPACETIME_HOST=http://spacetimedb:3000 \
	SPACETIME_PUBLIC_HOST=http://localhost:$(SPACETIME_LOCAL_PORT) \
	SPACETIME_DB_NAME=$(SPACETIME_LOCAL_DB) SPACETIME_TOKEN= \
	$(COMPOSE) --profile local-spacetime up --build

dev-minio:
	$(COMPOSE) --profile minio up --build

dev-down:
	$(COMPOSE) --profile local-spacetime --profile minio down

# Publish the module to the local container (start it first: make dev-local,
# or `docker compose --profile local-spacetime up -d spacetimedb`).
# Maincloud publishing stays manual; see spacetime/README.md.
spacetime-publish:
	cd spacetime && spacetime publish --server http://localhost:$(SPACETIME_LOCAL_PORT) \
	  $(SPACETIME_LOCAL_DB) --delete-data=never --yes=migrate

# Host-only dev (no Docker). Each target runs in parallel under -j.
dev-host:
	@$(MAKE) --no-print-directory -j dev-spacetime dev-ts dev-py

dev-ts:
	@pnpm -r --parallel --reporter=append-only run dev

dev-py: $(addprefix dev-,$(PY_SERVICES))

dev-orchestrator:
	@cd services/orchestrator && uv run --quiet python -m rdf_orchestrator
dev-factory:
	@cd services/factory && uv run --quiet python -m rdf_factory
dev-training:
	@cd services/training && uv run --quiet python -m rdf_training
dev-robot-client:
	@cd services/robot-client && uv run --quiet python -m rdf_robot_client

dev-spacetime:
	@echo "[spacetime] placeholder: SpacetimeDB module lives in spacetime/ (start it per spacetime/ docs)"

# TDM-17: scripts/seed.py. LOCAL only: refuses maincloud (its event_log is the real-run evidence).
# Seeds robot test.seed-yam-office-1, contributor test.seed-contributor and one [SEED] ticket;
# idempotent. Default target is the dev-local container; point it elsewhere with SEED_HOST/SEED_DB.
SEED_HOST ?= http://127.0.0.1:$(SPACETIME_LOCAL_PORT)
SEED_DB ?= $(SPACETIME_LOCAL_DB)
seed:
	cd services/orchestrator && uv run --quiet python ../../scripts/seed.py --host $(SEED_HOST) --db $(SEED_DB) $(ARGS)

deploy:
	@echo "[deploy] placeholder: Fly.io deploy not wired up yet"

env-check:
	@./scripts/check-env.sh

# TDM-28: integration harness. Throwaway SpacetimeDB on 127.0.0.1:$(TDM28_PORT) (data under
# $$TMPDIR/tdm-28), the fake robot (rdf-robot-client --fake) and a stub Modal trainer. No arm, no
# maincloud. DRIVER=orchestrator runs the real orchestrator flywheel instead of the scripted one.
TDM28_PORT ?= 3028
DRIVER ?= scripted
.PHONY: test-loop fake-stack
test-loop:
	cd services/orchestrator && TDM28_PORT=$(TDM28_PORT) RDF_LOOP_DRIVER=$(DRIVER) \
	  uv run --quiet pytest ../../tests/integration -v -p no:cacheprovider

fake-stack:
	cd services/orchestrator && TDM28_PORT=$(TDM28_PORT) uv run --quiet python ../../tests/integration/harness.py up $(ARGS)

# TDM-38: production Modal app (services/training/modal_app.py, app rdf-training).
.PHONY: deploy-modal modal-secrets
deploy-modal:
	cd services/training && uv run --quiet --with 'modal>=1.6.0' modal deploy modal_app.py

modal-secrets:
	cd services/training && uv run --quiet --with 'modal>=1.6.0' python scripts/sync_modal_secret.py

# TDM-42: checkpoint registry / cleanup / download and Modal rates (services/training/scripts/tdm42_checkpoints.py).
# Cleanup keeps base + deployed + policy-server active/previous + latest KEEP (default 2) per ticket; dry run unless APPLY=1.
.PHONY: checkpoints checkpoint-cleanup download-checkpoint modal-rates
TDM42_CLI = cd services/training && uv run --quiet python scripts/tdm42_checkpoints.py
checkpoints:
	$(TDM42_CLI) list $(if $(TICKET),--ticket $(TICKET))
checkpoint-cleanup:
	$(TDM42_CLI) cleanup --keep $(or $(KEEP),2) $(if $(filter 1,$(APPLY)),--apply)
download-checkpoint:
	$(TDM42_CLI) download $(if $(TICKET),--ticket $(TICKET)) --which $(or $(WHICH),latest) $(if $(URI),--uri $(URI)) $(if $(DEST),--dest $(DEST)) $(ARGS)
modal-rates:
	$(TDM42_CLI) rates --refresh

# TDM-41: policy server (rdf-training Policy) warm-up / warm-down. `up` bills one GPU until `down`.
.PHONY: policy-up policy-down policy-status
POLICY_CTL = cd services/training && uv run --quiet --with 'modal>=1.6.0' --with httpx python scripts/policy_ctl.py
policy-up:
	$(POLICY_CTL) up $(ARGS)
policy-down:
	$(POLICY_CTL) down
policy-status:
	$(POLICY_CTL) status

# TDM-6: CI. One definition in scripts/ci/run.sh, used by .github/workflows/ci.yml and these targets.
# Fake env only (scripts/ci/fake.env), live/maincloud tests skip, the spacetime CLI is hidden from unit tests.
#   make ci                      every unit step, keeps going, prints a PASS/FAIL summary
#   make ci-py SERVICE=orchestrator   one Python service (uv sync + pytest)
#   make ci-ts / ci-stdb / ci-lint / ci-evlog
#   make ci-integration          optional: TDM-28 harness + TDM-48/49 tests on a local SpacetimeDB
.PHONY: ci ci-py ci-ts ci-stdb ci-lint ci-evlog ci-integration
ci:
	@scripts/ci/run.sh all
ci-py:
	@scripts/ci/run.sh $(if $(SERVICE),py $(SERVICE),py-all)
ci-ts ci-stdb ci-lint ci-evlog ci-integration:
	@scripts/ci/run.sh $(patsubst ci-%,%,$@)

# TDM-44: Jetson bootstrap / deploy over ssh (scripts/jetson/jetson.sh, checklist
# services/robot-client/docs/jetson-bootstrap-checklist.md). JETSON_HOST/JETSON_USER/JETSON_PORT come
# from the command line or .env. DRY_RUN=1 changes nothing (fully offline plan when no host is set).
#   make jetson-bootstrap JETSON_HOST=10.0.0.42 JETSON_USER=nvidia
#   make jetson-deploy [JETSON_SERVICE_MODE=auto|yam|sim|off] [ARGS=--restart]
#   make jetson-check [ARGS=--no-policy]   make jetson-logs [FOLLOW=0 LINES=500]
.PHONY: jetson-bootstrap jetson-plan jetson-deploy jetson-env jetson-check jetson-status jetson-logs jetson-ssh
JETSON_SH = JETSON_HOST="$(JETSON_HOST)" JETSON_USER="$(JETSON_USER)" JETSON_PORT="$(JETSON_PORT)" \
  JETSON_SRC="$(JETSON_SRC)" JETSON_SERVICE_MODE="$(JETSON_SERVICE_MODE)" RDF_DRY_RUN="$(DRY_RUN)" \
  ./scripts/jetson/jetson.sh
jetson-bootstrap:
	@$(JETSON_SH) bootstrap $(ARGS)
jetson-plan:
	@$(JETSON_SH) plan $(ARGS)
jetson-deploy:
	@$(JETSON_SH) deploy $(ARGS)
jetson-env:
	@$(JETSON_SH) env $(ARGS)
jetson-check:
	@$(JETSON_SH) check $(ARGS)
jetson-status:
	@$(JETSON_SH) status
jetson-logs:
	@$(JETSON_SH) logs
jetson-ssh:
	@$(JETSON_SH) ssh
