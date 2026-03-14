"""
Smoke tests for the Percona-DK API server.

Run: python -m pytest tests/test_api.py -v
"""

from fastapi.testclient import TestClient

from percona_dk.server import app

client = TestClient(app)


def test_health_endpoint():
    """GET /health returns status ok with doc count."""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["doc_count"] > 0
    assert "uptime_seconds" in data


def test_search_returns_results():
    """POST /search returns ranked results for a valid query."""
    resp = client.post("/search", json={"query": "innodb buffer pool", "top_k": 3})
    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "innodb buffer pool"
    assert len(data["results"]) > 0
    result = data["results"][0]
    assert "text" in result
    assert "source_repo" in result
    assert "score" in result
    assert 0 <= result["score"] <= 1


def test_search_respects_top_k():
    """POST /search respects the top_k parameter."""
    resp = client.post("/search", json={"query": "backup", "top_k": 2})
    assert resp.status_code == 200
    assert len(resp.json()["results"]) <= 2


def test_document_retrieval():
    """GET /document/{repo}/{path} returns full Markdown content."""
    resp = client.get("/document/psmysql-docs/docs/innodb-show-status.md")
    assert resp.status_code == 200
    data = resp.json()
    assert "content" in data
    assert len(data["content"]) > 100
    assert data["repo"] == "psmysql-docs"


def test_document_not_found():
    """GET /document returns 404 for nonexistent paths."""
    resp = client.get("/document/psmysql-docs/docs/nonexistent-page.md")
    assert resp.status_code == 404
