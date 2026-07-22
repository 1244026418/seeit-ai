from __future__ import annotations

import json
from pathlib import Path

from seeit.agent import AgentToolbox
from seeit.agent_graph import extract_goal_timestamps_ms, run_langgraph_agent
from seeit.retrieval import (
    CoverageAwareEvidenceRetriever,
    EvidenceRetriever,
    evaluate_retrieval_cases,
)


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


def test_langgraph_recursion_limit_scales_with_step_budget() -> None:
    segment = {
        "segmentId": "segment-1",
        "source": "ASR",
        "startMs": 0,
        "endMs": 1000,
        "content": "最终证据",
    }

    def normalize_report(value: dict) -> dict:
        return value

    def evaluate_report(_: dict, __: list[dict]) -> dict:
        return {
            "structuredValid": True,
            "evidenceSupportRate": 1.0,
            "criticPassed": True,
            "citationCount": 1,
            "supportedCitationCount": 1,
        }

    toolbox = AgentToolbox(
        metadata={"mediaId": 1},
        segments=[segment],
        normalize_report=normalize_report,
        evaluate_report=evaluate_report,
    )

    class SlowProvider:
        def __init__(self) -> None:
            self.calls = 0

        def _completion(self, messages: list[dict], tools: list[dict]) -> dict:
            self.calls += 1
            if self.calls < 12:
                return {"tool_calls": [{
                    "id": f"call-{self.calls}",
                    "type": "function",
                    "function": {"name": "get_video_metadata", "arguments": "{}"},
                }]}
            return {"tool_calls": [{
                "id": f"call-{self.calls}",
                "type": "function",
                "function": {
                    "name": "generate_report",
                    "arguments": json.dumps({
                        "title": "测试",
                        "conclusions": ["最终证据"],
                        "evidence": [{"timestampMs": 0, "source": "ASR", "content": "最终证据"}],
                        "suggestions": [],
                    }, ensure_ascii=False),
                },
            }]}

    provider = SlowProvider()
    result = run_langgraph_agent(provider, toolbox, "测试", max_steps=12)

    assert result["evaluation"]["criticPassed"] is True
    assert result["agentGraph"]["steps"] == 12


def test_timestamp_parser_supports_chinese_seconds_and_clock_notation() -> None:
    assert extract_goal_timestamps_ms("第19秒左右和 01:20 的画面分别是什么？") == [19000, 80000]
    assert extract_goal_timestamps_ms("1分20秒附近出现了什么？") == [80000]


def test_budget_exhaustion_forces_final_report_with_prefetched_ocr() -> None:
    segment = {
        "segmentId": "ocr-19",
        "source": "OCR",
        "startMs": 19000,
        "endMs": 34000,
        "content": "我喜欢唱、跳、Rap和篮球",
    }
    toolbox = AgentToolbox(
        metadata={"mediaId": 2},
        segments=[segment],
        normalize_report=lambda value: value,
        evaluate_report=lambda report, _: {
            "structuredValid": bool(report.get("finalAnswer")),
            "evidenceSupportRate": 1.0,
            "criticPassed": bool(report.get("finalAnswer")),
            "citationCount": len(report.get("evidence", [])),
            "supportedCitationCount": len(report.get("evidence", [])),
        },
    )

    class BudgetProvider:
        def __init__(self) -> None:
            self.calls = 0
            self.finalize_messages: list[dict] = []

        def _completion(self, messages: list[dict], tools: list[dict], tool_choice=None) -> dict:
            self.calls += 1
            if tool_choice:
                self.finalize_messages = messages
                return {"tool_calls": [{
                    "id": "final-report",
                    "type": "function",
                    "function": {
                        "name": "generate_report",
                        "arguments": json.dumps({
                            "answerable": True,
                            "finalAnswer": "画面中的句子是：我喜欢唱、跳、Rap和篮球。",
                            "title": "画面文字回答",
                            "conclusions": ["画面显示我喜欢唱、跳、Rap和篮球。"],
                            "evidence": [{
                                "timestampMs": 19000,
                                "source": "OCR",
                                "content": "我喜欢唱、跳、Rap和篮球",
                            }],
                            "suggestions": [],
                        }, ensure_ascii=False),
                    },
                }]}
            return {"tool_calls": [{
                "id": f"search-{self.calls}",
                "type": "function",
                "function": {"name": "search_timeline", "arguments": json.dumps({"query": "这段话"})},
            }]}

    provider = BudgetProvider()
    result = run_langgraph_agent(provider, toolbox, "视频在第19秒左右说的这段话是什么？", max_steps=3)

    assert result["accepted"] is True
    assert result["report"]["finalAnswer"].endswith("我喜欢唱、跳、Rap和篮球。")
    assert result["agentGraph"]["steps"] == 3
    assert result["agentGraph"]["finalizeCalls"] == 1
    assert [item["tool"] for item in toolbox.trace()[:2]] == [
        "get_evidence_window",
        "search_timeline",
    ]
    assert "我喜欢唱、跳、Rap和篮球" in json.dumps(provider.finalize_messages, ensure_ascii=False)
    assert "likelyFollowingOcr" in json.dumps(provider.finalize_messages, ensure_ascii=False)


