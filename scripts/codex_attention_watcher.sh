#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHER_SCRIPT="$SCRIPT_DIR/codex_attention_watcher.py"
NOTIFY_SCRIPT="$SCRIPT_DIR/codex_notify.sh"
MCP_SCRIPT="$SCRIPT_DIR/codex_alert_mcp.py"

ENV_FILE_DEFAULT="$HOME/.codex/codex_alert_hub.env"
STATE_FILE_DEFAULT="$HOME/.codex/tmp/codex_attention_watcher_state.json"
LOG_FILE_DEFAULT="$HOME/.codex/log/codex_attention_watcher.log"
CONFIG_FILE_DEFAULT="$HOME/.codex/config.toml"

UNIT_NAME="codex-attention-watcher.service"
PLIST_ID="com.alerthub.codex-attention-watcher"

usage() {
  cat <<'EOF'
Usage:
  codex_attention_watcher.sh install --url URL --sender ID --source SOURCE [--secret SECRET | --secret-env ENV]
                                     [--env-file FILE] [--state-file FILE] [--log-file FILE]
                                     [--poll-seconds FLOAT] [--no-start]
  codex_attention_watcher.sh start|stop|restart|status|logs [lines]|uninstall [--env-file FILE]
EOF
}

platform_name() {
  if [[ -n "${ALERT_HUB_CODEX_TEST_OS:-}" ]]; then
    printf '%s\n' "$ALERT_HUB_CODEX_TEST_OS"
    return
  fi
  uname -s
}

uid_value() {
  if [[ -n "${ALERT_HUB_CODEX_TEST_UID:-}" ]]; then
    printf '%s\n' "$ALERT_HUB_CODEX_TEST_UID"
    return
  fi
  id -u
}

resolve_secret() {
  local secret="${INSTALL_SECRET:-}"
  if [[ -n "$secret" ]]; then
    printf '%s' "$secret"
    return
  fi
  local env_name="${INSTALL_SECRET_ENV:-ALERT_HUB_SECRET}"
  local resolved="${!env_name:-}"
  if [[ -z "$resolved" ]]; then
    printf 'missing secret; set --secret or environment variable %s\n' "$env_name" >&2
    exit 1
  fi
  printf '%s' "$resolved"
}

write_env_file() {
  local secret_value="$1"
  mkdir -p "$(dirname "$ENV_FILE")" "$(dirname "$STATE_FILE")" "$(dirname "$LOG_FILE")"
  if [[ "$INSTALL_URL" == *$'\n'* || "$INSTALL_SENDER" == *$'\n'* || "$INSTALL_SOURCE" == *$'\n'* || "$secret_value" == *$'\n'* ]]; then
    printf 'runtime values must not contain newlines\n' >&2
    exit 1
  fi
  cat >"$ENV_FILE" <<EOF
ALERT_HUB_CODEX_URL=$INSTALL_URL
ALERT_HUB_CODEX_SENDER=$INSTALL_SENDER
ALERT_HUB_SECRET=$secret_value
ALERT_HUB_CODEX_SOURCE=$INSTALL_SOURCE
ALERT_HUB_CODEX_POLL_SECONDS=$POLL_SECONDS
ALERT_HUB_CODEX_STATE_FILE=$STATE_FILE
ALERT_HUB_CODEX_LOG_FILE=$LOG_FILE
EOF
  chmod 600 "$ENV_FILE"
}

