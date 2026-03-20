#!/usr/bin/env python3
"""Watch Codex session logs and send Alert Hub events for attention-needed states."""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from pathlib import Path
from typing import Any

from codex_alert_common import build_body, build_completion_event, load_runtime_config, send_payload, stable_event_id

APPROVAL_EVENTS = {
    "approval_request",
    "exec_approval_request",
    "command_execution_request_approval",
    "apply_patch_approval_request",
    "file_change_request_approval",
}

INPUT_EVENTS = {
    "request_user_input",
    "elicitation_request",
    "tool_request_user_input",
}

EVENT_CONFIG = {
    "approval": {
        "event_type": "codex_approval_needed",
        "severity": "warning",
        "summary": "Codex approval needed",
        "tag": "codex-status-approval-needed",
        "status": "approval-needed",
    },
    "input": {
        "event_type": "codex_input_needed",
        "severity": "warning",
        "summary": "Codex input needed",
        "tag": "codex-status-input-needed",
        "status": "input-needed",
    },
    "plan_ready": {
        "event_type": "codex_plan_ready",
        "severity": "warning",
        "summary": "Codex plan ready",
        "tag": "codex-status-plan-ready",
        "status": "plan-ready",
    },
}


def parse_args() -> argparse.Namespace:
    home = Path.home()
    parser = argparse.ArgumentParser(description="Watch Codex sessions and send Alert Hub events.")
    parser.add_argument("--sessions-dir", default=str(home / ".codex" / "sessions"), help="Codex sessions directory")
    parser.add_argument(
        "--state-file",
        default=str(home / ".codex" / "tmp" / "codex_attention_watcher_state.json"),
        help="State file for offsets and cwd hints",
    )
    parser.add_argument("--env-file", default=None, help="Path to codex_alert_hub.env")
    parser.add_argument("--poll-seconds", type=float, default=None, help="Polling interval seconds")
    parser.add_argument("--recent-files", type=int, default=60, help="How many recent session files to scan each poll")
    parser.add_argument("--once", action="store_true", help="Run one iteration and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print generated payloads instead of sending")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logs")
    return parser.parse_args()


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def first_line(text: str, limit: int = 280) -> str:
    line = text.splitlines()[0].strip() if text else ""
    return line[:limit]


def read_state(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return {"version": 1, "offsets": {}, "cwd_by_file": {}, "mode_by_file": {}, "last_scan_started_at": 0.0}
    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "offsets": {}, "cwd_by_file": {}, "mode_by_file": {}, "last_scan_started_at": 0.0}
    if not isinstance(raw, dict):
        return {"version": 1, "offsets": {}, "cwd_by_file": {}, "mode_by_file": {}, "last_scan_started_at": 0.0}
    raw.setdefault("version", 1)
    raw.setdefault("offsets", {})
    raw.setdefault("cwd_by_file", {})
    raw.setdefault("mode_by_file", {})
    raw.setdefault("last_scan_started_at", 0.0)
    return raw


def write_state(state_file: Path, state: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    tmp.replace(state_file)


def latest_session_files(sessions_dir: Path, limit: int) -> list[Path]:
    pattern = str(sessions_dir / "**" / "*.jsonl")
    files = [Path(path) for path in glob.glob(pattern, recursive=True)]
    files = [path for path in files if path.exists()]
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return files[:limit]


def sniff_latest_cwd(file_path: Path) -> str:
    try:
        size = file_path.stat().st_size
        with file_path.open("rb") as handle:
            if size > 131072:
                handle.seek(size - 131072)
            blob = handle.read()
        text = blob.decode("utf-8", errors="ignore")
    except Exception:
        return ""

    for line in reversed(text.splitlines()):
        try:
            record = json.loads(line)
        except Exception:
            continue
        if record.get("type") != "turn_context":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            return cwd.strip()
    return ""


def sniff_latest_mode(file_path: Path) -> str:
    try:
        size = file_path.stat().st_size
        with file_path.open("rb") as handle:
            if size > 131072:
                handle.seek(size - 131072)
            blob = handle.read()
        text = blob.decode("utf-8", errors="ignore")
    except Exception:
        return ""

    for line in reversed(text.splitlines()):
        try:
            record = json.loads(line)
        except Exception:
            continue
        if record.get("type") != "turn_context":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        collab_mode = payload.get("collaboration_mode")
        if isinstance(collab_mode, dict):
            mode = collab_mode.get("mode")
            if isinstance(mode, str) and mode.strip():
                return mode.strip().lower()
        if isinstance(collab_mode, str) and collab_mode.strip():
            return collab_mode.strip().lower()
    return ""


def detect_attention_event(record: dict[str, Any]) -> tuple[str, str] | None:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    event_type = str(payload.get("type", ""))
    record_type = str(record.get("type", ""))
    if record_type == "event_msg":
        if event_type in APPROVAL_EVENTS or "approval" in event_type.lower():
            return ("approval", event_type)
        if event_type in INPUT_EVENTS or "request_user_input" in event_type.lower() or "elicitation" in event_type.lower():
            return ("input", event_type)
    if record_type == "response_item":
        if event_type in APPROVAL_EVENTS:
            return ("approval", event_type)
        if event_type in INPUT_EVENTS:
            return ("input", event_type)
    return None


def parse_function_call_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def pick_detail(payload: dict[str, Any]) -> str:
    candidates: list[str] = []
    for key in ("question", "message", "cmd", "command", "prompt", "reason"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value)
    questions = payload.get("questions")
    if isinstance(questions, list) and questions:
        first = questions[0]
        if isinstance(first, str) and first.strip():
            candidates.append(first)
        elif isinstance(first, dict):
            for key in ("question", "header", "id"):
                value = first.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value)
    for text in candidates:
        line = first_line(text)
        if line:
            return line
    return ""


