# Alert Hub

Alert Hub is a small self-hosted personal notification relay. It accepts signed events from your machines or services, normalizes and deduplicates them, routes them with simple rules, and forwards notifications to `ntfy`.

This repository is intentionally small:

- one Python service
- one SQLite database
- one built-in notification backend in v1: `ntfy`
- one Docker Compose deployment path

It is designed for one-person maintainability and low operational burden.

## Architecture

The v1 flow is:

1. A sender builds a JSON event and signs the raw request body with HMAC-SHA256.
2. Alert Hub verifies the sender, timestamp, signature, and optional sender IP allowlist.
3. The event is validated and normalized with Pydantic.
4. The hub records the event in SQLite, suppresses duplicates, and creates delivery jobs for matching targets.
5. A small in-process worker sends due jobs to `ntfy` and retries temporary failures.

Main components:

- `alert_hub/api.py`: HTTP API endpoints
- `alert_hub/auth.py`: header parsing, signature verification, sender IP allowlists
- `alert_hub/models.py`: incoming event model and normalized event structures
- `alert_hub/db.py`: SQLite schema and persistence logic
- `alert_hub/routing.py`: first-match-wins routing rules
- `alert_hub/notifiers/ntfy.py`: `ntfy` publish client
- `alert_hub/service.py`: application orchestration and retry policy
- `alert_hub/worker.py`: background worker thread startup

## Security Model

V1 authenticates requests but does not encrypt them in transit.

- Integrity and authenticity come from `X-AlertHub-Signature`.
- Replay protection comes from signed timestamps plus a short-lived signature cache in SQLite.
- Optional `allowed_cidrs` lets you restrict a sender to expected source networks.
- Confidentiality is not provided because this repo does not terminate HTTPS in v1.

Practical consequence: keep payloads low-sensitivity. Do not send secrets, customer data, raw logs, or anything that would be unsafe to expose on the network path.

## Event Format

Headers:

- `X-AlertHub-Sender`: sender id from config
- `X-AlertHub-Timestamp`: Unix epoch seconds
- `X-AlertHub-Signature`: `v1=<hex_hmac_sha256(secret, timestamp + "." + raw_body)>`

JSON body:

```json
{
  "event_id": "disk-space-prod-2026-03-18T12:00:00Z",
  "source": "prod-db-01",
  "event_type": "disk_space_low",
  "severity": "warning",
  "summary": "Disk usage is above 85%",
  "body": "Root filesystem is at 88% on prod-db-01",
  "links": [
    {
      "url": "https://internal.example.com/hosts/prod-db-01",
      "label": "Host details"
    }
  ],
  "metadata": {
    "filesystem": "/",
    "usage_percent": 88
  },
  "tags": ["disk-space", "prod"]
}
```

Required fields:

- `event_id`
- `source`
- `event_type`
- `severity`
- `summary`

Optional fields:

- `body`
- `occurred_at`
- `dedupe_key`
- `links`
- `metadata`
- `tags`

## Deduplication And Retries

Deduplication has two layers:

- Hard idempotency: `(sender_id, event_id)` must be unique. Re-sending the same `event_id` with the same payload returns `200 duplicate`. Re-using the same `event_id` with a different payload returns `409 conflict`.
- Soft dedupe: if the same effective dedupe key appears again within `dedupe.window_seconds`, the event is recorded as `suppressed` and no new notification is sent.

Retry behavior:

- Retryable failures: timeouts, network errors, HTTP `429`, HTTP `5xx`
- Permanent failures: all other HTTP `4xx`
- Backoff: exponential from `30s`, capped at `1h`, with small jitter
- Maximum attempts: `6`

Delivery is at-least-once. A crash at the wrong moment can produce a duplicate ntfy notification.

## Configuration

Copy the example files:

```bash
cp .env.example .env
cp config/config.example.yaml config/config.yaml
```

Edit `.env`:

```dotenv
ALERT_HUB_SENDER_HOME_LAPTOP_SECRET=replace-me
ALERT_HUB_SENDER_PROD_MONITOR_SECRET=replace-me
ALERT_HUB_NTFY_TOKEN=
```

Edit `config/config.yaml`:

```yaml
server:
  host: 0.0.0.0
  port: 8000
  database_path: ./data/alert_hub.db

senders:
  - id: home-laptop
    secret_env: ALERT_HUB_SENDER_HOME_LAPTOP_SECRET
  - id: prod-monitor
    secret_env: ALERT_HUB_SENDER_PROD_MONITOR_SECRET

targets:
  - id: personal-phone
    type: ntfy
    base_url: https://ntfy.sh
    topic: your-topic
    token_env: ALERT_HUB_NTFY_TOKEN
    tags: [alert-hub]

routes:
  default:
    targets: [personal-phone]
```

