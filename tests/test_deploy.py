"""
Tests for the container deployment pieces: index freshness on /health
(REST API and MCP server), header redaction in the MCP request log on
both HTTP transports, and REFRESH_DAYS=0 disabling auto-refresh.

Uses the fixture corpus from test_agent_tools.py, so it needs neither
ingested data nor the MiniLM model.

Run: python -m pytest tests/test_deploy.py -v
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import TestClient as StarletteClient

from percona_dk import mcp_server, server
from percona_dk.shared import REDACTED, index_freshness, redact_headers
from test_agent_tools import corpus  # noqa: F401

SECRET = "s3cr3t-value-that-must-not-be-logged"


# ---------------------------------------------------------------------------
# index_freshness
# ---------------------------------------------------------------------------

def test_index_freshness_without_marker(tmp_path):
    assert index_freshness(tmp_path) == {"last_ingest": None, "index_age_hours": None}


def test_index_freshness_reads_marker_mtime(tmp_path):
    marker = tmp_path / ".last_ingest"
    marker.write_text("ignored")
    finished = datetime(2026, 11, 9, 3, 0, 0, tzinfo=timezone.utc).timestamp()
    os.utime(marker, (finished, finished))

    out = index_freshness(tmp_path, now=finished + 5.5 * 3600)
    assert out == {"last_ingest": "2026-11-09T03:00:00+00:00", "index_age_hours": 5.5}


@pytest.fixture
def freshness(corpus, monkeypatch):  # noqa: F811
    """Point both servers' DATA_DIR at the fixture and stamp an ingest 2h ago."""
    for mod in (mcp_server, server):
        monkeypatch.setattr(mod, "DATA_DIR", corpus)
    marker = corpus / ".last_ingest"
    marker.write_text("")
    two_hours_ago = time.time() - 2 * 3600
    os.utime(marker, (two_hours_ago, two_hours_ago))
    return corpus


def _assert_fresh(data: dict) -> None:
    assert data["status"] == "ok"
    assert data["doc_count"] == 5
    assert datetime.fromisoformat(data["last_ingest"]).tzinfo is not None
    assert 1.9 < data["index_age_hours"] < 2.1


def test_rest_health_reports_index_age(freshness):
    data = TestClient(server.app).get("/health").json()
    _assert_fresh(data)


def test_mcp_health_reports_index_age(freshness):
    resp = asyncio.run(mcp_server.health(None))
    _assert_fresh(json.loads(resp.body))


def test_health_before_first_ingest(corpus, monkeypatch):  # noqa: F811
    monkeypatch.setattr(server, "DATA_DIR", corpus)
    data = TestClient(server.app).get("/health").json()
    assert data["last_ingest"] is None
    assert data["index_age_hours"] is None


# ---------------------------------------------------------------------------
# Header redaction
# ---------------------------------------------------------------------------

def test_redact_headers():
    headers = {
        "Authorization": "Bearer abc",
        "cookie": "session=abc",
        "Set-Cookie": "a=b",
        "Proxy-Authorization": "Basic abc",
        "X-Api-Key": "abc",
        "x-auth-token": "abc",
        "CSRF-Token": "abc",
        "User-Agent": "claude-code/2.0",
        "Content-Type": "application/json",
        "Mcp-Session-Id": "keep-me",
    }
    out = redact_headers(headers)
    for name in ("Authorization", "cookie", "Set-Cookie", "Proxy-Authorization",
                 "X-Api-Key", "x-auth-token", "CSRF-Token"):
        assert out[name] == REDACTED, name
    assert out["User-Agent"] == "claude-code/2.0"
    assert out["Content-Type"] == "application/json"
    assert out["Mcp-Session-Id"] == "keep-me"
    assert headers["Authorization"] == "Bearer abc"  # input not mutated


def _logged_headers(caplog) -> list[dict]:
    out = []
    for rec in caplog.records:
        msg = rec.getMessage()
        if msg.startswith("MCP headers: "):
            out.append(json.loads(msg[len("MCP headers: "):]))
    return out


@pytest.mark.parametrize("transport,path", [
    ("streamable-http", "/mcp"),
    ("sse", "/messages/"),
])
def test_http_transports_log_redacted_headers(transport, path, caplog):
    """Both transports log request headers, with credentials redacted."""
    caplog.set_level(logging.INFO, logger=mcp_server.log.name)
    app = mcp_server.build_http_app(transport)
    with StarletteClient(app) as client:
        client.post(path, content=b"{}", headers={
            "Authorization": f"Bearer {SECRET}",
            "Cookie": f"session={SECRET}",
            "X-Api-Key": SECRET,
            "User-Agent": "test-agent/1.0",
        })

    entries = [e for e in _logged_headers(caplog) if e["path"].startswith(path.rstrip("/"))]
    assert entries, f"no header log line for {transport}"
    headers = entries[-1]["headers"]
    assert headers["authorization"] == REDACTED
    assert headers["cookie"] == REDACTED
    assert headers["x-api-key"] == REDACTED
    assert headers["user-agent"] == "test-agent/1.0"
    assert SECRET not in caplog.text


def test_health_requests_are_not_header_logged(corpus, caplog):  # noqa: F811
    caplog.set_level(logging.INFO, logger=mcp_server.log.name)
    app = mcp_server.build_http_app("streamable-http")
    with StarletteClient(app) as client:
        assert client.get("/health").status_code == 200
    assert _logged_headers(caplog) == []


# ---------------------------------------------------------------------------
# Auto-refresh
# ---------------------------------------------------------------------------

def test_refresh_days_zero_disables_auto_refresh(monkeypatch):
    monkeypatch.setattr(mcp_server, "REFRESH_DAYS", 0)
    monkeypatch.setattr(mcp_server, "_days_since_last_ingest", lambda: 30.0)
    started = []
    monkeypatch.setattr(mcp_server.threading, "Thread",
                        lambda *a, **k: started.append(k) or pytest.fail("refresh started"))
    mcp_server._maybe_refresh()
    assert started == []
