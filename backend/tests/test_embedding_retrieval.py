from __future__ import annotations

from typing import Any

import numpy as np

from scripts.evaluate_retrieval_ab import comparison
from seeit.embedding_retrieval import (
    DenseEmbeddingRetriever,
    HybridRrfRetriever,
    dense_profile,
)


class FakeEmbeddingBackend:
    vectors = {
        "alpha lexical": [0.0, 1.0],
        "semantic target": [1.0, 0.0],
        "other": [0.6, 0.8],
        "find target": [1.0, 0.0],
        "alpha": [1.0, 0.0],
        "no lexical overlap": [1.0, 0.0],
    }

    def encode(self, texts, *, query: bool = False) -> np.ndarray:
        return np.asarray([self.vectors[str(item)] for item in texts], dtype=np.float32)


SEGMENTS: list[dict[str, Any]] = [
    {"segmentId": "s1", "source": "ASR", "startMs": 0, "endMs": 1000, "content": "alpha lexical"},
    {"segmentId": "s2", "source": "OCR", "startMs": 2000, "endMs": 3000, "content": "semantic target"},
    {"segmentId": "s3", "source": "ASR", "startMs": 4000, "endMs": 5000, "content": "other"},
]


def test_dense_embedding_retriever_ranks_cosine_and_filters_sources() -> None:
    retriever = DenseEmbeddingRetriever(SEGMENTS, FakeEmbeddingBackend())

    result = retriever.search("find target", top_k=3)
    filtered = retriever.search("find target", top_k=3, sources=["ASR"])

    assert result["retrievalMode"] == "LOCAL_DENSE_EMBEDDING"
    assert [item["segmentId"] for item in result["matches"]] == ["s2", "s3", "s1"]
    assert result["matches"][0]["score"] == 1.0
    assert [item["segmentId"] for item in filtered["matches"]] == ["s3", "s1"]


def test_hybrid_rrf_exposes_component_ranks_without_mixing_score_scales() -> None:
    retriever = HybridRrfRetriever(SEGMENTS, FakeEmbeddingBackend(), candidate_depth=8)

    result = retriever.search("alpha", top_k=3)
    by_id = {item["segmentId"]: item for item in result["matches"]}

    assert result["retrievalMode"] == "HYBRID_LEXICAL_DENSE_RRF"
    assert by_id["s1"]["scoreDetails"]["lexicalRank"] == 1
    assert by_id["s2"]["scoreDetails"]["denseRank"] == 1
    assert by_id["s1"]["scoreDetails"]["denseScore"] == 0.0
    assert by_id["s2"]["scoreDetails"]["lexicalRank"] is None


def test_hybrid_does_not_treat_lexical_timeline_fallback_as_positive_ranking() -> None:
    retriever = HybridRrfRetriever(SEGMENTS, FakeEmbeddingBackend(), candidate_depth=8)

    result = retriever.search("no lexical overlap", top_k=3)

    assert all(item["scoreDetails"]["lexicalRank"] is None for item in result["matches"])


def test_embedding_profile_metadata_and_ab_delta_are_reproducible() -> None:
    profile = dense_profile(FakeEmbeddingBackend(), {"embeddingModel": "fake", "modelRevision": "v1"})
    assert profile.metadata()["embeddingModel"] == "fake"

    def result(recall: float, outcome: str) -> dict[str, Any]:
        return {
            "aggregate": {
                "mrr": recall,
                "recallAt1": recall,
                "recallAt3": recall,
                "recallAt8": recall,
                "completeHitRateAt8": recall,
                "windowRecallAt8": recall,
                "windowCompleteHitRateAt8": recall,
                "unanswerablePositiveRetrievalRate": 1.0,
                "searchLatency": {"meanMs": 1.0},
            },
            "indexBuildLatency": {"totalMs": 1.0},
            "cases": [{"caseId": "case-1", "outcome": outcome, "recallAt8": recall, "windowRecallAt8": recall}],
        }

    compared = comparison({
        "lexical": result(0.5, "RETRIEVAL_MISS"),
        "dense": result(0.75, "HIT"),
        "hybrid": result(1.0, "HIT"),
    })

    assert compared["deltaVsLexical"]["dense"]["recallAt8"] == 0.25
    assert compared["deltaVsLexical"]["hybrid"]["completeHitRateAt8"] == 0.5
    assert compared["cases"][0]["hybrid"]["outcome"] == "HIT"
