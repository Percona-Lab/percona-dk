"""
Tests for the remote shim (percona-dk-mcp-remote) and the REST endpoints it
calls: GET /document by URL with paging, and POST /feedback.

The shim's HTTP calls are routed into the FastAPI app through a TestClient,
over the same fixture corpus as test_agent_tools.py, so each shim tool can
be compared with the local MCP server's tool on identical data.

Run: python -m pytest tests/test_remote_shim.py -v
"""

import asyncio
import json
import re

import pytest
import requests
from fastapi.testclient import TestClient
from fastmcp import Client

from percona_dk import mcp_remote_shim, mcp_server, server
from test_agent_tools import BIG_PAGE, FORUM, PXB_80, PXB_84, TOOLKIT, corpus  # noqa: F401


def _to_requests_response(resp) -> requests.Response:
    out = requests.Response()
    out.status_code = resp.status_code
    out.reason = resp.reason_phrase
    out.headers.update(resp.headers)
    out._content = resp.content
    out.url = str(resp.url)
    return out


@pytest.fixture
def backend(corpus, monkeypatch):  # noqa: F811
    """Send the shim's requests to server.app; record each call."""
    client = TestClient(server.app)
    calls = []

    def fake_request(method, url, timeout=None, **kwargs):
        assert url.startswith(mcp_remote_shim.BACKEND_URL)
        endpoint = url[len(mcp_remote_shim.BACKEND_URL):]
        calls.append((method, endpoint, kwargs))
        return _to_requests_response(client.request(method, endpoint, **kwargs))

    monkeypatch.setattr(mcp_remote_shim.requests, "request", fake_request)
    return calls


def call(mcp, name: str, **args) -> str:
    async def _run():
        async with Client(mcp) as c:
            result = await c.call_tool(name, args)
            return result.content[0].text
    return asyncio.run(_run())


def list_tools(mcp) -> dict:
    async def _run():
        async with Client(mcp) as c:
            return {t.name: t for t in await c.list_tools()}
    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tool parity
# ---------------------------------------------------------------------------

def test_shim_tools_match_mcp_server():
    """Same tool names and input schemas; get_percona_doc and give_feedback
    also share their descriptions."""
    local, shim = list_tools(mcp_server.mcp), list_tools(mcp_remote_shim.mcp)
    assert set(shim) == set(local) == {"search_percona_docs", "get_percona_doc", "give_feedback"}
    for name in local:
        assert shim[name].inputSchema == local[name].inputSchema, name
    for name in ("get_percona_doc", "give_feedback"):
        assert shim[name].description == local[name].description, name


def test_shim_give_feedback_warns_against_secrets():
    tool = list_tools(mcp_remote_shim.mcp)["give_feedback"]
    assert "Never include secrets or private data" in tool.description
    context_desc = " ".join(tool.inputSchema["properties"]["context"]["description"].split())
    assert "Redact anything from the user's environment" in context_desc


@pytest.mark.parametrize("args", [
    {"url": PXB_84},
    {"url": PXB_80 + "#taking-a-backup"},
    {"url": PXB_84.replace(".html", "/")},
    {"url": FORUM.rsplit("/", 1)[0]},          # forum topic link without the post number
    {"url": "https://docs.percona.com/nope/page.html"},
    {"url": TOOLKIT},                          # paged at the default 30000
    {"url": TOOLKIT, "max_chars": 5000, "offset": 5000},
    {"url": TOOLKIT, "offset": 10_000_000},    # past the end
    {"repo": "pxb-docs", "path": "docs/backup-overview.md", "version": "8.4"},
    {"repo": "percona/pxb-docs", "path": "docs/backup-overview.md", "version": "8.0"},
    {"repo": "percona-toolkit", "path": "docs/pt-online-schema-change.md", "max_chars": 1_000_000},
    {"repo": "pxb-docs"},
    {},
])
def test_get_percona_doc_matches_mcp_server(backend, args):
    assert call(mcp_remote_shim.mcp, "get_percona_doc", **args) == \
        call(mcp_server.mcp, "get_percona_doc", **args)


def test_get_percona_doc_by_url_keeps_version_scoping(backend):
    out84 = call(mcp_remote_shim.mcp, "get_percona_doc", url=PXB_84)
    out80 = call(mcp_remote_shim.mcp, "get_percona_doc", url=PXB_80)
    assert "8.4" in out84 and "8.0" not in out84
    assert "8.0" in out80 and "8.4" not in out80


def test_get_percona_doc_errors_from_backend(backend):
    out = call(mcp_remote_shim.mcp, "get_percona_doc", repo="pxb-docs", path="docs/backup-overview.md")
    assert out.startswith("Error: Repo 'pxb-docs' has multiple indexed versions: 8.0, 8.4.")
    out = call(mcp_remote_shim.mcp, "get_percona_doc", repo="pxb-docs", path="docs/nope.md", version="8.4")
    assert out.startswith("Error: Document 'docs/nope.md' not found")


def test_get_percona_doc_without_url_or_path_skips_backend(backend):
    call(mcp_remote_shim.mcp, "get_percona_doc", repo="pxb-docs")
    assert backend == []


def test_shim_pages_locally_against_older_backend(monkeypatch):
    """A backend without paging returns the whole page; the shim pages it."""
    def old_backend(method, url, timeout=None, **kwargs):
        resp = requests.Response()
        resp.status_code = 200
        resp._content = json.dumps({"repo": "percona-toolkit", "version": None,
                                    "path": "x.md", "content": BIG_PAGE}).encode()
        return resp

    monkeypatch.setattr(mcp_remote_shim.requests, "request", old_backend)
    out = call(mcp_remote_shim.mcp, "get_percona_doc", repo="percona-toolkit", path="x.md")
    assert len(out) < 31000
    assert re.search(r"offset=\d+\.\]$", out)


