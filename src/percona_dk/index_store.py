"""
Index generations: build a new ChromaDB index beside the live one, then
switch to it atomically, with no server restart.

Chroma isn't designed for one process writing files that other processes
are reading, so ingest never writes to the index the servers are serving.
Layout under DATA_DIR:

    index/
      CURRENT                  pointer file (JSON): current + previous generation,
                               and recently retired ones not yet safe to delete
      .ingest.lock             held by the running ingest
      generations/
        <generation>/
          chroma/              the chromadb persist directory
          index_meta.json      chunk count + repo->versions map (written by ingest)
          .last_ingest_files.json       change-detection state that matches
          .last_ingest_community.json   this generation's index
    chroma/                    legacy (pre-generation) index; served until the
                               first generation is published, removed once it
                               is no longer the previous generation

Ingest copies the current generation into a new one, updates it
incrementally, writes index_meta.json, and only then rewrites CURRENT (a
write to a temp file followed by os.replace, so readers see the old or the
new pointer, never a partial one). A failed ingest leaves CURRENT alone and
its half-built generation is deleted. The previous generation is kept for
`percona-dk-ingest --rollback`; older ones are removed once they have been
out of service for INDEX_RETIRE_SECONDS (default 5 minutes).

The servers use LiveIndex: it opens the generation CURRENT names (by its
real path, so it stays put while the next one is built), checks CURRENT
every INDEX_CHECK_SECONDS, and when it changes opens and warms the new
generation in the background before switching to it. Requests keep using
the old collection until the switch, so none fail during it.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    import chromadb

log = logging.getLogger(__name__)

INDEX_DIRNAME = "index"
GENERATIONS_DIRNAME = "generations"
POINTER_NAME = "CURRENT"
LOCK_NAME = ".ingest.lock"
CHROMA_SUBDIR = "chroma"
META_NAME = "index_meta.json"
LEGACY = "legacy"

# Change-detection state that has to travel with the index it describes.
# If it lived outside the generation, a run that failed after saving it
# would make the next run skip files the live index never received.
FILES_STATE_NAME = ".last_ingest_files.json"
COMMUNITY_STATE_NAME = ".last_ingest_community.json"
STATE_FILES = (FILES_STATE_NAME, COMMUNITY_STATE_NAME)

DEFAULT_CHECK_SECONDS = 30.0
# How long an old generation's client stays open after a switch, so requests
# that started on it can finish.
RETIRE_GRACE_SECONDS = 60.0
# After a generation fails to open, wait this long before trying it again.
FAILED_OPEN_BACKOFF_SECONDS = 300.0
# A generation that drops out of current/previous is deleted only once it
# has been out of service this long, so a server that hasn't noticed a
# recent switch yet never has its index deleted underneath it. Must exceed
# the servers' INDEX_CHECK_SECONDS plus the time to open an index.
DEFAULT_RETIRE_SECONDS = 300.0


class IngestBusy(RuntimeError):
    """Another ingest holds the lock for this DATA_DIR."""


# ---------------------------------------------------------------------------
# Paths and the CURRENT pointer
# ---------------------------------------------------------------------------

def index_root(data_dir: Path) -> Path:
    return Path(data_dir) / INDEX_DIRNAME


def generations_dir(data_dir: Path) -> Path:
    return index_root(data_dir) / GENERATIONS_DIRNAME


def pointer_path(data_dir: Path) -> Path:
    return index_root(data_dir) / POINTER_NAME


@dataclass(frozen=True)
class Generation:
    """One built index. `path` holds chroma/, index_meta.json and state files.

    The legacy generation is DATA_DIR itself, so its chroma dir and state
    files are the pre-generation ones (DATA_DIR/chroma, DATA_DIR/.last_*).
    """

    name: str
    path: Path

    @property
    def chroma_dir(self) -> Path:
        return self.path / CHROMA_SUBDIR

    @property
    def meta_path(self) -> Path:
        return self.path / META_NAME

    def state_file(self, name: str) -> Path:
        return self.path / name


def generation(data_dir: Path, name: str) -> Generation:
    if name == LEGACY:
        return Generation(LEGACY, Path(data_dir))
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"Invalid generation name {name!r}")
    return Generation(name, generations_dir(data_dir) / name)


def read_pointer(data_dir: Path) -> dict | None:
    """Return the CURRENT pointer as a dict, or None if there isn't one."""
    try:
        data = json.loads(pointer_path(data_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("Unreadable index pointer %s", pointer_path(data_dir), exc_info=True)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("current"), str):
        log.warning("Index pointer %s has no 'current' entry", pointer_path(data_dir))
        return None
    return data


def current_generation(data_dir: Path) -> Generation | None:
    """The generation servers should serve, or None if nothing is indexed.

    Falls back to the legacy DATA_DIR/chroma until a generation is published.
    """
    ptr = read_pointer(data_dir)
    if ptr is not None:
        try:
            return generation(data_dir, ptr["current"])
        except ValueError:
            log.warning("Index pointer names an invalid generation: %r", ptr["current"])
            return None
    legacy = generation(data_dir, LEGACY)
    if legacy.chroma_dir.is_dir():
        return legacy
    return None


def current_chroma_dir(data_dir: Path) -> Path:
    """Chroma persist dir of the current generation (for scripts and checks)."""
    gen = current_generation(data_dir)
    return gen.chroma_dir if gen else Path(data_dir) / CHROMA_SUBDIR


def _write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON so readers see either the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    if hasattr(os, "O_DIRECTORY"):  # make the rename durable (POSIX only)
        try:
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_pointer(
    data_dir: Path, current: str, previous: str | None, retiring: dict[str, str] | None = None
) -> None:
    """Point servers at `current`.

    `retiring` maps generations that just dropped out of service to the
    time they stopped being current; cleanup waits before deleting them.
    """
    retiring = {
        name: since for name, since in (retiring or {}).items()
        if name not in (current, previous) and generation(data_dir, name).chroma_dir.exists()
    }
    data = {"current": current, "previous": previous, "switched_at": _now_iso()}
    if retiring:
        data["retiring"] = retiring
    _write_json_atomic(pointer_path(data_dir), data)


# ---------------------------------------------------------------------------
# Ingest side: lock, stage, publish, roll back, clean up
# ---------------------------------------------------------------------------

@contextmanager
def ingest_lock(data_dir: Path) -> Iterator[None]:
    """Hold an exclusive lock so two ingests never build or clean up at once.

    Uses flock where available (Linux, macOS). On Windows, where only a
    single local user runs ingest, it is a no-op.
    """
    path = index_root(data_dir) / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        try:
            import fcntl
        except ImportError:
            yield
            return
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IngestBusy(f"Another ingest is running (lock held on {path})") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def new_generation_name(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def copy_chroma_dir(src: Path, dst: Path) -> None:
    """Copy a chroma persist dir that other processes may have open.

    Servers only read the HNSW segment files, but opening a client writes
    a row to chroma.sqlite3, so the database is copied with SQLite's online
    backup API (a consistent snapshot) instead of a plain file copy. Its
    -wal/-shm/-journal side files are not copied: the backup is complete.
    """
    dst.mkdir(parents=True, exist_ok=False)
    for entry in src.iterdir():
        if entry.name.startswith("chroma.sqlite3"):
            continue
        if entry.is_dir():
            shutil.copytree(entry, dst / entry.name)
        else:
            shutil.copy2(entry, dst / entry.name)
    db = src / "chroma.sqlite3"
    if db.exists():
        with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)) as source, \
                closing(sqlite3.connect(str(dst / "chroma.sqlite3"))) as target:
            source.backup(target)


