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

help:
	@echo "make install    pnpm install + uv sync in each Python service"
	@echo "make dev        docker compose up: orchestrator, web, mcp against Maincloud SpacetimeDB"
	@echo "make dev-local  same, plus a local SpacetimeDB container (offline dev)"
	@echo "make dev-minio  same as dev, plus MinIO + episodes bucket"
	@echo "make dev-down   stop every compose service (all profiles)"
	@echo "make dev-host   run every app and service directly on the host (no Docker)"
	@echo "make spacetime-publish  publish spacetime/ to the local SpacetimeDB container"
	@echo "make seed       seed SpacetimeDB with demo data (stub)"
	@echo "make deploy     deploy the backend (stub)"
	@echo "make env-check  report which .env keys are set (never prints values)"
	@echo "make deploy-modal  modal deploy services/training/modal_app.py (app rdf-training, TDM-38)"
	@echo "make modal-secrets create/refresh Modal secret rdf-training from .env (names only printed)"
	@echo "make test-loop  full-loop integration tests: local SpacetimeDB + fake robot, no arm (TDM-28)"
	@echo "make fake-stack boot local SpacetimeDB + fake robot and keep them running (TDM-28)"

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

seed:
	@echo "[seed] placeholder: will seed SpacetimeDB with a demo robot and ticket"

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