def test_finalize_retries_malformed_tool_arguments_before_refusing() -> None:
    segment = {
        "segmentId": "asr-token",
        "source": "ASR",
        "startMs": 1000,
        "endMs": 5000,
        "content": "鸡蛋是一个 token，关羽是一个 token。",
    }
    toolbox = AgentToolbox(
        metadata={"mediaId": 2},
        segments=[segment],
        normalize_report=lambda value: value,
        evaluate_report=lambda report, _: {
            "structuredValid": bool(report.get("finalAnswer")),
            "evidenceSupportRate": 1.0,
            "criticPassed": bool(report.get("finalAnswer")),
            "citationCount": len(report.get("evidence", [])),
            "supportedCitationCount": len(report.get("evidence", [])),
        },
    )

    class RepairingProvider:
        def __init__(self) -> None:
            self.finalize_calls = 0

        def _completion(self, messages: list[dict], tools: list[dict], tool_choice=None) -> dict:
            if not tool_choice:
                return {"tool_calls": [{
                    "id": "keep-searching",
                    "type": "function",
                    "function": {"name": "search_timeline", "arguments": '{"query":"token"}'},
                }]}
            self.finalize_calls += 1
            arguments = (
                '{"answerable":true,"finalAnswer":"未转义的"引号"","title":"错误"}'
                if self.finalize_calls == 1
                else json.dumps({
                    "answerable": True,
                    "finalAnswer": "鸡蛋关羽共两个 token。",
                    "title": "Token 数量",
                    "conclusions": ["1 + 1 = 2。"],
                    "evidence": [{"timestampMs": 1000, "source": "ASR", "content": segment["content"]}],
                    "suggestions": [],
                }, ensure_ascii=False)
            )
            return {"tool_calls": [{
                "id": f"finalize-{self.finalize_calls}",
                "type": "function",
                "function": {"name": "generate_report", "arguments": arguments},
            }]}

    result = run_langgraph_agent(RepairingProvider(), toolbox, "鸡蛋关羽有几个 token？", max_steps=3)

    assert result["accepted"] is True
    assert result["report"]["finalAnswer"] == "鸡蛋关羽共两个 token。"
    assert result["agentGraph"]["finalizeCalls"] == 2


