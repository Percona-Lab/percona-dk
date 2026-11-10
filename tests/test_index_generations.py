"""
Tests for index generations: ingest builds a new index beside the live one
and swaps it in only on success, and the servers switch to it in-process.

Runs offline: a tmp DATA_DIR, a fixture doc repo standing in for the git
clone, community ingestion off, and a deterministic hashing embedder.

Run: python -m pytest tests/test_index_generations.py -v
"""

import hashlib
import json
import re
import threading
import time

import chromadb
import pytest
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from fastapi.testclient import TestClient

from percona_dk import index_store, ingest, server
from percona_dk.index_store import LiveIndex

COLLECTION = "percona_docs"
REPO = ("percona/test-docs", "8.4")


class HashEmbedding(EmbeddingFunction):
    """Bag-of-words hashed into 64 dims: deterministic and offline."""

    def __init__(self) -> None:
        pass

    def __call__(self, input: Documents) -> Embeddings:
        out = []
        for text in input:
            vec = [0.0] * 64
            for word in re.findall(r"\w+", text.lower()):
                vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % 64] += 1.0
            out.append(vec)
        return out

    @staticmethod
    def name() -> str:
        return "test-hash"

    def get_config(self) -> dict:
        return {}

    @staticmethod
    def build_from_config(config: dict) -> "HashEmbedding":
        return HashEmbedding()


def _open_collection(chroma_dir, create=False):
    client = chromadb.PersistentClient(path=str(chroma_dir))
    if create:
        return client, client.get_or_create_collection(
            COLLECTION, metadata={"hnsw:space": "cosine"}, embedding_function=HashEmbedding()
        )
    return client, client.get_collection(COLLECTION, embedding_function=HashEmbedding())


def _write_page(repo_dir, n, body):
    f = repo_dir / "docs" / f"page{n}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"# Page {n}\n\n## Section {n}\n\n{body}\n", encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A tmp DATA_DIR wired into ingest, plus a fixture doc repo."""
    data = tmp_path / "data"
    data.mkdir()
    repo_dir = tmp_path / "src-repo"
    for n in range(10):
        _write_page(repo_dir, n, f"original text for page {n} about buffer pools")

    monkeypatch.setattr(ingest, "DATA_DIR", data)
    monkeypatch.setattr(ingest, "REPOS_DIR", data / "repos")
    monkeypatch.setattr(ingest, "_open_collection", _open_collection)
    monkeypatch.setattr(ingest, "clone_or_pull", lambda slug, branch=None: repo_dir)
    monkeypatch.setenv("INGEST_BLOG", "false")
    monkeypatch.setenv("INGEST_FORUM", "false")
    # Tests publish generations seconds apart; delete retired ones at once
    # unless a test opts back into the delay.
    monkeypatch.setenv("INDEX_RETIRE_SECONDS", "0")

    class Env:
        pass

    e = Env()
    e.data, e.repo = data, repo_dir
    e.run = lambda: ingest.ingest(repos=[REPO])
    return e


def _live_texts(data) -> list[str]:
    gen = index_store.current_generation(data)
    client, col = _open_collection(gen.chroma_dir)
    try:
        return sorted(col.get(include=["documents"])["documents"])
    finally:
        index_store.close_client(client)


def _gen_names(data) -> list[str]:
    d = index_store.generations_dir(data)
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def _live_index(data, **kw) -> LiveIndex:
    kw.setdefault("check_seconds", 0)
    return LiveIndex(data, COLLECTION, embedding_function=HashEmbedding(), **kw)


# ---------------------------------------------------------------------------
# Swap
# ---------------------------------------------------------------------------

