#!/usr/bin/env python3
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENV_FILE_NAME = "codex_alert_hub.env"
DEFAULT_STATE_FILE = "~/.codex/tmp/codex_attention_watcher_state.json"
DEFAULT_LOG_FILE = "~/.codex/log/codex_attention_watcher.log"
COMPLETION_EVENT_TYPE = "codex_job_completed"
COMPLETION_STATUS_TAG = "codex-status-completed"


@dataclass(frozen=True)
class RuntimeConfig:
    env_file: Path
    url: str
    sender: str
    secret: str
    source: str
    poll_seconds: float
    state_file: Path
    log_file: Path


class SendEventError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.stdout = stdout
        self.stderr = stderr


def default_env_file() -> Path:
    return Path.home() / ".codex" / ENV_FILE_NAME


def parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        data[key.strip()] = value.strip().strip("'").strip('"')
    return data


def load_runtime_config(env_file: str | os.PathLike[str] | None = None) -> RuntimeConfig:
    resolved_env_file = Path(env_file).expanduser() if env_file else default_env_file()
    file_values = parse_env_file(resolved_env_file)
    values = {**file_values, **os.environ}
    url = values.get("ALERT_HUB_CODEX_URL", "").strip()
    sender = values.get("ALERT_HUB_CODEX_SENDER", "").strip()
    secret = values.get("ALERT_HUB_SECRET", "").strip()
    source = values.get("ALERT_HUB_CODEX_SOURCE", "").strip() or sender
    if not url or not sender or not secret:
        raise RuntimeError(
            "missing runtime config; require ALERT_HUB_CODEX_URL, ALERT_HUB_CODEX_SENDER, and ALERT_HUB_SECRET"
        )
    poll_seconds_raw = values.get("ALERT_HUB_CODEX_POLL_SECONDS", "2.0").strip() or "2.0"
    state_file_raw = values.get("ALERT_HUB_CODEX_STATE_FILE", DEFAULT_STATE_FILE)
    log_file_raw = values.get("ALERT_HUB_CODEX_LOG_FILE", DEFAULT_LOG_FILE)
    return RuntimeConfig(
        env_file=resolved_env_file,
        url=url,
        sender=sender,
        secret=secret,
        source=source,
        poll_seconds=float(poll_seconds_raw),
        state_file=Path(state_file_raw).expanduser(),
        log_file=Path(log_file_raw).expanduser(),
    )


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def send_payload(config: RuntimeConfig, payload: dict[str, Any]) -> dict[str, Any]:
    script_path = repo_root() / "scripts" / "send_event.sh"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        payload_path = Path(handle.name)
    try:
        result = subprocess.run(
            [
                "bash",
                str(script_path),
                "--url",
                config.url,
                "--sender",
                config.sender,
                "--secret",
                config.secret,
                "--payload-file",
                str(payload_path),
            ],
            cwd=repo_root(),
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        payload_path.unlink(missing_ok=True)

    status_code = parse_status_code(result.stderr)
    stdout = result.stdout.strip()
    if result.returncode != 0:
        message = stdout or result.stderr.strip() or "send_event.sh failed"
        raise SendEventError(message, status_code=status_code, stdout=result.stdout, stderr=result.stderr)

    if not stdout:
        return {"status_code": status_code, "raw": ""}
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return {"status_code": status_code, "raw": stdout}
    if isinstance(parsed, dict):
        parsed.setdefault("status_code", status_code)
        return parsed
    return {"status_code": status_code, "raw": parsed}


def parse_status_code(stderr: str) -> int | None:
    match = re.search(r"status=(\d+)", stderr)
    if not match:
        return None
    return int(match.group(1))


def stable_event_id(prefix: str, raw: str) -> str:
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def first_line(text: str, limit: int = 280) -> str:
    line = text.splitlines()[0].strip() if text else ""
    return line[:limit]


def build_body(*parts: str) -> str | None:
    lines = [part.strip() for part in parts if isinstance(part, str) and part.strip()]
    return "\n".join(lines) if lines else None


def load_json_argument(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def prompt_from_completion_payload(payload: dict[str, Any]) -> str:
    inputs = payload.get("input-messages") or payload.get("input_messages") or []
    if not isinstance(inputs, list) or not inputs:
        return ""
    latest = inputs[-1]
    return first_line(latest) if isinstance(latest, str) else ""


def completion_event_id(payload: dict[str, Any]) -> str:
    turn_id = str(payload.get("turn_id", "")).strip()
    if turn_id:
        return f"codex-completed-{turn_id}"
    return stable_event_id("codex-completed", canonical_json(payload))


def build_completion_event(config: RuntimeConfig, payload: dict[str, Any]) -> dict[str, Any]:
    turn_id = str(payload.get("turn_id", "")).strip()
    thread_id = extract_thread_id(payload)
    prompt = prompt_from_completion_payload(payload)
    result_preview = first_line(str(payload.get("last_agent_message", "")).strip())
    cwd = str(payload.get("cwd", "")).strip()
    body = build_body(
        f"prompt: {prompt}" if prompt and not turn_id else "",
        f"result: {result_preview}" if result_preview else "",
        f"cwd: {cwd}" if cwd else "",
    )
    metadata = {
        "codex_status": "completed",
        "codex_notify_type": payload.get("type", ""),
        "cwd": cwd,
        "turn_id": turn_id,
        "result_preview": result_preview,
    }
    if not turn_id:
        metadata["thread_id"] = thread_id
        metadata["prompt"] = prompt
    return {
        "event_id": completion_event_id(payload),
        "source": config.source,
        "event_type": COMPLETION_EVENT_TYPE,
        "severity": "info",
        "summary": "Codex task completed",
        "body": body,
        "metadata": {key: value for key, value in metadata.items() if value not in ("", None)},
        "tags": [COMPLETION_STATUS_TAG],
    }


def extract_thread_id(payload: dict[str, Any]) -> str:
    for key in ("thread-id", "thread_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def payload_has_subagent_marker(payload: dict[str, Any]) -> bool:
    for key in ("subagent", "sub-agent", "subAgent"):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip():
            return value.strip().lower() in ("1", "true", "yes", "subagent")
        if isinstance(value, dict):
            return True

    for key in ("source", "session-source", "session_source", "thread-source", "thread_source"):
        value = payload.get(key)
        if isinstance(value, dict):
            if "subagent" in value or "subAgent" in value:
                return True
            if "subagent" in json.dumps(value).lower():
                return True
        if isinstance(value, str) and "subagent" in value.lower():
            return True
    return False


def is_subagent_thread(thread_id: str, sessions_root: Path | None = None) -> bool:
    if not thread_id:
        return False

    root = sessions_root or (Path.home() / ".codex" / "sessions")
    pattern = str(root / "**" / f"*{thread_id}*.jsonl")
    matches = glob.glob(pattern, recursive=True)
    if not matches:
        return False

    matches.sort(key=lambda p: Path(p).stat().st_mtime, reverse=True)
    for match in matches:
        try:
            with open(match, "r", encoding="utf-8") as handle:
                first = handle.readline()
            record = json.loads(first)
        except Exception:
            continue
        if record.get("type") != "session_meta":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        source = payload.get("source")
        if isinstance(source, dict) and ("subagent" in source or "subAgent" in source):
            return True
        if isinstance(source, str) and "subagent" in source.lower():
            return True
    return False