def stage_generation(data_dir: Path, name: str | None = None) -> Generation:
    """Create a new, unpublished generation seeded from the current one.

    The chroma dir and the change-detection state are copied, so ingest can
    update the copy incrementally. With no current index, the new
    generation starts empty and ingest does a full build.
    """
    gen = generation(data_dir, name or new_generation_name())
    gen.path.mkdir(parents=True, exist_ok=False)
    cur = current_generation(data_dir)
    if cur is not None and cur.chroma_dir.is_dir():
        t0 = time.monotonic()
        copy_chroma_dir(cur.chroma_dir, gen.chroma_dir)
        log.info("Staged generation %s from %s in %.1fs", gen.name, cur.name, time.monotonic() - t0)
        for state_name in STATE_FILES:
            src = cur.state_file(state_name)
            if src.is_file():
                shutil.copy2(src, gen.state_file(state_name))
    else:
        log.info("Staged generation %s (empty: no current index)", gen.name)
    return gen


def discard_generation(gen: Generation) -> None:
    """Delete an unpublished generation (after a failed ingest)."""
    if gen.name == LEGACY:
        raise ValueError("Refusing to discard the legacy index")
    shutil.rmtree(gen.path, ignore_errors=True)


def publish_generation(data_dir: Path, gen: Generation) -> str | None:
    """Make `gen` current. Returns the name of the generation it replaced.

    The replaced generation becomes `previous` (kept for rollback).
    """
    if not gen.chroma_dir.is_dir():
        raise FileNotFoundError(f"Generation {gen.name} has no chroma dir")
    ptr = read_pointer(data_dir) or {}
    cur = current_generation(data_dir)
    previous = cur.name if cur is not None and cur.name != gen.name else None
    retiring = dict(ptr.get("retiring") or {})
    if ptr.get("previous"):
        # The old previous drops out now. It stopped being current when the
        # old pointer was written.
        retiring[ptr["previous"]] = ptr.get("switched_at") or _now_iso()
    _write_pointer(data_dir, gen.name, previous, retiring)
    log.info("Published index generation %s (previous: %s)", gen.name, previous)
    return previous


