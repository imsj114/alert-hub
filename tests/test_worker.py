from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from alert_hub.main import create_app
from alert_hub.time_utils import utc_now
from tests.conftest import build_config, sample_payload, signed_request


def _delivery_row(database_path: str):
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT status, attempts, next_attempt_at, last_error, delivered_at FROM deliveries ORDER BY id ASC LIMIT 1"
        ).fetchone()


def test_worker_sends_ntfy_notification_and_marks_delivered(tmp_path: Path) -> None:
    recorded = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, text="ok")

    app = create_app(
        build_config(tmp_path),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        enable_worker=False,
    )

    with TestClient(app) as client:
        headers, raw_body = signed_request(sample_payload(event_id="evt-worker-success"))
        ingest = client.post("/api/v1/events", content=raw_body, headers=headers)
        assert ingest.status_code == 202

        client.app.state.service.process_due_deliveries_once(now=utc_now())
        row = _delivery_row(client.app.state.service.config.server.database_path)

    assert row["status"] == "delivered"
    assert row["attempts"] == 1
    assert recorded[0].headers["Title"] == "[WARNING] Disk usage high"
    assert recorded[0].headers["Priority"] == "4"
    assert "alert-hub" in recorded[0].headers["Tags"]
    assert recorded[0].headers["Authorization"] == "Bearer token-123"
    assert recorded[0].headers["Click"] == "https://internal.example/host/prod-db-01"
    assert recorded[0].url.path == "/personal"


def test_worker_retries_retryable_failures(tmp_path: Path) -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(503, text="temporary failure")
        return httpx.Response(200, text="ok")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    app = create_app(build_config(tmp_path), http_client=http_client, enable_worker=False)

    with TestClient(app) as client:
        headers, raw_body = signed_request(sample_payload(event_id="evt-worker-retry"))
        assert client.post("/api/v1/events", content=raw_body, headers=headers).status_code == 202

        now = utc_now()
        service = client.app.state.service
        service.process_due_deliveries_once(now=now)

        first_row = _delivery_row(service.config.server.database_path)
        assert first_row["status"] == "pending"
        assert first_row["attempts"] == 1
        assert "503" in first_row["last_error"]

        service.process_due_deliveries_once(now=now + timedelta(hours=1))
        final_row = _delivery_row(service.config.server.database_path)

    assert final_row["status"] == "delivered"
    assert final_row["attempts"] == 2


def test_worker_marks_non_retryable_failures_dead(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    app = create_app(build_config(tmp_path), http_client=http_client, enable_worker=False)

    with TestClient(app) as client:
        headers, raw_body = signed_request(sample_payload(event_id="evt-worker-dead"))
        assert client.post("/api/v1/events", content=raw_body, headers=headers).status_code == 202

        client.app.state.service.process_due_deliveries_once(now=utc_now())
        row = _delivery_row(client.app.state.service.config.server.database_path)

    assert row["status"] == "dead"
    assert row["attempts"] == 1
    assert "400" in row["last_error"]
