from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


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


def write_runtime_env(home: Path, url: str) -> Path:
    env_file = home / ".codex" / "codex_alert_hub.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "\n".join(
            [
                f"ALERT_HUB_CODEX_URL={url}",
                "ALERT_HUB_CODEX_SENDER=codex-sender",
                "ALERT_HUB_SECRET=test-secret",
                "ALERT_HUB_CODEX_SOURCE=codex-host",
                "ALERT_HUB_CODEX_POLL_SECONDS=0.01",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return env_file


def run_script(command: list[str], *, home: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_codex_notify_sends_completion_event(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with RecordingServer() as server:
        write_runtime_env(home, server.url)
        payload = {
            "type": "agent-turn-complete",
            "cwd": "/tmp/project",
            "thread-id": "main-thread",
            "input-messages": ["Ship the fix"],
        }

        result = run_script(["bash", str(SCRIPTS_DIR / "codex_notify.sh"), json.dumps(payload)], home=home)

    assert result.returncode == 0
    assert len(server.requests) == 1
    sent = json.loads(server.requests[0]["body"].decode("utf-8"))
    assert sent["event_type"] == "codex_job_completed"
    assert sent["summary"] == "Codex task completed"
    assert sent["tags"] == ["codex-status-completed"]
    assert sent["metadata"]["thread_id"] == "main-thread"


def test_codex_notify_sends_task_complete_event(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with RecordingServer() as server:
        write_runtime_env(home, server.url)
        payload = {
            "type": "task_complete",
            "turn_id": "turn-456",
            "cwd": "/tmp/project",
            "thread-id": "main-thread",
            "last_agent_message": "Patched the notifier.",
        }

        result = run_script(["bash", str(SCRIPTS_DIR / "codex_notify.sh"), json.dumps(payload)], home=home)

    assert result.returncode == 0
    assert len(server.requests) == 1
    sent = json.loads(server.requests[0]["body"].decode("utf-8"))
    assert sent["event_type"] == "codex_job_completed"
    assert sent["event_id"] == "codex-completed-turn-456"
    assert sent["metadata"]["turn_id"] == "turn-456"
    assert sent["metadata"]["result_preview"] == "Patched the notifier."


def test_codex_notify_sends_wrapped_task_complete_event(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with RecordingServer() as server:
        write_runtime_env(home, server.url)
        payload = {
            "type": "event_msg",
            "cwd": "/tmp/project",
            "thread-id": "main-thread",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-123",
                "last_agent_message": "Wrapped completion event.",
            },
        }

        result = run_script(["bash", str(SCRIPTS_DIR / "codex_notify.sh"), json.dumps(payload)], home=home)

    assert result.returncode == 0
    assert len(server.requests) == 1
    sent = json.loads(server.requests[0]["body"].decode("utf-8"))
    assert sent["event_type"] == "codex_job_completed"
    assert sent["event_id"] == "codex-completed-turn-123"
    assert sent["metadata"]["turn_id"] == "turn-123"
    assert sent["metadata"]["result_preview"] == "Wrapped completion event."


def test_codex_notify_ignores_subagent_thread(tmp_path: Path) -> None:
    home = tmp_path / "home"
    sessions_dir = home / ".codex" / "sessions" / "2026" / "03" / "20"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "rollout-sub123.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"source": "subagent"}}) + "\n",
        encoding="utf-8",
    )

    with RecordingServer() as server:
        write_runtime_env(home, server.url)
        payload = {
            "type": "agent-turn-complete",
            "cwd": "/tmp/project",
            "thread-id": "sub123",
            "input-messages": ["Do background work"],
        }

        result = run_script(["bash", str(SCRIPTS_DIR / "codex_notify.sh"), json.dumps(payload)], home=home)

    assert result.returncode == 0
    assert server.requests == []


def test_attention_watcher_sends_approval_event_once(tmp_path: Path) -> None:
    home = tmp_path / "home"
    sessions_dir = home / ".codex" / "sessions" / "2026" / "03" / "20"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions_dir / "approval.jsonl"
    session_file.write_text(
        json.dumps({"type": "turn_context", "payload": {"cwd": "/tmp/project", "collaboration_mode": {"mode": "default"}}})
        + "\n",
        encoding="utf-8",
    )

    state_file = tmp_path / "state.json"
    with RecordingServer() as server:
        env_file = write_runtime_env(home, server.url)
        first = run_script(
            [
                "python3",
                str(SCRIPTS_DIR / "codex_attention_watcher.py"),
                "--env-file",
                str(env_file),
                "--sessions-dir",
                str(sessions_dir),
                "--state-file",
                str(state_file),
                "--once",
            ],
            home=home,
        )
        assert first.returncode == 0
        assert server.requests == []

        with session_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "event_msg", "payload": {"type": "approval_request", "message": "approve"}}) + "\n")

        second = run_script(
            [
                "python3",
                str(SCRIPTS_DIR / "codex_attention_watcher.py"),
                "--env-file",
                str(env_file),
                "--sessions-dir",
                str(sessions_dir),
                "--state-file",
                str(state_file),
                "--once",
            ],
            home=home,
        )
        third = run_script(
            [
                "python3",
                str(SCRIPTS_DIR / "codex_attention_watcher.py"),
                "--env-file",
                str(env_file),
                "--sessions-dir",
                str(sessions_dir),
                "--state-file",
                str(state_file),
                "--once",
            ],
            home=home,
        )

    assert second.returncode == 0
    assert third.returncode == 0
    assert len(server.requests) == 1
    sent = json.loads(server.requests[0]["body"].decode("utf-8"))
    assert sent["event_type"] == "codex_approval_needed"
    assert sent["tags"] == ["codex-status-approval-needed"]


