"""
Percona Developer Knowledge — Ingestion Pipeline

Clones Percona doc repos from GitHub, parses Markdown source files,
chunks by h2/h3 headings, and loads into ChromaDB for semantic search.
ChromaDB handles embedding locally via its default model (all-MiniLM-L6-v2).

Supports incremental re-ingestion: on subsequent runs, only files whose
content changed since the last ingest are re-chunked and re-embedded.
"""

import json
import os
import re
import sys
import hashlib
import logging
from pathlib import Path

import chromadb
import git
from dotenv import load_dotenv

# Find .env relative to repo root so CLI commands work regardless of cwd.
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
# Configuration
# ---------------------------------------------------------------------------
_raw_data = os.getenv("DATA_DIR", "data")
_data_path = Path(_raw_data)
DATA_DIR = (_env_dir / _data_path).resolve() if not _data_path.is_absolute() else _data_path.resolve()
REPOS_DIR = DATA_DIR / "repos"
CHROMA_DIR = DATA_DIR / "chroma"
COLLECTION_NAME = "percona_docs"
FILES_MARKER = DATA_DIR / ".last_ingest_files.json"

DEFAULT_REPOS = [r.strip() for r in os.getenv("REPOS", "percona/psmysql-docs").split(",") if r.strip()]

MAX_CHUNK_CHARS = 4000  # rough limit to stay within token budget

_INTERACTIVE = sys.stdout.isatty()


def _bar(current: int, total: int, width: int = 35) -> str:
    filled = int(width * current / total) if total else width
    filled = min(filled, width)
    bar = "=" * filled + (">" if filled < width else "") + " " * (width - filled - (1 if filled < width else 0))
    pct = int(100 * current / total) if total else 100
    return f"[{bar}] {pct:3d}%  {current}/{total}"


# ---------------------------------------------------------------------------
# Repo cloning / pulling
# ---------------------------------------------------------------------------

def clone_or_pull(repo_slug: str) -> Path | None:
    """Clone a GitHub repo (or pull if already cloned). Returns the local path, or None on failure."""
    repo_url = f"https://github.com/{repo_slug}.git"
    local_path = REPOS_DIR / repo_slug.replace("/", "_")

    try:
        if (local_path / ".git").exists():
            log.info("Pulling latest for %s", repo_slug)
            repo = git.Repo(local_path)
            repo.remotes.origin.pull()
        else:
            log.info("Cloning %s → %s", repo_url, local_path)
            local_path.mkdir(parents=True, exist_ok=True)
            git.Repo.clone_from(repo_url, local_path, depth=1)
    except git.GitCommandError as e:
        stderr = str(e).lower()
        if "repository not found" in stderr or "not found" in stderr:
            print(f"\n  ! Repo not found: {repo_slug}")
            print(f"    The repository https://github.com/{repo_slug} does not exist.")
            print(f"    Check the repo name in your .env file and remove or correct it.\n")
        else:
            print(f"\n  ! Could not clone {repo_slug}: {e}\n")
        return None

    return local_path


# ---------------------------------------------------------------------------
# Markdown chunking
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)


def _build_page_url(repo_slug: str, file_path: str) -> str:
    """Construct a docs.percona.com URL from repo slug and file path."""
    rel = file_path
    for prefix in ("docs/", "source/"):
        if rel.startswith(prefix):
            rel = rel[len(prefix):]
    rel = re.sub(r"\.md$", "", rel)
    product = repo_slug.split("/")[-1].replace("-docs", "").replace("_", "-")
    return f"https://docs.percona.com/{product}/latest/{rel}/"


def chunk_markdown(text: str, repo_slug: str, file_path: str) -> list[dict]:
    """Split a Markdown file into chunks at h2/h3 boundaries."""
    headings: list[tuple[int, int, str]] = []
    for m in _HEADING_RE.finditer(text):
        headings.append((m.start(), len(m.group(1)), m.group(2).strip()))

    if not headings:
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
    hierarchy: list[str] = []

    for i, (pos, level, title) in enumerate(headings):
        start = pos
        end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        section_text = text[start:end].strip()
        if not section_text:
            continue
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
        rel_path = str(md_file.relative_to(repo_path))
        if any(part.startswith(".") for part in Path(rel_path).parts):
            continue
        text = md_file.read_text(encoding="utf-8", errors="replace")
        chunks = chunk_markdown(text, repo_slug, rel_path)
        all_chunks.extend(chunks)
    log.info("Collected %d chunks from %s", len(all_chunks), repo_slug)
    return all_chunks


