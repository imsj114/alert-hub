#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any

from codex_alert_common import load_runtime_config, send_payload

SERVER_NAME = "alert_hub"
SERVER_VERSION = "0.1.0"
MANUAL_STATUS_TAG = "codex-status-manual"
OUTPUT_MODE = "content-length"


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alert Hub MCP server for Codex")
    parser.add_argument("--env-file", default=None, help="Path to codex_alert_hub.env")
    return parser.parse_args()


def read_message() -> dict[str, Any] | None:
    global OUTPUT_MODE
    first_line = sys.stdin.buffer.readline()
    if not first_line:
        return None

    stripped = first_line.strip()
    if not stripped:
        return None

    if stripped.startswith(b"{"):
        OUTPUT_MODE = "line"
        return json.loads(stripped.decode("utf-8"))

    headers: dict[str, str] = {}
    OUTPUT_MODE = "content-length"
    decoded = first_line.decode("utf-8").strip()
    if ":" in decoded:
        key, value = decoded.split(":", 1)
        headers[key.lower()] = value.strip()

    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        decoded = line.decode("utf-8").strip()
        if ":" not in decoded:
            continue
        key, value = decoded.split(":", 1)
        headers[key.lower()] = value.strip()

    content_length = int(headers.get("content-length", "0"))
    if content_length <= 0:
        return None
    body = sys.stdin.buffer.read(content_length)
    return json.loads(body.decode("utf-8"))


def write_message(payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if OUTPUT_MODE == "line":
        sys.stdout.buffer.write(raw + b"\n")
    else:
        sys.stdout.buffer.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("utf-8"))
        sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def send_response(message_id: Any, *, result: dict[str, Any] | None = None, error: JsonRpcError | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        payload["error"] = {"code": error.code, "message": error.message}
    else:
        payload["result"] = result or {}
    write_message(payload)


def tool_schema() -> dict[str, Any]:
    return {
        "name": "send_alert",
        "description": "Send a manual alert through Alert Hub.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "minLength": 1},
                "severity": {"type": "string", "enum": ["info", "warning", "error", "critical"]},
                "body": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "event_type": {"type": "string"},
                "links": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "minLength": 1},
                            "label": {"type": "string"},
                        },
                        "required": ["url"],
                    },
                },
                "metadata": {"type": "object"},
            },
            "required": ["summary"],
        },
    }


def coerce_links(raw: Any) -> list[dict[str, str]]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise JsonRpcError(-32602, "links must be a list")
    links: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise JsonRpcError(-32602, "each link must be an object")
        url = str(item.get("url", "")).strip()
        if not url:
            raise JsonRpcError(-32602, "link.url is required")
        link: dict[str, str] = {"url": url}
        label = str(item.get("label", "")).strip()
        if label:
            link["label"] = label
        links.append(link)
    return links


def validate_tool_args(args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise JsonRpcError(-32602, "tool arguments must be an object")
    summary = str(args.get("summary", "")).strip()
    if not summary:
        raise JsonRpcError(-32602, "summary is required")
    severity = str(args.get("severity", "info")).strip() or "info"
    if severity not in {"info", "warning", "error", "critical"}:
        raise JsonRpcError(-32602, "severity must be one of info, warning, error, critical")
    body = str(args.get("body", "")).strip()
    event_type = str(args.get("event_type", "codex_manual_alert")).strip() or "codex_manual_alert"
    metadata = args.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise JsonRpcError(-32602, "metadata must be an object")
    raw_tags = args.get("tags") or []
    if not isinstance(raw_tags, list):
        raise JsonRpcError(-32602, "tags must be a list")
    tags = []
    for tag in raw_tags:
        normalized = str(tag).strip()
        if normalized:
            tags.append(normalized)
    tags.append(MANUAL_STATUS_TAG)
    return {
        "summary": summary,
        "severity": severity,
        "body": body,
        "event_type": event_type,
        "metadata": metadata,
        "tags": list(dict.fromkeys(tags)),
        "links": coerce_links(args.get("links")),
    }


def send_alert(config, args: dict[str, Any]) -> dict[str, Any]:
    payload = validate_tool_args(args)
    payload.update(
        {
            "event_id": f"codex-manual-{int(time.time())}-{uuid.uuid4().hex[:12]}",
            "source": config.source,
        }
    )
    response = send_payload(config, payload)
    return {
        "event_id": payload["event_id"],
        "sender_id": config.sender,
        "status_code": response.get("status_code"),
        "response": response,
    }


def handle_request(config, message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {}
    if method == "initialize":
        requested_version = "2024-11-05"
        params = message.get("params")
        if isinstance(params, dict):
            candidate = params.get("protocolVersion")
            if isinstance(candidate, str) and candidate.strip():
                requested_version = candidate.strip()
        return {
            "protocolVersion": requested_version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "tools/list":
        return {"tools": [tool_schema()]}
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict):
            raise JsonRpcError(-32602, "missing tools/call params")
        if params.get("name") != "send_alert":
            raise JsonRpcError(-32601, "unknown tool")
        result = send_alert(config, params.get("arguments") or {})
        return {
            "content": [{"type": "text", "text": json.dumps(result, sort_keys=True)}],
            "structuredContent": result,
        }
    raise JsonRpcError(-32601, f"method not found: {method}")


def main() -> int:
    args = parse_args()
    config = load_runtime_config(args.env_file)
    while True:
        message = read_message()
        if message is None:
            return 0
        message_id = message.get("id")
        try:
            result = handle_request(config, message)
        except JsonRpcError as exc:
            if message_id is not None:
                send_response(message_id, error=exc)
            continue
        except Exception as exc:
            if message_id is not None:
                send_response(message_id, error=JsonRpcError(-32000, str(exc)))
            continue
        if message_id is not None and result is not None:
            send_response(message_id, result=result)


if __name__ == "__main__":
    raise SystemExit(main())