def test_goal_anchor_gate_blocks_answerable_report_but_allows_refusal() -> None:
    segment = {
        "segmentId": "checkpoint",
        "source": "ASR",
        "startMs": 0,
        "endMs": 5000,
        "content": "Agent Checkpoint 用来保存执行状态。",
    }
    retriever = CoverageAwareEvidenceRetriever([segment], EvidenceRetriever([segment]))
    toolbox = AgentToolbox(
        metadata={"mediaId": 3},
        segments=[segment],
        retriever=retriever,
        normalize_report=lambda value: value,
        evaluate_report=lambda report, _: {
            "structuredValid": bool(report.get("finalAnswer")),
            "evidenceSupportRate": 1.0,
            "criticPassed": bool(report.get("finalAnswer")),
            "citationCount": len(report.get("evidence", [])),
            "supportedCitationCount": len(report.get("evidence", [])),
        },
    )

    prefetched = toolbox.prefetch_goal_evidence("Agent 的 RAG 是什么？")
    unsupported = toolbox.execute("generate_report", {
        "answerable": True,
        "finalAnswer": "RAG 是检索增强生成。",
        "title": "RAG",
        "conclusions": ["RAG 是检索增强生成。"],
        "evidence": [],
        "suggestions": [],
    })
    refusal = toolbox.execute("generate_report", {
        "answerable": False,
        "finalAnswer": "视频中没有说明 RAG，无法从视频确定。",
        "title": "视频证据不足",
        "conclusions": ["当前视频证据不足。"],
        "evidence": [],
        "suggestions": [],
    })

    assert prefetched["abstention"]["abstained"] is True
    assert unsupported["accepted"] is False
    assert unsupported["evaluation"]["coverageGatePassed"] is False
    assert refusal["accepted"] is True


def test_followup_search_can_complete_missing_goal_requirement() -> None:
    segments = [
        {
            "segmentId": "first",
            "source": "ASR",
            "startMs": 0,
            "endMs": 1000,
            "content": "机制一是循环控制。",
        },
        {
            "segmentId": "second",
            "source": "ASR",
            "startMs": 2000,
            "endMs": 3000,
            "content": "机制二是状态管理。",
        },
    ]

    class PartialRetriever:
        def search(self, query: str, *, top_k: int = 8, sources=None) -> dict:
            if "状态" in query or "机制二" in query:
                return {
                    "ok": True,
                    "query": query,
                    "retrievalMode": "TEST",
                    "matches": [segments[1]],
                    "matchedCount": 1,
                    "fallbackToTimelineStart": False,
                }
            return {
                "ok": True,
                "query": query,
                "retrievalMode": "TEST",
                "matches": [segments[0]],
                "matchedCount": 1,
                "fallbackToTimelineStart": False,
                "coveragePlan": {"requirementCount": 2},
                "evidenceSufficiency": {
                    "decision": "PARTIAL_EVIDENCE",
                    "requirementCount": 2,
                    "satisfiedRequirementCount": 1,
                    "fullyCovered": False,
                    "requirements": [
                        {
                            "requirementId": "requirement-1",
                            "markers": ["机制一"],
                            "satisfied": True,
                        },
                        {
                            "requirementId": "requirement-2",
                            "markers": ["机制二"],
                            "satisfied": False,
                        },
                    ],
                },
            }

    toolbox = AgentToolbox(
        metadata={"mediaId": 4},
        segments=segments,
        retriever=PartialRetriever(),
        normalize_report=lambda value: value,
        evaluate_report=lambda report, _: {
            "structuredValid": True,
            "evidenceSupportRate": 1.0,
            "criticPassed": True,
            "citationCount": len(report.get("evidence", [])),
            "supportedCitationCount": len(report.get("evidence", [])),
        },
    )

    toolbox.prefetch_goal_evidence("两个机制是什么？")
    blocked = toolbox.execute("generate_report", {
        "answerable": True,
        "finalAnswer": "只有机制一。",
        "title": "机制",
        "conclusions": ["只有机制一。"],
        "evidence": [],
        "suggestions": [],
    })
    toolbox.execute("search_timeline", {"query": "机制二 状态管理"})
    accepted = toolbox.execute("generate_report", {
        "answerable": True,
        "finalAnswer": "机制一是循环控制，机制二是状态管理。",
        "title": "机制",
        "conclusions": ["两个机制均已覆盖。"],
        "evidence": [],
        "suggestions": [],
    })

    assert blocked["accepted"] is False
    assert toolbox.goal_evidence_sufficiency()["fullyCovered"] is True
    assert accepted["accepted"] is True
