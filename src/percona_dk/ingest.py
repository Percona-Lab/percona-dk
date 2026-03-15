"""
Percona Developer Knowledge — Ingestion Pipeline

Clones Percona doc repos from GitHub, parses Markdown source files,
chunks by h2/h3 headings, and loads into ChromaDB for semantic search.
ChromaDB handles embedding locally via its default model (all-MiniLM-L6-v2).
"""

import os
import re
import hashlib
import logging
from pathlib import Path

import chromadb
import git
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
REPOS_DIR = DATA_DIR / "repos"
CHROMA_DIR = DATA_DIR / "chroma"
COLLECTION_NAME = "percona_docs"

DEFAULT_REPOS = [r.strip() for r in os.getenv("REPOS", "percona/psmysql-docs").split(",") if r.strip()]

MAX_CHUNK_CHARS = 4000  # rough limit to stay within token budget


# ---------------------------------------------------------------------------
# Repo cloning / pulling
# ---------------------------------------------------------------------------

def clone_or_pull(repo_slug: str) -> Path:
    """Clone a GitHub repo (or pull if already cloned). Returns the local path."""
    repo_url = f"https://github.com/{repo_slug}.git"
    local_path = REPOS_DIR / repo_slug.replace("/", "_")

    if (local_path / ".git").exists():
        log.info("Pulling latest for %s", repo_slug)
        repo = git.Repo(local_path)
        repo.remotes.origin.pull()
    else:
        log.info("Cloning %s → %s", repo_url, local_path)
        local_path.mkdir(parents=True, exist_ok=True)
        git.Repo.clone_from(repo_url, local_path, depth=1)

    return local_path


# ---------------------------------------------------------------------------
# Markdown chunking
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)


def _build_page_url(repo_slug: str, file_path: str) -> str:
    """Construct a docs.percona.com URL from repo slug and file path.

    Pattern: the docs repos typically map
      docs/some-page.md  →  https://docs.percona.com/<product>/latest/some-page/
    This is a best-effort construction; exact mapping varies per repo.
    """
    # Strip leading docs/ directory and .md extension
    rel = file_path
    for prefix in ("docs/", "source/"):
        if rel.startswith(prefix):
            rel = rel[len(prefix):]
    rel = re.sub(r"\.md$", "", rel)

    # Derive product slug from repo name
    product = repo_slug.split("/")[-1].replace("-docs", "").replace("_", "-")
    return f"https://docs.percona.com/{product}/latest/{rel}/"


def chunk_markdown(text: str, repo_slug: str, file_path: str) -> list[dict]:
    """Split a Markdown file into chunks at h2/h3 boundaries.

    Each chunk contains:
      - text: the Markdown content of the section
      - source_repo: e.g. "percona/percona-server-docs"
      - file_path: relative path within the repo
      - heading_hierarchy: list of heading strings leading to this chunk
      - page_url: constructed docs.percona.com URL
    """
    # Find all headings and their positions
    headings: list[tuple[int, int, str]] = []  # (pos, level, title)
    for m in _HEADING_RE.finditer(text):
        headings.append((m.start(), len(m.group(1)), m.group(2).strip()))

    if not headings:
        # No headings — treat the entire file as one chunk
        stripped = text.strip()
        if not stripped:
            return []
        return [
            {
                "text": stripped[:MAX_CHUNK_CHARS],
                "source_repo": repo_slug,
                "file_path": file_path,
                "heading_hierarchy": [],
                "page_url": _build_page_url(repo_slug, file_path),
            }
        ]

    chunks: list[dict] = []
    hierarchy: list[str] = []  # current heading stack

    for i, (pos, level, title) in enumerate(headings):
        # Determine the text range for this section
        start = pos
        end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        section_text = text[start:end].strip()

        if not section_text:
            continue

        # Maintain heading hierarchy
        # Trim hierarchy to current level, then append
        hierarchy = [h for j, h in enumerate(hierarchy) if j < level - 1]
        while len(hierarchy) < level - 1:
            hierarchy.append("")
        hierarchy = hierarchy[: level - 1] + [title]

        chunks.append(
            {
                "text": section_text[:MAX_CHUNK_CHARS],
                "source_repo": repo_slug,
                "file_path": file_path,
                "heading_hierarchy": list(hierarchy),
                "page_url": _build_page_url(repo_slug, file_path),
            }
        )

    # If there's content before the first heading, capture it too
    pre_heading_text = text[: headings[0][0]].strip()
    if pre_heading_text:
        chunks.insert(
            0,
            {
                "text": pre_heading_text[:MAX_CHUNK_CHARS],
                "source_repo": repo_slug,
                "file_path": file_path,
                "heading_hierarchy": [],
                "page_url": _build_page_url(repo_slug, file_path),
            },
        )

    return chunks


# ---------------------------------------------------------------------------
# Walk repo and collect chunks
# ---------------------------------------------------------------------------

def collect_chunks(repo_slug: str, repo_path: Path) -> list[dict]:
    """Walk all .md files in a repo and return chunks."""
    all_chunks: list[dict] = []

    md_files = sorted(repo_path.rglob("*.md"))
    log.info("Found %d .md files in %s", len(md_files), repo_slug)

    for md_file in md_files:
        # Skip hidden dirs, vendor, node_modules, etc.
        rel_path = str(md_file.relative_to(repo_path))
        if any(part.startswith(".") for part in md_file.parts):
            continue

        text = md_file.read_text(encoding="utf-8", errors="replace")
        chunks = chunk_markdown(text, repo_slug, rel_path)
        all_chunks.extend(chunks)

    log.info("Collected %d chunks from %s", len(all_chunks), repo_slug)
    return all_chunks


# ---------------------------------------------------------------------------
# ChromaDB loading (embeddings handled automatically by ChromaDB's default model)
# ---------------------------------------------------------------------------

def load_into_chroma(chunks: list[dict]) -> chromadb.Collection:
    """Load chunks into ChromaDB. Embeddings are generated locally by ChromaDB."""
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))

    # Delete existing collection to do a clean reload
    try:
        client.delete_collection(COLLECTION_NAME)
        log.info("Deleted existing collection '%s'", COLLECTION_NAME)
    except Exception:
        pass

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    # Generate deterministic IDs from content hash, dedup
    seen_ids: set[str] = set()
    ids = []
    documents = []
    metadatas = []

    for chunk in chunks:
        chunk_id = hashlib.sha256(
            f"{chunk['source_repo']}:{chunk['file_path']}:{chunk['text'][:500]}".encode()
        ).hexdigest()
        if chunk_id in seen_ids:
            continue
        seen_ids.add(chunk_id)
        ids.append(chunk_id)
        documents.append(chunk["text"])
        metadatas.append(
            {
                "source_repo": chunk["source_repo"],
                "file_path": chunk["file_path"],
                "heading_hierarchy": " > ".join(chunk["heading_hierarchy"]),
                "page_url": chunk["page_url"],
            }
        )

    # Upsert in batches — ChromaDB embeds documents automatically
    batch_size = 500  # smaller batches since local embedding is CPU-bound
    for i in range(0, len(ids), batch_size):
        end = i + batch_size
        log.info("Embedding + upserting batch %d–%d of %d", i, min(end, len(ids)), len(ids))
        collection.upsert(
            ids=ids[i:end],
            documents=documents[i:end],
            metadatas=metadatas[i:end],
        )

    log.info("ChromaDB collection '%s' now has %d documents", COLLECTION_NAME, collection.count())
    return collection


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def ingest(repos: list[str] | None = None) -> dict:
    """Run the full ingestion pipeline. Returns summary stats."""
    repos = repos or DEFAULT_REPOS

    all_chunks: list[dict] = []

    for repo_slug in repos:
        repo_path = clone_or_pull(repo_slug)
        chunks = collect_chunks(repo_slug, repo_path)
        all_chunks.extend(chunks)

    if not all_chunks:
        log.warning("No chunks collected — nothing to embed.")
        return {"repos": repos, "chunks": 0}

    log.info("Total chunks to embed: %d", len(all_chunks))

    # Load into ChromaDB (embeddings generated locally by ChromaDB)
    collection = load_into_chroma(all_chunks)

    stats = {
        "repos": repos,
        "chunks": len(all_chunks),
        "collection_count": collection.count(),
    }
    log.info("Ingestion complete: %s", stats)
    return stats


def main():
    """CLI entrypoint for percona-dk-ingest."""
    from percona_dk.version_check import print_version_notice
    print_version_notice()
    result = ingest()
    print(f"\n✓ Ingestion complete: {result['chunks']} chunks loaded into ChromaDB")


if __name__ == "__main__":
    main()
