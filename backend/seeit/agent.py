from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

from seeit.retrieval import EvidenceRetriever, SearchRetriever


ReportNormalizer = Callable[[dict[str, Any]], dict[str, Any]]
ReportEvaluator = Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]]


def build_agent_plan(goal: str) -> dict[str, Any]:
    normalized = re.sub(r"\s+", " ", goal).strip()
    if re.search(r"会议|决策|待办|负责人|风险", normalized):
        intent = "MEETING_REVIEW"
        tasks = [
            "读取视频元数据并确认会议分析范围",
            "检索决策、待办、负责人和风险相关的时间轴证据",
            "展开关键证据窗口并生成会议复盘",
            "校验每条结论的时间戳与原始证据",
        ]
    elif re.search(r"步骤|操作|教程|流程|怎么做|怎样做|如何(?:操作|完成|实现|使用|配置|部署)", normalized):
        intent = "OPERATION_GUIDE"
        tasks = [
            "读取视频元数据并识别操作教程目标",
            "检索步骤词、界面动作和前后依赖相关证据",
            "展开关键证据窗口并按顺序组织操作指南",
            "校验步骤引用和时间戳是否可回看",
        ]
    elif re.search(r"定位|查找|哪里|何时|什么时候|为什么|问题|[?？]", normalized):
        intent = "EVIDENCE_QA"
        tasks = [
            "读取视频元数据并理解问题边界",
            "围绕问题检索最相关的 ASR/OCR 时间轴证据",
            "展开命中片段前后的上下文并形成回答",
            "校验回答是否完全由视频证据支持",
        ]
    else:
        intent = "STRUCTURED_SUMMARY"
        tasks = [
            "读取视频元数据并确定总结范围",
            "检索主题、观点和示例相关的时间轴证据",
            "展开关键证据窗口并生成结构化报告",
            "校验结论、引用和报告结构",
        ]
    return {
        "understoodGoal": normalized,
        "intent": intent,
        "tasks": tasks,
        "steps": [
            {"stage": "CONTEXT", "tools": ["get_video_metadata"]},
            {"stage": "RETRIEVAL", "tools": ["search_timeline", "get_evidence_window"]},
            {"stage": "CRITIC", "tools": ["verify_citations", "generate_report"]},
        ],
    }


