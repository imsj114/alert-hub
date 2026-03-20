#!/usr/bin/env python3
from __future__ import annotations

import sys
from typing import Any

from codex_alert_common import (
    build_completion_event,
    extract_thread_id,
    is_subagent_thread,
    load_json_argument,
    load_runtime_config,
    payload_has_subagent_marker,
    send_payload,
)

COMPLETION_TYPES = {
    "agent-turn-complete",
    "agent_turn_complete",
    "assistant-turn-complete",
    "assistant_turn_complete",
    "task_complete",
    "turn-complete",
    "turn_complete",
}


def completion_payload(raw: dict[str, Any]) -> dict[str, Any]:
    payload_type = str(raw.get("type", "")).strip()
    if payload_type in COMPLETION_TYPES:
        return raw

    nested = raw.get("payload")
    if payload_type == "event_msg" and isinstance(nested, dict):
        nested_type = str(nested.get("type", "")).strip()
        if nested_type in COMPLETION_TYPES:
            merged = dict(raw)
            merged.update(nested)
            merged["type"] = nested_type
            return merged

    return {}


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    if not args:
        return 0

    payload = completion_payload(load_json_argument(args[0]))
    if not payload:
        return 0

    thread_id = extract_thread_id(payload)
    if payload_has_subagent_marker(payload) or is_subagent_thread(thread_id):
        return 0

    try:
        config = load_runtime_config()
        send_payload(config, build_completion_event(config, payload))
    except Exception as exc:
        print(f"codex_notify: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
