"""
Helpers shared by the MCP server (mcp_server.py), the REST API (server.py)
and the remote shim (mcp_remote_shim.py).

Kept free of module-level config so both servers can pass in their own
cached collection and repo->versions map. chromadb is imported for type
hints only, so the shim can use this module without loading it.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

if TYPE_CHECKING:
    import chromadb


def version_where(version: str | None) -> dict | None:
    """Chroma `where` filter for a version-scoped search.

    Matches the requested version plus version-agnostic content (empty
    version) so single-branch repos and community content still surface.
    """
    if not version:
        return None
    return {"$or": [
        {"version": {"$eq": version}},
        {"version": {"$eq": ""}},
    ]}


def _score(distance: float) -> float:
    # ChromaDB cosine distance: 0 = identical, 2 = opposite.
    # Convert to a 0-1 relevance score.
    return round(1.0 - distance / 2.0, 4)


def format_search_markdown(
    query: str,
    results: dict,
    version: str | None,
    get_repo_versions: Callable[[], dict[str, list[str]]],
) -> str:
    """Render a chroma query result as the Markdown layout agents see.

    Used by the `search_percona_docs` MCP tool and by `GET /search`, so
    both surfaces return identical text.
    """
    from percona_dk.repo_registry import suggest_repos

    if not results["documents"] or not results["documents"][0]:
        msg = "No results found for your query."
        suggestion = suggest_repos(query, 0.0)
        if suggestion:
            msg += suggestion
        return msg

    output_parts: list[str] = []
    for i, (doc, meta, dist) in enumerate(
        zip(results["documents"][0], results["metadatas"][0], results["distances"][0])
    ):
        version_meta = meta.get("version") or ""
        version_line = f"**Version:** {version_meta}\n" if version_meta else ""
        output_parts.append(
            f"### Result {i + 1} (relevance: {_score(dist)})\n"
            f"**Source:** {meta['source_repo']} - `{meta['file_path']}`\n"
            f"{version_line}"
            f"**Section:** {meta['heading_hierarchy']}\n"
            f"**URL:** {meta['page_url']}\n\n"
            f"{doc}\n"
        )

    output = "\n---\n".join(output_parts)

    # If any returned chunk is from a multi-version repo, hint that the
    # caller can scope to a specific version. Helps the model avoid
    # assuming a repo is "only partially indexed" when it's actually
    # split across version-tagged subsets.
    if not version:
        repo_versions = get_repo_versions()
        repos_in_results = {
            meta["source_repo"]
            for meta in results["metadatas"][0]
            if meta.get("version")
        }
        hint_lines = [
            f"  - {r}: {', '.join(repo_versions[r])}"
            for r in sorted(repos_in_results)
            if len(repo_versions.get(r, [])) > 1
        ]
        if hint_lines:
            output += (
                "\n\n---\n"
                "Some results above are from repos with multiple indexed "
                "versions. To scope to one version, re-run with the "
                "`version` argument. Available versions:\n"
                + "\n".join(hint_lines)
            )

    # Check if the query might match an unconfigured repo
    max_score = max((_score(d) for d in results["distances"][0]), default=0.0)
    suggestion = suggest_repos(query, max_score)
    if suggestion:
        output += suggestion

    return output


# ---------------------------------------------------------------------------
# Page URL -> (repo, path, version)
# ---------------------------------------------------------------------------

_FORUM_TOPIC_RE = re.compile(r"^https://forums\.percona\.com/t/[^/]+/\d+$")


def url_candidates(url: str) -> list[str]:
    """Spellings of `url` that may match a stored `page_url`, best first.

    Search results print the canonical form, but agents also paste browser
    URLs: http vs https, a #fragment or ?query, a trailing slash instead of
    `.html`, or a forum topic link without the post number.
    """
    u = url.strip().split("#", 1)[0].split("?", 1)[0]
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    elif not u.startswith("https://"):
        u = "https://" + u.lstrip("/")

    cands = [u]
    if u.endswith("/index.html"):
        cands.append(u[: -len("index.html")])
    elif u.endswith(".html"):
        stem = u[: -len(".html")]
        cands += [stem + "/", stem]
    elif u.endswith("/"):
        stem = u.rstrip("/")
        cands += [stem + ".html", stem]
    else:
        cands += [u + ".html", u + "/"]
        if _FORUM_TOPIC_RE.match(u):
            cands.append(u + "/1")  # topic link -> its first post

    seen: set[str] = set()
    return [c for c in cands if not (c in seen or seen.add(c))]


def resolve_page_url(
    collection: chromadb.Collection, url: str
) -> tuple[str, str, str | None] | None:
    """Map a page URL to (source_repo, file_path, version) via chunk metadata.

    Returns None when no indexed chunk carries a matching `page_url`.
    `version` is None for single-branch repos and community content.
    """
    cands = url_candidates(url)
    res = collection.get(
        where={"page_url": {"$in": cands}},
        include=["metadatas"],
        limit=50,
    )
    metas = res.get("metadatas") or []
    for cand in cands:
        for m in metas:
            if m.get("page_url") == cand:
                return m["source_repo"], m["file_path"], (m.get("version") or None)
    return None


# ---------------------------------------------------------------------------
# Pagination for large documents
# ---------------------------------------------------------------------------

DEFAULT_MAX_CHARS = 30000


def paginate(text: str, max_chars: int = DEFAULT_MAX_CHARS, offset: int = 0) -> str:
    """Return one page of `text`, starting at character `offset`.

    Pages end on a line break when one falls in the second half of the
    window, so Markdown isn't cut mid-line. A truncated page ends with a
    line giving the `offset` for the next call.
    """
    max_chars = max(1, max_chars)
    offset = max(0, offset)
    total = len(text)

    if offset == 0 and total <= max_chars:
        return text
    if offset >= total:
        return (
            f"Error: offset {offset} is past the end of the document "
            f"({total} characters)."
        )

    end = min(offset + max_chars, total)
    if end < total:
        newline = text.rfind("\n", offset + max_chars // 2, end)
        if newline != -1:
            end = newline + 1

    page = text[offset:end]
    if offset > 0:
        page = f"[Continued from character {offset} of {total}.]\n\n" + page
    if end < total:
        page = page.rstrip("\n") + (
            f"\n\n---\n[Truncated: showing characters {offset}-{end} of {total}. "
            f"To read the next page, call get_percona_doc again with the same "
            f"arguments and offset={end}.]"
        )
    return page


# ---------------------------------------------------------------------------
# Agent feedback log
# ---------------------------------------------------------------------------

FeedbackCategory = Literal["documentation", "product", "mcp_server", "other"]
FeedbackSeverity = Literal["high", "medium", "low"]

FEEDBACK_MAX_CHARS = 8000  # per field; keeps a shared instance's log bounded
_feedback_lock = threading.Lock()


def append_feedback(
    feedback_file: Path,
    message: str,
    category: str,
    context: str | None = None,
    severity: str | None = None,
    tool_name: str | None = None,
) -> dict:
    """Append one feedback entry as a JSON line to `feedback_file`.

    Used by the `give_feedback` MCP tool and by `POST /feedback`, so both
    write the same format. Returns the entry written (with its short
    `id`). Raises OSError if the file can't be written.
    """
    def _clip(value: str | None) -> str | None:
        return value[:FEEDBACK_MAX_CHARS] if value else value

    entry = {
        "id": uuid.uuid4().hex[:12],
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "category": category,
        "severity": severity,
        "tool_name": tool_name,
        "message": _clip(message),
        "context": _clip(context),
    }
    with _feedback_lock:
        feedback_file.parent.mkdir(parents=True, exist_ok=True)
        with feedback_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry
