from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from alert_hub.models import event_to_response_payload

router = APIRouter()


@router.post("/api/v1/events")
async def ingest_event(request: Request) -> JSONResponse:
    service = request.app.state.service
    raw_body = await request.body()
    result, prepared = service.handle_ingest(
        headers=request.headers,
        content_type=request.headers.get("content-type"),
        client_ip=request.client.host if request.client else None,
        raw_body=raw_body,
    )
    return JSONResponse(status_code=result.http_status, content=event_to_response_payload(result, prepared))


@router.get("/healthz")
def healthz(request: Request) -> dict[str, str]:
    service = request.app.state.service
    service.ping()
    return {"status": "ok"}