patch_codex_config() {
  local mode="$1"
  mkdir -p "$(dirname "$CONFIG_FILE")"
  export CODEX_PATCH_MODE="$mode"
  export CODEX_CONFIG_FILE="$CONFIG_FILE"
  export CODEX_NOTIFY_SCRIPT="$NOTIFY_SCRIPT"
  export CODEX_MCP_SCRIPT="$MCP_SCRIPT"
  export CODEX_ENV_FILE="$ENV_FILE"
  export CODEX_REPO_ROOT="$REPO_ROOT"
  python3 - <<'PY'
from __future__ import annotations

import os
import re
from pathlib import Path

mode = os.environ["CODEX_PATCH_MODE"]
config_path = Path(os.environ["CODEX_CONFIG_FILE"]).expanduser()
notify_script = os.environ["CODEX_NOTIFY_SCRIPT"]
mcp_script = os.environ["CODEX_MCP_SCRIPT"]
env_file = os.environ["CODEX_ENV_FILE"]
repo_root = os.environ["CODEX_REPO_ROOT"]

text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""


def remove_mcp_section(raw: str) -> str:
    pattern = re.compile(r"(?ms)^\[mcp_servers\.alert_hub\]\n.*?(?=^\[|\Z)")
    return re.sub(pattern, "", raw).rstrip() + ("\n" if raw.strip() else "")


text = remove_mcp_section(text)

notify_line = f'notify = ["{notify_script}"]'
notify_pattern = re.compile(r"(?m)^notify\s*=.*$")
match = notify_pattern.search(text)

if mode == "install":
    if match:
        text = notify_pattern.sub(notify_line, text, count=1)
    else:
        text = f"{notify_line}\n\n{text}" if text.strip() else f"{notify_line}\n"
    block = "\n".join(
        [
            "[mcp_servers.alert_hub]",
            'command = "python3"',
            f'args = ["{mcp_script}", "--env-file", "{env_file}"]',
            f'cwd = "{repo_root}"',
            "startup_timeout_sec = 20",
            "tool_timeout_sec = 20",
        ]
    )
    text = text.rstrip() + "\n\n" + block + "\n"
else:
    if match and match.group(0).strip() == notify_line:
        start, end = match.span()
        text = (text[:start] + text[end:]).lstrip("\n")

config_path.write_text(text, encoding="utf-8")
PY
}

linux_unit_file() {
  printf '%s\n' "$HOME/.config/systemd/user/$UNIT_NAME"
}

macos_plist_file() {
  printf '%s\n' "$HOME/Library/LaunchAgents/$PLIST_ID.plist"
}

install_linux_service() {
  local unit_file
  unit_file="$(linux_unit_file)"
  mkdir -p "$(dirname "$unit_file")"
  cat >"$unit_file" <<EOF
[Unit]
Description=Codex attention watcher for Alert Hub

[Service]
Type=simple
WorkingDirectory=$REPO_ROOT
ExecStart=python3 $WATCHER_SCRIPT --env-file $ENV_FILE
Restart=always
RestartSec=2
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=default.target
EOF
  if [[ "$NO_START" == "1" ]]; then
    return
  fi
  systemctl --user daemon-reload >/dev/null
  systemctl --user enable --now "$UNIT_NAME" >/dev/null
}

install_macos_service() {
  local plist_file
  plist_file="$(macos_plist_file)"
  mkdir -p "$(dirname "$plist_file")" "$(dirname "$LOG_FILE")"
  cat >"$plist_file" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$PLIST_ID</string>
  <key>ProgramArguments</key>
  <array>
    <string>python3</string>
    <string>$WATCHER_SCRIPT</string>
    <string>--env-file</string>
    <string>$ENV_FILE</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$REPO_ROOT</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_FILE</string>
  <key>StandardErrorPath</key>
  <string>$LOG_FILE</string>
</dict>
</plist>
EOF
  if [[ "$NO_START" == "1" ]]; then
    return
  fi
  launchctl bootout "gui/$(uid_value)" "$plist_file" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(uid_value)" "$plist_file"
}

start_service() {
  case "$(platform_name)" in
    Linux)
      systemctl --user daemon-reload >/dev/null
      systemctl --user enable --now "$UNIT_NAME" >/dev/null
      ;;
    Darwin)
      launchctl bootout "gui/$(uid_value)" "$(macos_plist_file)" >/dev/null 2>&1 || true
      launchctl bootstrap "gui/$(uid_value)" "$(macos_plist_file)"
      ;;
    *)
      printf 'unsupported platform\n' >&2
      exit 1
      ;;
  esac
}

stop_service() {
  case "$(platform_name)" in
    Linux)
      systemctl --user stop "$UNIT_NAME" >/dev/null 2>&1 || true
      ;;
    Darwin)
      launchctl bootout "gui/$(uid_value)" "$(macos_plist_file)" >/dev/null 2>&1 || true
      ;;
    *)
      printf 'unsupported platform\n' >&2
      exit 1
      ;;
  esac
}