def rollback(data_dir: Path) -> Generation:
    """Switch CURRENT back to the previous generation.

    The current one becomes `previous`, so running it again rolls forward.
    """
    ptr = read_pointer(data_dir)
    if ptr is None or not ptr.get("previous"):
        raise RuntimeError("No previous index generation to roll back to")
    prev = generation(data_dir, ptr["previous"])
    if not prev.chroma_dir.is_dir():
        raise RuntimeError(f"Previous generation {prev.name} is missing ({prev.chroma_dir})")
    _write_pointer(data_dir, prev.name, ptr["current"], ptr.get("retiring"))
    log.info("Rolled back index generation %s -> %s", ptr["current"], prev.name)
    return prev


def cleanup_generations(data_dir: Path, retire_seconds: float | None = None) -> list[str]:
    """Remove generations other than current and previous. Returns their names.

    A generation that dropped out of service less than `retire_seconds` ago
    (default INDEX_RETIRE_SECONDS, 300) is kept until a later run. Leftovers
    of failed runs were never served and go at once. Does nothing until a
    generation has been published, so a legacy-only DATA_DIR is never
    touched. Call with the ingest lock held, so an in-progress build isn't
    removed.
    """
    ptr = read_pointer(data_dir)
    if ptr is None:
        return []
    if retire_seconds is None:
        retire_seconds = float(os.getenv("INDEX_RETIRE_SECONDS", DEFAULT_RETIRE_SECONDS))
    keep = {ptr["current"], ptr.get("previous")}
    now = datetime.now(timezone.utc)
    for name, since in (ptr.get("retiring") or {}).items():
        try:
            age = (now - datetime.fromisoformat(since)).total_seconds()
        except (TypeError, ValueError):
            age = 0.0
        if age < retire_seconds:
            keep.add(name)
    removed: list[str] = []

    gens = generations_dir(data_dir)
    if gens.is_dir():
        for d in sorted(gens.iterdir()):
            if d.name in keep or not d.is_dir():
                continue
            try:
                shutil.rmtree(d)
                removed.append(d.name)
            except OSError:
                # Windows can't delete files a process still has open; the
                # next run tries again.
                log.warning("Could not remove old index generation %s", d, exc_info=True)

    if LEGACY not in keep:
        legacy = generation(data_dir, LEGACY)
        if legacy.chroma_dir.is_symlink():
            legacy.chroma_dir.unlink()
            removed.append(LEGACY)
        elif legacy.chroma_dir.is_dir():
            try:
                shutil.rmtree(legacy.chroma_dir)
                for state_name in STATE_FILES:
                    legacy.state_file(state_name).unlink(missing_ok=True)
                removed.append(LEGACY)
            except OSError:
                log.warning("Could not remove legacy index %s", legacy.chroma_dir, exc_info=True)

    if removed:
        log.info("Removed old index generations: %s", ", ".join(removed))
    return removed


# ---------------------------------------------------------------------------
# index_meta.json: what servers would otherwise scan 87k chunks to learn
# ---------------------------------------------------------------------------

