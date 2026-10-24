# Robot Deployment Factory: top-level dev commands.
# TS apps live under apps/ (pnpm workspace); Python services under services/
# (one uv project each, Python 3.11).

PY_SERVICES := orchestrator factory training robot-client

.PHONY: help install dev dev-ts dev-py dev-spacetime seed deploy env-check \
        $(addprefix dev-,$(PY_SERVICES))

help:
	@echo "make install    pnpm install + uv sync in each Python service"
	@echo "make dev        start every app and service (placeholders for now)"
	@echo "make seed       seed SpacetimeDB with demo data (stub)"
	@echo "make deploy     deploy the backend (stub)"
	@echo "make env-check  report which .env keys are set (never prints values)"

install:
	pnpm install
	@for s in $(PY_SERVICES); do echo "uv sync: services/$$s"; (cd services/$$s && uv sync) || exit 1; done

# Each target runs in parallel under -j so long-running servers can coexist later.
dev:
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