def test_attention_watcher_sends_plan_ready_event(tmp_path: Path) -> None:
    home = tmp_path / "home"
    sessions_dir = home / ".codex" / "sessions" / "2026" / "03" / "20"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions_dir / "plan.jsonl"
    session_file.write_text(
        json.dumps({"type": "turn_context", "payload": {"cwd": "/tmp/plan", "collaboration_mode": {"mode": "plan"}}})
        + "\n",
        encoding="utf-8",
    )

    state_file = tmp_path / "state-plan.json"
    with RecordingServer() as server:
        env_file = write_runtime_env(home, server.url)
        assert (
            run_script(
                [
                    "python3",
                    str(SCRIPTS_DIR / "codex_attention_watcher.py"),
                    "--env-file",
                    str(env_file),
                    "--sessions-dir",
                    str(sessions_dir),
                    "--state-file",
                    str(state_file),
                    "--once",
                ],
                home=home,
            ).returncode
            == 0
        )
        with session_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
        result = run_script(
            [
                "python3",
                str(SCRIPTS_DIR / "codex_attention_watcher.py"),
                "--env-file",
                str(env_file),
                "--sessions-dir",
                str(sessions_dir),
                "--state-file",
                str(state_file),
                "--once",
            ],
            home=home,
        )

    assert result.returncode == 0
    assert len(server.requests) == 1
    sent = json.loads(server.requests[0]["body"].decode("utf-8"))
    assert sent["event_type"] == "codex_plan_ready"
    assert sent["tags"] == ["codex-status-plan-ready"]


def test_attention_watcher_sends_completion_event_for_default_mode(tmp_path: Path) -> None:
    home = tmp_path / "home"
    sessions_dir = home / ".codex" / "sessions" / "2026" / "03" / "20"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions_dir / "complete.jsonl"
    session_file.write_text(
        json.dumps({"type": "turn_context", "payload": {"cwd": "/tmp/default", "collaboration_mode": {"mode": "default"}}})
        + "\n",
        encoding="utf-8",
    )

    state_file = tmp_path / "state-complete.json"
    with RecordingServer() as server:
        env_file = write_runtime_env(home, server.url)
        assert (
            run_script(
                [
                    "python3",
                    str(SCRIPTS_DIR / "codex_attention_watcher.py"),
                    "--env-file",
                    str(env_file),
                    "--sessions-dir",
                    str(sessions_dir),
                    "--state-file",
                    str(state_file),
                    "--once",
                ],
                home=home,
            ).returncode
            == 0
        )
        with session_file.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-complete-1",
                            "last_agent_message": "Default-mode completion.",
                        },
                    }
                )
                + "\n"
            )
        result = run_script(
            [
                "python3",
                str(SCRIPTS_DIR / "codex_attention_watcher.py"),
                "--env-file",
                str(env_file),
                "--sessions-dir",
                str(sessions_dir),
                "--state-file",
                str(state_file),
                "--once",
            ],
            home=home,
        )

    assert result.returncode == 0
    assert len(server.requests) == 1
    sent = json.loads(server.requests[0]["body"].decode("utf-8"))
    assert sent["event_type"] == "codex_job_completed"
    assert sent["event_id"] == "codex-completed-turn-complete-1"
    assert sent["metadata"]["turn_id"] == "turn-complete-1"
    assert sent["metadata"]["result_preview"] == "Default-mode completion."


