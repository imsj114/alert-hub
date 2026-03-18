from __future__ import annotations

import ipaddress
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from alert_hub.main import create_app
from alert_hub.service import AlertHubService
from alert_hub.time_utils import utc_now
from tests.conftest import build_config, sample_payload, signed_request


def test_healthz_returns_ok(app_client) -> None:
    client, _ = app_client
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ingest_accepts_event_and_creates_delivery(app_client) -> None:
    client, _ = app_client
    headers, raw_body = signed_request(sample_payload())
    response = client.post("/api/v1/events", content=raw_body, headers=headers)

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert response.json()["targets"] == ["personal-phone"]

    service = client.app.state.service
    jobs = service.db.fetch_due_deliveries(utc_now())
    assert len(jobs) == 1
    assert jobs[0].target_id == "personal-phone"


def test_duplicate_event_returns_200(app_client) -> None:
    client, _ = app_client
    payload = sample_payload(event_id="evt-duplicate")
    base_timestamp = int(time.time())

    headers_one, raw_body = signed_request(payload, timestamp=base_timestamp)
    first = client.post("/api/v1/events", content=raw_body, headers=headers_one)
    assert first.status_code == 202

    headers_two, raw_body_two = signed_request(payload, timestamp=base_timestamp + 1)
    second = client.post("/api/v1/events", content=raw_body_two, headers=headers_two)

    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"


def test_conflicting_event_id_returns_409(app_client) -> None:
    client, _ = app_client
    base_timestamp = int(time.time())
    first_payload = sample_payload(event_id="evt-conflict")
    headers_one, raw_body_one = signed_request(first_payload, timestamp=base_timestamp)
    assert client.post("/api/v1/events", content=raw_body_one, headers=headers_one).status_code == 202

    conflicting_payload = sample_payload(event_id="evt-conflict", summary="Different summary")
    headers_two, raw_body_two = signed_request(conflicting_payload, timestamp=base_timestamp + 1)
    response = client.post("/api/v1/events", content=raw_body_two, headers=headers_two)

    assert response.status_code == 409
    assert response.json()["status"] == "conflict"


def test_signature_replay_is_rejected(app_client) -> None:
    client, _ = app_client
    headers, raw_body = signed_request(sample_payload(event_id="evt-replay"), timestamp=int(time.time()))

    first = client.post("/api/v1/events", content=raw_body, headers=headers)
    second = client.post("/api/v1/events", content=raw_body, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 401
    assert second.json()["detail"] == "request signature was already seen"


def test_soft_dedupe_suppresses_repeated_events(app_client) -> None:
    client, _ = app_client
    base_timestamp = int(time.time())
    first_headers, first_body = signed_request(sample_payload(event_id="evt-soft-1"), timestamp=base_timestamp)
    second_headers, second_body = signed_request(sample_payload(event_id="evt-soft-2"), timestamp=base_timestamp + 1)

    first = client.post("/api/v1/events", content=first_body, headers=first_headers)
    second = client.post("/api/v1/events", content=second_body, headers=second_headers)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["status"] == "suppressed"

    service = client.app.state.service
    jobs = service.db.fetch_due_deliveries(utc_now())
    assert len(jobs) == 1


def test_critical_events_route_to_critical_target(app_client) -> None:
    client, _ = app_client
    headers, raw_body = signed_request(sample_payload(event_id="evt-critical", severity="critical"))

    response = client.post("/api/v1/events", content=raw_body, headers=headers)

    assert response.status_code == 202
    assert response.json()["targets"] == ["critical-phone"]


def test_sender_allowlist_is_enforced(tmp_path: Path) -> None:
    config = build_config(tmp_path, allowed_networks=(ipaddress.ip_network("10.0.0.0/24"),))
    service = AlertHubService(config)
    service.initialize()
    headers, raw_body = signed_request(sample_payload(event_id="evt-allowlist"))

    with pytest.raises(HTTPException) as exc_info:
        service.handle_ingest(
            headers=headers,
            content_type="application/json",
            client_ip="192.168.1.22",
            raw_body=raw_body,
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "client IP is not allowed for this sender"


def test_invalid_content_type_returns_415(app_client) -> None:
    client, _ = app_client
    headers, raw_body = signed_request(sample_payload(event_id="evt-content-type"))
    headers["Content-Type"] = "text/plain"
    response = client.post("/api/v1/events", content=raw_body, headers=headers)
    assert response.status_code == 415
