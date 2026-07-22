from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.evaluate_real_evidence import (
    aggregate,
    evaluate_case,
    evaluate_profile,
    load_snapshots,
)
from seeit.retrieval import (
    LEXICAL_PROFILE,
    EvidenceRetriever,
    RetrieverProfile,
    get_retriever_profile,
)


def segment(segment_id: str, source: str, start_ms: int) -> dict[str, Any]:
    return {
        "segmentId": segment_id,
        "source": source,
        "startMs": start_ms,
        "endMs": start_ms + 1000,
        "content": segment_id,
        "score": 1.0,
    }


SEGMENTS = [
    segment("asr-1", "ASR", 0),
    segment("ocr-1", "OCR", 1000),
    segment("asr-2", "ASR", 100000),
    segment("asr-3", "ASR", 200000),
    segment("ocr-2", "OCR", 300000),
    segment("noise-1", "ASR", 400000),
    segment("noise-2", "OCR", 500000),
]


RANKINGS = {
    "cross": ["asr-1", "noise-1", "ocr-1", "noise-2"],
    "asr-multi": ["noise-1", "asr-2", "noise-2", "asr-3"],
    "ocr": ["noise-1", "noise-2", "ocr-2"],
    "unanswerable-positive": ["noise-1"],
    "unanswerable-empty": ["noise-2"],
}


CASES = [
    {
        "caseId": "cross",
        "videoId": "video-1",
        "type": "asr_ocr",
        "answerable": True,
        "question": "cross",
        "goldEvidence": [{
            "evidenceId": "gold-cross",
            "startMs": 0,
            "endMs": 2000,
            "sources": ["ASR", "OCR"],
        }],
    },
    {
        "caseId": "asr-multi",
        "videoId": "video-1",
        "type": "multi_evidence",
        "answerable": True,
        "question": "asr-multi",
        "goldEvidence": [
            {"evidenceId": "gold-asr-2", "startMs": 100000, "endMs": 101000, "sources": ["ASR"]},
            {"evidenceId": "gold-asr-3", "startMs": 200000, "endMs": 201000, "sources": ["ASR"]},
        ],
    },
    {
        "caseId": "ocr",
        "videoId": "video-1",
        "type": "ocr",
        "answerable": True,
        "question": "ocr",
        "goldEvidence": [{
            "evidenceId": "gold-ocr",
            "startMs": 300000,
            "endMs": 301000,
            "sources": ["OCR"],
        }],
    },
    {
        "caseId": "unanswerable-positive",
        "videoId": "video-1",
        "type": "unanswerable",
        "answerable": False,
        "question": "unanswerable-positive",
        "goldEvidence": [],
    },
    {
        "caseId": "unanswerable-empty",
        "videoId": "video-1",
        "type": "unanswerable",
        "answerable": False,
        "question": "unanswerable-empty",
        "goldEvidence": [],
    },
]


