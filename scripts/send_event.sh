#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  send_event.sh --url URL --sender ID [--secret SECRET | --secret-env ENV]
                --event-id ID --source SOURCE --event-type TYPE --severity LEVEL --summary TEXT
                [--body TEXT] [--occurred-at RFC3339] [--dedupe-key KEY] [--link URL]...

  send_event.sh --url URL --sender ID [--secret SECRET | --secret-env ENV]
                --payload-file FILE

Options:
  --url URL              Alert Hub /api/v1/events endpoint
  --sender ID            Configured sender id
  --secret SECRET        Shared sender secret
  --secret-env ENV       Environment variable name holding the sender secret
  --payload-file FILE    Send a prebuilt JSON payload from FILE
  --event-id ID          Event id for flag mode
  --source SOURCE        Event source for flag mode
  --event-type TYPE      Event type for flag mode
  --severity LEVEL       One of: info, warning, error, critical
  --summary TEXT         Summary text for flag mode
  --body TEXT            Optional body text
  --occurred-at RFC3339  Optional occurred_at timestamp
  --dedupe-key KEY       Optional dedupe key
  --link URL             Optional link URL, repeatable
  --timeout SECONDS      curl max time, default 10
  --help                 Show this help

If --secret is omitted, the script reads the secret from --secret-env.
If --secret-env is also omitted, ALERT_HUB_SECRET is used.
EOF
}

require_command() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    printf 'missing required command: %s\n' "$name" >&2
    exit 1
  fi
}

json_escape() {
  local value="${1-}"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\b'/\\b}
  value=${value//$'\f'/\\f}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '%s' "$value"
}

append_json_field() {
  local current="$1"
  local key="$2"
  local value="$3"
  local escaped_value
  escaped_value="$(json_escape "$value")"
  if [[ -n "$current" ]]; then
    current+=","
  fi
  current+="\"$key\":\"$escaped_value\""
  printf '%s' "$current"
}

append_link_array() {
  local current="$1"
  shift
  local link
  if (($# == 0)); then
    printf '%s' "$current"
    return
  fi

  if [[ -n "$current" ]]; then
    current+=","
  fi
  current+="\"links\":["
  for link in "$@"; do
    current+="{\"url\":\"$(json_escape "$link")\"},"
  done
  current="${current%,}]"
  printf '%s' "$current"
}

build_flag_payload() {
  local payload=""
  payload="$(append_json_field "$payload" "event_id" "$EVENT_ID")"
  payload="$(append_json_field "$payload" "source" "$SOURCE")"
  payload="$(append_json_field "$payload" "event_type" "$EVENT_TYPE")"
  payload="$(append_json_field "$payload" "severity" "$SEVERITY")"
  payload="$(append_json_field "$payload" "summary" "$SUMMARY")"

  if [[ -n "${BODY:-}" ]]; then
    payload="$(append_json_field "$payload" "body" "$BODY")"
  fi
  if [[ -n "${OCCURRED_AT:-}" ]]; then
    payload="$(append_json_field "$payload" "occurred_at" "$OCCURRED_AT")"
  fi
  if [[ -n "${DEDUPE_KEY:-}" ]]; then
    payload="$(append_json_field "$payload" "dedupe_key" "$DEDUPE_KEY")"
  fi
  payload="$(append_link_array "$payload" "${LINKS[@]}")"
  printf '{%s}' "$payload"
}

copy_payload_file() {
  local from_file="$1"
  if [[ ! -f "$from_file" ]]; then
    printf 'payload file not found: %s\n' "$from_file" >&2
    exit 1
  fi
  cat "$from_file"
}

validate_flag_mode() {
  local missing=()
  [[ -z "${EVENT_ID:-}" ]] && missing+=("--event-id")
  [[ -z "${SOURCE:-}" ]] && missing+=("--source")
  [[ -z "${EVENT_TYPE:-}" ]] && missing+=("--event-type")
  [[ -z "${SEVERITY:-}" ]] && missing+=("--severity")
  [[ -z "${SUMMARY:-}" ]] && missing+=("--summary")

  if ((${#missing[@]} > 0)); then
    printf 'missing required arguments: %s\n' "${missing[*]}" >&2
    exit 1
  fi

  case "$SEVERITY" in
    info|warning|error|critical) ;;
    *)
      printf 'invalid severity: %s\n' "$SEVERITY" >&2
      exit 1
      ;;
  esac
}

resolve_secret() {
  if [[ -n "$SECRET" ]]; then
    printf '%s' "$SECRET"
    return
  fi

  if [[ -z "$SECRET_ENV" ]]; then
    SECRET_ENV="ALERT_HUB_SECRET"
  fi
  local resolved="${!SECRET_ENV:-}"
  if [[ -z "$resolved" ]]; then
    printf 'missing secret; set --secret or environment variable %s\n' "$SECRET_ENV" >&2
    exit 1
  fi
  printf '%s' "$resolved"
}

require_command curl
require_command openssl

URL=""
SENDER=""
SECRET=""
SECRET_ENV=""
PAYLOAD_FILE=""
EVENT_ID=""
SOURCE=""
EVENT_TYPE=""
SEVERITY=""
SUMMARY=""
BODY=""
OCCURRED_AT=""
DEDUPE_KEY=""
TIMEOUT=10
LINKS=()

while (($# > 0)); do
  case "$1" in
    --url)
      URL="${2-}"
      shift 2
      ;;
    --sender)
      SENDER="${2-}"
      shift 2
      ;;
    --secret)
      SECRET="${2-}"
      shift 2
      ;;
    --secret-env)
      SECRET_ENV="${2-}"
      shift 2
      ;;
    --payload-file)
      PAYLOAD_FILE="${2-}"
      shift 2
      ;;
    --event-id)
      EVENT_ID="${2-}"
      shift 2
      ;;
    --source)
      SOURCE="${2-}"
      shift 2
      ;;
    --event-type)
      EVENT_TYPE="${2-}"
      shift 2
      ;;
    --severity)
      SEVERITY="${2-}"
      shift 2
      ;;
    --summary)
      SUMMARY="${2-}"
      shift 2
      ;;
    --body)
      BODY="${2-}"
      shift 2
      ;;
    --occurred-at)
      OCCURRED_AT="${2-}"
      shift 2
      ;;
    --dedupe-key)
      DEDUPE_KEY="${2-}"
      shift 2
      ;;
    --link)
      LINKS+=("${2-}")
      shift 2
      ;;
    --timeout)
      TIMEOUT="${2-}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$URL" || -z "$SENDER" ]]; then
  printf 'missing required arguments: --url --sender\n' >&2
  exit 1
