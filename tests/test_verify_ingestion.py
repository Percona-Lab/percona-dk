"""
Verification script for Phase 1 — confirms chunks are in ChromaDB
and semantic search returns relevant results.

Run: python -m pytest tests/test_verify_ingestion.py -v
  or: python tests/test_verify_ingestion.py  (standalone)
"""

import os
import sys
from pathlib import Path

import chromadb
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
CHROMA_DIR = DATA_DIR / "chroma"
COLLECTION_NAME = "percona_docs"


def get_collection() -> chromadb.Collection:
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return client.get_collection(COLLECTION_NAME)


def test_collection_has_documents():
    """ChromaDB collection exists and contains chunks."""
    coll = get_collection()
    count = coll.count()
    print(f"  Collection has {count} documents")
    assert count > 0, "Collection is empty — ingestion may not have run"


def test_chunks_have_metadata():
    """Stored chunks have the expected metadata fields."""
    coll = get_collection()
    result = coll.peek(limit=5)
    for meta in result["metadatas"]:
        assert "source_repo" in meta, f"Missing source_repo in {meta}"
        assert "file_path" in meta, f"Missing file_path in {meta}"
        assert "page_url" in meta, f"Missing page_url in {meta}"
        assert "heading_hierarchy" in meta, f"Missing heading_hierarchy in {meta}"
    print(f"  Metadata check passed for {len(result['metadatas'])} sample chunks")


def test_semantic_search():
    """Semantic search returns relevant results for a Percona-related query."""
    coll = get_collection()

    query = "How to configure innodb_buffer_pool_size in Percona Server"
    results = coll.query(query_texts=[query], n_results=5)

    assert results["documents"] and len(results["documents"][0]) > 0, "Search returned no results"
    print(f"\n  Query: {query}")
    print(f"  Top {len(results['documents'][0])} results:")
    for i, (doc, meta, dist) in enumerate(
        zip(results["documents"][0], results["metadatas"][0], results["distances"][0])
    ):
        print(f"    {i+1}. [{dist:.4f}] {meta['file_path']} — {meta['heading_hierarchy'][:60]}")
        print(f"       {doc[:120]}...")


if __name__ == "__main__":
    print("=== Percona-DK Ingestion Verification ===\n")
    tests = [test_collection_has_documents, test_chunks_have_metadata, test_semantic_search]
    passed = 0
    for test_fn in tests:
        try:
            print(f"▸ {test_fn.__doc__.strip()}")
            test_fn()
            print("  ✓ PASSED\n")
            passed += 1
        except Exception as e:
            print(f"  ✗ FAILED: {e}\n")

    print(f"Results: {passed}/{len(tests)} passed")
    sys.exit(0 if passed == len(tests) else 1)
