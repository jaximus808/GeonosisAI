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

    @app.get("/healthz")
    def healthz(request: Request) -> dict:
        """Subscription state, last event, handler error counts. Always 200: the bus reconnects on
        its own, so a SpacetimeDB outage should not get the machine restarted. Read ``status``."""
        return request.app.state.bus.health()

    return app


def main(host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn

    configure_logging()
    uvicorn.run(create_app(), host=host, port=port, log_config=None)