fi

if [[ -n "$PAYLOAD_FILE" && ( -n "$EVENT_ID" || -n "$SOURCE" || -n "$EVENT_TYPE" || -n "$SEVERITY" || -n "$SUMMARY" || -n "$BODY" || -n "$OCCURRED_AT" || -n "$DEDUPE_KEY" || ${#LINKS[@]} -gt 0 ) ]]; then
  printf 'cannot combine --payload-file with flag-based event fields\n' >&2
  exit 1
fi

SECRET_VALUE="$(resolve_secret)"

body_file="$(mktemp)"
response_file="$(mktemp)"
cleanup() {
  rm -f "$body_file" "$response_file"
}
trap cleanup EXIT

if [[ -n "$PAYLOAD_FILE" ]]; then
  copy_payload_file "$PAYLOAD_FILE" >"$body_file"
else
  validate_flag_mode
  build_flag_payload >"$body_file"
fi

timestamp="$(date +%s)"
signature="$(
  {
    printf '%s.' "$timestamp"
    cat "$body_file"
  } | openssl dgst -sha256 -hmac "$SECRET_VALUE" -hex | sed 's/^.* //'
)"

http_code="$(
  curl -sS \
    --output "$response_file" \
    --write-out '%{http_code}' \
    --max-time "$TIMEOUT" \
    -X POST "$URL" \
    -H 'Content-Type: application/json' \
    -H "X-AlertHub-Sender: $SENDER" \
    -H "X-AlertHub-Timestamp: $timestamp" \
    -H "X-AlertHub-Signature: v1=$signature" \
    --data-binary "@$body_file"
)"

cat "$response_file"
printf 'status=%s\n' "$http_code" >&2

if [[ "$http_code" == 2* ]]; then
  exit 0
fi
exit 1