def _scan_file_hashes(repo_slug: str, repo_path: Path) -> dict[str, str]:
    """Return {rel_path: sha256} for all tracked .md files in the repo."""
    hashes = {}
    for md_file in sorted(repo_path.rglob("*.md")):
        rel_path = str(md_file.relative_to(repo_path))
        if any(part.startswith(".") for part in Path(rel_path).parts):
            continue
        hashes[rel_path] = hashlib.sha256(md_file.read_bytes()).hexdigest()
    return hashes


# ---------------------------------------------------------------------------
# ChromaDB helpers
# ---------------------------------------------------------------------------

def _get_collection(create: bool = False) -> chromadb.Collection:
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    if create:
        return client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
    return client.get_collection(name=COLLECTION_NAME)


def _delete_chunks_for_files(collection: chromadb.Collection, repo_slug: str, file_paths: list[str]) -> int:
    """Delete all chunks belonging to specific files. Returns count deleted."""
    total_deleted = 0
    for file_path in file_paths:
        try:
            results = collection.get(
                where={"$and": [
                    {"source_repo": {"$eq": repo_slug}},
                    {"file_path": {"$eq": file_path}},
                ]}
            )
            if results["ids"]:
                collection.delete(ids=results["ids"])
                total_deleted += len(results["ids"])
        except Exception as e:
            log.warning("Could not delete chunks for %s:%s: %s", repo_slug, file_path, e)
    return total_deleted


def _delete_all_repo_chunks(collection: chromadb.Collection, repo_slug: str) -> int:
    """Delete all chunks for a repo. Returns count deleted."""
    try:
        results = collection.get(where={"source_repo": {"$eq": repo_slug}})
        if results["ids"]:
            collection.delete(ids=results["ids"])
            return len(results["ids"])
    except Exception as e:
        log.warning("Could not delete chunks for %s: %s", repo_slug, e)
    return 0


def _upsert_chunks(collection: chromadb.Collection, chunks: list[dict], label: str = "") -> int:
    """Upsert chunks into ChromaDB with dedup. Returns count upserted."""
    seen_ids: set[str] = set()
    ids, documents, metadatas = [], [], []

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

    batch_size = 500
    total = len(ids)
    first = True
    for i in range(0, total, batch_size):
        end = min(i + batch_size, total)
        if _INTERACTIVE:
            step = label or metadatas[i].get("source_repo", "")
            if not first:
                print("\033[2A\033[2K", end="", flush=True)
            print(f"  {_bar(end, total)}", flush=True)
            print(f"  \033[2m{step}\033[0m", flush=True)
            first = False
        else:
            log.info("Embedding + upserting batch %d-%d of %d", i, end, total)
        collection.upsert(
            ids=ids[i:end],
            documents=documents[i:end],
            metadatas=metadatas[i:end],
        )

    if _INTERACTIVE and total > 0:
        print("\033[2A\033[2K", end="", flush=True)
        print(f"  {_bar(total, total)}  done", flush=True)
        print(f"  \033[2m{total} chunks embedded\033[0m", flush=True)

    return total


# ---------------------------------------------------------------------------
# Main ingestion
# ---------------------------------------------------------------------------