def detect_function_call_attention(record: dict[str, Any]) -> tuple[str, str, str] | None:
    if record.get("type") != "response_item":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "function_call":
        return None

    name = payload.get("name")
    args = parse_function_call_arguments(payload.get("arguments"))
    if name == "request_user_input":
        return ("input", "function_call.request_user_input", pick_detail(args))
    if name in ("exec_command", "apply_patch") and args.get("sandbox_permissions") == "require_escalated":
        return ("approval", f"function_call.{name}.require_escalated", pick_detail(args))
    return None


def build_status_payload(
    *,
    config,
    key: str,
    detail: str,
    cwd: str,
    file_path: Path,
    offset: int,
    event_name: str,
) -> dict[str, Any]:
    event = EVENT_CONFIG[key]
    event_id = stable_event_id(
        event["event_type"],
        f"{file_path}|{offset}|{event_name}|{detail}|{cwd}",
    )
    metadata = {
        "codex_status": event["status"],
        "codex_event_type": event_name,
        "cwd": cwd,
        "session_file": str(file_path),
        "session_offset": offset,
    }
    if detail:
        metadata["detail"] = detail
    return {
        "event_id": event_id,
        "source": config.source,
        "event_type": event["event_type"],
        "severity": event["severity"],
        "summary": event["summary"],
        "body": build_body(f"detail: {detail}" if detail else "", f"cwd: {cwd}" if cwd else ""),
        "dedupe_key": event_id,
        "metadata": metadata,
        "tags": [event["tag"]],
    }


def emit_event(*, config, payload: dict[str, Any], dry_run: bool, verbose: bool, event_name: str, file_path: Path) -> None:
    if dry_run:
        print(
            f"[{now_utc()}] DRY-RUN event={event_name} file={file_path.name}\n{json.dumps(payload, indent=2, sort_keys=True)}\n",
            flush=True,
        )
        return
    try:
        send_payload(config, payload)
        if verbose:
            print(f"[{now_utc()}] sent event={event_name} file={file_path.name}", flush=True)
    except Exception as exc:
        print(f"[{now_utc()}] send failed: {exc}", file=sys.stderr, flush=True)


def should_process_new_file(file_path: Path, discovery_cutoff_unix_time: float) -> bool:
    try:
        return file_path.stat().st_mtime >= discovery_cutoff_unix_time
    except OSError:
        return False


