#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

from alert_hub.auth import compute_signature


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a signed event to Alert Hub")
    parser.add_argument("--url", required=True, help="Alert Hub event endpoint URL")
    parser.add_argument("--sender", required=True, help="Configured sender id")
    parser.add_argument("--secret", help="Shared secret for this sender")
    parser.add_argument(
        "--secret-env",
        default="ALERT_HUB_SECRET",
        help="Environment variable containing the sender secret when --secret is omitted",
    )
    parser.add_argument("--payload-file", help="Path to a JSON file containing the event payload")
    parser.add_argument("--event-id", help="Event id when building the payload inline")
    parser.add_argument("--source", help="Event source when building the payload inline")
    parser.add_argument("--event-type", help="Event type when building the payload inline")
    parser.add_argument("--severity", choices=["info", "warning", "error", "critical"], help="Event severity")
    parser.add_argument("--summary", help="Short summary when building the payload inline")
    parser.add_argument("--body", help="Optional event body when building the payload inline")
    parser.add_argument(
        "--link",
        action="append",
        default=[],
        help="Optional link URL. Repeat to send multiple links.",
    )
    return parser.parse_args()


def load_payload(args: argparse.Namespace) -> dict:
    if args.payload_file:
        return json.loads(Path(args.payload_file).read_text(encoding="utf-8"))

    required = ["event_id", "source", "event_type", "severity", "summary"]
    missing = [name for name in required if getattr(args, name) in (None, "")]
    if missing:
        raise SystemExit(f"missing inline payload fields: {', '.join(missing)}")

    payload = {
        "event_id": args.event_id,
        "source": args.source,
        "event_type": args.event_type,
        "severity": args.severity,
        "summary": args.summary,
    }
    if args.body:
        payload["body"] = args.body
    if args.link:
        payload["links"] = [{"url": url} for url in args.link]
    return payload


def resolve_secret(args: argparse.Namespace) -> str:
    if args.secret:
        return args.secret
    value = os.environ.get(args.secret_env, "").strip()
    if not value:
        raise SystemExit(f"missing secret; set --secret or env var {args.secret_env}")
    return value


def main() -> int:
    args = parse_args()
    secret = resolve_secret(args)
    payload = load_payload(args)
    raw_body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    timestamp = int(time.time())
    signature = compute_signature(secret, timestamp, raw_body)
    headers = {
        "Content-Type": "application/json",
        "X-AlertHub-Sender": args.sender,
        "X-AlertHub-Timestamp": str(timestamp),
        "X-AlertHub-Signature": f"v1={signature}",
    }

    with httpx.Client(timeout=10.0) as client:
        response = client.post(args.url, content=raw_body, headers=headers)

    print(f"status={response.status_code}", file=sys.stderr)
    print(response.text)
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
