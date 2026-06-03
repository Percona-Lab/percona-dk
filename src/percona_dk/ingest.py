"""
Percona Developer Knowledge — Ingestion Pipeline

Clones Percona doc repos from GitHub, parses Markdown and reStructuredText
source files, chunks by h2/h3 headings, and loads into ChromaDB for
semantic search.  ChromaDB handles embedding locally via its default model
(all-MiniLM-L6-v2).

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

def _parse_repos(raw: str) -> list[tuple[str, str | None]]:
    """Parse a comma-separated REPOS string into (slug, branch) pairs.

    Each entry is either "owner/repo" (clone default branch) or
    "owner/repo:branch" (clone the named branch). Used to index multiple
    versions of a doc repo as separate corpora tagged with version metadata.
    """
    out: list[tuple[str, str | None]] = []
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        if ":" in entry:
            slug, _, branch = entry.partition(":")
            out.append((slug.strip(), branch.strip() or None))
        else:
            out.append((entry, None))
    return out


DEFAULT_REPOS = _parse_repos(os.getenv("REPOS", "percona/psmysql-docs"))

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

def clone_or_pull(repo_slug: str, branch: str | None = None) -> Path | None:
    """Clone a GitHub repo (or pull if already cloned). Returns the local path, or None on failure.

    When `branch` is given, that branch is checked out and the local path is
    suffixed with `__<branch>` so multiple version-branches of the same repo
    can coexist on disk.
    """
    repo_url = f"https://github.com/{repo_slug}.git"
    base_name = repo_slug.replace("/", "_")
    local_name = f"{base_name}__{branch}" if branch else base_name
    local_path = REPOS_DIR / local_name

    try:
        if (local_path / ".git").exists():
            log.info("Pulling latest for %s%s", repo_slug, f"@{branch}" if branch else "")
            repo = git.Repo(local_path)
            repo.remotes.origin.pull()
        else:
            log.info("Cloning %s%s → %s", repo_url, f" (branch {branch})" if branch else "", local_path)
            local_path.mkdir(parents=True, exist_ok=True)
            kwargs = {"depth": 1}
            if branch:
                kwargs["branch"] = branch
            git.Repo.clone_from(repo_url, local_path, **kwargs)
    except git.GitCommandError as e:
        stderr = str(e).lower()
        if "repository not found" in stderr or "not found" in stderr:
            print(f"\n  ! Repo not found: {repo_slug}")
            print(f"    The repository https://github.com/{repo_slug} does not exist.")
            print(f"    Check the repo name in your .env file and remove or correct it.\n")
        elif "remote branch" in stderr and "not found" in stderr:
            print(f"\n  ! Branch '{branch}' not found in {repo_slug}\n")
        else:
            print(f"\n  ! Could not clone {repo_slug}: {e}\n")
        return None

    return local_path


# ---------------------------------------------------------------------------
# Markdown chunking
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)

# RST heading detection: a line of text followed by an underline of =, -, or ~.
# =  -> h1,  -  -> h2,  ~  -> h3  (common Sphinx convention)
_RST_HEADING_RE = re.compile(
    r"^(?P<title>.+)\n(?P<underline>[=\-~]{3,})\s*$", re.MULTILINE
)
_RST_LEVEL = {"=": 1, "-": 2, "~": 3}

DOC_EXTENSIONS = ("*.md", "*.rst")


# Map repo slug -> (docs.percona.com URL slug, default version segment).
# - URL slug is the path under docs.percona.com (may include sub-paths like
#   "percona-operator-for-mysql/ps").
# - Default version segment is what to put in the URL when no explicit branch
#   was indexed:
#     None -> use "latest"  (the docs site usually redirects to current LTS)
#     ""   -> omit the version segment entirely (e.g. /percona-toolkit/page/)
#     "3"  -> fixed (e.g. PMM, where docs are versioned by major)
# When an explicit branch IS indexed (multi-version repos), it always wins
# over the default and is inserted as the version segment.
_REPO_URL_MAP: dict[str, tuple[str, str | None]] = {
    "percona/psmysql-docs":         ("percona-server", None),
    "percona/pxc-docs":             ("percona-xtradb-cluster", None),
    "percona/pxb-docs":             ("percona-xtrabackup", None),
    "percona/pdmysql-docs":         ("percona-distribution-for-mysql", None),
    "percona/psmdb-docs":           ("percona-server-for-mongodb", None),
    "percona/postgresql-docs":      ("postgresql", None),
    "percona/pbm-docs":             ("percona-backup-mongodb", ""),
    "percona/pmm-doc":              ("percona-monitoring-and-management", "3"),
    "percona/k8sps-docs":           ("percona-operator-for-mysql/ps", ""),
    "percona/k8spxc-docs":          ("percona-operator-for-mysql/pxc", ""),
    "percona/k8spsmdb-docs":        ("percona-operator-for-mongodb", ""),
    "percona/k8spg-docs":           ("percona-operator-for-postgresql", ""),
    "percona/percona-toolkit":      ("percona-toolkit", ""),
    "percona/pg_tde":               ("pg-tde", ""),
    "percona/pgsm-docs":            ("pg-stat-monitor", ""),
    "percona/percona-valkey-doc":   ("valkey", ""),
    "openeverest/everest-doc":      ("everest", ""),
    "percona/proxysql-admin-tool-doc": ("proxysql", ""),
    # No public docs.percona.com pages (yet) — fall back to GitHub source URLs:
    # percona/ps-binlog-server-docs, percona/pmm_dump_docs,
    # percona/pcsm-docs, percona/repo-config-docs
}

# Repos whose docs site is built with mkdocs `use_directory_urls: false`
# (or Sphinx) — their published page URLs end in `.html`, not a trailing
# slash. Every other mapped repo uses directory-style URLs (trailing slash).
# Getting this wrong yields 404s (e.g. /pg-tde/variables/ vs the real
# /pg-tde/variables.html).
_HTML_URL_REPOS: set[str] = {
    "percona/pg_tde",
    "percona/pgsm-docs",
    "percona/percona-valkey-doc",
    "percona/percona-toolkit",
}


def _build_page_url(repo_slug: str, file_path: str, version: str | None = None) -> str:
    """Construct a docs.percona.com URL from repo slug and file path.

    Uses _REPO_URL_MAP for the canonical product slug; falls back to a
    GitHub source URL for repos that aren't published on docs.percona.com.
    """
    rel = file_path
    # Strip the repo's doc-source folder prefix. Most repos keep docs under
    # docs/ (or source/ for older Sphinx layouts); pg_tde uses
    # documentation/docs/. Most-specific prefix first.
    for prefix in ("documentation/docs/", "docs/", "source/"):
        if rel.startswith(prefix):
            rel = rel[len(prefix):]
            break
    rel = re.sub(r"\.(md|rst)$", "", rel)
    rel = re.sub(r"/index$", "", rel)   # section index -> its directory
    if rel == "index":
        rel = ""                        # site root

    mapping = _REPO_URL_MAP.get(repo_slug)
    if mapping is None:
        # Unmapped repo: link to the GitHub source so users still get a
        # clickable target. Branch defaults to "main" if unknown.
        branch_seg = version or "main"
        return f"https://github.com/{repo_slug}/blob/{branch_seg}/{file_path}"

    url_slug, default_version = mapping
    if version:
        version_segment = version
    elif default_version is None:
        version_segment = "latest"
    else:
        version_segment = default_version  # "" allowed for unversioned URLs

    base = f"https://docs.percona.com/{url_slug}"
    if version_segment:
        base = f"{base}/{version_segment}"

    if rel == "":
        return f"{base}/"
    if repo_slug in _HTML_URL_REPOS:
        return f"{base}/{rel}.html"
    return f"{base}/{rel}/"


def chunk_markdown(text: str, repo_slug: str, file_path: str, version: str | None = None) -> list[dict]:
    """Split a Markdown file into chunks at h2/h3 boundaries."""
    headings: list[tuple[int, int, str]] = []
    for m in _HEADING_RE.finditer(text):
        headings.append((m.start(), len(m.group(1)), m.group(2).strip()))

    page_url = _build_page_url(repo_slug, file_path, version)
    version_tag = version or ""

    if not headings:
        stripped = text.strip()
        if not stripped:
            return []
        return [
            {
                "text": stripped[:MAX_CHUNK_CHARS],
                "source_repo": repo_slug,
                "version": version_tag,
                "file_path": file_path,
                "heading_hierarchy": [],
                "page_url": page_url,
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
                "version": version_tag,
                "file_path": file_path,
                "heading_hierarchy": list(hierarchy),
                "page_url": page_url,
            }
        )

    pre_heading_text = text[: headings[0][0]].strip()
    if pre_heading_text:
        chunks.insert(
            0,
            {
                "text": pre_heading_text[:MAX_CHUNK_CHARS],
                "source_repo": repo_slug,
                "version": version_tag,
                "file_path": file_path,
                "heading_hierarchy": [],
                "page_url": page_url,
            },
        )

    return chunks


def chunk_rst(text: str, repo_slug: str, file_path: str, version: str | None = None) -> list[dict]:
    """Split a reStructuredText file into chunks at heading boundaries."""
    headings: list[tuple[int, int, str]] = []
    for m in _RST_HEADING_RE.finditer(text):
        char = m.group("underline")[0]
        level = _RST_LEVEL.get(char, 2)
        title = m.group("title").strip().strip(":").strip("`").strip()
        if title:
            headings.append((m.start(), level, title))

    page_url = _build_page_url(repo_slug, file_path, version)
    version_tag = version or ""

    if not headings:
        stripped = text.strip()
        if not stripped:
            return []
        return [
            {
                "text": stripped[:MAX_CHUNK_CHARS],
                "source_repo": repo_slug,
                "version": version_tag,
                "file_path": file_path,
                "heading_hierarchy": [],
                "page_url": page_url,
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
                "version": version_tag,
                "file_path": file_path,
                "heading_hierarchy": list(hierarchy),
                "page_url": page_url,
            }
        )

    pre_heading_text = text[: headings[0][0]].strip()
    if pre_heading_text:
        chunks.insert(
            0,
            {
                "text": pre_heading_text[:MAX_CHUNK_CHARS],
                "source_repo": repo_slug,
                "version": version_tag,
                "file_path": file_path,
                "heading_hierarchy": [],
                "page_url": page_url,
            },
        )

    return chunks


# ---------------------------------------------------------------------------
# Walk repo and collect chunks
# ---------------------------------------------------------------------------

def _find_doc_files(repo_path: Path) -> list[Path]:
    """Return all .md and .rst files in a repo, sorted."""
    files = []
    for ext in DOC_EXTENSIONS:
        files.extend(repo_path.rglob(ext))
    return sorted(set(files))


def collect_chunks(repo_slug: str, repo_path: Path, version: str | None = None) -> list[dict]:
    """Walk all doc files (.md, .rst) in a repo and return chunks."""
    all_chunks: list[dict] = []
    doc_files = _find_doc_files(repo_path)
    log.info("Found %d doc files in %s%s", len(doc_files), repo_slug, f"@{version}" if version else "")
    for doc_file in doc_files:
        rel_path = str(doc_file.relative_to(repo_path))
        if any(part.startswith(".") for part in Path(rel_path).parts):
            continue
        text = doc_file.read_text(encoding="utf-8", errors="replace")
        if doc_file.suffix == ".rst":
            chunks = chunk_rst(text, repo_slug, rel_path, version)
        else:
            chunks = chunk_markdown(text, repo_slug, rel_path, version)
        all_chunks.extend(chunks)
    log.info("Collected %d chunks from %s%s", len(all_chunks), repo_slug, f"@{version}" if version else "")
    return all_chunks


def _scan_file_hashes(repo_slug: str, repo_path: Path) -> dict[str, str]:
    """Return {rel_path: sha256} for all tracked doc files in the repo."""
    hashes = {}
    for doc_file in _find_doc_files(repo_path):
        rel_path = str(doc_file.relative_to(repo_path))
        if any(part.startswith(".") for part in Path(rel_path).parts):
            continue
        hashes[rel_path] = hashlib.sha256(doc_file.read_bytes()).hexdigest()
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


def _delete_chunks_for_files(
    collection: chromadb.Collection,
    repo_slug: str,
    file_paths: list[str],
    version: str | None = None,
) -> int:
    """Delete all chunks belonging to specific files (optionally scoped to a version).

    Returns count deleted.
    """
    total_deleted = 0
    version_tag = version or ""
    for file_path in file_paths:
        try:
            results = collection.get(
                where={"$and": [
                    {"source_repo": {"$eq": repo_slug}},
                    {"version": {"$eq": version_tag}},
                    {"file_path": {"$eq": file_path}},
                ]}
            )
            if results["ids"]:
                collection.delete(ids=results["ids"])
                total_deleted += len(results["ids"])
        except Exception as e:
            log.warning("Could not delete chunks for %s@%s:%s: %s", repo_slug, version_tag, file_path, e)
    return total_deleted


def _delete_all_repo_chunks(
    collection: chromadb.Collection,
    repo_slug: str,
    version: str | None = None,
) -> int:
    """Delete all chunks for a repo (optionally scoped to a version). Returns count deleted."""
    version_tag = version or ""
    try:
        results = collection.get(
            where={"$and": [
                {"source_repo": {"$eq": repo_slug}},
                {"version": {"$eq": version_tag}},
            ]}
        )
        if results["ids"]:
            collection.delete(ids=results["ids"])
            return len(results["ids"])
    except Exception as e:
        log.warning("Could not delete chunks for %s@%s: %s", repo_slug, version_tag, e)
    return 0


def _upsert_chunks(collection: chromadb.Collection, chunks: list[dict], label: str = "") -> int:
    """Upsert chunks into ChromaDB with dedup. Returns count upserted."""
    seen_ids: set[str] = set()
    ids, documents, metadatas = [], [], []

    for chunk in chunks:
        version_tag = chunk.get("version", "") or ""
        # Only mix the version into the id when it is non-empty, so unversioned
        # content (community blog/forum, single-branch repos) keeps stable ids
        # across the multi-version migration. Multi-version repos still get
        # disambiguated because their chunks always carry a version_tag.
        if version_tag:
            id_key = f"{chunk['source_repo']}:{version_tag}:{chunk['file_path']}:{chunk['text'][:500]}"
        else:
            id_key = f"{chunk['source_repo']}:{chunk['file_path']}:{chunk['text'][:500]}"
        chunk_id = hashlib.sha256(id_key.encode()).hexdigest()
        if chunk_id in seen_ids:
            continue
        seen_ids.add(chunk_id)
        ids.append(chunk_id)
        documents.append(chunk["text"])
        metadatas.append(
            {
                "source_repo": chunk["source_repo"],
                "version": version_tag,
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

def ingest(repos: list[tuple[str, str | None]] | list[str] | None = None) -> dict:
    """Run the ingestion pipeline. Uses incremental updates when possible.

    `repos` may be a list of (slug, branch) tuples, or legacy list of plain
    slug strings (treated as default branch). When omitted, DEFAULT_REPOS is
    used. Each (slug, branch) is treated as a distinct corpus, with chunks
    tagged with the branch as a `version` metadata field.
    """
    if repos is None:
        repo_specs: list[tuple[str, str | None]] = DEFAULT_REPOS
    else:
        repo_specs = [r if isinstance(r, tuple) else (r, None) for r in repos]
    total_repos = len(repo_specs)

    # Load stored file hashes from last run. Keys are "slug" (single-branch)
    # or "slug@branch" (multi-version) so different branches have isolated
    # change-detection state.
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

    for idx, (repo_slug, branch) in enumerate(repo_specs, 1):
        label = f"{repo_slug}@{branch}" if branch else repo_slug
        hash_key = f"{repo_slug}@{branch}" if branch else repo_slug
        if _INTERACTIVE:
            print(f"  [{idx}/{total_repos}] {label} ...", flush=True)
        else:
            log.info("[%d/%d] Processing %s", idx, total_repos, label)

        repo_path = clone_or_pull(repo_slug, branch)
        if repo_path is None:
            if _INTERACTIVE:
                print(f"\033[A\033[2K  [{idx}/{total_repos}] {label}  (skipped - not found)", flush=True)
            continue

        current_hashes = _scan_file_hashes(repo_slug, repo_path)
        new_hashes[hash_key] = current_hashes
        repo_stored = stored_hashes.get(hash_key, {})

        # Determine what changed
        changed = [p for p, h in current_hashes.items() if repo_stored.get(p) != h]
        deleted = [p for p in repo_stored if p not in current_hashes]

        if collection_exists and repo_stored:
            if not changed and not deleted:
                if _INTERACTIVE:
                    print(f"\033[A\033[2K  [{idx}/{total_repos}] {label}  (no changes)", flush=True)
                else:
                    log.info("No changes in %s", label)
                continue

            # Incremental: delete stale chunks, re-embed changed files
            if _INTERACTIVE:
                print(f"\033[A\033[2K  [{idx}/{total_repos}] {label}  ({len(changed)} changed, {len(deleted)} deleted)", flush=True)
            else:
                log.info("%s: %d changed, %d deleted files", label, len(changed), len(deleted))

            if deleted or changed:
                n_del = _delete_chunks_for_files(collection, repo_slug, deleted + changed, branch)
                total_deleted += n_del

            # Re-chunk and upsert changed files
            chunks: list[dict] = []
            for file_path in changed:
                full_path = repo_path / file_path
                if full_path.exists():
                    text = full_path.read_text(encoding="utf-8", errors="replace")
                    if full_path.suffix == ".rst":
                        chunks.extend(chunk_rst(text, repo_slug, file_path, branch))
                    else:
                        chunks.extend(chunk_markdown(text, repo_slug, file_path, branch))

            if chunks:
                if _INTERACTIVE:
                    print(f"\n  Embedding {len(chunks)} updated chunks ({label})...", flush=True)
                n_added = _upsert_chunks(collection, chunks, label=label)
                total_added += n_added

        else:
            # First time seeing this repo+branch - full chunk and embed
            if _INTERACTIVE:
                print(f"\033[A\033[2K  [{idx}/{total_repos}] {label}  (new - full ingest)...", flush=True)
            chunks = collect_chunks(repo_slug, repo_path, branch)
            if _INTERACTIVE:
                print(f"\n  Embedding {len(chunks)} chunks ({label})...", flush=True)
            if chunks:
                n_added = _upsert_chunks(collection, chunks, label=label)
                total_added += n_added
                if _INTERACTIVE:
                    print(f"\033[A\033[2K  [{idx}/{total_repos}] {label}  ({len(chunks)} chunks)", flush=True)

    # Persist updated file hashes (merge: keep repos not in this run unchanged)
    merged_hashes = {**stored_hashes, **new_hashes}
    FILES_MARKER.parent.mkdir(parents=True, exist_ok=True)
    FILES_MARKER.write_text(json.dumps(merged_hashes))

    # Community content (blog + forum) — shares the same collection.
    community_stats = {}
    try:
        from percona_dk.community import ingest_community
        if _INTERACTIVE:
            print("  [community] blog + forum ...", flush=True)
        community_stats = ingest_community(collection)
        total_added += community_stats.get("blog_added", 0) + community_stats.get("forum_added", 0)
        total_deleted += community_stats.get("blog_deleted", 0) + community_stats.get("forum_deleted", 0)
    except Exception:
        log.exception("Community ingestion failed (continuing)")

    # Write timestamp marker for auto-refresh checks
    marker = DATA_DIR / ".last_ingest"
    marker.write_text(str(__import__("time").time()))

    stats = {
        "repos": repos,
        "chunks_added": total_added,
        "chunks_deleted": total_deleted,
        "collection_count": collection.count(),
        "community": community_stats,
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
