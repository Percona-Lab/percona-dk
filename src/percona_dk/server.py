"""
Percona Developer Knowledge — API Server

FastAPI service exposing semantic search and document retrieval
over the ingested Percona documentation corpus.
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Find .env relative to repo root so the server works regardless of cwd.
_pkg_dir = Path(__file__).resolve().parent.parent.parent
for _candidate in [Path.cwd() / ".env", _pkg_dir / ".env"]:
    if _candidate.is_file():
        load_dotenv(_candidate)
        break
else:
    load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (shared with ingest.py)
# ---------------------------------------------------------------------------
_raw_data = os.getenv("DATA_DIR", "data")
_data_path = Path(_raw_data)
DATA_DIR = (_pkg_dir / _data_path).resolve() if not _data_path.is_absolute() else _data_path.resolve()
REPOS_DIR = DATA_DIR / "repos"
CHROMA_DIR = DATA_DIR / "chroma"
COLLECTION_NAME = "percona_docs"

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Percona Developer Knowledge",
    description="Semantic search and retrieval API for Percona documentation",
    version="0.1.0",
)

_startup_time = datetime.now(timezone.utc)


def _get_collection() -> chromadb.Collection:
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return client.get_collection(COLLECTION_NAME)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=5, ge=1, le=50)


class SearchResult(BaseModel):
    text: str
    source_repo: str
    file_path: str
    heading_hierarchy: str
    page_url: str
    score: float


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    suggestion: str | None = None


class HealthResponse(BaseModel):
    status: str
    doc_count: int
    uptime_seconds: float


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/search", response_model=SearchResponse)
def search(req: SearchRequest):
    """Semantic search over Percona documentation. Returns top-k ranked chunks."""
    collection = _get_collection()

    results = collection.query(query_texts=[req.query], n_results=req.top_k)

    items: list[SearchResult] = []
    if results["documents"] and results["documents"][0]:
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            # ChromaDB cosine distance: 0 = identical, 2 = opposite
            # Convert to a 0-1 relevance score
            score = round(1.0 - dist / 2.0, 4)
            items.append(
                SearchResult(
                    text=doc,
                    source_repo=meta["source_repo"],
                    file_path=meta["file_path"],
                    heading_hierarchy=meta["heading_hierarchy"],
                    page_url=meta["page_url"],
                    score=score,
                )
            )

    log.info("Search: %r → %d results", req.query[:80], len(items))

    # Check if the query might match an unconfigured repo
    max_score = max((r.score for r in items), default=0.0)
    from percona_dk.repo_registry import suggest_repos
    suggestion = suggest_repos(req.query, max_score)

    return SearchResponse(query=req.query, results=items, suggestion=suggestion)


@app.get("/document/{repo}/{path:path}")
def get_document(repo: str, path: str):
    """Retrieve full Markdown content for a given doc page.

    Example: GET /document/psmysql-docs/docs/innodb-show-status.md
    """
    # Map short repo name to the cloned directory
    repo_dir = None
    for candidate in REPOS_DIR.iterdir():
        if candidate.is_dir() and repo in candidate.name:
            repo_dir = candidate
            break

    if repo_dir is None:
        raise HTTPException(status_code=404, detail=f"Repo '{repo}' not found in ingested repos")

    file_path = repo_dir / path
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"Document '{path}' not found in repo '{repo}'")

    # Security: ensure path doesn't escape the repo dir
    try:
        file_path.resolve().relative_to(repo_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")

    content = file_path.read_text(encoding="utf-8", errors="replace")
    return {
        "repo": repo,
        "path": path,
        "content": content,
    }


@app.get("/health", response_model=HealthResponse)
def health():
    """Service health check with doc count and uptime."""
    try:
        collection = _get_collection()
        doc_count = collection.count()
    except Exception:
        doc_count = 0

    uptime = (datetime.now(timezone.utc) - _startup_time).total_seconds()
    return HealthResponse(
        status="ok",
        doc_count=doc_count,
        uptime_seconds=round(uptime, 1),
    )


def main():
    """CLI entrypoint for percona-dk-server."""
    from percona_dk.version_check import print_version_notice
    print_version_notice()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


@app.get("/stats")
def stats():
    """Corpus statistics: total docs, chunks per repo, ingested repos."""
    try:
        collection = _get_collection()
        total = collection.count()
    except Exception:
        return {"total_chunks": 0, "repos": {}}

    # Sample all docs to count per-repo (ChromaDB doesn't support GROUP BY)
    # Use get with limit to pull all metadata
    all_meta = collection.get(include=["metadatas"])
    repo_counts: dict[str, int] = {}
    for meta in all_meta["metadatas"]:
        repo = meta.get("source_repo", "unknown")
        repo_counts[repo] = repo_counts.get(repo, 0) + 1

    # Check which repos are cloned locally
    ingested_repos: list[str] = []
    if REPOS_DIR.exists():
        for d in sorted(REPOS_DIR.iterdir()):
            if d.is_dir() and (d / ".git").exists():
                ingested_repos.append(d.name)

    return {
        "total_chunks": total,
        "chunks_per_repo": repo_counts,
        "ingested_repos": ingested_repos,
    }