restart_service() {
  case "$(platform_name)" in
    Linux)
      systemctl --user daemon-reload >/dev/null
      systemctl --user restart "$UNIT_NAME"
      ;;
    Darwin)
      launchctl bootout "gui/$(uid_value)" "$(macos_plist_file)" >/dev/null 2>&1 || true
      launchctl bootstrap "gui/$(uid_value)" "$(macos_plist_file)"
      ;;
    *)
      printf 'unsupported platform\n' >&2
      exit 1
      ;;
  esac
}

status_service() {
  case "$(platform_name)" in
    Linux)
      systemctl --user --no-pager --full status "$UNIT_NAME"
      ;;
    Darwin)
      launchctl print "gui/$(uid_value)/$PLIST_ID"
      ;;
    *)
      printf 'unsupported platform\n' >&2
      exit 1
      ;;
  esac
}

logs_service() {
  local lines="${1:-80}"
  case "$(platform_name)" in
    Linux)
      journalctl --user -u "$UNIT_NAME" -n "$lines" --no-pager
      ;;
    Darwin)
      tail -n "$lines" "$LOG_FILE"
      ;;
    *)
      printf 'unsupported platform\n' >&2
      exit 1
      ;;
  esac
}

uninstall_service() {
  stop_service
  case "$(platform_name)" in
    Linux)
      systemctl --user disable "$UNIT_NAME" >/dev/null 2>&1 || true
      rm -f "$(linux_unit_file)"
      systemctl --user daemon-reload >/dev/null
      ;;
    Darwin)
      rm -f "$(macos_plist_file)"
      ;;
    *)
      printf 'unsupported platform\n' >&2
      exit 1
      ;;
  esac
}

COMMAND="${1:-}"
shift || true

ENV_FILE="$ENV_FILE_DEFAULT"
STATE_FILE="$STATE_FILE_DEFAULT"
LOG_FILE="$LOG_FILE_DEFAULT"
CONFIG_FILE="$CONFIG_FILE_DEFAULT"
POLL_SECONDS="2.0"
INSTALL_URL=""
INSTALL_SENDER=""
INSTALL_SOURCE=""
INSTALL_SECRET=""
INSTALL_SECRET_ENV=""
NO_START="0"

case "$COMMAND" in
  install)
    while (($# > 0)); do
      case "$1" in
        --url) INSTALL_URL="${2-}"; shift 2 ;;
        --sender) INSTALL_SENDER="${2-}"; shift 2 ;;
        --source) INSTALL_SOURCE="${2-}"; shift 2 ;;
        --secret) INSTALL_SECRET="${2-}"; shift 2 ;;
        --secret-env) INSTALL_SECRET_ENV="${2-}"; shift 2 ;;
        --env-file) ENV_FILE="${2-}"; shift 2 ;;
        --state-file) STATE_FILE="${2-}"; shift 2 ;;
        --log-file) LOG_FILE="${2-}"; shift 2 ;;
        --poll-seconds) POLL_SECONDS="${2-}"; shift 2 ;;
        --no-start) NO_START="1"; shift ;;
        *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 1 ;;
      esac
    done
    if [[ -z "$INSTALL_URL" || -z "$INSTALL_SENDER" || -z "$INSTALL_SOURCE" ]]; then
      printf 'missing required arguments: --url --sender --source\n' >&2
      exit 1
    fi
    ENV_FILE="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser())' "$ENV_FILE")"
    STATE_FILE="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser())' "$STATE_FILE")"
    LOG_FILE="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser())' "$LOG_FILE")"
    CONFIG_FILE="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser())' "$CONFIG_FILE")"
    SECRET_VALUE="$(resolve_secret)"
    write_env_file "$SECRET_VALUE"
    patch_codex_config install
    case "$(platform_name)" in
      Linux) install_linux_service ;;
      Darwin) install_macos_service ;;
      *) printf 'unsupported platform\n' >&2; exit 1 ;;
    esac
    ;;
  start)
    start_service
    ;;
  stop)
    stop_service
    ;;
  restart)
    restart_service
    ;;
  status)
    status_service
    ;;
  logs)
    logs_service "${1:-80}"
    ;;
  uninstall)
    uninstall_service
    patch_codex_config remove
    rm -f "$ENV_FILE"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
