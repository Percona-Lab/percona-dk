"""
The tool descriptions and server instructions list the indexed versions
from the ingest config (REPOS), so they can't drift from what is ingested.
These tests fail if a configured repo and version is missing from any of
them, in the MCP server or in the remote shim.

Run: python -m pytest tests/test_indexed_versions.py -v
"""

import json
import os
import subprocess
import sys

import requests

from percona_dk import mcp_remote_shim, mcp_server
from percona_dk.shared import _PRODUCT_LABELS, configured_versions
from test_remote_shim import backend, list_tools  # noqa: F401
from test_agent_tools import corpus  # noqa: F401

# The production config, plus a repo the label table doesn't know yet.
REPOS = ",".join([
    "percona/psmysql-docs:8.0", "percona/psmysql-docs:8.4", "percona/psmysql-docs:9.7",
    "percona/pxc-docs:8.0", "percona/pxc-docs:8.4", "percona/pxc-docs:9.7",
    "percona/pxb-docs:8.0", "percona/pxb-docs:8.4", "percona/pxb-docs:9.7",
    "percona/pdmysql-docs:8.0", "percona/pdmysql-docs:8.4", "percona/pdmysql-docs:9.7",
    "percona/psmdb-docs:6.0", "percona/psmdb-docs:7.0", "percona/psmdb-docs:8.0",
    "percona/postgresql-docs:16", "percona/postgresql-docs:17",
    "percona/pmm-doc", "percona/percona-toolkit",
    "percona/new-docs:1.0",
])


def _listed(text: str, name: str, version: str, sep: str = "\n") -> bool:
    """True if one line (or `sep`-separated part) names the repo and version."""
    return any(name in part and f'"{version}"' in part for part in text.split(sep))


def assert_all_listed(repos: str, instructions: str, search_version_doc: str, get_doc_desc: str) -> None:
    versions = configured_versions(repos)
    for slug, vs in versions.items():
        short = slug.split("/")[-1]
        label = _PRODUCT_LABELS.get(slug, short)
        for v in vs:
            assert _listed(search_version_doc, label, v), (slug, v, "search_percona_docs")
            assert _listed(get_doc_desc, short, v), (slug, v, "get_percona_doc")
            if instructions is not None:
                assert _listed(instructions, label, v, sep=";"), (slug, v, "instructions")


def test_configured_versions_parses_and_sorts():
    assert configured_versions("a/x:8.4, a/x:10.0,a/x:8.0,a/y,a/x:9.7,a/x:8.4") == {
        "a/x": ["8.0", "8.4", "9.7", "10.0"],
    }


_DUMP = """
import asyncio, json
from fastmcp import Client
from percona_dk import mcp_server
async def main():
    async with Client(mcp_server.mcp) as c:
        tools = {t.name: t for t in await c.list_tools()}
    print(json.dumps({
        "instructions": mcp_server.mcp.instructions,
        "search_version": tools["search_percona_docs"].inputSchema["properties"]["version"]["description"],
        "get_doc": tools["get_percona_doc"].description,
    }))
asyncio.run(main())
"""


def test_mcp_server_lists_every_configured_version():
    """Import the server with a known REPOS, as it starts in production."""
    env = {**os.environ, "REPOS": REPOS}
    out = subprocess.run([sys.executable, "-c", _DUMP], env=env, capture_output=True,
                         text=True, check=True).stdout
    data = json.loads(out.strip().splitlines()[-1])
    assert_all_listed(REPOS, data["instructions"], data["search_version"], data["get_doc"])
    assert "PXB 9.x" not in data["search_version"]


def test_mcp_server_lists_this_checkouts_config():
    tools = list_tools(mcp_server.mcp)
    assert_all_listed(os.getenv("REPOS", ""), mcp_server.mcp.instructions,
                      tools["search_percona_docs"].inputSchema["properties"]["version"]["description"],
                      tools["get_percona_doc"].description)


