"""
Percona Developer Knowledge - Community Content Ingestion

Pulls Percona blog posts (percona.community/blog) by parsing the Hugo
sitemap and scraping post HTML, and Percona forum threads
(forums.percona.com) via the Discourse sitemap + JSON API, chunks them,
and upserts into the same ChromaDB collection used for docs.

Blog posts and forum posts are saved as local .md files under
REPOS_DIR/<source>/ so the existing get_percona_doc MCP tool can read them
back by (source, path) without special-casing.

State is tracked per-source in .last_ingest_community.json, keyed by post id
or topic id. The forum scan is resumable: progress is persisted every 50
topics so a crash during initial backfill (which can take hours) doesn't
re-do all the work.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from percona_dk.ingest import (
    DATA_DIR,
    REPOS_DIR,
    _delete_chunks_for_files,
    _upsert_chunks,
    chunk_markdown,
)

log = logging.getLogger(__name__)

BLOG_BASE = os.getenv("BLOG_BASE_URL", "https://percona.community").rstrip("/")
FORUM_BASE = os.getenv("FORUM_BASE_URL", "https://forums.percona.com").rstrip("/")
REQUEST_DELAY = float(os.getenv("COMMUNITY_REQUEST_DELAY", "0.25"))
REQUEST_TIMEOUT = 30
# Accumulate this many chunks across topics before flushing to ChromaDB.
# Larger = fewer embedding/upsert round-trips = much faster.
FORUM_UPSERT_BATCH = int(os.getenv("FORUM_UPSERT_BATCH", "500"))
STATE_FILE = DATA_DIR / ".last_ingest_community.json"

BLOG_SOURCE = "percona-community-blog"
FORUM_SOURCE = "percona-forums"

USER_AGENT = "percona-dk-ingest/0.2 (+https://github.com/percona/percona-dk)"

_session: requests.Session | None = None


def _http() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT})
        _session = s
    return _session


def _get_json(url: str, params: dict | None = None, retries: int = 3) -> dict | list:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = _http().get(
                url, params=params, timeout=REQUEST_TIMEOUT,
                headers={"Accept": "application/json"},
            )
            if resp.status_code == 429:
                sleep_for = float(resp.headers.get("Retry-After", 5))
                log.warning("Rate limited on %s, sleeping %.1fs", url, sleep_for)
                time.sleep(sleep_for)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            last_exc = e
            if attempt == retries - 1:
                break
            time.sleep(2 ** attempt)
    assert last_exc is not None
    raise last_exc


def _get_xml(url: str, retries: int = 3) -> str:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = _http().get(
                url, timeout=REQUEST_TIMEOUT,
                headers={"Accept": "application/xml"},
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            last_exc = e
            if attempt == retries - 1:
                break
            time.sleep(2 ** attempt)
    assert last_exc is not None
    raise last_exc


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"blog": {}, "forum": {}, "forum_categories": {}}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def _strip_html(html_content: str, preserve_headings: bool = True) -> str:
    """Convert HTML to lightweight markdown-ish plain text.

    Keeps h1-h6 as markdown headings so chunk_markdown can split on them,
    and wraps pre/code blocks in fenced code so structure is preserved.
    """
    soup = BeautifulSoup(html_content or "", "html.parser")

    for script in soup(["script", "style"]):
        script.decompose()

    if preserve_headings:
        for level in range(1, 7):
            for h in soup.find_all(f"h{level}"):
                title = h.get_text(" ", strip=True)
                h.replace_with(f"\n\n{'#' * level} {title}\n\n")

    for pre in soup.find_all("pre"):
        text = pre.get_text()
        pre.replace_with(f"\n```\n{text.strip()}\n```\n")

    for li in soup.find_all("li"):
        li.insert_before("- ")

    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _write_local(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Blog (Hugo static site — sitemap + HTML scrape)
# ---------------------------------------------------------------------------

# Blog post URL pattern: /blog/YYYY/MM/DD/slug/
_BLOG_POST_URL_RE = re.compile(r"/blog/\d{4}/\d{2}/\d{2}/[^/]+/?$")


def _blog_id_from_url(url: str) -> str:
    """Stable slug-based id for a blog post URL."""
    m = re.search(r"/blog/(\d{4})/(\d{2})/(\d{2})/([^/?#]+)", url)
    if not m:
        # Fallback: hash the URL
        import hashlib
        return hashlib.sha1(url.encode()).hexdigest()[:16]
    y, mo, d, slug = m.groups()
    return f"{y}-{mo}-{d}-{slug}"


def _fetch_blog_sitemap() -> list[tuple[str, str]]:
    """Return [(url, lastmod)] for all blog post pages in the sitemap."""
    try:
        xml = _get_xml(f"{BLOG_BASE}/sitemap.xml")
    except requests.RequestException as e:
        log.warning("Blog sitemap fetch failed: %s", e)
        return []
    soup = BeautifulSoup(xml, "xml")
    out: list[tuple[str, str]] = []
    for url_tag in soup.find_all("url"):
        loc_tag = url_tag.find("loc")
        if not loc_tag or not loc_tag.text:
            continue
        loc = loc_tag.text.strip()
        if not _BLOG_POST_URL_RE.search(loc):
            continue
        lastmod_tag = url_tag.find("lastmod")
        lastmod = lastmod_tag.text.strip() if lastmod_tag and lastmod_tag.text else ""
        out.append((loc, lastmod))
    return out


def _parse_blog_page(url: str, html: str) -> tuple[str, str, str]:
    """Extract (title, date, body_markdown) from a blog post HTML page."""
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.split("|")[0].strip()
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True) or title

    date = ""
    time_tag = soup.find("time")
    if time_tag:
        date = (time_tag.get("datetime") or time_tag.get_text(strip=True) or "")[:10]
    if not date:
        m = re.search(r"/blog/(\d{4})/(\d{2})/(\d{2})/", url)
        if m:
            date = "-".join(m.groups())

    article = soup.find("article") or soup.find("main") or soup.body
    if article is None:
        return title, date, ""

    for selector in ["nav", "header", "footer", "aside", ".share", ".social", "script", "style"]:
        for el in article.select(selector):
            el.decompose()

    body = _strip_html(str(article))
    return title or "Untitled", date, body


def ingest_blog(collection, state: dict) -> tuple[int, int]:
    """Ingest blog posts via sitemap + HTML. Returns (added, deleted)."""
    blog_state: dict = state.setdefault("blog", {})
    log.info("Fetching blog sitemap from %s ...", BLOG_BASE)
    entries = _fetch_blog_sitemap()
    log.info("Blog: %d posts in sitemap", len(entries))

    current: dict[str, dict] = {}
    to_update: list[tuple[str, str, str]] = []  # (pid, url, lastmod)
    for url, lastmod in entries:
        pid = _blog_id_from_url(url)
        file_path = f"posts/{pid}.md"
        current[pid] = {"modified": lastmod, "file_path": file_path, "url": url}
        if blog_state.get(pid, {}).get("modified") != lastmod:
            to_update.append((pid, url, lastmod))

    # Remove posts that vanished from the sitemap
    deleted_ids = [pid for pid in blog_state if pid not in current]
    total_deleted = 0
    if deleted_ids:
        deleted_paths = [blog_state[pid].get("file_path", f"posts/{pid}.md") for pid in deleted_ids]
        total_deleted = _delete_chunks_for_files(collection, BLOG_SOURCE, deleted_paths)
        for pid in deleted_ids:
            local = REPOS_DIR / BLOG_SOURCE / blog_state[pid].get("file_path", f"posts/{pid}.md")
            if local.exists():
                local.unlink()

    if not to_update:
        log.info("Blog: no changes")
        state["blog"] = current
        _save_state(state)
        return 0, total_deleted

    log.info("Blog: %d posts to update", len(to_update))

    # Remove stale chunks for updated posts before re-upsert
    update_paths = [f"posts/{pid}.md" for pid, _u, _l in to_update]
    total_deleted += _delete_chunks_for_files(collection, BLOG_SOURCE, update_paths)

    chunks: list[dict] = []
    for pid, url, _lastmod in to_update:
        try:
            resp = _http().get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning("Blog fetch failed %s: %s", url, e)
            time.sleep(REQUEST_DELAY)
            continue

        title, date, body = _parse_blog_page(url, resp.text)
        if not body:
            log.warning("Blog: empty body for %s", url)
            time.sleep(REQUEST_DELAY)
            continue

        text_md = f"# {title}\n\n"
        if date:
            text_md += f"*Published: {date}*\n\n"
        text_md += body

        file_path = f"posts/{pid}.md"
        _write_local(REPOS_DIR / BLOG_SOURCE / file_path, text_md)

        post_chunks = chunk_markdown(text_md, BLOG_SOURCE, file_path)
        for c in post_chunks:
            c["page_url"] = url
        chunks.extend(post_chunks)
        time.sleep(REQUEST_DELAY)

    added = _upsert_chunks(collection, chunks, label=BLOG_SOURCE) if chunks else 0
    state["blog"] = current
    _save_state(state)
    return added, total_deleted


# ---------------------------------------------------------------------------
# Forum (Discourse)
# ---------------------------------------------------------------------------

_TOPIC_URL_RE = re.compile(r"/t/[^/]+/(\d+)/?$")


def _discourse_categories() -> dict[int, str]:
    """Return {category_id: name}. Best-effort; empty dict on failure."""
    try:
        data = _get_json(f"{FORUM_BASE}/site.json")
    except requests.RequestException as e:
        log.warning("Could not fetch Discourse site.json: %s", e)
        return {}
    cats = {}
    if isinstance(data, dict):
        for c in data.get("categories", []) or []:
            cats[int(c["id"])] = c.get("name", "")
    return cats


def _discourse_sitemap_topics() -> list[tuple[int, str, str]]:
    """Return [(topic_id, url, lastmod)] from the Discourse sitemap index."""
    try:
        index_xml = _get_xml(f"{FORUM_BASE}/sitemap.xml")
    except requests.RequestException as e:
        log.warning("Sitemap index fetch failed: %s", e)
        return []

    idx_soup = BeautifulSoup(index_xml, "xml")
    sub_urls = [loc.text for loc in idx_soup.find_all("loc")]

    topics: dict[int, tuple[str, str]] = {}
    for sm_url in sub_urls:
        time.sleep(REQUEST_DELAY)
        try:
            xml = _get_xml(sm_url)
        except requests.RequestException as e:
            log.warning("Sub-sitemap fetch failed %s: %s", sm_url, e)
            continue
        sm_soup = BeautifulSoup(xml, "xml")
        for url_tag in sm_soup.find_all("url"):
            loc_tag = url_tag.find("loc")
            if not loc_tag or not loc_tag.text:
                continue
            loc = loc_tag.text.strip()
            m = _TOPIC_URL_RE.search(loc)
            if not m:
                continue
            tid = int(m.group(1))
            lastmod_tag = url_tag.find("lastmod")
            lastmod = lastmod_tag.text.strip() if lastmod_tag and lastmod_tag.text else ""
            # Prefer the entry with the latest lastmod if dupes appear
            if tid not in topics or lastmod > topics[tid][1]:
                topics[tid] = (loc, lastmod)

    return [(tid, url, lm) for tid, (url, lm) in topics.items()]


def _fetch_topic(topic_id: int) -> dict | None:
    """Fetch a topic and all its posts. Returns None if inaccessible."""
    try:
        data = _get_json(f"{FORUM_BASE}/t/{topic_id}.json", params={"print": "true"})
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", None)
        if status in (403, 404, 410):
            return None
        raise
    return data if isinstance(data, dict) else None


def _build_topic_chunks(topic: dict, categories: dict[int, str]) -> tuple[list[dict], list[str], list[tuple[str, str]]]:
    """Return (chunks, file_paths, local_files[(path, content)]) for a topic."""
    tid = topic.get("id")
    if not tid:
        return [], [], []
    title = (topic.get("title") or "").strip() or f"Topic {tid}"
    slug = topic.get("slug") or "topic"
    cat_id = topic.get("category_id")
    category_name = categories.get(int(cat_id), "") if cat_id else ""
    posts = topic.get("post_stream", {}).get("posts", []) or []

    chunks: list[dict] = []
    file_paths: list[str] = []
    local_files: list[tuple[str, str]] = []

    for post in posts:
        post_num = post.get("post_number")
        if not post_num:
            continue
        cooked = post.get("cooked") or ""
        body = _strip_html(cooked)
        if not body:
            continue
        username = post.get("username") or "anonymous"
        created = (post.get("created_at") or "")[:10]
        accepted = post.get("accepted_answer", False)

        header = f"# {title}\n\n"
        meta_line = f"*Post #{post_num} by @{username}"
        if created:
            meta_line += f" on {created}"
        if accepted:
            meta_line += " (accepted answer)"
        meta_line += "*\n\n"

        text_md = header + meta_line + body
        file_path = f"t/{tid}/{post_num}.md"
        page_url = f"{FORUM_BASE}/t/{slug}/{tid}/{post_num}"

        local_files.append((file_path, text_md))
        file_paths.append(file_path)

        hierarchy = [category_name, title, f"Post #{post_num}"] if category_name else [title, f"Post #{post_num}"]

        chunks.append({
            "text": text_md[:4000],
            "source_repo": FORUM_SOURCE,
            "file_path": file_path,
            "heading_hierarchy": hierarchy,
            "page_url": page_url,
        })

    return chunks, file_paths, local_files


def ingest_forum(collection, state: dict) -> tuple[int, int]:
    """Ingest forum topics. Returns (chunks_added, chunks_deleted)."""
    forum_state: dict = state.setdefault("forum", {})
    categories = _discourse_categories()
    state["forum_categories"] = {str(k): v for k, v in categories.items()}

    log.info("Fetching forum sitemap from %s ...", FORUM_BASE)
    topics = _discourse_sitemap_topics()
    log.info("Forum: %d topics in sitemap", len(topics))

    if not topics:
        return 0, 0

    current_seen: set[str] = set()
    to_update: list[tuple[int, str]] = []
    for tid, _url, lastmod in topics:
        key = str(tid)
        current_seen.add(key)
        if forum_state.get(key, {}).get("bumped_at") != lastmod:
            to_update.append((tid, lastmod))

    # Remove topics that vanished from the sitemap
    deleted_keys = [k for k in forum_state if k not in current_seen]
    total_deleted = 0
    for k in deleted_keys:
        old_paths = forum_state[k].get("file_paths", [])
        if old_paths:
            total_deleted += _delete_chunks_for_files(collection, FORUM_SOURCE, old_paths)
        for fp in old_paths:
            local = REPOS_DIR / FORUM_SOURCE / fp
            if local.exists():
                local.unlink()
        forum_state.pop(k, None)

    if not to_update:
        log.info("Forum: no changes")
        _save_state(state)
        return 0, total_deleted

    log.info("Forum: %d topics to update (batch_size=%d, delay=%.2fs)",
             len(to_update), FORUM_UPSERT_BATCH, REQUEST_DELAY)

    total_added = 0
    pending: list[dict] = []  # chunks accumulated across topics
    t_start = time.time()
    t_window = t_start
    win_topics = 0
    win_fetch_ms = 0.0
    win_build_ms = 0.0

    def _flush() -> int:
        nonlocal pending
        if not pending:
            return 0
        t0 = time.time()
        n = _upsert_chunks(collection, pending, label=f"{FORUM_SOURCE} batch")
        log.info("Forum: flushed %d chunks in %.1fs", n, time.time() - t0)
        pending = []
        return n

    for i, (tid, lastmod) in enumerate(to_update, 1):
        key = str(tid)
        tf0 = time.time()
        try:
            topic = _fetch_topic(tid)
        except requests.RequestException as e:
            log.warning("Topic %d fetch failed: %s", tid, e)
            time.sleep(REQUEST_DELAY)
            continue
        fetch_ms = (time.time() - tf0) * 1000

        if topic is None:
            old_paths = forum_state.get(key, {}).get("file_paths", [])
            if old_paths:
                total_deleted += _delete_chunks_for_files(collection, FORUM_SOURCE, old_paths)
                for fp in old_paths:
                    local = REPOS_DIR / FORUM_SOURCE / fp
                    if local.exists():
                        local.unlink()
            forum_state.pop(key, None)
            time.sleep(REQUEST_DELAY)
            continue

        tb0 = time.time()
        chunks, file_paths, local_files = _build_topic_chunks(topic, categories)
        build_ms = (time.time() - tb0) * 1000

        old_paths = forum_state.get(key, {}).get("file_paths", [])
        if old_paths:
            total_deleted += _delete_chunks_for_files(collection, FORUM_SOURCE, old_paths)
            stale = set(old_paths) - set(file_paths)
            for fp in stale:
                local = REPOS_DIR / FORUM_SOURCE / fp
                if local.exists():
                    local.unlink()

        for fp, content in local_files:
            _write_local(REPOS_DIR / FORUM_SOURCE / fp, content)

        pending.extend(chunks)
        forum_state[key] = {"bumped_at": lastmod, "file_paths": file_paths}

        win_topics += 1
        win_fetch_ms += fetch_ms
        win_build_ms += build_ms

        # Flush when batch is full
        if len(pending) >= FORUM_UPSERT_BATCH:
            total_added += _flush()
            _save_state(state)

        # Periodic progress log + rate window
        if i % 100 == 0:
            now = time.time()
            window_elapsed = now - t_window
            overall = now - t_start
            rate = win_topics / window_elapsed if window_elapsed > 0 else 0
            eta_min = (len(to_update) - i) / rate / 60 if rate > 0 else -1
            log.info(
                "Forum %d/%d (%.1f t/s window, overall %.2fs/topic) "
                "avg fetch=%.0fms build=%.0fms pending=%d ETA=%.0fmin",
                i, len(to_update), rate, overall / i,
                win_fetch_ms / win_topics, win_build_ms / win_topics,
                len(pending), eta_min,
            )
            t_window = now
            win_topics = 0
            win_fetch_ms = 0.0
            win_build_ms = 0.0
            _save_state(state)

        time.sleep(REQUEST_DELAY)

    total_added += _flush()
    _save_state(state)
    log.info("Forum: done in %.1fs, %d chunks added", time.time() - t_start, total_added)
    return total_added, total_deleted


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def ingest_community(collection) -> dict:
    """Run blog + forum ingestion based on env flags. Returns stats dict."""
    blog_enabled = os.getenv("INGEST_BLOG", "true").lower() in ("1", "true", "yes")
    forum_enabled = os.getenv("INGEST_FORUM", "true").lower() in ("1", "true", "yes")

    state = _load_state()
    stats = {"blog_added": 0, "blog_deleted": 0, "forum_added": 0, "forum_deleted": 0}

    if blog_enabled:
        try:
            added, deleted = ingest_blog(collection, state)
            stats["blog_added"] = added
            stats["blog_deleted"] = deleted
        except Exception:
            log.exception("Blog ingestion failed")
    else:
        log.info("Blog ingestion disabled (INGEST_BLOG=false)")

    if forum_enabled:
        try:
            added, deleted = ingest_forum(collection, state)
            stats["forum_added"] = added
            stats["forum_deleted"] = deleted
        except Exception:
            log.exception("Forum ingestion failed")
    else:
        log.info("Forum ingestion disabled (INGEST_FORUM=false)")

    _save_state(state)
    return stats
