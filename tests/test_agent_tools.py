"""
Tests for the agent-facing additions: get_percona_doc by URL, pagination
of large pages, GET /search, and the give_feedback tool.

Runs against a small fixture corpus (tmp repo clones + a tmp Chroma
collection with a deterministic hashing embedder), so it needs neither
ingested data nor the MiniLM model.

Run: python -m pytest tests/test_agent_tools.py -v
"""

import asyncio
import hashlib
import json
import re

import chromadb
import pytest
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from fastapi.testclient import TestClient
from fastmcp import Client

from percona_dk import mcp_server, server
from percona_dk.shared import paginate, url_candidates


class HashEmbedding(EmbeddingFunction):
    """Bag-of-words hashed into 64 dims: deterministic and offline."""

    def __init__(self) -> None:
        pass

    def __call__(self, input: Documents) -> Embeddings:
        out = []
        for text in input:
            vec = [0.0] * 64
            for word in re.findall(r"\w+", text.lower()):
                vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % 64] += 1.0
            out.append(vec)
        return out

    @staticmethod
    def name() -> str:
        return "test-hash"

    def get_config(self) -> dict:
        return {}

    @staticmethod
    def build_from_config(config: dict) -> "HashEmbedding":
        return HashEmbedding()


PXB_80 = "https://docs.percona.com/percona-xtrabackup/8.0/backup-overview.html"
PXB_84 = "https://docs.percona.com/percona-xtrabackup/8.4/backup-overview.html"
TOOLKIT = "https://docs.percona.com/percona-toolkit/pt-online-schema-change.html"
FORUM = "https://forums.percona.com/t/pxc-node-will-not-join/123/1"
BLOG = "https://percona.community/blog/2025/01/02/valkey-tips/"

# A Toolkit-sized page: well over the default 30000-char page size.
BIG_PAGE = "# pt-online-schema-change\n\n" + "".join(
    f"Line {i}: --alter-foreign-keys-method controls how foreign keys are handled.\n"
    for i in range(1500)
)

FILES = {
    "percona_pxb-docs__8.0/docs/backup-overview.md": "# Backup overview\n\nXtraBackup 8.0 uses --backup.\n",
    "percona_pxb-docs__8.4/docs/backup-overview.md": "# Backup overview\n\nXtraBackup 8.4 uses --backup with redo log archiving.\n",
    "percona_percona-toolkit/docs/pt-online-schema-change.md": BIG_PAGE,
    "percona-forums/t/123/1.md": "# PXC node will not join\n\nCheck grastate.dat safe_to_bootstrap.\n",
    "percona-community-blog/posts/valkey-tips.md": "# Valkey tips\n\nUse io-threads.\n",
}

CHUNKS = [
    # (id, text, source_repo, version, file_path, page_url)
    ("pxb80", "XtraBackup 8.0 backup overview uses --backup", "percona/pxb-docs", "8.0",
     "docs/backup-overview.md", PXB_80),
    ("pxb84", "XtraBackup 8.4 backup overview uses --backup with redo log archiving",
     "percona/pxb-docs", "8.4", "docs/backup-overview.md", PXB_84),
    ("tk1", "pt-online-schema-change alter foreign keys method", "percona/percona-toolkit", "",
     "docs/pt-online-schema-change.md", TOOLKIT),
    ("forum1", "PXC node will not join grastate safe_to_bootstrap", "percona-forums", "",
     "t/123/1.md", FORUM),
    ("blog1", "Valkey tips io-threads", "percona-community-blog", "",
     "posts/valkey-tips.md", BLOG),
]


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    repos = tmp_path / "repos"
    for rel, text in FILES.items():
        f = repos / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")

    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    col = client.create_collection(
        "percona_docs",
        embedding_function=HashEmbedding(),
        metadata={"hnsw:space": "cosine"},
    )
    col.add(
        ids=[c[0] for c in CHUNKS],
        documents=[c[1] for c in CHUNKS],
        metadatas=[
            {"source_repo": r, "version": v, "file_path": p,
             "heading_hierarchy": "Overview", "page_url": u}
            for _id, _t, r, v, p, u in CHUNKS
        ],
    )

    for mod in (mcp_server, server):
        monkeypatch.setattr(mod, "_collection", col)
        monkeypatch.setattr(mod, "_repo_versions_cache", None)
        monkeypatch.setattr(mod, "REPOS_DIR", repos)
        monkeypatch.setattr(mod, "FEEDBACK_FILE", tmp_path / "feedback.jsonl")
    return tmp_path


