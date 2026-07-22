from __future__ import annotations

from typing import Any

from seeit.retrieval import (
    ContextualEvidenceRetriever,
    CoverageAwareEvidenceRetriever,
    EvidenceRetriever,
    parse_time_hints,
    plan_evidence_requirements,
    required_query_anchors,
)


def segment(
    segment_id: str,
    source: str,
    start_ms: int,
    end_ms: int,
    content: str,
) -> dict[str, Any]:
    return {
        "segmentId": segment_id,
        "source": source,
        "startMs": start_ms,
        "endMs": end_ms,
        "content": content,
    }


def test_parse_time_hints_supports_clock_minutes_and_seconds() -> None:
    assert parse_time_hints("看 02:25、1分30秒和第19秒左右") == [145000, 90000, 19000]
    assert parse_time_hints("01:02:03 的画面") == [3723000]


def test_explicit_time_and_visual_hint_prioritize_nearby_ocr() -> None:
    segments = [
        segment("far", "ASR", 100000, 105000, "这段话出现在画面中"),
        segment("asr-19", "ASR", 17920, 26320, "比如这段话"),
        segment("ocr-15", "OCR", 15000, 30000, "DeepSeek"),
        segment("ocr-30", "OCR", 30000, 45000, "我喜欢唱、跳、Rap和篮球"),
    ]
    retriever = ContextualEvidenceRetriever(segments, EvidenceRetriever(segments))

    result = retriever.search("视频在第19秒左右说的这段话是什么（出现在画面中）？", top_k=4)

    assert result["timeHintsMs"] == [19000]
    assert [item["segmentId"] for item in result["matches"][:2]] == ["ocr-15", "ocr-30"]
    assert all(item["selectionReason"] == "TIME_ANCHOR" for item in result["matches"][:2])
    assert all("timeDistanceMs" in item["scoreDetails"] for item in result["matches"][:2])


def test_context_query_expands_same_source_neighbors_without_duplicates() -> None:
    segments = [
        segment("before", "ASR", 221900, 229700, "这就是注意力残差技术"),
        segment("anchor", "ASR", 229800, 237300, "这次模型使用了当时这篇论文中的技术"),
        segment("ocr", "OCR", 225000, 240000, "Attention Residuals"),
        segment("after", "ASR", 237300, 245600, "参数量问题得到解决"),
        segment("far", "ASR", 500000, 505000, "其他技术"),
    ]
    retriever = ContextualEvidenceRetriever(segments, EvidenceRetriever(segments))

    result = retriever.search("这次模型使用了当时这篇论文中的什么技术？", top_k=4)
    ids = [item["segmentId"] for item in result["matches"]]

    assert ids[0] == "anchor"
    assert "before" in ids[:4]
    assert len(ids) == len(set(ids))
    assert next(item for item in result["matches"] if item["segmentId"] == "before")[
        "selectionReason"
    ] == "ADJACENT_CONTEXT"


class LowConfidenceHybrid:
    def __init__(self, segments: list[dict[str, Any]]) -> None:
        self.segments = segments

    def search(self, query: str, *, top_k: int = 8, sources=None) -> dict[str, Any]:
        matches = [{
            **self.segments[0],
            "score": 0.01,
            "scoreDetails": {"denseScore": 0.42, "lexicalScore": 0.12},
        }]
        return {
            "ok": True,
            "query": query,
            "retrievalMode": "HYBRID_TEST",
            "matches": matches[:top_k],
            "matchedCount": len(matches),
            "fallbackToTimelineStart": False,
        }


def test_dual_low_confidence_abstains_but_explicit_time_remains_retrievable() -> None:
    segments = [segment("weak", "ASR", 18000, 22000, "token 在其他领域也会出现")]
    retriever = ContextualEvidenceRetriever(
        segments,
        LowConfidenceHybrid(segments),
        enable_abstention=True,
    )

    refused = retriever.search("加密领域的 token 是什么意思？")
    timed = retriever.search("第19秒的 token 是什么？")

    assert refused["matches"] == []
    assert refused["matchedCount"] == 0
    assert refused["abstention"]["abstained"] is True
    assert [item["segmentId"] for item in timed["matches"]] == ["weak"]
    assert timed["abstention"]["abstained"] is False


def test_source_filter_applies_to_time_and_adjacency_candidates() -> None:
    segments = [
        segment("asr", "ASR", 18000, 22000, "口播"),
        segment("ocr", "OCR", 18000, 22000, "画面答案"),
    ]
    retriever = ContextualEvidenceRetriever(segments, EvidenceRetriever(segments))

    result = retriever.search("第19秒画面显示什么？", sources=["OCR"])

    assert [item["segmentId"] for item in result["matches"]] == ["ocr"]