def test_first_ingest_publishes_generation_with_meta(env):
    stats = env.run()

    ptr = index_store.read_pointer(env.data)
    assert ptr["current"] == stats["generation"]
    assert ptr["previous"] is None

    gen = index_store.current_generation(env.data)
    meta = index_store.load_meta(gen)
    assert meta["chunk_count"] == stats["collection_count"] > 0
    assert meta["repo_versions"] == {"percona/test-docs": ["8.4"]}
    assert meta["chunks_per_repo"] == {"percona/test-docs": meta["chunk_count"]}
    # Change-detection state travels with the index it describes.
    assert gen.state_file(index_store.FILES_STATE_NAME).is_file()
    assert not (env.data / index_store.FILES_STATE_NAME).exists()
    assert (env.data / ".last_ingest").is_file()


def test_reingest_swaps_and_keeps_one_previous(env):
    first = env.run()["generation"]

    _write_page(env.repo, 3, "rewritten text for page three about redo logs")
    second = env.run()
    assert second["generation"] != first
    assert second["previous_generation"] == first
    assert second["chunks_added"] > 0  # incremental: only the changed file
    ptr = index_store.read_pointer(env.data)
    assert (ptr["current"], ptr["previous"]) == (second["generation"], first)
    texts = _live_texts(env.data)
    assert any("redo logs" in t for t in texts)
    assert not any("original text for page 3 " in t for t in texts)
    assert _gen_names(env.data) == sorted([first, second["generation"]])

    third = env.run()
    assert third["removed_generations"] == [first]
    assert _gen_names(env.data) == sorted([second["generation"], third["generation"]])


def test_unchanged_reingest_copies_index_forward(env):
    first = env.run()
    second = env.run()
    assert second["chunks_added"] == 0
    assert second["collection_count"] == first["collection_count"]


# ---------------------------------------------------------------------------
# Failed ingest
# ---------------------------------------------------------------------------

def test_failed_ingest_leaves_live_index_untouched(env, monkeypatch):
    first = env.run()["generation"]
    before = _live_texts(env.data)
    pointer_before = index_store.read_pointer(env.data)

    # Fail after the stale chunks were already deleted from the staged copy.
    _write_page(env.repo, 3, "rewritten text that never lands")

    def boom(*a, **kw):
        raise RuntimeError("embedding failed")

    real_upsert = ingest._upsert_chunks
    monkeypatch.setattr(ingest, "_upsert_chunks", boom)
    with pytest.raises(RuntimeError, match="embedding failed"):
        env.run()

    assert index_store.read_pointer(env.data) == pointer_before
    assert _live_texts(env.data) == before
    assert _gen_names(env.data) == [first]  # the half-built one was discarded

    # The next good run still picks up the change: the live generation's
    # change-detection state was never advanced.
    monkeypatch.setattr(ingest, "_upsert_chunks", real_upsert)
    env.run()
    assert any("never lands" in t for t in _live_texts(env.data))


def test_interrupted_ingest_leftovers_are_removed(env, monkeypatch):
    monkeypatch.setenv("INDEX_RETIRE_SECONDS", "300")  # leftovers were never served
    first = env.run()["generation"]
    leftover = index_store.generations_dir(env.data) / "20000101T000000Z-dead00"
    (leftover / "chroma").mkdir(parents=True)
    second = env.run()
    assert leftover.name not in _gen_names(env.data)
    assert _gen_names(env.data) == sorted([first, second["generation"]])


def test_sharp_drop_in_chunks_is_not_published(env):
    env.run()
    pointer_before = index_store.read_pointer(env.data)
    for n in range(2, 10):
        (env.repo / "docs" / f"page{n}.md").unlink()
    with pytest.raises(RuntimeError, match="refusing to publish"):
        env.run()
    assert index_store.read_pointer(env.data) == pointer_before


def test_concurrent_ingest_is_refused(env):
    with index_store.ingest_lock(env.data):
        with pytest.raises(index_store.IngestBusy):
            env.run()


# ---------------------------------------------------------------------------
# Legacy layout, rollback, cleanup
# ---------------------------------------------------------------------------

