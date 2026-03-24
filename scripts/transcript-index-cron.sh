#!/bin/bash
# Cron-ready wrapper for transcript indexing
# Add to crontab: */30 * * * * /path/to/scripts/transcript-index-cron.sh
# Or via OpenClaw: openclaw cron add transcript-indexer --every 30m --message "Run: bash scripts/transcript-index-cron.sh"
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/transcript-search.py" index --quiet 2>/dev/null
