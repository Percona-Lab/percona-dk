#!/usr/bin/env bash
# Daily summary email of MCP usage on the shared sherpa instance.
# Counts only - no query text. Runs once a day via the timer below.
#
# Triggered by percona-dk-daily-summary.timer at ~00:10 UTC.
# Exits 0 unconditionally so the timer never enters a failed state.

set -u

TO="${SUMMARY_EMAIL:-dennis.kittrell@percona.com}"
HOST=$(hostname -f 2>/dev/null || hostname)
TS=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

# Window: previous full UTC day. Override DATE for backfills.
DATE="${1:-$(date -u -d 'yesterday' '+%Y-%m-%d')}"
SINCE="${DATE} 00:00:00 UTC"
UNTIL="${DATE} 23:59:59 UTC"

if ! command -v msmtp >/dev/null 2>&1 || [ ! -r "$HOME/.msmtprc" ]; then
    echo "summary: msmtp not configured; cannot send email" >&2
    exit 0
fi

# All search log lines for the day.
LINES=$(sudo -n journalctl _SYSTEMD_USER_UNIT=percona-dk-mcp.service \
    --since "$SINCE" --until "$UNTIL" --no-pager 2>/dev/null \
    | grep "MCP search:" || true)

TOTAL=$(printf '%s\n' "$LINES" | grep -c "MCP search:" || true)
TOTAL=${TOTAL:-0}

# Per-hour breakdown (UTC).
if [ "$TOTAL" -gt 0 ]; then
    HOURLY=$(printf '%s\n' "$LINES" | awk '{print $3}' | cut -d: -f1 \
        | sort | uniq -c | awk '{printf "  %02d:00 UTC  %5d\n", $2, $1}')
    PEAK_LINE=$(printf '%s\n' "$LINES" | awk '{print $3}' | cut -d: -f1 \
        | sort | uniq -c | sort -rn | head -1)
    PEAK_COUNT=$(echo "$PEAK_LINE" | awk '{print $1}')
    PEAK_HOUR=$(echo "$PEAK_LINE" | awk '{print $2}')
    AVG_PER_HOUR=$(awk -v t="$TOTAL" 'BEGIN { printf "%.1f", t/24 }')
    ACTIVE_HOURS=$(printf '%s\n' "$LINES" | awk '{print $3}' | cut -d: -f1 | sort -u | wc -l)
else
    HOURLY="  (no traffic)"
    PEAK_COUNT=0
    PEAK_HOUR="-"
    AVG_PER_HOUR="0.0"
    ACTIVE_HOURS=0
fi

# Health snapshot (right now, not for the report day).
HEALTH=$(curl -sf --max-time 5 http://localhost:8000/health 2>/dev/null || echo '{}')
DOC_COUNT=$(printf '%s' "$HEALTH" | grep -oE '"doc_count":[0-9]+' | cut -d: -f2)
DOC_COUNT=${DOC_COUNT:-unknown}
UPTIME_S=$(printf '%s' "$HEALTH" | grep -oE '"uptime_seconds":[0-9.]+' | cut -d: -f2)
UPTIME_H=$(awk -v s="${UPTIME_S:-0}" 'BEGIN { printf "%.1f", s/3600 }')

SUBJECT="[percona-dk] usage summary $DATE  ($TOTAL searches)"

{
    printf 'From: percona-dk reports <%s>\n' "$TO"
    printf 'To: %s\n' "$TO"
    printf 'Subject: %s\n' "$SUBJECT"
    printf 'Content-Type: text/plain; charset=utf-8\n'
    printf '\n'
    printf 'Daily usage summary for the shared percona-dk MCP server.\n\n'
    printf 'Date (UTC):      %s\n' "$DATE"
    printf 'Host:            %s\n' "$HOST"
    printf 'Generated at:    %s\n' "$TS"
    printf '\n'
    printf -- '--- Traffic ---\n'
    printf 'Total searches:  %d\n' "$TOTAL"
    printf 'Active hours:    %d / 24\n' "$ACTIVE_HOURS"
    printf 'Peak hour:       %s UTC  (%d searches)\n' "$PEAK_HOUR" "$PEAK_COUNT"
    printf 'Avg per hour:    %s\n' "$AVG_PER_HOUR"
    printf '\n'
    printf -- '--- Hourly breakdown (UTC) ---\n'
    printf '%s\n' "$HOURLY"
    printf '\n'
    printf -- '--- Service health (now) ---\n'
    printf 'Chunks indexed:  %s\n' "$DOC_COUNT"
    printf 'API uptime:      %s hours\n' "$UPTIME_H"
} | msmtp -t 2>>"$HOME/.msmtp.log" || echo "summary: msmtp send failed (see ~/.msmtp.log)" >&2

exit 0