def compute_meta(collection: chromadb.Collection, batch: int = 5000) -> dict:
    """Scan chunk metadata for the repo->versions map and per-repo counts."""
    versions: dict[str, set[str]] = {}
    per_repo: Counter = Counter()
    offset = 0
    while True:
        res = collection.get(include=["metadatas"], limit=batch, offset=offset)
        metas = res.get("metadatas") or []
        if not metas:
            break
        for m in metas:
            repo = m.get("source_repo", "unknown")
            per_repo[repo] += 1
            v = (m.get("version") or "").strip()
            if v:
                versions.setdefault(repo, set()).add(v)
        offset += batch
    return {
        "chunk_count": collection.count(),
        "repo_versions": {r: sorted(vs) for r, vs in sorted(versions.items())},
        "chunks_per_repo": dict(sorted(per_repo.items())),
    }


def write_meta(gen: Generation, meta: dict) -> dict:
    full = {
        **meta,
        "generation": gen.name,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _write_json_atomic(gen.meta_path, full)
    return full


def load_meta(gen: Generation) -> dict | None:
    try:
        data = json.loads(gen.meta_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("Unreadable index meta %s", gen.meta_path, exc_info=True)
        return None
    if not isinstance(data.get("repo_versions"), dict):
        return None
    return data


# ---------------------------------------------------------------------------
# Server side: LiveIndex
# ---------------------------------------------------------------------------

def close_client(client: Any) -> None:
    close = getattr(client, "close", None)  # older chromadb releases have none
    if close is None:
        return
    try:
        close()
    except Exception:
        log.debug("Error closing chroma client", exc_info=True)


@dataclass
class _Loaded:
    generation: str | None
    collection: Any
    client: Any = None
    meta: dict | None = None
    meta_lock: threading.Lock = field(default_factory=threading.Lock)


class LiveIndex:
    """The collection a server answers from, switched in-process on reindex.

    `collection()` returns the open collection; the first call opens it. A
    watcher thread (`start_watcher()`), and calls to `collection()` at most
    every `check_seconds`, read CURRENT; when it names another generation,
    that generation is opened and warmed off the request path, then swapped
    in with one assignment. The old client is closed after a grace period.
    """

    def __init__(
        self,
        data_dir: Path,
        collection_name: str,
        check_seconds: float | None = None,
        embedding_function: Any = None,
        retire_grace_seconds: float = RETIRE_GRACE_SECONDS,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.collection_name = collection_name
        if check_seconds is None:
            check_seconds = float(os.getenv("INDEX_CHECK_SECONDS", DEFAULT_CHECK_SECONDS))
        self.check_seconds = check_seconds
        self.embedding_function = embedding_function
        self.retire_grace_seconds = retire_grace_seconds
        self._state: _Loaded | None = None
        self._switch_lock = threading.Lock()
        self._last_check = time.monotonic()
        self._failed: tuple[str, float] | None = None
        self._retired: list[tuple[float, Any]] = []
        self._retired_lock = threading.Lock()
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()
        self._pinned = False

    @classmethod
    def pinned(cls, collection: Any, meta: dict | None = None) -> "LiveIndex":
        """Serve one already-open collection and never switch (tests, scripts)."""
        idx = cls(Path("."), getattr(collection, "name", ""), check_seconds=0)
        idx._state = _Loaded(generation=None, collection=collection, meta=meta)
        idx._pinned = True
        return idx

    # -- reads ---------------------------------------------------------------

    def collection(self) -> chromadb.Collection:
        st = self._state
        if st is None:
            st = self._load_initial()
        elif self.check_seconds > 0 and not self._pinned:
            now = time.monotonic()
            if now - self._last_check >= self.check_seconds:
                self._last_check = now
                self.check_for_update(wait=False)
        return st.collection

    @property
    def generation(self) -> str | None:
        st = self._state
        return st.generation if st else None

    def meta(self) -> dict:
        """index_meta.json of the served generation, or computed on first use."""
        if self._state is None:
            self.collection()
        return self._ensure_meta(self._state)

    def repo_versions(self) -> dict[str, list[str]]:
        return self.meta().get("repo_versions") or {}

    # -- switching -----------------------------------------------------------

    def _load_initial(self) -> _Loaded:
        with self._switch_lock:
            if self._state is not None:
                return self._state
            target = current_generation(self.data_dir)
            if target is None:
                raise RuntimeError(
                    f"No index found under {self.data_dir}; run percona-dk-ingest first."
                )
            self._state = self._open(target)
            self._last_check = time.monotonic()
            log.info("Serving index generation %s", target.name)
            return self._state

    def check_for_update(self, wait: bool = True) -> bool:
        """Switch to the generation CURRENT names, if it differs.

        With wait=False the open happens on a background thread and this
        returns at once. Returns True if a switch happened (wait=True) or
        was started (wait=False).
        """
        if self._pinned:
            return False
        self._reap()
        target = current_generation(self.data_dir)
        st = self._state
        if target is None or st is None or target.name == st.generation:
            return False
        if self._failed and self._failed[0] == target.name \
                and time.monotonic() - self._failed[1] < FAILED_OPEN_BACKOFF_SECONDS:
            return False
        if wait:
            return self._switch(target, blocking=True)
        threading.Thread(
            target=self._switch, args=(target, False), name="index-switch", daemon=True
        ).start()
        return True

    def _switch(self, target: Generation, blocking: bool) -> bool:
        if not self._switch_lock.acquire(blocking=blocking):
            return False  # another switch is in progress
        try:
            old = self._state
            if old is not None and old.generation == target.name:
                return False
            t0 = time.monotonic()
            try:
                new = self._open(target)
            except Exception:
                self._failed = (target.name, time.monotonic())
                log.warning(
                    "Could not open index generation %s; still serving %s",
                    target.name, old.generation if old else None, exc_info=True,
                )
                return False
            self._state = new
            self._failed = None
            log.info(
                "Switched index generation %s -> %s (%s chunks, opened in %.1fs)",
                old.generation if old else None, target.name,
                (new.meta or {}).get("chunk_count", "?"), time.monotonic() - t0,
            )
            if old is not None and old.client is not None:
                with self._retired_lock:
                    self._retired.append((time.monotonic() + self.retire_grace_seconds, old.client))
            self._reap()
            return True
        finally:
            self._switch_lock.release()

    def _open(self, target: Generation) -> _Loaded:
        """Open and warm a generation. Raises if it can't serve queries."""
        import chromadb

        chroma_dir = target.chroma_dir
        if not chroma_dir.is_dir():
            # PersistentClient would silently create an empty index here.
            raise FileNotFoundError(f"Index generation {target.name} is missing ({chroma_dir})")
        client = chromadb.PersistentClient(path=str(chroma_dir.resolve()))
        try:
            kwargs = {}
            if self.embedding_function is not None:
                kwargs["embedding_function"] = self.embedding_function
            col = client.get_collection(self.collection_name, **kwargs)
            # Load the HNSW index and embedding model before taking traffic.
            col.query(query_texts=["warmup"], n_results=1)
            meta = load_meta(target)
            loaded = _Loaded(generation=target.name, collection=col, client=client, meta=meta)
            if meta is None:
                # Legacy index or one built before index_meta.json: scan once.
                self._ensure_meta(loaded)
            return loaded
        except Exception:
            close_client(client)
            raise

    @staticmethod
    def _ensure_meta(st: _Loaded) -> dict:
        with st.meta_lock:
            if st.meta is None:
                try:
                    st.meta = compute_meta(st.collection)
                except Exception:
                    log.warning("Failed to compute repo->versions map", exc_info=True)
                    return {"repo_versions": {}}
        return st.meta

    def _reap(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._retired_lock:
            due = [c for t, c in self._retired if force or t <= now]
            self._retired = [(t, c) for t, c in self._retired if not (force or t <= now)]
        for client in due:
            close_client(client)

    # -- watcher -------------------------------------------------------------

    def start_watcher(self) -> None:
        """Check CURRENT every check_seconds on a daemon thread."""
        if self._pinned or self.check_seconds <= 0 or self._watcher is not None:
            return
        self._watcher = threading.Thread(target=self._watch, name="index-watcher", daemon=True)
        self._watcher.start()

    def _watch(self) -> None:
        while not self._stop.wait(self.check_seconds):
            try:
                if self._state is None:
                    if current_generation(self.data_dir) is not None:
                        self._load_initial()
                else:
                    self.check_for_update(wait=True)
                self._reap()
            except Exception:
                log.warning("Index watcher check failed", exc_info=True)

    def stop(self) -> None:
        self._stop.set()
        if self._watcher is not None:
            self._watcher.join(timeout=5)
            self._watcher = None
        self._reap(force=True)
