#!/bin/bash
# Runs a command and sends its output to the Xcore log service while it runs (scripts/stream_log.py), keeping the command's own
# exit code. Without a log stream configured (or without python3) it simply runs the command, exactly as before.
#
#   scripts/run_streamed.sh <command> [args...]
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -z "${XLOG_TOKEN:-}" ] || [ ! -f "$HERE/stream_log.py" ] || ! command -v python3 >/dev/null 2>&1; then
  exec "$@"
fi
set -o pipefail
"$@" 2>&1 | python3 "$HERE/stream_log.py"
exit "${PIPESTATUS[0]}"