def test_migrates_legacy_index_then_removes_it(env):
    # A pre-generation install: DATA_DIR/chroma + DATA_DIR/.last_ingest_files.json
    legacy = index_store.generation(env.data, index_store.LEGACY)
    built = ingest._build_generation(legacy, [REPO])
    legacy.meta_path.unlink()  # legacy installs have no index_meta.json
    assert legacy.chroma_dir == env.data / "chroma"
    assert index_store.read_pointer(env.data) is None
    assert index_store.current_generation(env.data) == legacy

    first = env.run()
    assert first["previous_generation"] == index_store.LEGACY
    assert first["chunks_added"] == 0  # seeded from the legacy state: incremental
    assert first["collection_count"] == built["collection_count"]
    assert legacy.chroma_dir.is_dir()  # kept while it's the rollback target

    env.run()
    assert not legacy.chroma_dir.exists()
    assert not legacy.state_file(index_store.FILES_STATE_NAME).exists()


def test_live_index_serves_legacy_layout(env):
    legacy = index_store.generation(env.data, index_store.LEGACY)
    ingest._build_generation(legacy, [REPO])
    legacy.meta_path.unlink()
    idx = _live_index(env.data)
    assert idx.collection().count() > 0
    assert idx.generation == index_store.LEGACY
    assert idx.repo_versions() == {"percona/test-docs": ["8.4"]}  # scanned once

    published = env.run()["generation"]
    assert idx.check_for_update(wait=True)
    assert idx.generation == published
    idx.stop()


def test_recently_retired_generation_is_kept_until_safe(env, monkeypatch):
    # A server that hasn't noticed a switch yet may still be serving the
    # generation that just dropped out (rollback, then a quick ingest).
    monkeypatch.setenv("INDEX_RETIRE_SECONDS", "300")
    first = env.run()["generation"]
    second = env.run()["generation"]
    index_store.rollback(env.data)              # servers head back to `first`
    third = env.run()                            # `second` drops out right away
    assert third["previous_generation"] == first
    assert third["removed_generations"] == []
    assert second in _gen_names(env.data)
    assert second in index_store.read_pointer(env.data)["retiring"]

    monkeypatch.setenv("INDEX_RETIRE_SECONDS", "0")
    fourth = env.run()
    assert _gen_names(env.data) == sorted([third["generation"], fourth["generation"]])


def test_cleanup_does_nothing_before_first_publish(tmp_path):
    (tmp_path / "chroma").mkdir()
    assert index_store.cleanup_generations(tmp_path) == []
    assert (tmp_path / "chroma").is_dir()


def test_rollback_and_roll_forward(env):
    first = env.run()["generation"]
    second = env.run()["generation"]

    assert index_store.rollback(env.data).name == first
    assert index_store.read_pointer(env.data)["previous"] == second
    assert index_store.rollback(env.data).name == second


def test_rollback_without_previous_fails(env):
    env.run()
    with pytest.raises(RuntimeError, match="No previous"):
        index_store.rollback(env.data)


def test_copy_of_open_index_is_complete(env):
    env.run()
    gen = index_store.current_generation(env.data)
    reader, col = _open_collection(gen.chroma_dir)  # a server holding it open
    try:
        col.query(query_texts=["buffer pools"], n_results=1)
        dst = env.data / "copy"
        index_store.copy_chroma_dir(gen.chroma_dir, dst)
        client, copied = _open_collection(dst)
        assert copied.count() == col.count()
        assert copied.query(query_texts=["page 7"], n_results=1)["ids"][0]
        index_store.close_client(client)
    finally:
        index_store.close_client(reader)


# ---------------------------------------------------------------------------
# In-process reload
# ---------------------------------------------------------------------------

