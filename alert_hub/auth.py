from __future__ import annotations

import hashlib
import hmac
import ipaddress
from dataclasses import dataclass


class AuthError(Exception):
    def __init__(self, detail: str, status_code: int = 401) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class VerifiedHeaders:
    sender_id: str
    timestamp: int
    signature: str


def extract_verified_headers(headers: dict[str, str]) -> VerifiedHeaders:
    sender_id = headers.get("x-alerthub-sender", "").strip()
    timestamp_raw = headers.get("x-alerthub-timestamp", "").strip()
    signature = headers.get("x-alerthub-signature", "").strip()

    if not sender_id:
        raise AuthError("missing X-AlertHub-Sender header")
    if not timestamp_raw:
        raise AuthError("missing X-AlertHub-Timestamp header")
    if not signature:
        raise AuthError("missing X-AlertHub-Signature header")
    try:
        timestamp = int(timestamp_raw)
    except ValueError as exc:
        raise AuthError("X-AlertHub-Timestamp must be a Unix epoch integer") from exc

    return VerifiedHeaders(sender_id=sender_id, timestamp=timestamp, signature=signature)


def verify_client_ip(client_ip: str | None, allowed_networks: tuple[ipaddress._BaseNetwork, ...]) -> None:
    if not allowed_networks:
        return
    if not client_ip:
        raise AuthError("client IP not available for sender allowlist check", status_code=403)
    try:
        client_address = ipaddress.ip_address(client_ip)
    except ValueError as exc:
        raise AuthError("client IP address is invalid", status_code=403) from exc
    if not any(client_address in network for network in allowed_networks):
        raise AuthError("client IP is not allowed for this sender", status_code=403)


def compute_signature(secret: str, timestamp: int, raw_body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)
    mac.update(str(timestamp).encode("utf-8"))
    mac.update(b".")
    mac.update(raw_body)
    return mac.hexdigest()


def verify_signature(secret: str, timestamp: int, raw_body: bytes, provided_signature: str) -> bool:
    expected = f"v1={compute_signature(secret, timestamp, raw_body)}"
    return hmac.compare_digest(expected, provided_signature)
