from __future__ import annotations

from typing import Any

import numpy as np
from qdrant_client import QdrantClient

from scripts.evaluate_retrieval_ab import evaluate_qdrant_profile
from seeit.qdrant_retrieval import (
    QdrantDenseRetriever,
    QdrantEvidenceStore,
    QdrantHybridRetriever,
)
from seeit import runtime_retrieval


class FakeEmbeddingBackend:
    vectors = {
        "alpha": [1.0, 0.0],
        "beta": [0.0, 1.0],
        "gamma": [0.7, 0.7],
        "find alpha": [1.0, 0.0],
    }

    def __init__(self) -> None:
        self.document_encode_count = 0

    def encode(self, texts, *, query: bool = False) -> np.ndarray:
        if not query:
            self.document_encode_count += 1
        matrix = np.asarray([self.vectors[str(item)] for item in texts], dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix / np.maximum(norms, 1e-12)


def segment(segment_id: str, source: str, start_ms: int, content: str) -> dict[str, Any]:
    return {
        "segmentId": segment_id,
        "source": source,
        "startMs": start_ms,
        "endMs": start_ms + 1000,
        "content": content,
    }


SEGMENTS = [
    segment("s1", "ASR", 0, "alpha"),
    segment("s2", "OCR", 2000, "beta"),
    segment("s3", "ASR", 4000, "gamma"),
]


def memory_store(name: str) -> QdrantEvidenceStore:
    return QdrantEvidenceStore(QdrantClient(":memory:"), collection_name=name)


def test_qdrant_upsert_search_payload_and_source_filter() -> None:
    backend = FakeEmbeddingBackend()
    store = memory_store("payload_filter")
    retriever = QdrantDenseRetriever("video-1", SEGMENTS, backend, store)

    result = retriever.search("find alpha", top_k=3)
    filtered = retriever.search("find alpha", top_k=3, sources=["OCR"])

    assert result["retrievalMode"] == "QDRANT_DENSE_EMBEDDING"
    assert [item["segmentId"] for item in result["matches"]] == ["s1", "s3", "s2"]
    assert result["matches"][0]["content"] == "alpha"
    assert [item["segmentId"] for item in filtered["matches"]] == ["s2"]
    assert backend.document_encode_count == 1


def test_snapshot_reuse_skips_embedding_and_changed_snapshot_rebuilds() -> None:
    backend = FakeEmbeddingBackend()
    store = memory_store("snapshot_lifecycle")

    first = QdrantDenseRetriever("video-1", SEGMENTS, backend, store)
    second = QdrantDenseRetriever("video-1", SEGMENTS, backend, store)
    changed = [*SEGMENTS[:2], segment("s3", "ASR", 4000, "alpha")]
    third = QdrantDenseRetriever("video-1", changed, backend, store)

    assert first.index_state["reused"] is False
    assert second.index_state["reused"] is True
    assert third.index_state["reused"] is False
    assert backend.document_encode_count == 2
    assert store.client.count("snapshot_lifecycle").count == 3


def test_delete_is_scoped_to_one_video() -> None:
    backend = FakeEmbeddingBackend()
    store = memory_store("delete_scope")
    QdrantDenseRetriever("video-1", SEGMENTS, backend, store)
    QdrantDenseRetriever("video-2", SEGMENTS[:1], backend, store)

    store.delete_video("video-1")

    assert store.client.count("delete_scope").count == 1
    assert store.search("video-2", np.asarray([1.0, 0.0]), top_k=3)[0]["segmentId"] == "s1"


def test_qdrant_hybrid_exposes_rrf_component_scores() -> None:
    backend = FakeEmbeddingBackend()
    retriever = QdrantHybridRetriever("video-1", SEGMENTS, backend, memory_store("hybrid"))

    result = retriever.search("alpha", top_k=3)
    by_id = {item["segmentId"]: item for item in result["matches"]}

    assert result["retrievalMode"] == "QDRANT_HYBRID_LEXICAL_DENSE_RRF"
    assert by_id["s1"]["scoreDetails"]["lexicalRank"] == 1
    assert by_id["s1"]["scoreDetails"]["denseRank"] == 1


def test_runtime_profile_uses_qdrant_and_falls_back_when_initialization_fails(
    monkeypatch,
) -> None:
    backend = FakeEmbeddingBackend()
    store = memory_store("runtime_profile")
    monkeypatch.setenv("EVIDENCE_RETRIEVER_PROFILE", "contextual-qdrant-hybrid-v1")
    monkeypatch.setattr(runtime_retrieval, "_embedding", lambda: backend)
    monkeypatch.setattr(runtime_retrieval, "_store", lambda: store)

    retriever = runtime_retrieval.build_runtime_retriever({"mediaId": 7}, SEGMENTS)
    result = retriever.search("alpha", top_k=3)

    assert result["retrievalMode"] == "CONTEXTUAL_QDRANT_HYBRID_LEXICAL_DENSE_RRF"

    def broken_store():
        raise ConnectionError("qdrant unavailable")

    monkeypatch.setattr(runtime_retrieval, "_store", broken_store)
    fallback = runtime_retrieval.build_runtime_retriever({"mediaId": 8}, SEGMENTS)
    fallback_result = fallback.search("alpha", top_k=3)

    assert fallback_result["retrievalMode"] == "CONTEXTUAL_HYBRID_LEXICAL_BASELINE"


def test_runtime_coverage_profile_uses_qdrant_and_coverage_fallback(monkeypatch) -> None:
    backend = FakeEmbeddingBackend()
    store = memory_store("runtime_coverage_profile")
    monkeypatch.setenv("EVIDENCE_RETRIEVER_PROFILE", "coverage-aware-qdrant-hybrid-v2")
    monkeypatch.setattr(runtime_retrieval, "_embedding", lambda: backend)
    monkeypatch.setattr(runtime_retrieval, "_store", lambda: store)

    retriever = runtime_retrieval.build_runtime_retriever({"mediaId": 17}, SEGMENTS)
    result = retriever.search("alpha", top_k=3)

    assert result["retrievalMode"] == (
        "COVERAGE_AWARE_CONTEXTUAL_QDRANT_HYBRID_LEXICAL_DENSE_RRF"
    )
    assert result["evidenceSufficiency"]["fullyCovered"] is True

    monkeypatch.setattr(
        runtime_retrieval,
        "_store",
        lambda: (_ for _ in ()).throw(ConnectionError("qdrant unavailable")),
    )
    fallback = runtime_retrieval.build_runtime_retriever({"mediaId": 18}, SEGMENTS)
    fallback_result = fallback.search("alpha", top_k=3)

    assert fallback_result["retrievalMode"] == (
        "COVERAGE_AWARE_CONTEXTUAL_HYBRID_LEXICAL_BASELINE"
    )


def test_qdrant_profile_uses_offline_metrics_contract() -> None:
    backend = FakeEmbeddingBackend()
    snapshots = {"video-1": SEGMENTS}
    dataset = {
        "topK": [1, 3],
        "cases": [{
            "caseId": "case-1",
            "videoId": "video-1",
            "type": "direct_fact",
            "answerable": True,
            "question": "alpha",
            "goldEvidence": [{
                "evidenceId": "gold-1",
                "startMs": 0,
                "endMs": 1000,
                "sources": ["ASR"],
            }],
        }],
    }

    result = evaluate_qdrant_profile(dataset, snapshots, backend, memory_store("profile_eval"))

    assert result["profile"]["profileId"] == "contextual-qdrant-hybrid-v1"
    assert result["aggregate"]["recallAt1"] == 1.0
    assert result["cases"][0]["indexReused"] is False

    coverage = evaluate_qdrant_profile(
        dataset,
        snapshots,
        backend,
        memory_store("coverage_profile_eval"),
        coverage_aware=True,
    )

    assert coverage["profile"]["profileId"] == "coverage-aware-qdrant-hybrid-v2"
    assert coverage["aggregate"]["requirementCoverage"]["fullyCoveredCaseRate"] == 1.0
