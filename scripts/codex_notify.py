#!/usr/bin/env python3
from __future__ import annotations

import sys
from typing import Any

from codex_alert_common import (
    build_body,
    canonical_json,
    extract_thread_id,
    first_line,
    is_subagent_thread,
    load_json_argument,
    load_runtime_config,
    payload_has_subagent_marker,
    send_payload,
    stable_event_id,
)

EVENT_TYPE = "codex_job_completed"
STATUS_TAG = "codex-status-completed"


def prompt_from_payload(payload: dict[str, Any]) -> str:
    inputs = payload.get("input-messages") or payload.get("input_messages") or []
    if not isinstance(inputs, list) or not inputs:
        return ""
    latest = inputs[-1]
    return first_line(latest) if isinstance(latest, str) else ""


def build_payload(config, payload: dict[str, Any]) -> dict[str, Any]:
    thread_id = extract_thread_id(payload)
    prompt = prompt_from_payload(payload)
    cwd = str(payload.get("cwd", "")).strip()
    body = build_body(
        f"prompt: {prompt}" if prompt else "",
        f"cwd: {cwd}" if cwd else "",
    )
    metadata = {
        "codex_status": "completed",
        "codex_notify_type": payload.get("type", ""),
        "thread_id": thread_id,
        "cwd": cwd,
        "prompt": prompt,
    }
    return {
        "event_id": stable_event_id("codex-completed", canonical_json(payload)),
        "source": config.source,
        "event_type": EVENT_TYPE,
        "severity": "info",
        "summary": "Codex task completed",
        "body": body,
        "metadata": {key: value for key, value in metadata.items() if value not in ("", None)},
        "tags": [STATUS_TAG],
    }


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    if not args:
        return 0

    payload = load_json_argument(args[0])
    if payload.get("type") != "agent-turn-complete":
        return 0

    thread_id = extract_thread_id(payload)
    if payload_has_subagent_marker(payload) or is_subagent_thread(thread_id):
        return 0

    try:
        config = load_runtime_config()
        send_payload(config, build_payload(config, payload))
    except Exception as exc:
        print(f"codex_notify: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
