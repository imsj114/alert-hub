from __future__ import annotations

import httpx
from fastapi import FastAPI
from contextlib import asynccontextmanager

from alert_hub.api import router
from alert_hub.config import AppConfig, load_config
from alert_hub.service import AlertHubService
from alert_hub.worker import start_worker


def create_app(
    config: AppConfig | None = None,
    *,
    http_client: httpx.Client | None = None,
    enable_worker: bool = True,
) -> FastAPI:
    resolved_config = config or load_config()
    service = AlertHubService(resolved_config, http_client=http_client)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service.initialize()
        app.state.service = service
        worker_handle = start_worker(service) if enable_worker else None
        try:
            yield
        finally:
            if worker_handle is not None:
                worker_handle.stop()
            service.close()

    app = FastAPI(title="Alert Hub", lifespan=lifespan)
    app.include_router(router)
    return app