def test_live_index_switches_without_restart(env, monkeypatch):
    first = env.run()["generation"]

    # Servers read repo versions from index_meta.json, never scan metadata.
    real_compute = index_store.compute_meta

    def no_scan(*a, **kw):
        raise AssertionError("servers should not scan metadata when index_meta.json exists")

    monkeypatch.setattr(index_store, "compute_meta", no_scan)
    idx = _live_index(env.data, retire_grace_seconds=0)
    assert idx.generation is None
    old_col = idx.collection()
    assert idx.generation == first
    assert idx.repo_versions() == {"percona/test-docs": ["8.4"]}

    monkeypatch.setattr(index_store, "compute_meta", real_compute)
    _write_page(env.repo, 4, "kittens and zebras")
    second = env.run()["generation"]
    monkeypatch.setattr(index_store, "compute_meta", no_scan)

    assert idx.check_for_update(wait=True) is True
    assert idx.generation == second
    new_col = idx.collection()
    assert new_col is not old_col
    hit = new_col.query(query_texts=["kittens zebras"], n_results=1)
    assert "kittens" in hit["documents"][0][0]
    assert idx.check_for_update(wait=True) is False  # nothing new
    idx.stop()


def test_live_index_ignores_unopenable_generation(env, caplog):
    first = env.run()["generation"]
    idx = _live_index(env.data)
    col = idx.collection()
    index_store._write_pointer(env.data, "20990101T000000Z-gone00", first)
    assert idx.check_for_update(wait=True) is False
    assert idx.generation == first
    assert idx.collection() is col
    assert "still serving" in caplog.text
    idx.stop()


def test_queries_keep_succeeding_during_ingest_and_swap(env):
    first = env.run()["generation"]
    idx = _live_index(env.data, check_seconds=0.05, retire_grace_seconds=1.0)
    idx.collection()
    idx.start_watcher()

    errors: list[BaseException] = []
    seen: set[str] = set()
    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            try:
                res = idx.collection().query(query_texts=["buffer pools"], n_results=3)
                assert res["documents"][0], "empty result"
                seen.add(idx.generation)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        generations = [first]
        for n in range(3):
            _write_page(env.repo, n, f"revision {n} of the buffer pools page")
            generations.append(env.run()["generation"])
        deadline = time.monotonic() + 10
        while idx.generation != generations[-1] and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        for t in threads:
            t.join()
        idx.stop()

    assert not errors, errors[:3]
    assert idx.generation == generations[-1]
    assert first in seen and generations[-1] in seen


def test_rest_api_follows_new_generation(env, monkeypatch):
    first = env.run()["generation"]
    idx = _live_index(env.data, retire_grace_seconds=0)
    monkeypatch.setattr(server, "_index", idx)
    client = TestClient(server.app)

    resp = client.get("/health")
    assert resp.json()["index_generation"] == first
    assert "kittens" not in client.get("/search", params={"q": "kittens zebras"}).text

    _write_page(env.repo, 5, "kittens and zebras")
    second = env.run()["generation"]
    idx.check_for_update(wait=True)

    assert client.get("/health").json()["index_generation"] == second
    assert "kittens" in client.get("/search", params={"q": "kittens zebras", "top_k": 1}).text
    stats = client.get("/stats").json()
    assert stats["chunks_per_repo"] == {"percona/test-docs": stats["total_chunks"]}
    idx.stop()


def test_live_index_checks_pointer_on_request(env):
    env.run()
    idx = _live_index(env.data, check_seconds=0.01, retire_grace_seconds=0)
    idx.collection()
    _write_page(env.repo, 6, "kittens and zebras")
    second = env.run()["generation"]
    time.sleep(0.02)
    idx.collection()  # notices CURRENT changed; switches off the request path
    deadline = time.monotonic() + 10
    while idx.generation != second and time.monotonic() < deadline:
        time.sleep(0.02)
    assert idx.generation == second
    idx.stop()


def test_pointer_is_replaced_atomically(tmp_path):
    index_store._write_pointer(tmp_path, "a", None)
    index_store._write_pointer(tmp_path, "b", "a")
    assert json.loads(index_store.pointer_path(tmp_path).read_text())["current"] == "b"
    leftovers = [p.name for p in index_store.index_root(tmp_path).iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
