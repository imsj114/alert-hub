from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from alert_hub.auth import compute_signature
from alert_hub.config import (
    AppConfig,
    DedupeConfig,
    NtfyTargetConfig,
    RouteMatch,
    RouteRule,
    RoutesConfig,
    SecurityConfig,
    SenderConfig,
    ServerConfig,
    WorkerConfig,
)
from alert_hub.main import create_app
from alert_hub.time_utils import format_utc, utc_now


def build_config(tmp_path: Path, *, allowed_networks: tuple = ()) -> AppConfig:
    return AppConfig(
        server=ServerConfig(host="127.0.0.1", port=8000, database_path=str(tmp_path / "alert_hub.db")),
        security=SecurityConfig(timestamp_skew_seconds=300, replay_window_seconds=300),
        dedupe=DedupeConfig(window_seconds=900),
        worker=WorkerConfig(
            poll_interval_seconds=5,
            max_attempts=6,
            base_backoff_seconds=30,
            max_backoff_seconds=3600,
        ),
        senders={
            "home-laptop": SenderConfig(id="home-laptop", secret="home-secret", allowed_networks=allowed_networks),
            "prod-monitor": SenderConfig(id="prod-monitor", secret="prod-secret", allowed_networks=()),
        },
        targets={
            "personal-phone": NtfyTargetConfig(
                id="personal-phone",
                type="ntfy",
                base_url="https://ntfy.example",
                topic="personal",
                token="token-123",
                tags=("alert-hub",),
            ),
            "critical-phone": NtfyTargetConfig(
                id="critical-phone",
                type="ntfy",
                base_url="https://ntfy.example",
                topic="critical",
                token=None,
                tags=("critical-route",),
            ),
        },
        routes=RoutesConfig(
            default_targets=("personal-phone",),
            rules=(
                RouteRule(
                    match=RouteMatch(
                        sender_ids=(),
                        source_globs=(),
                        event_types=(),
                        severities=("critical",),
                    ),
                    targets=("critical-phone",),
                ),
            ),
        ),
    )


def signed_request(payload: dict, *, sender_id: str = "home-laptop", secret: str = "home-secret", timestamp: int | None = None):
    raw_body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    timestamp = timestamp or int(time.time())
    headers = {
        "Content-Type": "application/json",
        "X-AlertHub-Sender": sender_id,
        "X-AlertHub-Timestamp": str(timestamp),
        "X-AlertHub-Signature": f"v1={compute_signature(secret, timestamp, raw_body)}",
    }
    return headers, raw_body


def sample_payload(*, event_id: str = "evt-1", severity: str = "warning", summary: str = "Disk usage high") -> dict:
    return {
        "event_id": event_id,
        "source": "prod-db-01",
        "event_type": "disk_space_low",
        "severity": severity,
        "summary": summary,
        "body": "Root filesystem is above threshold",
        "links": [{"url": "https://internal.example/host/prod-db-01", "label": "Host"}],
        "metadata": {"usage_percent": 88},
    }


def db_row(database_path: str, query: str):
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query).fetchone()


@pytest.fixture
def transport_recorder():
    recorded = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        yield client, recorded
    finally:
        client.close()


@pytest.fixture
def app_client(tmp_path: Path, transport_recorder):
    http_client, recorded = transport_recorder
    app = create_app(build_config(tmp_path), http_client=http_client, enable_worker=False)
    with TestClient(app) as client:
        yield client, recorded