def test_requirement_planner_splits_clauses_enumerations_and_comparisons() -> None:
    clauses = plan_evidence_requirements(
        "视频以什么事件说明 State 溯源？恢复后会导致什么问题？"
    )
    enumeration = plan_evidence_requirements("LangGraph有哪四个底层的硬核机制？")
    comparison = plan_evidence_requirements("LangChain和LangGraph分别适合什么场景？")

    assert clauses["strategy"] == "CLAUSE_DECOMPOSITION"
    assert clauses["requirementCount"] == 2
    assert enumeration["strategy"] == "ENUMERATION_DECOMPOSITION"
    assert enumeration["requirementCount"] == 4
    assert enumeration["requirements"][2]["markers"][:2] == ["机制三", "机制3"]
    assert comparison["strategy"] == "COMPARISON_DECOMPOSITION"
    assert [item["query"] for item in comparison["requirements"]] == [
        "LangChain 适合什么场景",
        "LangGraph 适合什么场景",
    ]


def test_required_anchor_extraction_ignores_generic_terms_and_keeps_domain() -> None:
    anchors = required_query_anchors("视频中加密领域的 token 和 Dify 是什么？")

    assert [(item["normalized"], item["kind"]) for item in anchors] == [
        ("dify", "ASCII_ENTITY"),
        ("加密", "DOMAIN_MODIFIER"),
    ]


def test_coverage_aware_retrieval_reserves_evidence_for_each_enumerated_item() -> None:
    segments = []
    for index, label in enumerate(("一", "二", "三", "四"), start=1):
        start = index * 30000
        segments.extend([
            segment(
                f"ocr-{index}",
                "OCR",
                start,
                start + 15000,
                f"LangGraph 机制{label}：第{label}个底层机制",
            ),
            segment(
                f"asr-{index}",
                "ASR",
                start + 1000,
                start + 5000,
                f"现在讲第{label}个机制的具体价值",
            ),
        ])
    retriever = CoverageAwareEvidenceRetriever(segments, EvidenceRetriever(segments))

    result = retriever.search("LangGraph有哪四个底层的硬核机制？", top_k=8)
    ids = {item["segmentId"] for item in result["matches"]}

    assert result["coveragePlan"]["requirementCount"] == 4
    assert result["evidenceSufficiency"]["fullyCovered"] is True
    assert result["evidenceSufficiency"]["satisfiedRequirementCount"] == 4
    assert {f"ocr-{index}" for index in range(1, 5)} <= ids
    assert {f"asr-{index}" for index in range(1, 5)} <= ids


def test_coverage_anchor_gate_is_scoped_to_current_video() -> None:
    checkpoint_segments = [
        segment("checkpoint", "ASR", 0, 5000, "Agent Checkpoint 用来保存执行状态")
    ]
    rag_segments = [segment("rag", "OCR", 0, 5000, "RAG 是检索增强生成")]

    checkpoint = CoverageAwareEvidenceRetriever(
        checkpoint_segments,
        EvidenceRetriever(checkpoint_segments),
    )
    rag = CoverageAwareEvidenceRetriever(rag_segments, EvidenceRetriever(rag_segments))

    refused = checkpoint.search("Agent 的 RAG 是什么？")
    accepted = rag.search("Agent 的 RAG 是什么？")

    assert refused["matches"] == []
    assert refused["abstention"]["abstained"] is True
    assert refused["abstention"]["missingRequiredAnchors"][0]["normalized"] == "rag"
    assert accepted["abstention"]["abstained"] is False
    assert accepted["evidenceSufficiency"]["fullyCovered"] is True


def test_domain_modifier_can_trigger_conservative_refusal() -> None:
    segments = [segment("token", "ASR", 0, 5000, "大模型 token 是分词后的编号")]
    retriever = CoverageAwareEvidenceRetriever(segments, EvidenceRetriever(segments))

    result = retriever.search("加密领域的 token 是什么意思？")

    assert result["abstention"]["abstained"] is True
    assert result["evidenceSufficiency"]["decision"] == "INSUFFICIENT_EVIDENCE"


def test_title_case_model_anchor_can_fall_back_to_asr_variant() -> None:
    segments = [
        segment(
            "qwen-asr-error",
            "ASR",
            240000,
            248000,
            "苹果的苹字在千万模型里是一个 token。",
        )
    ]
    retriever = CoverageAwareEvidenceRetriever(segments, EvidenceRetriever(segments))

    result = retriever.search("苹果的苹字在 Qwen 模型里是几个 token？")

    assert result["abstention"]["abstained"] is False
    assert result["evidenceSufficiency"]["decision"] == (
        "SUFFICIENT_CANDIDATES_WITH_ANCHOR_VARIANCE"
    )
    assert result["evidenceSufficiency"]["missingRequiredAnchors"][0]["normalized"] == "qwen"