def test_shim_lists_backend_versions(backend, monkeypatch):  # noqa: F811
    monkeypatch.setenv("REPOS", REPOS)  # read by GET /versions on the backend
    try:
        mcp_remote_shim._load_versions()
        assert ("GET", "/versions") in [c[:2] for c in backend]
        tools = list_tools(mcp_remote_shim.mcp)
        assert_all_listed(REPOS, None,
                          tools["search_percona_docs"].inputSchema["properties"]["version"]["description"],
                          tools["get_percona_doc"].description)
    finally:
        monkeypatch.undo()
        mcp_remote_shim._load_versions()


def test_shim_keeps_pending_text_when_backend_unreachable(monkeypatch):
    def down(*args, **kwargs):
        raise requests.ConnectionError("no route")

    before = list_tools(mcp_remote_shim.mcp)["search_percona_docs"].inputSchema
    monkeypatch.setattr(mcp_remote_shim.requests, "request", down)
    mcp_remote_shim._load_versions()
    assert list_tools(mcp_remote_shim.mcp)["search_percona_docs"].inputSchema == before


# ---------------------------------------------------------------------------
# Queries that name a version without passing `version`
# ---------------------------------------------------------------------------

from percona_dk.shared import VERSION_BOOST, query_version, search_chunks  # noqa: E402

KNOWN = {"8.0", "8.4", "9.7", "6.0", "7.0"}


def test_query_version():
    assert query_version("Percona Server for MySQL 9.7 release notes", KNOWN) == "9.7"
    assert query_version("Percona XtraBackup 8.0.35 release notes", KNOWN) == "8.0"
    assert query_version("PS 8.4.2-2 audit log", KNOWN) == "8.4"
    assert query_version("upgrade from 8.0 to 8.4", KNOWN) is None  # names two
    assert query_version("PS 5.7.41 released", KNOWN) is None        # not indexed
    assert query_version("ver 19.7 and 8.40", KNOWN) is None         # no partial matches
    assert query_version("pxc bootstrap first node", KNOWN) is None


class FakeCollection:
    """Returns canned rows per `where` filter and records each query."""

    def __init__(self, rows_by_version: dict):
        self.rows = rows_by_version
        self.calls = []

    def query(self, query_texts, n_results, where=None):
        self.calls.append(where)
        if where is None:
            rows = self.rows["any"]
        elif "$or" in where:
            rows = self.rows["scoped"]
        else:
            rows = self.rows[where["version"]["$eq"]]
        rows = rows[:n_results]
        return {
            "ids": [[r[0] for r in rows]],
            "documents": [[r[0] for r in rows]],
            "metadatas": [[{"version": r[1]} for r in rows]],
            "distances": [[2 * (1 - r[2]) for r in rows]],  # score -> cosine distance
        }


def _ids(res):
    return res["ids"][0]


def test_search_boosts_the_version_the_query_names():
    col = FakeCollection({
        "any": [("forum-5.7", "", 0.910), ("operator-rn", "", 0.905), ("pd-9.7", "9.7", 0.900)],
        "9.7": [("pd-9.7", "9.7", 0.900), ("ps-9.7", "9.7", 0.895)],
    })
    res = search_chunks(col, "PS 9.7 release notes", 3, None, KNOWN)
    assert _ids(res) == ["pd-9.7", "ps-9.7", "forum-5.7"]
    assert col.calls == [None, {"version": {"$eq": "9.7"}}]
    # Scores shown to the agent are the real ones, not boosted.
    assert res["distances"][0][0] == 2 * (1 - 0.900)


def test_boost_does_not_beat_a_much_better_match():
    col = FakeCollection({
        "any": [("forum", "", 0.95)],
        "8.0": [("doc-8.0", "8.0", 0.95 - VERSION_BOOST - 0.01)],
    })
    assert _ids(search_chunks(col, "redo log 8.0", 2, None, KNOWN)) == ["forum", "doc-8.0"]


def test_no_boost_with_version_arg_or_no_version_named():
    col = FakeCollection({"any": [("a", "", 0.9)], "scoped": [("b", "8.4", 0.9)]})
    assert _ids(search_chunks(col, "PS 9.7 release notes", 5, "8.4", KNOWN)) == ["b"]
    assert _ids(search_chunks(col, "upgrade 8.0 to 8.4", 5, None, KNOWN)) == ["a"]
    assert len(col.calls) == 2  # one query each, no extra version query
