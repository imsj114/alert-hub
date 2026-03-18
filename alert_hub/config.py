from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


@dataclass(frozen=True)
class SenderConfig:
    id: str
    secret: str
    allowed_networks: tuple[ipaddress._BaseNetwork, ...]


@dataclass(frozen=True)
class NtfyTargetConfig:
    id: str
    type: Literal["ntfy"]
    base_url: str
    topic: str
    token: str | None
    tags: tuple[str, ...]


@dataclass(frozen=True)
class RouteMatch:
    sender_ids: tuple[str, ...]
    source_globs: tuple[str, ...]
    event_types: tuple[str, ...]
    severities: tuple[str, ...]


@dataclass(frozen=True)
class RouteRule:
    match: RouteMatch
    targets: tuple[str, ...]


@dataclass(frozen=True)
class RoutesConfig:
    default_targets: tuple[str, ...]
    rules: tuple[RouteRule, ...]


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    database_path: str


@dataclass(frozen=True)
class SecurityConfig:
    timestamp_skew_seconds: int
    replay_window_seconds: int


@dataclass(frozen=True)
class DedupeConfig:
    window_seconds: int


@dataclass(frozen=True)
class WorkerConfig:
    poll_interval_seconds: int
    max_attempts: int
    base_backoff_seconds: int
    max_backoff_seconds: int


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    security: SecurityConfig
    dedupe: DedupeConfig
    worker: WorkerConfig
    senders: dict[str, SenderConfig]
    targets: dict[str, NtfyTargetConfig]
    routes: RoutesConfig


class RawServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    database_path: str = "./data/alert_hub.db"


class RawSecurityConfig(BaseModel):
    timestamp_skew_seconds: int = 300
    replay_window_seconds: int = 300


class RawDedupeConfig(BaseModel):
    window_seconds: int = 900


class RawWorkerConfig(BaseModel):
    poll_interval_seconds: int = 5
    max_attempts: int = 6
    base_backoff_seconds: int = 30
    max_backoff_seconds: int = 3600


class RawSenderConfig(BaseModel):
    id: str
    secret_env: str
    allowed_cidrs: list[str] = Field(default_factory=list)


class RawNtfyTargetConfig(BaseModel):
    id: str
    type: Literal["ntfy"]
    base_url: str
    topic: str
    token_env: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("base_url", "topic")
    @classmethod
    def strip_required_fields(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped


class RawRouteMatch(BaseModel):
    sender_ids: list[str] = Field(default_factory=list)
    source_globs: list[str] = Field(default_factory=list)
    event_types: list[str] = Field(default_factory=list)
    severities: list[str] = Field(default_factory=list)


class RawRouteRule(BaseModel):
    match: RawRouteMatch
    targets: list[str]


class RawDefaultRoute(BaseModel):
    targets: list[str]


class RawRoutesConfig(BaseModel):
    default: RawDefaultRoute
    rules: list[RawRouteRule] = Field(default_factory=list)


class RawAppConfig(BaseModel):
    server: RawServerConfig = Field(default_factory=RawServerConfig)
    security: RawSecurityConfig = Field(default_factory=RawSecurityConfig)
    dedupe: RawDedupeConfig = Field(default_factory=RawDedupeConfig)
    worker: RawWorkerConfig = Field(default_factory=RawWorkerConfig)
    senders: list[RawSenderConfig]
    targets: list[RawNtfyTargetConfig]
    routes: RawRoutesConfig

    @model_validator(mode="after")
    def ensure_collections_are_present(self) -> "RawAppConfig":
        if not self.senders:
            raise ValueError("at least one sender is required")
        if not self.targets:
            raise ValueError("at least one target is required")
        if not self.routes.default.targets:
            raise ValueError("routes.default.targets must not be empty")
        return self


def _load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"missing required environment variable: {name}")
    return value


def _parse_networks(values: list[str]) -> tuple[ipaddress._BaseNetwork, ...]:
    networks = []
    for value in values:
        networks.append(ipaddress.ip_network(value, strict=False))
    return tuple(networks)


def load_config(config_path: str | os.PathLike[str] | None = None, env_path: str | os.PathLike[str] | None = None) -> AppConfig:
    env_file = Path(env_path or os.getenv("ALERT_HUB_ENV", ".env"))
    _load_dotenv_file(env_file)

    resolved_config_path = Path(config_path or os.getenv("ALERT_HUB_CONFIG", "config/config.yaml"))
    if not resolved_config_path.exists():
        raise FileNotFoundError(f"config file not found: {resolved_config_path}")

    raw_data = yaml.safe_load(resolved_config_path.read_text(encoding="utf-8")) or {}
    raw = RawAppConfig.model_validate(raw_data)

    senders = {
        sender.id: SenderConfig(
            id=sender.id,
            secret=_require_env(sender.secret_env),
            allowed_networks=_parse_networks(sender.allowed_cidrs),
        )
        for sender in raw.senders
    }
    targets = {
        target.id: NtfyTargetConfig(
            id=target.id,
            type=target.type,
            base_url=target.base_url.rstrip("/"),
            topic=target.topic,
            token=os.environ.get(target.token_env, "").strip() if target.token_env else None,
            tags=tuple(tag.strip() for tag in target.tags if tag.strip()),
        )
        for target in raw.targets
    }

    referenced_targets = set(raw.routes.default.targets)
    for rule in raw.routes.rules:
        referenced_targets.update(rule.targets)
    unknown_targets = sorted(target_id for target_id in referenced_targets if target_id not in targets)
    if unknown_targets:
        raise ValueError(f"routes reference unknown targets: {', '.join(unknown_targets)}")

    rules = tuple(
        RouteRule(
            match=RouteMatch(
                sender_ids=tuple(rule.match.sender_ids),
                source_globs=tuple(rule.match.source_globs),
                event_types=tuple(rule.match.event_types),
                severities=tuple(rule.match.severities),
            ),
            targets=tuple(rule.targets),
        )
        for rule in raw.routes.rules
    )

    return AppConfig(
        server=ServerConfig(
            host=raw.server.host,
            port=raw.server.port,
            database_path=raw.server.database_path,
        ),
        security=SecurityConfig(
            timestamp_skew_seconds=raw.security.timestamp_skew_seconds,
            replay_window_seconds=raw.security.replay_window_seconds,
        ),
        dedupe=DedupeConfig(window_seconds=raw.dedupe.window_seconds),
        worker=WorkerConfig(
            poll_interval_seconds=raw.worker.poll_interval_seconds,
            max_attempts=raw.worker.max_attempts,
            base_backoff_seconds=raw.worker.base_backoff_seconds,
            max_backoff_seconds=raw.worker.max_backoff_seconds,
        ),
        senders=senders,
        targets=targets,
        routes=RoutesConfig(
            default_targets=tuple(raw.routes.default.targets),
            rules=rules,
        ),
    )
