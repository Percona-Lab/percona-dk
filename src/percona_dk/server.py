"""
Percona Developer Knowledge — API Server

FastAPI service exposing semantic search and document retrieval
over the ingested Percona documentation corpus.
"""

import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Find .env relative to repo root so the server works regardless of cwd.
_pkg_dir = Path(__file__).resolve().parent.parent.parent
_env_dir = Path.cwd()  # default; updated if we find an actual .env
for _candidate in [Path.cwd() / ".env", _pkg_dir / ".env"]:
    if _candidate.is_file():
        _env_dir = _candidate.parent
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
DATA_DIR = (_env_dir / _data_path).resolve() if not _data_path.is_absolute() else _data_path.resolve()
REPOS_DIR = DATA_DIR / "repos"
CHROMA_DIR = DATA_DIR / "chroma"
COLLECTION_NAME = "percona_docs"

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Percona Developer Knowledge",
    description="Semantic search and retrieval API for Percona documentation",
    version="0.2.0",
)

_startup_time = datetime.now(timezone.utc)

# Module-level cache for the chroma client and collection. Without this the
# DefaultEmbeddingFunction reloads the ~80 MB MiniLM ONNX model and the HNSW
# index reloads from disk on EVERY request, costing ~1-2s per query.
# Caching cuts steady-state latency to ~50-100ms. The systemd ingest unit
# restarts this service on completion, so cache invalidation is automatic.
_collection: chromadb.Collection | None = None
_collection_lock = threading.Lock()


def _get_collection() -> chromadb.Collection:
    global _collection
    if _collection is not None:
        return _collection
    with _collection_lock:
        if _collection is None:
            client = chromadb.PersistentClient(path=str(CHROMA_DIR))
            _collection = client.get_collection(COLLECTION_NAME)
    return _collection


def _warmup() -> None:
    """Pre-load the collection + embedding model so the first user query is fast."""
    try:
        col = _get_collection()
        col.query(query_texts=["warmup"], n_results=1)
        log.info("Collection warmed (%d chunks)", col.count())
        _get_repo_versions()
    except Exception:
        log.warning("Warmup failed; will retry on first request", exc_info=True)


_repo_versions_cache: dict[str, list[str]] | None = None


def _get_repo_versions() -> dict[str, list[str]]:
    """Return {source_repo: sorted distinct versions} excluding empty values.

    Cached at module level. Built lazily by paginating chroma metadata.
    """
    global _repo_versions_cache
    if _repo_versions_cache is not None:
        return _repo_versions_cache
    out: dict[str, set[str]] = {}
    try:
        col = _get_collection()
        offset = 0
        batch = 5000
        while True:
            res = col.get(include=["metadatas"], limit=batch, offset=offset)
            metas = res.get("metadatas") or []
            if not metas:
                break
            for m in metas:
                v = (m.get("version") or "").strip()
                if v:
                    out.setdefault(m["source_repo"], set()).add(v)
            offset += batch
    except Exception:
        log.warning("Failed to compute repo->versions map", exc_info=True)
    _repo_versions_cache = {r: sorted(vs) for r, vs in out.items()}
    return _repo_versions_cache


@app.on_event("startup")
def _on_startup() -> None:
    _warmup()


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=5, ge=1, le=50)
    version: str | None = Field(
        default=None,
        description=(
            "Optional version filter (e.g. '8.0', '8.4', '7.0'). When set, "
            "only chunks tagged with that version - plus version-agnostic "
            "content (community blog/forum, single-branch repos) - are "
            "returned."
        ),
    )


class SearchResult(BaseModel):
    text: str
    source_repo: str
    version: str | None = None
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

    where = None
    if req.version:
        # Match the requested version, plus version-agnostic content (empty
        # version) so single-branch repos and community content still surface.
        where = {"$or": [
            {"version": {"$eq": req.version}},
            {"version": {"$eq": ""}},
        ]}

    results = collection.query(
        query_texts=[req.query],
        n_results=req.top_k,
        where=where,
    )

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
                    version=meta.get("version") or None,
                    file_path=meta["file_path"],
                    heading_hierarchy=meta["heading_hierarchy"],
                    page_url=meta["page_url"],
                    score=score,
                )
            )

    log.info("Search: %r → %d results", req.query[:80], len(items))

    # If results span multiple versions of any repo and the caller didn't
    # already pass `version`, attach a hint listing the indexed versions
    # so the model can re-run scoped to one. Prevents the failure mode
    # where partial-vs-multi-version retrieval looks like an indexing gap.
    suggestion_parts: list[str] = []
    if not req.version:
        repo_versions = _get_repo_versions()
        repos_in_results = {it.source_repo for it in items if it.version}
        hint_lines = [
            f"  - {r}: {', '.join(repo_versions[r])}"
            for r in sorted(repos_in_results)
            if len(repo_versions.get(r, [])) > 1
        ]
        if hint_lines:
            suggestion_parts.append(
                "Some results above are from repos with multiple indexed "
                "versions. To scope to one version, re-run with `version=X`. "
                "Available versions:\n" + "\n".join(hint_lines)
            )

    # Check if the query might match an unconfigured repo
    max_score = max((r.score for r in items), default=0.0)
    from percona_dk.repo_registry import suggest_repos
    repo_suggestion = suggest_repos(req.query, max_score)
    if repo_suggestion:
        suggestion_parts.append(repo_suggestion.strip())

    suggestion = "\n\n".join(suggestion_parts) if suggestion_parts else None
    return SearchResponse(query=req.query, results=items, suggestion=suggestion)


@app.get("/document/{repo}/{path:path}")
def get_document(repo: str, path: str, version: str | None = None):
    """Retrieve full Markdown content for a given doc page.

    Example: GET /document/psmysql-docs/docs/innodb-show-status.md?version=8.0

    For multi-version repos (psmysql-docs, pxc-docs, pxb-docs,
    pdmysql-docs, psmdb-docs, postgresql-docs), `version` is required;
    when omitted, returns 409 with the available versions in the detail.
    """
    candidates = [c for c in REPOS_DIR.iterdir() if c.is_dir() and repo in c.name]
    if not candidates:
        raise HTTPException(status_code=404, detail=f"Repo '{repo}' not found in ingested repos")

    if version:
        suffix = f"__{version}"
        target = next((c for c in candidates if c.name.endswith(suffix)), None)
        if target is None:
            available = sorted({c.name.split("__")[-1] for c in candidates if "__" in c.name}) or ["(single-branch)"]
            raise HTTPException(
                status_code=404,
                detail=f"Repo '{repo}' has no local copy at version '{version}'. Available: {', '.join(available)}",
            )
        repo_dir = target
    else:
        single_branch = [c for c in candidates if "__" not in c.name]
        if single_branch:
            repo_dir = single_branch[0]
        else:
            available = sorted({c.name.split("__")[-1] for c in candidates})
            raise HTTPException(
                status_code=409,
                detail=f"Repo '{repo}' has multiple indexed versions: {', '.join(available)}. Specify ?version=X.",
            )

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
        "version": version,
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


if __name__ == "__main__":
    main()
