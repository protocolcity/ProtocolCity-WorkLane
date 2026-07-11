#!/usr/bin/env bash
set -euo pipefail

# Install WorkLane as a macOS Login Item (Launch Agent), so it
# survives reboots without any host repo (tradeOS or otherwise) involved.
#
# Usage:
#   scripts/install-macos-service.sh install [--python /path/to/python] [--port N] [--host HOST]
#   scripts/install-macos-service.sh uninstall
#   scripts/install-macos-service.sh install --dry-run   # print the plist, write nothing, load nothing
#
# --python defaults to `python3` on PATH. Pass the interpreter that has
# `worklane` importable (e.g. a host venv's python) if it isn't on
# PATH for launchd's minimal environment.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_DST="$HOME/Library/LaunchAgents/com.worklane.server.plist"
LABEL="com.worklane.server"
LOG_DIR="$ROOT/worklane/local/logs"

ACTION="${1:-}"
shift || true

PYTHON_BIN="python3"
TASK_HOST_VAL="${TASK_HOST:-127.0.0.1}"
TASK_PORT_VAL="${TASK_PORT:-8799}"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      shift
      PYTHON_BIN="${1:-python3}"
      ;;
    --python=*)
      PYTHON_BIN="${1#--python=}"
      ;;
    --host)
      shift
      TASK_HOST_VAL="${1:-127.0.0.1}"
      ;;
    --host=*)
      TASK_HOST_VAL="${1#--host=}"
      ;;
    --port)
      shift
      TASK_PORT_VAL="${1:-8799}"
      ;;
    --port=*)
      TASK_PORT_VAL="${1#--port=}"
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
  shift
done

case "$ACTION" in
  uninstall)
    echo "Stopping WorkLane LaunchAgent..."
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "Done. TP will no longer auto-start at login."
    exit 0
    ;;
  install)
    ;;
  *)
    echo "Usage: $0 install [--python PATH] [--host HOST] [--port N] [--dry-run]" >&2
    echo "       $0 uninstall" >&2
    exit 2
    ;;
esac

PLIST_BODY=$(cat <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON_BIN}</string>
    <string>-m</string>
    <string>worklane.server</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin</string>
    <key>TASK_HOST</key>
    <string>${TASK_HOST_VAL}</string>
    <key>TASK_PORT</key>
    <string>${TASK_PORT_VAL}</string>
    <key>PYTHONDONTWRITEBYTECODE</key>
    <string>1</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/server.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/server.err.log</string>
</dict>
</plist>
PLIST_EOF
)

if [[ "$DRY_RUN" == "1" ]]; then
  echo "$PLIST_BODY"
  exit 0
fi

mkdir -p "$LOG_DIR"
mkdir -p "$HOME/Library/LaunchAgents"
launchctl unload "$PLIST_DST" 2>/dev/null || true
printf '%s\n' "$PLIST_BODY" > "$PLIST_DST"
launchctl load "$PLIST_DST"

echo ""
echo "  WorkLane installed as a macOS Login Item."
echo ""
echo "  What this means:"
echo "    - Starts automatically when you log in"
echo "    - Restarts if it crashes"
echo "    - Runs: ${PYTHON_BIN} -m worklane.server (TASK_HOST=${TASK_HOST_VAL} TASK_PORT=${TASK_PORT_VAL})"
echo "    - Logs: ${LOG_DIR}/server.log"
echo ""
echo "  Commands:"
echo "    launchctl stop $LABEL     # stop"
echo "    launchctl start $LABEL    # start"
echo "    launchctl list | grep worklane  # check status"
echo ""
echo "  To remove:"
echo "    scripts/install-macos-service.sh uninstall"
echo ""
