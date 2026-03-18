from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from alert_hub.auth import compute_signature

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "send_event.sh"


class RecordingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], status_code: int, response_body: bytes) -> None:
        super().__init__(server_address, RecordingRequestHandler)
        self.status_code = status_code
        self.response_body = response_body
        self.requests: list[dict[str, Any]] = []


class RecordingRequestHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(
            {
                "path": self.path,
                "headers": {key: value for key, value in self.headers.items()},
                "body": body,
            }
        )
        self.send_response(self.server.status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.server.response_body)))
        self.end_headers()
        self.wfile.write(self.server.response_body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


class RecordingServer:
    def __init__(self, *, status_code: int = 202, response_body: bytes = b'{"status":"accepted"}') -> None:
        self._server = RecordingHTTPServer(("127.0.0.1", 0), status_code, response_body)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "RecordingServer":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/api/v1/events"

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self._server.requests


def run_sender(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT_PATH), *args],
        cwd=REPO_ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_shell_sender_flag_mode_signs_and_sends_expected_payload() -> None:
    with RecordingServer() as server:
        result = run_sender(
            "--url",
            server.url,
            "--sender",
            "home-laptop",
            "--event-id",
            "manual-test-001",
            "--source",
            "home-laptop",
            "--event-type",
            "manual_test",
            "--severity",
            "warning",
            "--summary",
            'Disk "usage" is high',
            "--body",
            "Line one\nLine two",
            "--link",
            "https://example.test/host",
            "--link",
            "https://example.test/runbook",
            env={"ALERT_HUB_SECRET": "home-secret"},
        )

    assert result.returncode == 0
    assert result.stdout == '{"status":"accepted"}'
    assert "status=202" in result.stderr
    assert len(server.requests) == 1

    request = server.requests[0]
    body = request["body"]
    headers = request["headers"]
    payload = json.loads(body.decode("utf-8"))

    assert request["path"] == "/api/v1/events"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-AlertHub-Sender"] == "home-laptop"
    assert payload == {
        "event_id": "manual-test-001",
        "source": "home-laptop",
        "event_type": "manual_test",
        "severity": "warning",
        "summary": 'Disk "usage" is high',
        "body": "Line one\nLine two",
        "links": [
            {"url": "https://example.test/host"},
            {"url": "https://example.test/runbook"},
        ],
    }

    timestamp = int(headers["X-AlertHub-Timestamp"])
    expected_signature = compute_signature("home-secret", timestamp, body)
    assert headers["X-AlertHub-Signature"] == f"v1={expected_signature}"


def test_shell_sender_payload_file_mode_sends_exact_file_contents(tmp_path: Path) -> None:
    payload_file = tmp_path / "event.json"
    payload_file.write_text(
        '{\n  "event_id": "payload-file-001",\n  "source": "prod-monitor",\n  "event_type": "deploy",\n'
        '  "severity": "info",\n  "summary": "Deployment complete"\n}\n',
        encoding="utf-8",
    )

    with RecordingServer() as server:
        result = run_sender(
            "--url",
            server.url,
            "--sender",
            "prod-monitor",
            "--secret",
            "prod-secret",
            "--payload-file",
            str(payload_file),
        )

    assert result.returncode == 0
    assert len(server.requests) == 1

    request = server.requests[0]
    body = request["body"]
    headers = request["headers"]

    assert body == payload_file.read_bytes()
    timestamp = int(headers["X-AlertHub-Timestamp"])
    expected_signature = compute_signature("prod-secret", timestamp, body)
    assert headers["X-AlertHub-Signature"] == f"v1={expected_signature}"


def test_shell_sender_returns_nonzero_on_non_2xx_response() -> None:
    with RecordingServer(status_code=409, response_body=b'{"status":"conflict"}') as server:
        result = run_sender(
            "--url",
            server.url,
            "--sender",
            "home-laptop",
            "--event-id",
            "conflict-001",
            "--source",
            "home-laptop",
            "--event-type",
            "manual_test",
            "--severity",
            "info",
            "--summary",
            "Conflict test",
            env={"ALERT_HUB_SECRET": "home-secret"},
        )

    assert result.returncode == 1
    assert result.stdout == '{"status":"conflict"}'
    assert "status=409" in result.stderr


def test_shell_sender_rejects_missing_required_fields() -> None:
    result = run_sender(
        "--url",
        "http://127.0.0.1:9/api/v1/events",
        "--sender",
        "home-laptop",
        "--event-id",
        "missing-summary",
        "--source",
        "home-laptop",
        "--event-type",
        "manual_test",
        "--severity",
        "info",
        env={"ALERT_HUB_SECRET": "home-secret"},
    )

    assert result.returncode == 1
    assert "missing required arguments" in result.stderr


def test_shell_sender_rejects_missing_payload_file() -> None:
    result = run_sender(
        "--url",
        "http://127.0.0.1:9/api/v1/events",
        "--sender",
        "home-laptop",
        "--secret",
        "home-secret",
        "--payload-file",
        "/does/not/exist.json",
    )

    assert result.returncode == 1
    assert "payload file not found" in result.stderr