def ingest(repos: list[str] | None = None) -> dict:
    """Run the ingestion pipeline. Uses incremental updates when possible."""
    repos = repos or DEFAULT_REPOS
    total_repos = len(repos)

    # Load stored file hashes from last run
    stored_hashes: dict[str, dict[str, str]] = {}
    if FILES_MARKER.exists():
        try:
            stored_hashes = json.loads(FILES_MARKER.read_text())
        except Exception:
            pass

    # Get or create ChromaDB collection
    try:
        collection = _get_collection(create=False)
        collection_exists = True
    except Exception:
        collection = _get_collection(create=True)
        collection_exists = False

    new_hashes: dict[str, dict[str, str]] = {}
    total_added = 0
    total_deleted = 0

    for idx, repo_slug in enumerate(repos, 1):
        if _INTERACTIVE:
            print(f"  [{idx}/{total_repos}] {repo_slug} ...", flush=True)
        else:
            log.info("[%d/%d] Processing %s", idx, total_repos, repo_slug)

        repo_path = clone_or_pull(repo_slug)
        if repo_path is None:
            if _INTERACTIVE:
                print(f"\033[A\033[2K  [{idx}/{total_repos}] {repo_slug}  (skipped - not found)", flush=True)
            continue

        current_hashes = _scan_file_hashes(repo_slug, repo_path)
        new_hashes[repo_slug] = current_hashes
        repo_stored = stored_hashes.get(repo_slug, {})

        # Determine what changed
        changed = [p for p, h in current_hashes.items() if repo_stored.get(p) != h]
        deleted = [p for p in repo_stored if p not in current_hashes]

        if collection_exists and repo_stored:
            if not changed and not deleted:
                if _INTERACTIVE:
                    print(f"\033[A\033[2K  [{idx}/{total_repos}] {repo_slug}  (no changes)", flush=True)
                else:
                    log.info("No changes in %s", repo_slug)
                continue

            # Incremental: delete stale chunks, re-embed changed files
            n_files = len(changed) + len(deleted)
            if _INTERACTIVE:
                print(f"\033[A\033[2K  [{idx}/{total_repos}] {repo_slug}  ({len(changed)} changed, {len(deleted)} deleted)", flush=True)
            else:
                log.info("%s: %d changed, %d deleted files", repo_slug, len(changed), len(deleted))

            if deleted or changed:
                n_del = _delete_chunks_for_files(collection, repo_slug, deleted + changed)
                total_deleted += n_del

            # Re-chunk and upsert changed files
            chunks: list[dict] = []
            for file_path in changed:
                full_path = repo_path / file_path
                if full_path.exists():
                    text = full_path.read_text(encoding="utf-8", errors="replace")
                    chunks.extend(chunk_markdown(text, repo_slug, file_path))

            if chunks:
                if _INTERACTIVE:
                    print(f"\n  Embedding {len(chunks)} updated chunks ({repo_slug})...", flush=True)
                n_added = _upsert_chunks(collection, chunks, label=repo_slug)
                total_added += n_added

        else:
            # First time seeing this repo - full chunk and embed
            if _INTERACTIVE:
                print(f"\033[A\033[2K  [{idx}/{total_repos}] {repo_slug}  (new - full ingest)...", flush=True)
            chunks = collect_chunks(repo_slug, repo_path)
            if _INTERACTIVE:
                print(f"\n  Embedding {len(chunks)} chunks ({repo_slug})...", flush=True)
            if chunks:
                n_added = _upsert_chunks(collection, chunks, label=repo_slug)
                total_added += n_added
                if _INTERACTIVE:
                    print(f"\033[A\033[2K  [{idx}/{total_repos}] {repo_slug}  ({len(chunks)} chunks)", flush=True)

    # Persist updated file hashes (merge: keep repos not in this run unchanged)
    merged_hashes = {**stored_hashes, **new_hashes}
    FILES_MARKER.parent.mkdir(parents=True, exist_ok=True)
    FILES_MARKER.write_text(json.dumps(merged_hashes))

    # Write timestamp marker for auto-refresh checks
    marker = DATA_DIR / ".last_ingest"
    marker.write_text(str(__import__("time").time()))

    stats = {
        "repos": repos,
        "chunks_added": total_added,
        "chunks_deleted": total_deleted,
        "collection_count": collection.count(),
    }
    if not _INTERACTIVE:
        log.info("Ingestion complete: %s", stats)
    return stats


def main():
    """CLI entrypoint for percona-dk-ingest."""
    from percona_dk.version_check import print_version_notice
    print_version_notice()
    result = ingest()
    added = result["chunks_added"]
    deleted = result["chunks_deleted"]
    total = result["collection_count"]
    print(f"\n✓ Ingestion complete: {added} added, {deleted} removed — {total} chunks total")


if __name__ == "__main__":
    main()
