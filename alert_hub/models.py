from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, StringConstraints, field_validator

from alert_hub.time_utils import format_utc, parse_rfc3339

ShortText = StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
MediumText = StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
LongText = StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class EventLink(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    label: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("url")
    @classmethod
    def strip_url(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("url must not be empty")
        return stripped


class IncomingEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=200)
    event_type: str = Field(min_length=1, max_length=200)
    severity: Severity
    summary: str = Field(min_length=1, max_length=500)
    body: str | None = Field(default=None, min_length=1, max_length=4000)
    occurred_at: datetime | None = None
    dedupe_key: str | None = Field(default=None, min_length=1, max_length=200)
    links: list[EventLink] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    @field_validator("event_id", "source", "event_type", "summary", "body", "dedupe_key")
    @classmethod
    def strip_text_fields(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @field_validator("occurred_at", mode="before")
    @classmethod
    def parse_occurred_at(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("occurred_at must include a timezone")
            return value
        if isinstance(value, str):
            return parse_rfc3339(value)
        raise ValueError("occurred_at must be a string timestamp")

    @field_validator("metadata")
    @classmethod
    def require_object_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("metadata must be an object")
        return value

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for tag in value:
            stripped = str(tag).strip()
            if not stripped:
                raise ValueError("tags must not contain blank values")
            if len(stripped) > 100:
                raise ValueError("tags must be at most 100 characters")
            normalized.append(stripped)
        return normalized

    def canonical_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload.setdefault("links", [])
        payload.setdefault("metadata", {})
        payload.setdefault("tags", [])
        return payload


@dataclass(frozen=True)
class PreparedEvent:
    sender_id: str
    payload: IncomingEvent
    occurred_at: datetime
    received_at: datetime
    effective_dedupe_key: str
    payload_json: str
    payload_hash: str

    @property
    def event_id(self) -> str:
        return self.payload.event_id

    @property
    def source(self) -> str:
        return self.payload.source

    @property
    def event_type(self) -> str:
        return self.payload.event_type

    @property
    def severity(self) -> Severity:
        return self.payload.severity

    @property
    def summary(self) -> str:
        return self.payload.summary

    @property
    def body(self) -> str | None:
        return self.payload.body

    @property
    def links_json(self) -> str:
        return json.dumps([link.model_dump(mode="json") for link in self.payload.links], separators=(",", ":"))

    @property
    def metadata_json(self) -> str:
        return json.dumps(self.payload.metadata, sort_keys=True, separators=(",", ":"))

    @property
    def tags_json(self) -> str:
        return json.dumps(list(dict.fromkeys(self.payload.tags)), separators=(",", ":"))

    @classmethod
    def from_incoming(cls, sender_id: str, payload: IncomingEvent, received_at: datetime) -> "PreparedEvent":
        occurred_at = payload.occurred_at or received_at
        canonical_payload = payload.canonical_payload()
        payload_json = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        effective_dedupe_key = payload.dedupe_key or hashlib.sha256(
            f"{sender_id}|{payload.source}|{payload.event_type}|{payload.severity.value}|{payload.summary}".encode("utf-8")
        ).hexdigest()
        return cls(
            sender_id=sender_id,
            payload=payload,
            occurred_at=occurred_at,
            received_at=received_at,
            effective_dedupe_key=effective_dedupe_key,
            payload_json=payload_json,
            payload_hash=payload_hash,
        )


class IngestOutcome(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    SUPPRESSED = "suppressed"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class IngestResult:
    outcome: IngestOutcome
    event_db_id: int | None = None
    target_ids: tuple[str, ...] = ()
    message: str | None = None
    existing_status: str | None = None

    @property
    def http_status(self) -> int:
        if self.outcome == IngestOutcome.ACCEPTED:
            return 202
        if self.outcome in {IngestOutcome.DUPLICATE, IngestOutcome.SUPPRESSED}:
            return 200
        return 409


@dataclass(frozen=True)
class DeliveryJob:
    delivery_id: int
    event_db_id: int
    target_id: str
    attempts: int
    sender_id: str
    event_id: str
    source: str
    event_type: str
    severity: Severity
    summary: str
    body: str | None
    links: tuple[dict[str, str | None], ...]
    tags: tuple[str, ...]


class DeliveryState(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    DEAD = "dead"


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    retryable: bool
    error: str | None = None


def event_to_response_payload(result: IngestResult, prepared_event: PreparedEvent) -> dict[str, Any]:
    payload = {
        "status": result.outcome.value,
        "sender_id": prepared_event.sender_id,
        "event_id": prepared_event.event_id,
    }
    if result.target_ids:
        payload["targets"] = list(result.target_ids)
    if result.message:
        payload["message"] = result.message
    return payload
