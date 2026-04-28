#!/usr/bin/env bash
# Daily MCP usage report: parses journalctl for the previous UTC day and
# POSTs aggregate stats to the Google Apps Script webhook configured in
# ~/.percona-dk-webhook.env (WEBHOOK_URL + WEBHOOK_SECRET).
#
# Triggered by percona-dk-usage-report.timer at ~00:05 UTC daily.

set -u

ENV_FILE="${ENV_FILE:-$HOME/.percona-dk-webhook.env}"
if [ ! -r "$ENV_FILE" ]; then
    echo "report: $ENV_FILE not found; cannot post stats" >&2
    exit 0
fi
# shellcheck source=/dev/null
. "$ENV_FILE"

if [ -z "${WEBHOOK_URL:-}" ] || [ -z "${WEBHOOK_SECRET:-}" ]; then
    echo "report: WEBHOOK_URL or WEBHOOK_SECRET not set in $ENV_FILE" >&2
    exit 0
fi

# Yesterday in UTC (the day we're reporting on).
DATE=$(date -u -d 'yesterday' '+%Y-%m-%d')
SINCE="${DATE} 00:00:00 UTC"
UNTIL="${DATE} 23:59:59 UTC"

LINES=$(journalctl --user -u percona-dk-mcp.service \
    --since "$SINCE" --until "$UNTIL" --no-pager 2>/dev/null \
    | grep "MCP search:" || true)

TOTAL=$(printf '%s\n' "$LINES" | grep -c "MCP search:" || true)

# Hour buckets from the systemd timestamp (e.g. "Apr 28 14:23:11").
PEAK=$(printf '%s\n' "$LINES" \
    | awk '{print $3}' | cut -d: -f1 \
    | sort | uniq -c | sort -rn | head -1)
PEAK_COUNT=$(echo "$PEAK" | awk '{print $1}')
PEAK_HOUR=$(echo "$PEAK" | awk '{print $2}')
PEAK_COUNT=${PEAK_COUNT:-0}
PEAK_HOUR=${PEAK_HOUR:-0}

# Top queries: extract the quoted query string after "MCP search: '...'"
QUERIES=$(printf '%s\n' "$LINES" \
    | sed -nE "s/.*MCP search: '([^']*)'.*/\1/p")
DISTINCT=$(printf '%s\n' "$QUERIES" | sort -u | grep -c . || true)

# Build top-20 as a JSON array of [query, count] pairs.
TOP_JSON=$(printf '%s\n' "$QUERIES" \
    | sort | uniq -c | sort -rn | head -20 \
    | awk '{
        count=$1; $1="";
        sub(/^ /, "", $0);
        gsub(/\\/, "\\\\", $0);
        gsub(/"/, "\\\"", $0);
        printf "%s[\"%s\",%d]", (NR==1?"":","), $0, count;
      }')
TOP_JSON="[${TOP_JSON}]"

PAYLOAD=$(cat <<EOF
{
  "secret": "${WEBHOOK_SECRET}",
  "date": "${DATE}",
  "total_searches": ${TOTAL:-0},
  "peak_hour": ${PEAK_HOUR},
  "peak_hour_count": ${PEAK_COUNT},
  "distinct_queries": ${DISTINCT:-0},
  "top_queries": ${TOP_JSON}
}
EOF
)

echo "report: ${DATE} total=${TOTAL} peak=${PEAK_HOUR}h(${PEAK_COUNT}) distinct=${DISTINCT}"

# Apps Script returns 302 on successful POST (redirect to a one-shot result
# page we do not need to read). Treat 302 as success; do not follow.
CODE=$(curl -sS -X POST "$WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    --max-time 30 \
    -o /dev/null \
    -w "%{http_code}" \
    --data "$PAYLOAD" 2>&1) || {
    echo "report: POST failed (curl exit nonzero)" >&2
    exit 0
}

case "$CODE" in
    200|302) echo "report: webhook accepted (HTTP $CODE)" ;;
    *)       echo "report: webhook unexpected HTTP $CODE" >&2 ;;
esac
exit 0
