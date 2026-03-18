from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from typing import Mapping

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

from alert_hub.auth import AuthError, extract_verified_headers, verify_client_ip, verify_signature
from alert_hub.config import AppConfig, NtfyTargetConfig
from alert_hub.db import Database
from alert_hub.models import IncomingEvent, IngestResult, PreparedEvent
from alert_hub.notifiers.ntfy import NtfyNotifier
from alert_hub.routing import resolve_targets
from alert_hub.time_utils import utc_now

logger = logging.getLogger(__name__)


class AlertHubService:
    def __init__(
        self,
        config: AppConfig,
        *,
        db: Database | None = None,
        http_client: httpx.Client | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self.db = db or Database(config.server.database_path)
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.Client(timeout=10.0)
        self._notifier = NtfyNotifier(self._http_client)
        self._rng = rng or random.Random()

    def initialize(self) -> None:
        self.db.initialize()

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def ping(self) -> None:
        self.db.ping()

    def handle_ingest(
        self,
        *,
        headers: Mapping[str, str],
        content_type: str | None,
        client_ip: str | None,
        raw_body: bytes,
    ) -> tuple[IngestResult, PreparedEvent]:
        if not content_type or not content_type.lower().startswith("application/json"):
            raise HTTPException(status_code=415, detail="content type must be application/json")

        normalized_headers = {key.lower(): value for key, value in headers.items()}
        try:
            verified_headers = extract_verified_headers(normalized_headers)
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

        sender = self.config.senders.get(verified_headers.sender_id)
        if sender is None:
            raise HTTPException(status_code=401, detail="unknown sender")

        now = utc_now()
        verify_client_ip_or_raise(client_ip, sender.allowed_networks)

        if abs(int(now.timestamp()) - verified_headers.timestamp) > self.config.security.timestamp_skew_seconds:
            raise HTTPException(status_code=401, detail="request timestamp is outside the allowed skew window")

        if not verify_signature(sender.secret, verified_headers.timestamp, raw_body, verified_headers.signature):
            raise HTTPException(status_code=401, detail="request signature is invalid")

        if not self.db.record_signature(
            verified_headers.sender_id,
            verified_headers.signature,
            now,
            self.config.security.replay_window_seconds,
        ):
            raise HTTPException(status_code=401, detail="request signature was already seen")

        try:
            payload = IncomingEvent.model_validate_json(raw_body)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc

        prepared_event = PreparedEvent.from_incoming(verified_headers.sender_id, payload, now)
        target_ids = resolve_targets(self.config, prepared_event)
        result = self.db.ingest_event(prepared_event, target_ids, self.config.dedupe.window_seconds)
        return result, prepared_event

    def process_due_deliveries_once(self, now: datetime | None = None, limit: int = 20) -> None:
        attempt_time = now or utc_now()
        jobs = self.db.fetch_due_deliveries(attempt_time, limit=limit)
        for job in jobs:
            target = self.config.targets.get(job.target_id)
            if target is None:
                self.db.mark_delivery_dead(job.delivery_id, attempt_time, "target is no longer configured")
                continue

            result = self._notifier.send(job, target)
            if result.delivered:
                self.db.mark_delivery_delivered(job.delivery_id, attempt_time)
                continue

            error = result.error or "delivery failed"
            next_attempt_number = job.attempts + 1
            if not result.retryable or next_attempt_number >= self.config.worker.max_attempts:
                self.db.mark_delivery_dead(job.delivery_id, attempt_time, error)
                continue

            delay_seconds = self._compute_backoff_seconds(next_attempt_number)
            self.db.reschedule_delivery(
                job.delivery_id,
                attempt_time,
                attempt_time + timedelta(seconds=delay_seconds),
                error,
            )

    def run_worker_loop(self, stop_event) -> None:
        poll_interval = self.config.worker.poll_interval_seconds
        while not stop_event.is_set():
            try:
                self.process_due_deliveries_once()
            except Exception:
                logger.exception("worker loop failed")
            stop_event.wait(poll_interval)

    def _compute_backoff_seconds(self, attempt_number: int) -> int:
        base = self.config.worker.base_backoff_seconds
        cap = self.config.worker.max_backoff_seconds
        delay = min(base * (2 ** max(attempt_number - 1, 0)), cap)
        jitter = self._rng.uniform(0.85, 1.15)
        return max(1, int(delay * jitter))


def verify_client_ip_or_raise(client_ip: str | None, allowed_networks) -> None:
    try:
        verify_client_ip(client_ip, allowed_networks)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
