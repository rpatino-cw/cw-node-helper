#!/bin/bash
# ============================================================================
# watch_queue.sh — macOS notification watcher for US-SITE-01A DO queue
#
# Polls your Jira queue every 5 minutes. When a new ticket appears that
# wasn't there before, pops a macOS notification.
#
# Usage:
#   cd ~/Documents/cw-node-helper (old)
#   source load_env.sh
#   bash watch_queue.sh
#
# To run in background:
#   bash watch_queue.sh &
#
# To stop:
#   kill %1   (or close the terminal)
# ============================================================================

SITE="US-SITE-01A"
INTERVAL=300  # seconds between checks (5 min)
STATE_FILE="/tmp/cw-queue-state.json"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "  Watching DO queue for $SITE (every ${INTERVAL}s)"
echo "  Press Ctrl+C to stop."
echo ""

while true; do
    # Fetch current queue as JSON (open tickets at your site)
    NEW=$(python3 "$SCRIPT_DIR/get_node_context.py" queue --site "$SITE" --json 2>/dev/null)

    if [ -f "$STATE_FILE" ]; then
        OLD=$(cat "$STATE_FILE")

        # Extract ticket keys from new and old
        NEW_KEYS=$(echo "$NEW" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for t in data:
        print(t['key'])
except:
    pass
" 2>/dev/null | sort)

        OLD_KEYS=$(echo "$OLD" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for t in data:
        print(t['key'])
except:
    pass
" 2>/dev/null | sort)

        # Find tickets in NEW that weren't in OLD
        DIFF=$(comm -23 <(echo "$NEW_KEYS") <(echo "$OLD_KEYS"))

        if [ -n "$DIFF" ]; then
            COUNT=$(echo "$DIFF" | wc -l | tr -d ' ')
            FIRST=$(echo "$DIFF" | head -1)

            # Get summary for the first new ticket
            SUMMARY=$(echo "$NEW" | python3 -c "
import sys, json
key = '$FIRST'
try:
    data = json.load(sys.stdin)
    for t in data:
        if t['key'] == key:
            tag = t.get('service_tag', '')
            status = t.get('status', '')
            print(f'{tag} [{status}]')
            break
except:
    print('')
" 2>/dev/null)

            # macOS notification
            osascript -e "display notification \"$COUNT new: $FIRST $SUMMARY\" with title \"CW Node Helper\" subtitle \"$SITE queue\" sound name \"Glass\""

            # Also print to terminal
            echo "  [$(date '+%H:%M')] NEW: $DIFF"
        fi
    else
        # First run — count current tickets
        COUNT=$(echo "$NEW" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null)
        echo "  [$(date '+%H:%M')] Initial state: $COUNT open tickets"
    fi

    # Save current state
    echo "$NEW" > "$STATE_FILE"

    sleep $INTERVAL
done
