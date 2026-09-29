#!/bin/bash
# Cron-ready wrapper for transcript indexing.
# Scheduled via ~/Library/LaunchAgents/com.openclaw.transcript-index.plist
# (source: workspace launch-agents/com.openclaw.transcript-index.plist),
# every 5 min (consolidated from crontab + 30-min plist on 2026-09-29). The
# indexer was stale
# since whenever compressed transcript_events rows (event_zstd, added by
# OpenClaw sometime after 2026-07-31) started appearing and crashing every
# manual run with an uncaught TypeError on the None event_json.
#
# Deliberately no `-e` and no `2>/dev/null`: a failing run must produce
# visible stderr in the log, not exit silently. The prior `2>/dev/null` here
# is exactly what hid the crash for ~8 weeks.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$SCRIPT_DIR/transcript-search.py" index --quiet
RC=$?
echo "transcript-index exited rc=$RC" >&2
exit $RC