class FixedRetriever:
    def __init__(self, segments: list[dict[str, Any]]) -> None:
        self.by_id = {str(item["segmentId"]): dict(item) for item in segments}

    def search(
        self,
        query: str,
        *,
        top_k: int = 8,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        matches = [self.by_id[segment_id] for segment_id in RANKINGS[query]][:top_k]
        positive = query != "unanswerable-empty"
        return {
            "ok": True,
            "retrievalMode": "FIXED_OFFLINE_TEST",
            "matches": matches,
            "matchedCount": len(matches) if positive else 0,
            "fallbackToTimelineStart": not positive,
            "abstention": {
                "abstained": query == "unanswerable-empty",
            },
        }


FIXED_PROFILE = RetrieverProfile(
    profile_id="fixed-test",
    retrieval_mode="FIXED_OFFLINE_TEST",
    factory=FixedRetriever,
    description="仅用于离线指标单元测试",
)


def test_offline_metrics_cover_recall_mrr_sources_abstention_and_latency() -> None:
    retriever = FixedRetriever(SEGMENTS)
    details = [
        evaluate_case(case, SEGMENTS, [1, 3, 8], retriever=retriever, profile=FIXED_PROFILE)
        for case in CASES
    ]
    for detail, latency in zip(details, [1, 2, 3, 4, 10], strict=True):
        detail["searchLatencyMs"] = latency
    details[0]["evidenceSufficiency"] = {
        "requirementCount": 2,
        "satisfiedRequirementCount": 2,
        "fullyCovered": True,
    }
    details[1]["evidenceSufficiency"] = {
        "requirementCount": 2,
        "satisfiedRequirementCount": 1,
        "fullyCovered": False,
    }

    summary = aggregate(details, [1, 3, 8])

    assert [item["outcome"] for item in details] == [
        "HIT",
        "HIT",
        "HIT",
        "UNANSWERABLE",
        "UNANSWERABLE",
    ]
    assert summary["recallAt1"] == 0.1667
    assert summary["recallAt3"] == 0.8333
    assert summary["recallAt8"] == 1.0
    assert summary["mrr"] == 0.6111
    assert summary["completeHitRateAt1"] == 0.0
    assert summary["completeHitRateAt3"] == 0.6667
    assert summary["completeHitRateAt8"] == 1.0
    assert summary["unanswerablePositiveRetrievalRate"] == 0.5
    assert summary["abstention"] == {
        "predictedCount": 1,
        "truePositiveCount": 1,
        "falsePositiveCount": 0,
        "precision": 1.0,
        "recall": 0.5,
        "f1": 0.6667,
        "answerableFalseAbstentionRate": 0.0,
    }
    assert summary["requirementCoverage"] == {
        "caseCount": 2,
        "requirementCount": 4,
        "satisfiedRequirementCount": 3,
        "satisfiedRate": 0.75,
        "fullyCoveredCaseRate": 0.5,
    }

    assert summary["bySource"]["ASR"]["caseCount"] == 2
    assert summary["bySource"]["ASR"]["recallAt1"] == 0.5
    assert summary["bySource"]["ASR"]["recallAt3"] == 0.75
    assert summary["bySource"]["ASR"]["mrr"] == 0.75
    assert summary["bySource"]["OCR"]["caseCount"] == 2
    assert summary["bySource"]["OCR"]["recallAt1"] == 0.0
    assert summary["bySource"]["OCR"]["recallAt3"] == 1.0
    assert summary["bySource"]["OCR"]["mrr"] == 0.3333

    assert summary["searchLatency"] == {
        "sampleCount": 5,
        "totalMs": 20.0,
        "meanMs": 4.0,
        "p50Ms": 3.0,
        "p95Ms": 10.0,
        "maxMs": 10.0,
    }

    missing_evidence_case = {
        **CASES[2],
        "caseId": "missing-ocr",
        "goldEvidence": [{
            "evidenceId": "missing-ocr-e1",
            "startMs": 700000,
            "endMs": 701000,
            "sources": ["OCR"],
        }],
    }
    missing_detail = evaluate_case(
        missing_evidence_case,
        SEGMENTS,
        [1, 3, 8],
        retriever=retriever,
        profile=FIXED_PROFILE,
    )
    assert missing_detail["outcome"] == "EVIDENCE_EXTRACTION_MISSING"


def test_profile_interface_keeps_lexical_v1_behavior_and_reuses_one_index() -> None:
    direct = EvidenceRetriever(SEGMENTS).search("asr-1", top_k=3)
    profiled = get_retriever_profile("lexical").create(SEGMENTS).search("asr-1", top_k=3)
    assert profiled == direct
    assert LEXICAL_PROFILE.profile_id == "lexical-v1"

    build_count = 0

    def factory(segments: list[dict[str, Any]]) -> FixedRetriever:
        nonlocal build_count
        build_count += 1
        return FixedRetriever(segments)

    profile = RetrieverProfile("fixed-counted", "FIXED_OFFLINE_TEST", factory, "测试复用")
    output = evaluate_profile(
        {"topK": [1, 3, 8], "cases": CASES},
        {"video-1": SEGMENTS},
        profile,
    )

    assert build_count == 1
    assert output["profile"]["profileId"] == "fixed-counted"
    assert output["aggregate"]["recallAt8"] == 1.0


def test_offline_snapshot_loader_accepts_plain_and_prior_run_shapes(tmp_path: Path) -> None:
    plain_path = tmp_path / "plain.json"
    prior_path = tmp_path / "prior.json"
    plain_path.write_text(json.dumps({"video-1": SEGMENTS}), encoding="utf-8-sig")
    prior_path.write_text(json.dumps({"segmentSnapshots": {"video-1": SEGMENTS}}), encoding="utf-8")

    assert load_snapshots(plain_path) == {"video-1": SEGMENTS}
    assert load_snapshots(prior_path) == {"video-1": SEGMENTS}
