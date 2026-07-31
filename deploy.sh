#!/usr/bin/env bash
# deploy.sh - Set up percona-dk on a remote VM with systemd services
#
# Prerequisites: Python 3.11+, git, (optional) msmtp for failure alerts
#
# What it does:
#   1. Creates venv + installs percona-dk
#   2. Runs initial doc ingestion (22 repos)
#   3. Installs systemd user services:
#      - percona-dk-mcp   (MCP over SSE on port 8402)
#      - percona-dk-api   (REST API on port 8000)
#   4. Installs a systemd timer for daily re-ingestion
#   5. Installs an OnFailure= email alert (requires ~/.msmtprc)

set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$INSTALL_DIR/.venv"
PYTHON="${PYTHON:-python3.12}"
SYSTEMD_DIR="$HOME/.config/systemd/user"
MCP_PORT="${MCP_PORT:-8402}"
API_PORT="${API_PORT:-8000}"
ALERT_EMAIL="${ALERT_EMAIL:-dennis.kittrell@percona.com}"

echo "============================================"
echo "  Percona Developer Knowledge - Remote Setup"
echo "============================================"
echo ""
echo "Install dir: $INSTALL_DIR"
echo ""

# ---- 1. Create venv and install ----
echo "[1/4] Setting up Python environment ..."
if [ ! -d "$VENV" ]; then
    $PYTHON -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
# -c constraints.txt keeps a redeploy from silently upgrading a dependency under
# a working server; see the header of that file for how to move a pin forward.
"$VENV/bin/pip" install --quiet -c "$INSTALL_DIR/constraints.txt" .
echo "  Installed."

# ---- 2. Ensure .env exists ----
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "[2/4] Creating .env from .env.example ..."
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
else
    echo "[2/4] .env already exists - keeping current config."
fi

# ---- 3. Initial ingestion ----
echo "[3/4] Running initial doc ingestion (this takes a few minutes on first run) ..."
"$VENV/bin/python" -m percona_dk.ingest

# ---- 4. Install systemd services ----
echo "[4/4] Installing systemd services ..."
mkdir -p "$SYSTEMD_DIR"

# MCP SSE service
cat > "$SYSTEMD_DIR/percona-dk-mcp.service" <<EOF
[Unit]
Description=Percona DK MCP Server (SSE on port $MCP_PORT)
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
Environment=DOTENV_PATH=$INSTALL_DIR/.env
ExecStart=$VENV/bin/python -m percona_dk.mcp_server --transport sse --port $MCP_PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

# REST API service
cat > "$SYSTEMD_DIR/percona-dk-api.service" <<EOF
[Unit]
Description=Percona DK REST API (port $API_PORT)
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
Environment=DOTENV_PATH=$INSTALL_DIR/.env
ExecStart=$VENV/bin/python -m percona_dk.server
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

mkdir -p "$INSTALL_DIR/data"

# Daily ingestion service + timer
#
# ExecStartPost restarts the REST API and MCP server so they reload their
# in-memory ChromaDB HNSW index and see newly-ingested chunks. Without this,
# those long-running services keep serving from a stale index even though
# the underlying SQLite already has the new data. "try-restart" is a no-op
# if the service isn't running.
cat > "$SYSTEMD_DIR/percona-dk-ingest.service" <<EOF
[Unit]
Description=Percona DK daily doc ingestion
OnFailure=percona-dk-alert.service

[Service]
Type=oneshot
WorkingDirectory=$INSTALL_DIR
Environment=DOTENV_PATH=$INSTALL_DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV/bin/python -m percona_dk.ingest
ExecStartPost=/usr/bin/systemctl --user try-restart percona-dk-api.service percona-dk-mcp.service
StandardOutput=append:$INSTALL_DIR/data/ingest.log
StandardError=append:$INSTALL_DIR/data/ingest.log
EOF

# Failure alert service (fires via OnFailure= above)
cat > "$SYSTEMD_DIR/percona-dk-alert.service" <<EOF
[Unit]
Description=Send email alert when percona-dk-ingest fails

[Service]
Type=oneshot
WorkingDirectory=$INSTALL_DIR
Environment=ALERT_EMAIL=$ALERT_EMAIL
ExecStart=$INSTALL_DIR/scripts/alert-ingest-failure.sh percona-dk-ingest.service
EOF

cat > "$SYSTEMD_DIR/percona-dk-ingest.timer" <<EOF
[Unit]
Description=Run Percona DK ingestion daily at 03:00 UTC

[Timer]
OnCalendar=*-*-* 03:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF

# Enable and start
systemctl --user daemon-reload
systemctl --user enable --now percona-dk-mcp.service
systemctl --user enable --now percona-dk-api.service
systemctl --user enable --now percona-dk-ingest.timer

# Alert service is pulled in by OnFailure= — no enable needed, but warn if
# msmtp/~/.msmtprc aren't set up so the user knows alerts will be silent.
if ! command -v msmtp >/dev/null 2>&1; then
    echo ""
    echo "  [!] msmtp is not installed — ingest failure alerts will not send."
    echo "      To enable:  sudo apt-get install -y msmtp msmtp-mta"
fi
if [ ! -r "$HOME/.msmtprc" ]; then
    echo ""
    echo "  [!] ~/.msmtprc not found — ingest failure alerts will not send."
    echo "      See scripts/README.md (or ask claude) for setup."
fi

# Keep services running after logout
loginctl enable-linger "$(whoami)" 2>/dev/null || true

HOST=$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo "localhost")

echo ""
echo "============================================"
echo "  Percona DK is running on $HOST"
echo "============================================"
echo ""
echo "  MCP (SSE):   http://$HOST:$MCP_PORT/sse"
echo "  REST API:    http://$HOST:$API_PORT"
echo "  Swagger UI:  http://$HOST:$API_PORT/docs"
echo "  Health:      http://$HOST:$API_PORT/health"
echo ""
echo "  Logs:        journalctl --user -u percona-dk-mcp -f"
echo "  Status:      systemctl --user status percona-dk-mcp percona-dk-api"
echo "  Re-ingest:   systemctl --user start percona-dk-ingest"
echo ""
echo "--- MCP Client Config (Claude Desktop / Code / Cursor) ---"
echo ""
echo "  \"percona-dk\": {"
echo "    \"url\": \"http://$HOST:$MCP_PORT/sse\""
echo "  }"
echo ""
echo "Docs auto-update daily at 03:00 UTC."
echo "Failure alerts email: $ALERT_EMAIL (requires msmtp + ~/.msmtprc)."
echo ""
