#!/usr/bin/env bash
# Triggered by systemd OnFailure= when percona-dk-ingest.service exits non-zero.
# Sends an email via msmtp using credentials in ~/.msmtprc + ~/.msmtp.password.
# Exits 0 unconditionally so the alert service itself never enters a failed state
# (which would suppress subsequent alerts).

set -u

TO="${ALERT_EMAIL:-dennis.kittrell@percona.com}"
UNIT="${1:-percona-dk-ingest.service}"
HOST=$(hostname -f 2>/dev/null || hostname)
TS=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

STATUS=$(systemctl --user status "$UNIT" --no-pager -n 60 2>&1 || true)

if ! command -v msmtp >/dev/null 2>&1; then
    echo "alert: msmtp not installed; cannot send email" >&2
    exit 0
fi

if [ ! -r "$HOME/.msmtprc" ]; then
    echo "alert: ~/.msmtprc not found; cannot send email" >&2
    exit 0
fi

{
    printf 'From: percona-dk alerts <%s>\n' "$TO"
    printf 'To: %s\n' "$TO"
    printf 'Subject: [percona-dk] ingest FAILED on %s at %s\n' "$HOST" "$TS"
    printf 'Content-Type: text/plain; charset=utf-8\n'
    printf '\n'
    printf 'The percona-dk-ingest service exited non-zero.\n\n'
    printf 'Host:  %s\n' "$HOST"
    printf 'Unit:  %s\n' "$UNIT"
    printf 'Time:  %s\n' "$TS"
    printf '\n'
    printf 'Inspect:\n'
    printf '  ssh %s '"'"'systemctl --user status %s'"'"'\n' "$HOST" "$UNIT"
    printf '  ssh %s '"'"'journalctl --user -u %s -n 200 --no-pager'"'"'\n' "$HOST" "$UNIT"
    printf '\n'
    printf -- '--- systemctl status (last 60 lines) ---\n'
    printf '%s\n' "$STATUS"
} | msmtp -t 2>>"$HOME/.msmtp.log" || echo "alert: msmtp send failed (see ~/.msmtp.log)" >&2

exit 0
