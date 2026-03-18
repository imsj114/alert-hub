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
  }
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

Use the included helper script:

```bash
export ALERT_HUB_SECRET="$ALERT_HUB_SENDER_HOME_LAPTOP_SECRET"
python scripts/send_event.py \
  --url http://127.0.0.1:8000/api/v1/events \
  --sender home-laptop \
  --event-id manual-test-001 \
  --source home-laptop \
  --event-type manual_test \
  --severity info \
  --summary "Manual alert test"
```

You can also send from a JSON file:

```bash
python scripts/send_event.py \
  --url http://127.0.0.1:8000/api/v1/events \
  --sender prod-monitor \
  --secret-env ALERT_HUB_SENDER_PROD_MONITOR_SECRET \
  --payload-file /path/to/event.json
```

The helper script signs the raw JSON body and sends these headers:

- `X-AlertHub-Sender`
- `X-AlertHub-Timestamp`
- `X-AlertHub-Signature`

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