def test_manager_install_writes_linux_unit_and_codex_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config_file = home / ".codex" / "config.toml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        'notify = ["/old/notify.sh"]\n\n[mcp_servers.other]\ncommand = "other"\n',
        encoding="utf-8",
    )

    result = run_script(
        [
            "bash",
            str(SCRIPTS_DIR / "codex_attention_watcher.sh"),
            "install",
            "--url",
            "http://127.0.0.1:8000/api/v1/events",
            "--sender",
            "codex-sender",
            "--source",
            "codex-host",
            "--secret",
            "test-secret",
            "--no-start",
        ],
        home=home,
        extra_env={"ALERT_HUB_CODEX_TEST_OS": "Linux"},
    )

    assert result.returncode == 0
    updated = config_file.read_text(encoding="utf-8")
    assert f'notify = ["{SCRIPTS_DIR / "codex_notify.sh"}"]' in updated
    assert "[mcp_servers.alert_hub]" in updated
    assert '[mcp_servers.other]' in updated
    unit_file = home / ".config" / "systemd" / "user" / "codex-attention-watcher.service"
    assert unit_file.exists()
    assert str(SCRIPTS_DIR / "codex_attention_watcher.py") in unit_file.read_text(encoding="utf-8")


def test_manager_install_writes_macos_plist(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = run_script(
        [
            "bash",
            str(SCRIPTS_DIR / "codex_attention_watcher.sh"),
            "install",
            "--url",
            "http://127.0.0.1:8000/api/v1/events",
            "--sender",
            "codex-sender",
            "--source",
            "codex-host",
            "--secret",
            "test-secret",
            "--no-start",
        ],
        home=home,
        extra_env={"ALERT_HUB_CODEX_TEST_OS": "Darwin", "ALERT_HUB_CODEX_TEST_UID": "501"},
    )

    assert result.returncode == 0
    plist_file = home / "Library" / "LaunchAgents" / "com.alerthub.codex-attention-watcher.plist"
    assert plist_file.exists()
    plist = plist_file.read_text(encoding="utf-8")
    assert str(SCRIPTS_DIR / "codex_attention_watcher.py") in plist
    assert "--env-file" in plist


def write_frame(proc: subprocess.Popen[bytes], payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    proc.stdin.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("utf-8"))  # type: ignore[union-attr]
    proc.stdin.write(raw)  # type: ignore[union-attr]
    proc.stdin.flush()  # type: ignore[union-attr]


def read_frame(proc: subprocess.Popen[bytes]) -> dict[str, Any]:
    headers: dict[str, str] = {}
    while True:
        line = proc.stdout.readline()  # type: ignore[union-attr]
        if line in (b"\r\n", b"\n"):
            break
        key, value = line.decode("utf-8").strip().split(":", 1)
        headers[key.lower()] = value.strip()
    body = proc.stdout.read(int(headers["content-length"]))  # type: ignore[union-attr]
    return json.loads(body.decode("utf-8"))


def test_mcp_server_sends_manual_alert(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with RecordingServer() as server:
        env_file = write_runtime_env(home, server.url)
        proc = subprocess.Popen(
            ["python3", str(SCRIPTS_DIR / "codex_alert_mcp.py"), "--env-file", str(env_file)],
            cwd=REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        try:
            write_frame(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
            init = read_frame(proc)
            write_frame(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            tools = read_frame(proc)
            write_frame(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "send_alert",
                        "arguments": {
                            "summary": "Manual alert",
                            "severity": "warning",
                            "tags": ["custom-tag"],
                            "metadata": {"origin": "test"},
                        },
                    },
                },
            )
            result = read_frame(proc)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    assert init["result"]["serverInfo"]["name"] == "alert_hub"
    assert tools["result"]["tools"][0]["name"] == "send_alert"
    assert result["result"]["structuredContent"]["sender_id"] == "codex-sender"
    assert len(server.requests) == 1
    sent = json.loads(server.requests[0]["body"].decode("utf-8"))
    assert sent["event_type"] == "codex_manual_alert"
    assert "codex-status-manual" in sent["tags"]
    assert "custom-tag" in sent["tags"]
    assert "body" not in sent


def test_mcp_server_accepts_newline_delimited_initialize(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with RecordingServer() as server:
        env_file = write_runtime_env(home, server.url)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
        result = subprocess.run(
            ["python3", str(SCRIPTS_DIR / "codex_alert_mcp.py"), "--env-file", str(env_file)],
            cwd=REPO_ROOT,
            input=json.dumps(payload) + "\n",
            text=True,
            capture_output=True,
            check=False,
        )

    assert result.returncode == 0
    parsed = json.loads(result.stdout.strip())
    assert parsed["result"]["serverInfo"]["name"] == "alert_hub"
    assert parsed["result"]["serverInfo"]["version"] == "0.1.0"