def call_tool(name: str, **args) -> str:
    async def _run():
        async with Client(mcp_server.mcp) as c:
            result = await c.call_tool(name, args)
            return result.content[0].text
    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------

def test_url_candidates_normalizes_browser_urls():
    assert url_candidates("http://docs.percona.com/x/8.4/a.html#sec?x")[0] == \
        "https://docs.percona.com/x/8.4/a.html"
    assert "https://docs.percona.com/x/8.4/a.html" in url_candidates("https://docs.percona.com/x/8.4/a/")
    assert "https://docs.percona.com/x/8.4/a/" in url_candidates("docs.percona.com/x/8.4/a.html")
    assert "https://docs.percona.com/x/" in url_candidates("https://docs.percona.com/x/index.html")
    assert "https://forums.percona.com/t/slug/123/1" in url_candidates("https://forums.percona.com/t/slug/123")


@pytest.mark.parametrize("url, expected", [
    (PXB_84, "8.4 uses --backup with redo log archiving"),
    (PXB_80, "8.0 uses --backup."),
    (PXB_84.replace(".html", "/"), "8.4 uses"),                  # trailing-slash form
    (PXB_80 + "#taking-a-backup", "8.0 uses"),                    # anchor from a browser
    (FORUM, "safe_to_bootstrap"),
    ("https://forums.percona.com/t/pxc-node-will-not-join/123", "safe_to_bootstrap"),
    (BLOG, "io-threads"),
])
def test_get_doc_by_url(corpus, url, expected):
    """The URL alone picks the right repo, path and version."""
    assert expected in call_tool("get_percona_doc", url=url)


def test_get_doc_by_url_keeps_version_scoping(corpus):
    """8.0 and 8.4 URLs for the same path return different files."""
    out84 = call_tool("get_percona_doc", url=PXB_84)
    out80 = call_tool("get_percona_doc", url=PXB_80)
    assert "8.4" in out84 and "8.0" not in out84
    assert "8.0" in out80 and "8.4" not in out80


def test_get_doc_unknown_url(corpus):
    out = call_tool("get_percona_doc", url="https://docs.percona.com/nope/page.html")
    assert out.startswith("Error: No indexed page matches URL")


def test_get_doc_by_repo_path_version_unchanged(corpus):
    assert "8.4 uses" in call_tool("get_percona_doc", repo="pxb-docs",
                                   path="docs/backup-overview.md", version="8.4")
    # Positional-style full slug, as search results print it.
    assert "8.0 uses" in call_tool("get_percona_doc", repo="percona/pxb-docs",
                                   path="docs/backup-overview.md", version="8.0")
    out = call_tool("get_percona_doc", repo="pxb-docs", path="docs/backup-overview.md")
    assert "multiple indexed versions" in out


def test_get_doc_requires_url_or_repo_and_path(corpus):
    assert call_tool("get_percona_doc").startswith("Error: Pass either `url`")
    assert call_tool("get_percona_doc", repo="pxb-docs").startswith("Error: Pass either `url`")


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def test_paginate_short_text_is_untouched():
    assert paginate("hello\nworld\n", max_chars=100) == "hello\nworld\n"


