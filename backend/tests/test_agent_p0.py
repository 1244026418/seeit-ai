from __future__ import annotations

import json
from pathlib import Path

from seeit.retrieval import EvidenceRetriever, evaluate_retrieval_cases


EVAL_DATASET = Path(__file__).resolve().parents[1] / "evals" / "evidence_rag_eval.json"


def test_evidence_retriever_exposes_mode_and_score_breakdown() -> None:
    retriever = EvidenceRetriever([
        {
            "segmentId": "index-segment",
            "source": "ASR",
            "startMs": 1000,
            "endMs": 3000,
            "content": "为用户表创建唯一索引。",
        },
        {
            "segmentId": "ocr-segment",
            "source": "OCR",
            "startMs": 4000,
            "endMs": 4000,
            "content": "提交任务",
        },
    ])

    result = retriever.search("用户表唯一索引", top_k=1, sources=["ASR"])

    assert result["retrievalMode"] == "HYBRID_LEXICAL_BASELINE"
    assert result["matches"][0]["segmentId"] == "index-segment"
    assert result["matches"][0]["score"] > 0
    assert result["matches"][0]["scoreDetails"]["termCoverage"] > 0


def test_synthetic_evidence_rag_evaluation_is_reproducible() -> None:
    payload = json.loads(EVAL_DATASET.read_text(encoding="utf-8"))

    result = evaluate_retrieval_cases(
        payload["segments"],
        payload["cases"],
        top_k=payload["topK"],
    )

    assert result["scope"] == "SYNTHETIC_EVIDENCE_BASELINE"
    assert result["caseCount"] == 9
    assert result["recallAtK"] >= 0.9
    assert result["mrr"] >= 0.9
    assert result["hitRate"] >= 0.9