class AgentToolbox:
    def __init__(
        self,
        *,
        metadata: dict[str, Any],
        segments: list[dict[str, Any]],
        normalize_report: ReportNormalizer,
        evaluate_report: ReportEvaluator,
        retriever: SearchRetriever | None = None,
    ) -> None:
        self.metadata = dict(metadata)
        self.segments = [self._public_segment(item, index) for index, item in enumerate(segments)]
        self.retriever = retriever or EvidenceRetriever(self.segments)
        self.normalize_report = normalize_report
        self.evaluate_report = evaluate_report
        self._trace: list[dict[str, Any]] = []
        self._goal_coverage_plan: dict[str, Any] | None = None
        self._goal_evidence_sufficiency: dict[str, Any] | None = None

    @staticmethod
    def _public_segment(item: dict[str, Any], index: int) -> dict[str, Any]:
        return {
            "segmentId": item.get("segmentId") or f"segment-{index + 1}",
            "source": str(item.get("source", "ASR")).upper(),
            "startMs": max(0, int(item.get("startMs", 0))),
            "endMs": max(0, int(item.get("endMs", item.get("startMs", 0)))),
            "content": str(item.get("content", "")).strip()[:2000],
        }

    @staticmethod
    def _preview(value: Any, limit: int = 800) -> str:
        rendered = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        return rendered if len(rendered) <= limit else rendered[:limit] + "..."

    @staticmethod
    def _terms(value: str) -> set[str]:
        return EvidenceRetriever.terms(value)

    @classmethod
    def _relevance(cls, query: str, content: str) -> float:
        return EvidenceRetriever.relevance(query, content)

    def tool_schemas(self) -> list[dict[str, Any]]:
        citation_schema = {
            "type": "object",
            "properties": {
                "timestampMs": {"type": "integer", "minimum": 0},
                "source": {"type": "string", "enum": ["ASR", "OCR", "SYSTEM"]},
                "content": {"type": "string"},
            },
            "required": ["timestampMs", "source", "content"],
            "additionalProperties": False,
        }
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_video_metadata",
                    "description": "读取当前视频的文件名、来源、状态和证据数量。分析开始时调用。",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_timeline",
                    "description": (
                        "结合词法、语义和时间锚点检索 ASR/OCR 时间轴；多问句、枚举和分别题会返回"
                        "coveragePlan 与 evidenceSufficiency，生成答案前应检查所有证据需求是否覆盖。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "minLength": 1},
                            "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                            "sources": {
                                "type": "array",
                                "items": {"type": "string", "enum": ["ASR", "OCR", "SYSTEM"]},
                            },
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_evidence_window",
                    "description": "展开某个时间戳前后的连续证据，避免脱离上下文理解单个命中片段。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "timestamp_ms": {"type": "integer", "minimum": 0},
                            "before_ms": {"type": "integer", "minimum": 0, "maximum": 120000, "default": 15000},
                            "after_ms": {"type": "integer", "minimum": 0, "maximum": 120000, "default": 15000},
                        },
                        "required": ["timestamp_ms"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "verify_citations",
                    "description": "检查候选引用是否能在原始时间轴中找到，并返回逐条校验结果。",
                    "parameters": {
                        "type": "object",
                        "properties": {"citations": {"type": "array", "items": citation_schema, "maxItems": 20}},
                        "required": ["citations"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "generate_report",
                    "description": (
                        "提交最终结构化报告。answerable=true 时必须给出由视频证据支持的 finalAnswer 和引用；"
                        "answerable=false 时 finalAnswer 必须明确说明视频未提供答案，不得补充外部知识。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "answerable": {"type": "boolean"},
                            "finalAnswer": {"type": "string", "minLength": 1, "maxLength": 2000},
                            "title": {"type": "string"},
                            "conclusions": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                            "evidence": {"type": "array", "items": citation_schema, "maxItems": 20},
                            "suggestions": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                        },
                        "required": [
                            "answerable",
                            "finalAnswer",
                            "title",
                            "conclusions",
                            "evidence",
                            "suggestions",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def execute(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = arguments or {}
        started = time.perf_counter()
        try:
            handlers = {
                "get_video_metadata": self._get_video_metadata,
                "search_timeline": self._search_timeline,
                "get_evidence_window": self._get_evidence_window,
                "verify_citations": self._verify_citations,
                "generate_report": self._generate_report,
            }
            if name not in handlers:
                raise ValueError(f"未知 Agent 工具：{name}")
            result = handlers[name](**arguments)
            success = True
        except (TypeError, ValueError) as exc:
            result = {"ok": False, "error": str(exc)}
            success = False
        duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        self._trace.append({
            "index": len(self._trace) + 1,
            "tool": name,
            "arguments": self._preview(arguments),
            "resultPreview": self._preview(result),
            "durationMs": duration_ms,
            "success": success,
        })
        return result

    def trace(self) -> list[dict[str, Any]]:
        return list(self._trace)

    def duration_for(self, *tool_names: str) -> int:
        selected = set(tool_names)
        return sum(item["durationMs"] for item in self._trace if item["tool"] in selected)

    def prefetch_goal_evidence(self, goal: str, top_k: int = 8) -> dict[str, Any]:
        result = self.execute("search_timeline", {"query": goal, "top_k": top_k})
        self._goal_coverage_plan = dict(result.get("coveragePlan") or {}) or None
        self._goal_evidence_sufficiency = (
            dict(result.get("evidenceSufficiency") or {}) or None
        )
        return result

    def goal_evidence_sufficiency(self) -> dict[str, Any] | None:
        if self._goal_evidence_sufficiency is None:
            return None
        return json.loads(json.dumps(self._goal_evidence_sufficiency, ensure_ascii=False))

    def _merge_goal_coverage(self, matches: list[dict[str, Any]]) -> None:
        state = self._goal_evidence_sufficiency
        if not state or state.get("decision") == "INSUFFICIENT_EVIDENCE":
            return
        contents = [str(item.get("content", "")) for item in matches]
        requirements = [dict(item) for item in state.get("requirements", [])]
        for requirement in requirements:
            if requirement.get("satisfied"):
                continue
            markers = [str(item) for item in requirement.get("markers", [])]
            if markers and any(
                any(marker.replace(" ", "").lower() in content.replace(" ", "").lower()
                    for marker in markers)
                for content in contents
            ):
                requirement["satisfied"] = True
                requirement["markerCovered"] = True
                requirement["status"] = "SATISFIED_BY_FOLLOWUP"
        satisfied = sum(bool(item.get("satisfied")) for item in requirements)
        state["requirements"] = requirements
        state["satisfiedRequirementCount"] = satisfied
        state["fullyCovered"] = bool(requirements) and satisfied == len(requirements)
        state["decision"] = (
            "SUFFICIENT_CANDIDATES" if state["fullyCovered"] else "PARTIAL_EVIDENCE"
        )

    def _get_video_metadata(self) -> dict[str, Any]:
        return {"ok": True, **self.metadata, "evidenceSegmentCount": len(self.segments)}

    def _search_timeline(
        self,
        query: str,
        top_k: int = 8,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        result = self.retriever.search(query, top_k=top_k, sources=sources)
        self._merge_goal_coverage([dict(item) for item in result.get("matches", [])])
        return result

    def _get_evidence_window(
        self,
        timestamp_ms: int,
        before_ms: int = 15000,
        after_ms: int = 15000,
    ) -> dict[str, Any]:
        timestamp_ms = max(0, int(timestamp_ms))
        before_ms = max(0, min(int(before_ms), 120000))
        after_ms = max(0, min(int(after_ms), 120000))
        window_start = max(0, timestamp_ms - before_ms)
        window_end = timestamp_ms + after_ms
        segments = [
            item for item in self.segments
            if item["endMs"] >= window_start and item["startMs"] <= window_end
        ][:40]
        return {
            "ok": True,
            "windowStartMs": window_start,
            "windowEndMs": window_end,
            "segments": segments,
        }

    def _verify_citations(self, citations: list[dict[str, Any]]) -> dict[str, Any]:
        candidate = self.normalize_report({
            "answerable": True,
            "finalAnswer": "校验候选引用",
            "title": "引用校验",
            "conclusions": ["校验候选引用"],
            "evidence": citations,
            "suggestions": [],
        })
        evaluation = self.evaluate_report(candidate, self.segments)
        details = []
        for citation in candidate["evidence"]:
            single = self.evaluate_report({**candidate, "evidence": [citation]}, self.segments)
            details.append({**citation, "supported": single["supportedCitationCount"] == 1})
        return {"ok": True, **evaluation, "citations": details}

    def _generate_report(
        self,
        title: str,
        conclusions: list[str],
        evidence: list[dict[str, Any]],
        suggestions: list[str],
        answerable: bool | None = None,
        finalAnswer: str = "",
        **_: Any,
    ) -> dict[str, Any]:
        coverage = self.goal_evidence_sufficiency()
        if answerable and coverage and (
            coverage.get("decision") == "INSUFFICIENT_EVIDENCE"
            or not coverage.get("fullyCovered")
        ):
            missing_requirements = [
                str(item.get("requirementId"))
                for item in coverage.get("requirements", [])
                if not item.get("satisfied")
            ]
            return {
                "ok": True,
                "report": self.normalize_report({
                    "answerable": answerable,
                    "finalAnswer": finalAnswer,
                    "title": title,
                    "conclusions": conclusions,
                    "evidence": evidence,
                    "suggestions": suggestions,
                }),
                "evaluation": {
                    "structuredValid": True,
                    "criticPassed": False,
                    "coverageGatePassed": False,
                    "evidenceSufficiency": coverage,
                },
                "accepted": False,
                "message": (
                    "证据覆盖门禁未通过；缺少关键实体或未覆盖全部证据需求："
                    + ", ".join(missing_requirements or ["required-anchor"])
                ),
            }
        report = self.normalize_report({
            "answerable": answerable,
            "finalAnswer": finalAnswer,
            "title": title,
            "conclusions": conclusions,
            "evidence": evidence,
            "suggestions": suggestions,
        })
        evaluation = self.evaluate_report(report, self.segments)
        if report.get("answerable") and report.get("evidence") and not evaluation["criticPassed"]:
            supported_evidence = []
            for citation in report["evidence"]:
                single = self.evaluate_report({**report, "evidence": [citation]}, self.segments)
                if single.get("supportedCitationCount") == 1:
                    supported_evidence.append(citation)
            if supported_evidence and len(supported_evidence) < len(report["evidence"]):
                discarded_count = len(report["evidence"]) - len(supported_evidence)
                repaired = {**report, "evidence": supported_evidence}
                repaired_evaluation = self.evaluate_report(repaired, self.segments)
                if repaired_evaluation["criticPassed"]:
                    report = repaired
                    evaluation = {
                        **repaired_evaluation,
                        "citationRepairApplied": True,
                        "discardedCitationCount": discarded_count,
                    }
        return {
            "ok": True,
            "report": report,
            "evaluation": evaluation,
            "accepted": evaluation["criticPassed"],
            "message": "Critic 校验通过" if evaluation["criticPassed"] else "引用或结构未通过，请继续检索并修订",
        }
