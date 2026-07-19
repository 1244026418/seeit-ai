"""Evidence retrieval primitives and deterministic evaluation metrics."""

from __future__ import annotations

import re
from typing import Any


class EvidenceRetriever:
    """Hybrid lexical baseline for ASR/OCR timeline evidence.

    The score combines exact phrase matching, ASCII terms, and Chinese
    character bigrams. Keeping this behind a small interface makes it possible
    to add embedding/Qdrant candidates later without changing Agent tools.
    """

    def __init__(self, segments: list[dict[str, Any]]) -> None:
        self.segments = [dict(item) for item in segments]

    @staticmethod
    def terms(value: str) -> set[str]:
        normalized = re.sub(r"\s+", "", value.lower())
        ascii_words = set(re.findall(r"[a-z0-9_]{2,}", normalized))
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
        chinese_pairs = {
            chinese[index : index + 2]
            for index in range(max(0, len(chinese) - 1))
        }
        return ascii_words | chinese_pairs

    @classmethod
    def score(cls, query: str, content: str) -> dict[str, float]:
        compact_query = re.sub(r"\s+", "", query.lower())
        compact_content = re.sub(r"\s+", "", content.lower())
        if not compact_query or not compact_content:
            return {"score": 0.0, "termCoverage": 0.0, "exactBonus": 0.0}
        terms = cls.terms(query)
        matched = sum(1 for term in terms if term in compact_content)
        term_coverage = matched / max(1, len(terms))
        exact_bonus = 1.0 if compact_query in compact_content else 0.0
        return {
            "score": round(exact_bonus + term_coverage, 4),
            "termCoverage": round(term_coverage, 4),
            "exactBonus": exact_bonus,
        }

    @classmethod
    def relevance(cls, query: str, content: str) -> float:
        return cls.score(query, content)["score"]

    def search(
        self,
        query: str,
        *,
        top_k: int = 8,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_query = re.sub(r"\s+", " ", str(query)).strip()
        if not normalized_query:
            raise ValueError("检索词不能为空")
        top_k = max(1, min(int(top_k), 20))
        allowed_sources = {str(item).upper() for item in sources or []}
        candidates = [
            item
            for item in self.segments
            if not allowed_sources or str(item.get("source", "")).upper() in allowed_sources
        ]
        ranked = []
        for item in candidates:
            details = self.score(normalized_query, str(item.get("content", "")))
            ranked.append({
                **item,
                "score": details["score"],
                "scoreDetails": {
                    "termCoverage": details["termCoverage"],
                    "exactBonus": details["exactBonus"],
                },
            })
        ranked.sort(
            key=lambda item: (
                -float(item["score"]),
                int(item.get("startMs", 0)),
                str(item.get("segmentId", "")),
            )
        )
        positive = [item for item in ranked if item["score"] > 0]
        fallback = not positive
        matches = (positive or ranked)[:top_k]
        return {
            "ok": True,
            "query": normalized_query,
            "retrievalMode": "HYBRID_LEXICAL_BASELINE",
            "matches": matches,
            "matchedCount": len(positive),
            "fallbackToTimelineStart": fallback,
        }


def evaluate_retrieval_cases(
    segments: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    *,
    top_k: int = 5,
) -> dict[str, Any]:
    retriever = EvidenceRetriever(segments)
    details = []
    recall_total = 0.0
    reciprocal_rank_total = 0.0
    hit_count = 0

    for case in cases:
        expected = {str(item) for item in case.get("expectedSegmentIds", [])}
        result = retriever.search(
            str(case.get("query", "")),
            top_k=top_k,
            sources=case.get("sources"),
        )
        retrieved = [str(item.get("segmentId")) for item in result["matches"]]
        hits = expected.intersection(retrieved)
        recall = len(hits) / max(1, len(expected))
        reciprocal_rank = 0.0
        for index, segment_id in enumerate(retrieved, start=1):
            if segment_id in expected:
                reciprocal_rank = 1.0 / index
                break
        hit = bool(hits)
        recall_total += recall
        reciprocal_rank_total += reciprocal_rank
        hit_count += int(hit)
        details.append({
            "caseId": case.get("id"),
            "query": case.get("query"),
            "expectedSegmentIds": sorted(expected),
            "retrievedSegmentIds": retrieved,
            "recallAtK": round(recall, 4),
            "reciprocalRank": round(reciprocal_rank, 4),
            "hit": hit,
        })

    case_count = len(cases)
    return {
        "scope": "SYNTHETIC_EVIDENCE_BASELINE",
        "retrievalMode": "HYBRID_LEXICAL_BASELINE",
        "caseCount": case_count,
        "topK": top_k,
        "recallAtK": round(recall_total / max(1, case_count), 4),
        "mrr": round(reciprocal_rank_total / max(1, case_count), 4),
        "hitRate": round(hit_count / max(1, case_count), 4),
        "cases": details,
    }