def process_file(
    file_path: Path,
    state: dict[str, Any],
    *,
    config,
    dry_run: bool,
    verbose: bool,
    discovery_cutoff_unix_time: float,
) -> bool:
    offsets: dict[str, int] = state["offsets"]
    cwd_by_file: dict[str, str] = state["cwd_by_file"]
    mode_by_file: dict[str, str] = state["mode_by_file"]
    key = str(file_path)
    size = file_path.stat().st_size
    changed = False

    if key not in offsets:
        offsets[key] = 0 if should_process_new_file(file_path, discovery_cutoff_unix_time) else size
        cwd = sniff_latest_cwd(file_path)
        mode = sniff_latest_mode(file_path)
        if cwd:
            cwd_by_file[key] = cwd
        if mode:
            mode_by_file[key] = mode
        if verbose:
            print(f"[{now_utc()}] init offset {file_path} -> {offsets[key]}", flush=True)
        if offsets[key] == size:
            return True

    offset = int(offsets.get(key, 0))
    if size < offset:
        offset = 0
    if not str(cwd_by_file.get(key, "")).strip():
        cwd = sniff_latest_cwd(file_path)
        if cwd:
            cwd_by_file[key] = cwd
            changed = True
    if not str(mode_by_file.get(key, "")).strip():
        mode = sniff_latest_mode(file_path)
        if mode:
            mode_by_file[key] = mode
            changed = True

    with file_path.open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        while True:
            line = handle.readline()
            if not line:
                break
            current_offset = handle.tell() - len(line.encode("utf-8"))
            try:
                record = json.loads(line)
            except Exception:
                continue
            if not isinstance(record, dict):
                continue

            if record.get("type") == "turn_context":
                payload = record.get("payload")
                if isinstance(payload, dict):
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd.strip():
                        cwd_by_file[key] = cwd.strip()
                        changed = True
                    collab_mode = payload.get("collaboration_mode")
                    mode = ""
                    if isinstance(collab_mode, dict):
                        mode = str(collab_mode.get("mode", "")).strip().lower()
                    elif isinstance(collab_mode, str):
                        mode = collab_mode.strip().lower()
                    if mode:
                        mode_by_file[key] = mode
                        changed = True
                continue

            payload = record.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "task_complete":
                if mode_by_file.get(key) == "plan":
                    event_payload = build_status_payload(
                        config=config,
                        key="plan_ready",
                        detail="Implement this plan?",
                        cwd=cwd_by_file.get(key, ""),
                        file_path=file_path,
                        offset=current_offset,
                        event_name="plan_mode.task_complete",
                    )
                    emit_event(
                        config=config,
                        payload=event_payload,
                        dry_run=dry_run,
                        verbose=verbose,
                        event_name="plan_mode.task_complete",
                        file_path=file_path,
                    )
                    continue

                completion_payload = dict(payload)
                completion_payload.setdefault("cwd", cwd_by_file.get(key, ""))
                event_payload = build_completion_event(config, completion_payload)
                emit_event(
                    config=config,
                    payload=event_payload,
                    dry_run=dry_run,
                    verbose=verbose,
                    event_name="task_complete",
                    file_path=file_path,
                )
                continue

            detected_fn = detect_function_call_attention(record)
            if detected_fn:
                kind, event_name, detail = detected_fn
                event_payload = build_status_payload(
                    config=config,
                    key=kind,
                    detail=detail,
                    cwd=cwd_by_file.get(key, ""),
                    file_path=file_path,
                    offset=current_offset,
                    event_name=event_name,
                )
                emit_event(
                    config=config,
                    payload=event_payload,
                    dry_run=dry_run,
                    verbose=verbose,
                    event_name=event_name,
                    file_path=file_path,
                )
                continue

            detected = detect_attention_event(record)
            if not detected:
                continue
            kind, event_name = detected
            detail = pick_detail(payload if isinstance(payload, dict) else {})
            event_payload = build_status_payload(
                config=config,
                key=kind,
                detail=detail,
                cwd=cwd_by_file.get(key, ""),
                file_path=file_path,
                offset=current_offset,
                event_name=event_name,
            )
            emit_event(
                config=config,
                payload=event_payload,
                dry_run=dry_run,
                verbose=verbose,
                event_name=event_name,
                file_path=file_path,
            )

        new_offset = handle.tell()

    if new_offset != offsets.get(key):
        offsets[key] = new_offset
        changed = True
    return changed


def prune_missing_files(state: dict[str, Any]) -> bool:
    changed = False
    offsets: dict[str, int] = state["offsets"]
    cwd_by_file: dict[str, str] = state["cwd_by_file"]
    mode_by_file: dict[str, str] = state["mode_by_file"]
    missing = [key for key in offsets if not Path(key).exists()]
    for key in missing:
        offsets.pop(key, None)
        cwd_by_file.pop(key, None)
        mode_by_file.pop(key, None)
        changed = True
    return changed


def main() -> int:
    args = parse_args()
    config = load_runtime_config(args.env_file)
    state_file = Path(args.state_file).expanduser()
    poll_seconds = args.poll_seconds if args.poll_seconds is not None else config.poll_seconds
    state = read_state(state_file)
    sessions_dir = Path(args.sessions_dir).expanduser()
    startup_unix_time = time.time()

    while True:
        discovery_cutoff_unix_time = float(state.get("last_scan_started_at") or startup_unix_time)
        current_scan_started_at = time.time()
        changed = prune_missing_files(state)
        for file_path in latest_session_files(sessions_dir, args.recent_files):
            changed = process_file(
                file_path,
                state,
                config=config,
                dry_run=args.dry_run,
                verbose=args.verbose,
                discovery_cutoff_unix_time=discovery_cutoff_unix_time,
            ) or changed
        if state.get("last_scan_started_at") != current_scan_started_at:
            state["last_scan_started_at"] = current_scan_started_at
            changed = True
        if changed:
            write_state(state_file, state)
        if args.once:
            return 0
        time.sleep(poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
