#!/usr/bin/env python3
"""Recompute and fix stored ``page_url`` metadata in the ChromaDB index.

``page_url`` is baked into each chunk at ingest time, so when the
URL-construction logic changes (a slug fix, the ``.html`` change, a new repo
mapping), the *existing* index keeps serving the old URLs. Incremental
re-ingestion does NOT fix this: it only re-embeds files whose CONTENT changed,
and a URL-logic change leaves content untouched.

This script recomputes ``page_url`` for every non-community doc chunk using the
current ``ingest._build_page_url`` and updates the chunks whose URL changed, in
place, with no re-embedding (fast, no network). Blog/forum chunks are left
untouched -- their URLs come from a different code path.

Usage (on the box that hosts the index, e.g. sherpa):

    cd ~/percona-dk && git pull && .venv/bin/pip install .
    DOTENV_PATH=~/percona-dk/.env .venv/bin/python scripts/fix_page_urls.py --dry-run
    DOTENV_PATH=~/percona-dk/.env .venv/bin/python scripts/fix_page_urls.py

The MCP/REST servers read page_url from the index at query time, so no restart
is required for the corrected URLs to take effect.
"""
import argparse
import os
from collections import Counter

from dotenv import load_dotenv

load_dotenv(os.getenv("DOTENV_PATH"))

import chromadb  # noqa: E402

from percona_dk.ingest import (  # noqa: E402
    CHROMA_DIR,
    COLLECTION_NAME,
    _build_page_url,
)

# Community sources build their page_url elsewhere (community.py); never touch.
COMMUNITY = {"percona-community-blog", "percona-forums"}


def main() -> None:
    ap = argparse.ArgumentParser(description="Fix stored page_url metadata.")
    ap.add_argument(
        "--dry-run", action="store_true",
        help="report what would change without writing",
    )
    args = ap.parse_args()

    col = chromadb.PersistentClient(path=str(CHROMA_DIR)).get_collection(COLLECTION_NAME)
    total = col.count()
    print(f"collection {COLLECTION_NAME!r}: {total} chunks")

    changed_ids: list[str] = []
    changed_metas: list[dict] = []
    per_repo: Counter = Counter()
    samples: dict[str, tuple[str, str]] = {}

    offset, batch = 0, 5000
    while offset < total:
        res = col.get(include=["metadatas"], limit=batch, offset=offset)
        for cid, m in zip(res["ids"], res["metadatas"]):
            repo = m.get("source_repo", "")
            if repo in COMMUNITY:
                continue
            new_url = _build_page_url(repo, m.get("file_path", ""), m.get("version") or None)
            if new_url != m.get("page_url"):
                meta = dict(m)
                meta["page_url"] = new_url
                changed_ids.append(cid)
                changed_metas.append(meta)
                per_repo[repo] += 1
                samples.setdefault(repo, (m.get("page_url"), new_url))
        offset += batch

    print(f"chunks needing a page_url fix: {len(changed_ids)}\n")
    for repo, n in sorted(per_repo.items(), key=lambda kv: -kv[1]):
        old, new = samples[repo]
        print(f"  {n:6d}  {repo}")
        print(f"            {old}")
        print(f"         -> {new}")

    if args.dry_run:
        print("\n--dry-run: no changes written.")
        return

    for i in range(0, len(changed_ids), 1000):
        col.update(ids=changed_ids[i:i + 1000], metadatas=changed_metas[i:i + 1000])
    print(f"\nupdated {len(changed_ids)} chunk metadatas.")


if __name__ == "__main__":
    main()