Config notes:

- non-secret settings live in YAML
- secrets stay in `.env`
- sender secrets are independent and should be rotated independently
- routes are ordered and first-match-wins

## Local Development

Create a virtual environment and install the project:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

Run the app:

```bash
ALERT_HUB_CONFIG=config/config.yaml ALERT_HUB_ENV=.env uvicorn alert_hub.main:create_app --factory --reload --host 0.0.0.0 --port 8000
```

Run tests:

```bash
pytest
```

## Sending An Event

Use the bash sender script for manual tests, cron jobs, CI, or wrapper scripts:

```bash
export ALERT_HUB_SECRET="$ALERT_HUB_SENDER_HOME_LAPTOP_SECRET"
./scripts/send_event.sh \
  --url http://127.0.0.1:8000/api/v1/events \
  --sender home-laptop \
  --event-id manual-test-001 \
  --source home-laptop \
  --event-type manual_test \
  --severity info \
  --summary "Manual alert test"
```

For richer payloads, send a prebuilt JSON file:

```bash
./scripts/send_event.sh \
  --url http://127.0.0.1:8000/api/v1/events \
  --sender prod-monitor \
  --secret-env ALERT_HUB_SENDER_PROD_MONITOR_SECRET \
  --payload-file /path/to/event.json
```

The shell script signs the raw JSON body and sends these headers:

- `X-AlertHub-Sender`
- `X-AlertHub-Timestamp`
- `X-AlertHub-Signature`

If you prefer Python, the original helper remains available at `scripts/send_event.py`.

## Codex Alerting

This repo includes a portable Codex sender stack for Linux and macOS:

- `scripts/codex_notify.sh`: completion hook wired through Codex `notify`
- `scripts/codex_attention_watcher.py`: watches `~/.codex/sessions` for approval/input/plan-ready states
- `scripts/codex_attention_watcher.sh`: installer and service manager
- `scripts/codex_alert_mcp.py`: local MCP server with a `send_alert` tool for manual Codex-triggered alerts

Install it on a machine that runs Codex:

```bash
./scripts/codex_attention_watcher.sh install \
  --url http://127.0.0.1:8000/api/v1/events \
  --sender home-laptop \
  --source codex-home-laptop \
  --secret-env ALERT_HUB_SENDER_HOME_LAPTOP_SECRET
```

What `install` does:

- writes runtime settings to `~/.codex/codex_alert_hub.env`
- replaces the top-level `notify = [...]` entry in `~/.codex/config.toml` so Codex runs `scripts/codex_notify.sh`
- adds `[mcp_servers.alert_hub]` in `~/.codex/config.toml` for the local MCP server
- installs a background watcher as a `systemd --user` service on Linux or a `launchd` agent on macOS

Manage the watcher service:

```bash
./scripts/codex_attention_watcher.sh status
./scripts/codex_attention_watcher.sh logs 80
./scripts/codex_attention_watcher.sh restart
./scripts/codex_attention_watcher.sh uninstall
```

Automatic Codex events sent through Alert Hub:

- `codex_job_completed`
- `codex_approval_needed`
- `codex_input_needed`
- `codex_plan_ready`

Each Codex event includes one status tag so `ntfy` can surface the current state cleanly:

- `codex-status-completed`
- `codex-status-approval-needed`
- `codex-status-input-needed`
- `codex-status-plan-ready`
- `codex-status-manual`

The MCP server adds a separate manual path. Once installed, Codex can call the `send_alert` tool to send a signed Alert Hub event directly without waiting for the automatic watcher or completion hook.

## Deployment

Prepare files:

```bash
cp .env.example .env
cp config/config.example.yaml config/config.yaml
mkdir -p data
```

Build and run:

```bash
docker compose up -d --build
```

The service listens on port `8000`.

Because v1 does not include HTTPS:

- firewall the port to known sender IPs where possible
- keep payloads low-sensitivity
- prefer private networking or a VPN if available

## Operational Notes

- `GET /healthz` checks the API process and SQLite connectivity.
- The SQLite database lives at `server.database_path`.
- Back up the database by copying the file during a quiet period or while the service is stopped.
- Logs go to stdout/stderr.
- If you use public `ntfy.sh`, remember that notification content is handled by that external service. See ntfy documentation: https://docs.ntfy.sh/privacy/
- ntfy publish API reference: https://docs.ntfy.sh/publish/

## Extending Later

The clean extension points in v1 are:

- add another notifier alongside `alert_hub/notifiers/ntfy.py`
- extend target config parsing in `alert_hub/config.py`
- keep routing, dedupe, validation, and retries unchanged
