from __future__ import annotations

from fnmatch import fnmatch

from alert_hub.config import AppConfig, RouteMatch
from alert_hub.models import PreparedEvent


def _match_rule(route_match: RouteMatch, event: PreparedEvent) -> bool:
    if route_match.sender_ids and event.sender_id not in route_match.sender_ids:
        return False
    if route_match.source_globs and not any(fnmatch(event.source, pattern) for pattern in route_match.source_globs):
        return False
    if route_match.event_types and event.event_type not in route_match.event_types:
        return False
    if route_match.severities and event.severity.value not in route_match.severities:
        return False
    return True


def resolve_targets(config: AppConfig, event: PreparedEvent) -> tuple[str, ...]:
    for rule in config.routes.rules:
        if _match_rule(rule.match, event):
            return tuple(dict.fromkeys(rule.targets))
    return tuple(dict.fromkeys(config.routes.default_targets))
