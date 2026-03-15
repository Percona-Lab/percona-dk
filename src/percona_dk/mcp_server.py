"""
Percona Developer Knowledge — MCP Server

Exposes search_percona_docs and get_percona_doc as MCP tools,
consumable by Claude Desktop, Claude Code, Cursor, or any MCP client.
"""

import os
import logging
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (shared with ingest.py / server.py)
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
REPOS_DIR = DATA_DIR / "repos"
CHROMA_DIR = DATA_DIR / "chroma"
COLLECTION_NAME = "percona_docs"

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "Percona Developer Knowledge",
    instructions="Search and retrieve official Percona documentation. "
    "Use this when you need accurate, up-to-date information about "
    "Percona Server for MySQL, XtraDB Cluster, XtraBackup, PMM, "
    "Percona Operators, Percona Toolkit, or other Percona products.",
)


def _get_collection() -> chromadb.Collection:
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return client.get_collection(COLLECTION_NAME)


@mcp.tool()
def search_percona_docs(query: str, top_k: int = 5) -> str:
    """Search Percona documentation using semantic search.

    Use this tool when you need to find information about Percona products
    including configuration, troubleshooting, features, or best practices.
    Returns the most relevant documentation chunks with source links.

    Args:
        query: Natural language search query about Percona products.
        top_k: Number of results to return (1-20, default 5).
    """
    top_k = max(1, min(top_k, 20))
    collection = _get_collection()

    results = collection.query(query_texts=[query], n_results=top_k)

    if not results["documents"] or not results["documents"][0]:
        return "No results found for your query."

    output_parts: list[str] = []
    for i, (doc, meta, dist) in enumerate(
        zip(results["documents"][0], results["metadatas"][0], results["distances"][0])
    ):
        score = round(1.0 - dist / 2.0, 4)
        output_parts.append(
            f"### Result {i + 1} (relevance: {score})\n"
            f"**Source:** {meta['source_repo']} — `{meta['file_path']}`\n"
            f"**Section:** {meta['heading_hierarchy']}\n"
            f"**URL:** {meta['page_url']}\n\n"
            f"{doc}\n"
        )

    log.info("MCP search: %r → %d results", query[:80], len(output_parts))
    return "\n---\n".join(output_parts)


@mcp.tool()
def get_percona_doc(repo: str, path: str) -> str:
    """Retrieve the full Markdown content of a specific Percona documentation page.

    Use this tool when you already know which doc page you need (e.g., from
    a previous search result) and want to read the complete content.

    Args:
        repo: Repository short name, e.g. 'psmysql-docs', 'pxc-docs', 'pmm-doc'.
        path: File path within the repo, e.g. 'docs/innodb-show-status.md'.
    """
    repo_dir = None
    for candidate in REPOS_DIR.iterdir():
        if candidate.is_dir() and repo in candidate.name:
            repo_dir = candidate
            break

    if repo_dir is None:
        return f"Error: Repo '{repo}' not found in ingested repos."

    file_path = repo_dir / path
    if not file_path.exists() or not file_path.is_file():
        return f"Error: Document '{path}' not found in repo '{repo}'."

    try:
        file_path.resolve().relative_to(repo_dir.resolve())
    except ValueError:
        return "Error: Invalid path."

    return file_path.read_text(encoding="utf-8", errors="replace")


def main():
    """CLI entrypoint for percona-dk-mcp."""
    from percona_dk.version_check import print_version_notice
    print_version_notice()
    mcp.run()


if __name__ == "__main__":
    main()