def test_search_version_scoping(backend):
    out = call(mcp_remote_shim.mcp, "search_percona_docs", query="xtrabackup backup", version="8.0", top_k=10)
    assert "**Version:** 8.0" in out
    assert "**Version:** 8.4" not in out


# ---------------------------------------------------------------------------
# give_feedback
# ---------------------------------------------------------------------------

def test_give_feedback_through_shim_writes_same_format(backend, corpus):  # noqa: F811
    args = dict(message="innodb_log_file_size page is outdated for 8.4",
                category="documentation", severity="high",
                context=PXB_84, tool_name="get_percona_doc")
    via_shim = call(mcp_remote_shim.mcp, "give_feedback", **args)
    local = call(mcp_server.mcp, "give_feedback", **args)
    assert via_shim.startswith("Thanks - feedback recorded (id ")
    assert re.sub(r"id \w+", "id X", via_shim) == re.sub(r"id \w+", "id X", local)

    shim_entry, local_entry = (json.loads(line) for line in
                               (corpus / "feedback.jsonl").read_text().splitlines())
    assert shim_entry["id"] in via_shim
    for entry in (shim_entry, local_entry):
        del entry["id"], entry["timestamp"]
    assert shim_entry == local_entry


def test_give_feedback_rejects_unknown_category(backend, corpus):  # noqa: F811
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError):
        call(mcp_remote_shim.mcp, "give_feedback", message="x", category="bogus")
    assert backend == []
    assert not (corpus / "feedback.jsonl").exists()


# ---------------------------------------------------------------------------
# VPN-required handling
# ---------------------------------------------------------------------------

TOOL_CALLS = [
    ("search_percona_docs", {"query": "pxc bootstrap"}),
    ("get_percona_doc", {"url": PXB_84}),
    ("get_percona_doc", {"repo": "pxb-docs", "path": "docs/backup-overview.md", "version": "8.4"}),
    ("give_feedback", {"message": "x", "category": "other"}),
]


@pytest.mark.parametrize("exc, detail", [
    (requests.ConnectionError("no route to host"), "connection error reaching"),
    (requests.Timeout("slow"), "request timed out"),
])
@pytest.mark.parametrize("name, args", TOOL_CALLS)
def test_unreachable_backend_returns_vpn_message(monkeypatch, name, args, exc, detail):
    def unreachable(*a, **kw):
        raise exc

    monkeypatch.setattr(mcp_remote_shim.requests, "request", unreachable)
    out = call(mcp_remote_shim.mcp, name, **args)
    assert out.startswith("**Percona DK backend is unreachable.**")
    assert detail in out


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

def test_rest_document_by_url(corpus):  # noqa: F811
    resp = TestClient(server.app).get("/document", params={"url": PXB_84})
    assert resp.status_code == 200
    data = resp.json()
    assert data["repo"] == "percona/pxb-docs"
    assert data["path"] == "docs/backup-overview.md"
    assert data["version"] == "8.4"
    assert "8.4 uses" in data["content"]
    assert data["offset"] == 0 and data["total_chars"] == len(data["content"])


def test_rest_document_by_url_not_found(corpus):  # noqa: F811
    resp = TestClient(server.app).get("/document", params={"url": "https://docs.percona.com/nope.html"})
    assert resp.status_code == 404
    assert resp.json()["detail"].startswith("No indexed page matches URL")


def test_rest_document_requires_url(corpus):  # noqa: F811
    assert TestClient(server.app).get("/document").status_code == 422


def test_rest_document_paging(corpus):  # noqa: F811
    client = TestClient(server.app)
    path = "/document/percona-toolkit/docs/pt-online-schema-change.md"
    first = client.get(path).json()
    assert first["total_chars"] == len(BIG_PAGE)
    m = re.search(r"offset=(\d+)\.\]$", first["content"])
    assert m and len(first["content"]) < 31000

    second = client.get(path, params={"offset": m.group(1)}).json()
    assert second["offset"] == int(m.group(1))
    assert second["content"].startswith(f"[Continued from character {m.group(1)} ")

    assert client.get(path, params={"max_chars": 1_000_000}).json()["content"] == BIG_PAGE
    past = client.get(path, params={"offset": len(BIG_PAGE)})
    assert past.status_code == 400
    assert past.json()["detail"].startswith(f"offset {len(BIG_PAGE)} is past the end")


def test_rest_document_keeps_status_codes(corpus):  # noqa: F811
    client = TestClient(server.app)
    assert client.get("/document/pxb-docs/docs/backup-overview.md").status_code == 409
    assert client.get("/document/pxb-docs/docs/backup-overview.md", params={"version": "5.7"}).status_code == 404
    assert client.get("/document/no-such-repo/x.md").status_code == 404
    assert client.get("/document/pxb-docs/docs/nope.md", params={"version": "8.4"}).status_code == 404


def test_rest_feedback(corpus):  # noqa: F811
    client = TestClient(server.app)
    resp = client.post("/feedback", json={"message": "search ignored version", "category": "mcp_server"})
    assert resp.status_code == 200
    entry = json.loads((corpus / "feedback.jsonl").read_text())
    assert resp.json() == {"id": entry["id"], "category": "mcp_server"}
    assert entry["severity"] is None and entry["context"] is None
    assert entry["timestamp"].endswith("+00:00")

    assert client.post("/feedback", json={"message": "x", "category": "bogus"}).status_code == 422
    assert client.post("/feedback", json={"category": "other"}).status_code == 422
