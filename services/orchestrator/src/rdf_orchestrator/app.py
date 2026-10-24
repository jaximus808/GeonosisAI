"""FastAPI app: owns the SpacetimeDB subscription runtime (``bus.Bus``) for its lifetime.

Run:  uv run rdf-orchestrator            (or: uv run uvicorn rdf_orchestrator.app:create_app --factory)
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from .bus import Bus, BusClient, Registry
from .bus import registry as default_registry
from .log import configure_logging, get_logger

log = get_logger("app")


def _default_client() -> BusClient:
    from rdf_common.settings import OrchestratorSettings, load_settings

    from .stdb import SpacetimeClient

    s = load_settings(OrchestratorSettings, "orchestrator")  # exits naming any missing env var
    return SpacetimeClient(host=s.spacetime_host, db=s.spacetime_db_name, token=s.spacetime_token.get_secret_value(),
                           max_backoff=10.0)  # reconnect within ~10 s of SpacetimeDB coming back


def create_app(*, client: BusClient | None = None, registry: Registry | None = None, workers: int = 8) -> FastAPI:
    if registry is None:
        from . import handlers  # noqa: F401 - registers the handlers on the default registry

        registry = default_registry

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus = Bus(registry, client or _default_client(), workers=workers)
        app.state.bus = bus
        await bus.start()
        try:
            yield
        finally:
            await bus.stop()
            log.info("app.stopped")

    app = FastAPI(title="rdf-orchestrator", lifespan=lifespan)

    from .tickets_api import router as tickets_router  # TDM-19

    app.include_router(tickets_router)
    from .run_settings_api import run_settings_router  # TDM-20
    app.include_router(run_settings_router)
    from .payments_api import router as payments_router  # TDM-63: /stripe/webhook, /payments/*
    app.include_router(payments_router)

    @app.get("/healthz")
    def healthz(request: Request) -> dict:
        """Subscription state, last event, handler error counts. Always 200: the bus reconnects on
        its own, so a SpacetimeDB outage should not get the machine restarted. Read ``status``."""
        return request.app.state.bus.health()

    from .media_api import media_router  # TDM-4: /uploads/sign, /media/sign, /media/list, /media/healthz
    app.include_router(media_router)
    from .report_api import report_router  # TDM-71: GET /tickets/{id}/report(.md), POST .../report/save
    app.include_router(report_router)
    from .stream_api import stream_router  # TDM-55: POST /stream/token (camera stream, claimant only)
    app.include_router(stream_router)
    from .tdm57_control_api import tdm57_control_router  # TDM-57: POST /tasks/{id}/revoke (operator key)
    app.include_router(tdm57_control_router)
    from .flywheel_api import router as flywheel_router  # TDM-22: /tickets/{id}/advance, /flywheel/*
    app.include_router(flywheel_router)
    from .decision_api import decision_router  # TDM-68: GET /tickets/{id}/decision (evidence trail)
    app.include_router(decision_router)
    from .checkpoints_api import checkpoints_router  # TDM-42: GET /checkpoints, /checkpoints/resolve, /checkpoints/cleanup (dry run)
    app.include_router(checkpoints_router)
    from .offers_api import offers_router  # TDM-91: /tickets/{id}/offers, /offers/{id}/{accept,uploads/sign,episodes,finalize}
    app.include_router(offers_router)
    from .agents_api import agents_router  # TDM-35: GET /agents (AgentFeed v1, ?replay=1)
    app.include_router(agents_router)
    from .interventions_api import interventions_router  # TDM-69: GET /tickets/{id}/interventions, POST /robots/{id}/interventions/flag
    app.include_router(interventions_router)
    from .paging_api import paging_router  # TDM-65: /push/config, /push/subscribe, /push/test (web push)
    app.include_router(paging_router)
    from .robot_commands_api import robot_commands_router  # TDM-78: POST /robots/{id}/commands (operator key; soft_estop open)
    app.include_router(robot_commands_router)
    # TDM-68: the dashboard (apps/web, :3737) reads GET routes from the browser (decision trail,
    # /media/sign). GET only, listed origins only (RDF_WEB_ORIGINS adds more, comma-separated).
    import os

    from fastapi.middleware.cors import CORSMiddleware
    origins = ["http://localhost:3737", "http://127.0.0.1:3737"] + [
        o.strip() for o in os.environ.get("RDF_WEB_ORIGINS", "").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET"], allow_headers=["*"])

    return app


def main(host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn

    configure_logging()
    uvicorn.run(create_app(), host=host, port=port, log_config=None)
