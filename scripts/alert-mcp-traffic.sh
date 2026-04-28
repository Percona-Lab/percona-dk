#!/usr/bin/env bash
# Periodic check: emails an alert when MCP search traffic over the last
# WINDOW_MIN minutes exceeds THRESHOLD. Hour-long cooldown prevents flapping.
#
# Triggered by percona-dk-traffic-alert.timer (every 15 min).
# Exits 0 unconditionally so the timer never enters a failed state.

set -u

TO="${ALERT_EMAIL:-dennis.kittrell@percona.com}"
WINDOW_MIN="${WINDOW_MIN:-15}"
THRESHOLD="${THRESHOLD:-50}"
COOLDOWN_MIN="${COOLDOWN_MIN:-60}"
COOLDOWN_FILE="${COOLDOWN_FILE:-$HOME/.cache/percona-dk-traffic-alert.last}"
HOST=$(hostname -f 2>/dev/null || hostname)
TS=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

mkdir -p "$(dirname "$COOLDOWN_FILE")"

COUNT=$(sudo -n journalctl _SYSTEMD_USER_UNIT=percona-dk-mcp.service \
    --since "${WINDOW_MIN} min ago" --no-pager 2>/dev/null \
    | grep -c "MCP search:" || true)

echo "traffic check: $COUNT searches in last ${WINDOW_MIN}m (threshold $THRESHOLD)"

if [ "$COUNT" -lt "$THRESHOLD" ]; then
    exit 0
fi

if [ -f "$COOLDOWN_FILE" ]; then
    LAST=$(cat "$COOLDOWN_FILE" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    AGE=$(( NOW - LAST ))
    if [ "$AGE" -lt "$(( COOLDOWN_MIN * 60 ))" ]; then
        echo "in cooldown ($((AGE/60))m of ${COOLDOWN_MIN}m), skipping email"
        exit 0
    fi
fi

if ! command -v msmtp >/dev/null 2>&1 || [ ! -r "$HOME/.msmtprc" ]; then
    echo "alert: msmtp not configured; cannot send email" >&2
    exit 0
fi

TOP_QUERIES=$(sudo -n journalctl _SYSTEMD_USER_UNIT=percona-dk-mcp.service \
    --since "${WINDOW_MIN} min ago" --no-pager 2>/dev/null \
    | grep "MCP search:" \
    | sed -E "s/.*MCP search: '([^']*)'.*/\1/" \
    | sort | uniq -c | sort -rn | head -10)

{
    printf 'From: percona-dk alerts <%s>\n' "$TO"
    printf 'To: %s\n' "$TO"
    printf 'Subject: [percona-dk] traffic spike: %d searches in %dm on %s\n' "$COUNT" "$WINDOW_MIN" "$HOST"
    printf 'Content-Type: text/plain; charset=utf-8\n'
    printf '\n'
    printf 'MCP search volume crossed the alert threshold.\n\n'
    printf 'Host:      %s\n' "$HOST"
    printf 'Time:      %s\n' "$TS"
    printf 'Window:    last %d min\n' "$WINDOW_MIN"
    printf 'Count:     %d searches\n' "$COUNT"
    printf 'Threshold: %d\n' "$THRESHOLD"
    printf '\n'
    printf 'sherpa is 2 vCPU / 8 GB. Sustained >5 RPS will start to hurt.\n'
    printf 'If this looks organic and ongoing, consider sizing up the VM\n'
    printf 'or adding a per-IP rate limit at the LB.\n'
    printf '\n'
    printf -- '--- top queries in window ---\n'
    printf '%s\n' "$TOP_QUERIES"
} | msmtp -t 2>>"$HOME/.msmtp.log" || echo "alert: msmtp send failed (see ~/.msmtp.log)" >&2

date +%s > "$COOLDOWN_FILE"
exit 0