def test_paginate_pages_reassemble_to_original():
    """Following each page's offset hint walks the whole document exactly."""
    text = BIG_PAGE
    pieces, offset = [], 0
    for _ in range(100):
        page = paginate(text, max_chars=7000, offset=offset)
        m = re.search(r"offset=(\d+)\.\]$", page)
        body = re.sub(r"^\[Continued from character \d+ of \d+\.\]\n\n", "", page)
        if m:
            body = body[: body.rindex("\n\n---\n[Truncated:")]
            pieces.append(body)
            offset = int(m.group(1))
        else:
            pieces.append(body)
            break
    else:
        pytest.fail("pagination did not terminate")
    # Pages end on line breaks; the truncation footer drops that final newline.
    assert "\n".join(p.rstrip("\n") for p in pieces) + "\n" == text
    assert len(pieces) > 2


def test_paginate_offset_past_end():
    assert paginate("abc", max_chars=10, offset=50).startswith("Error: offset 50 is past the end")


def test_get_doc_paginates_large_page_by_default(corpus):
    out = call_tool("get_percona_doc", url=TOOLKIT)
    assert len(out) < 31000
    assert out.rstrip().endswith("]")
    m = re.search(r"call get_percona_doc again with the same arguments and offset=(\d+)\.\]$", out)
    assert m, out[-300:]

    nxt = call_tool("get_percona_doc", url=TOOLKIT, offset=int(m.group(1)))
    assert nxt.startswith(f"[Continued from character {m.group(1)} of {len(BIG_PAGE)}.]")

    whole = call_tool("get_percona_doc", repo="percona-toolkit",
                      path="docs/pt-online-schema-change.md", max_chars=1_000_000)
    assert whole == BIG_PAGE


# ---------------------------------------------------------------------------
# GET /search
# ---------------------------------------------------------------------------

def test_get_search_returns_markdown(corpus):
    resp = TestClient(server.app).get("/search", params={"q": "xtrabackup backup overview", "top_k": 3})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.text.startswith("### Result 1 (relevance: ")
    assert "**URL:** https://" in resp.text
    # Both pxb versions surfaced, so the multi-version hint is appended.
    assert "percona/pxb-docs: 8.0, 8.4" in resp.text


def test_get_search_matches_mcp_tool_output(corpus):
    params = {"q": "xtrabackup redo log archiving", "version": "8.4", "top_k": 5}
    http = TestClient(server.app).get("/search", params=params).text
    tool = call_tool("search_percona_docs", query=params["q"], version="8.4", top_k=5)
    assert http == tool


def test_get_search_version_scoping(corpus):
    text = TestClient(server.app).get("/search", params={"q": "xtrabackup backup", "version": "8.0", "top_k": 10}).text
    assert "**Version:** 8.0" in text
    assert "**Version:** 8.4" not in text
    assert "Available versions" not in text  # no hint once the caller scoped


def test_get_search_requires_q(corpus):
    assert TestClient(server.app).get("/search").status_code == 422


def test_post_search_still_returns_json(corpus):
    resp = TestClient(server.app).post("/search", json={"query": "valkey", "top_k": 2, "version": "8.4"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 2
    assert all(r["version"] in (None, "8.4") for r in data["results"])


# ---------------------------------------------------------------------------
# give_feedback
# ---------------------------------------------------------------------------

def test_give_feedback_appends_jsonl(corpus):
    out = call_tool("give_feedback", message="innodb_log_file_size page is outdated for 8.4",
                    category="documentation", severity="high",
                    context=PXB_84, tool_name="get_percona_doc")
    assert out.startswith("Thanks - feedback recorded")
    call_tool("give_feedback", message="search ignored version", category="mcp_server")

    lines = (corpus / "feedback.jsonl").read_text().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["category"] == "documentation"
    assert first["severity"] == "high"
    assert first["tool_name"] == "get_percona_doc"
    assert first["context"] == PXB_84
    assert first["timestamp"].endswith("+00:00")
    assert first["id"] in out
    assert second["severity"] is None and second["context"] is None


def test_give_feedback_rejects_unknown_category(corpus):
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError):
        call_tool("give_feedback", message="x", category="bogus")
    assert not (corpus / "feedback.jsonl").exists()


def test_give_feedback_description_warns_against_secrets():
    async def _desc():
        async with Client(mcp_server.mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
            return tools["give_feedback"].description
    desc = asyncio.run(_desc())
    assert "Never include secrets or private data" in desc
