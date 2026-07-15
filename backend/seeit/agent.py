from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any


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
    elif re.search(r"步骤|操作|教程|流程|怎么|如何", normalized):
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
    ) -> None:
        self.metadata = dict(metadata)
        self.segments = [self._public_segment(item, index) for index, item in enumerate(segments)]
        self.normalize_report = normalize_report
        self.evaluate_report = evaluate_report
        self._trace: list[dict[str, Any]] = []

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
    def _preview(value: Any, limit: int = 2400) -> str:
        rendered = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        return rendered if len(rendered) <= limit else rendered[:limit] + "..."

    @staticmethod
    def _terms(value: str) -> set[str]:
        normalized = re.sub(r"\s+", "", value.lower())
        ascii_words = set(re.findall(r"[a-z0-9_]{2,}", normalized))
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
        chinese_pairs = {chinese[index : index + 2] for index in range(max(0, len(chinese) - 1))}
        return ascii_words | chinese_pairs

    @classmethod
    def _relevance(cls, query: str, content: str) -> float:
        compact_query = re.sub(r"\s+", "", query.lower())
        compact_content = re.sub(r"\s+", "", content.lower())
        if not compact_query or not compact_content:
            return 0.0
        terms = cls._terms(query)
        matched = sum(1 for term in terms if term in compact_content)
        term_score = matched / max(1, len(terms))
        exact_bonus = 1.0 if compact_query in compact_content else 0.0
        return round(exact_bonus + term_score, 4)

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
                    "description": "按语义关键词检索 ASR/OCR 时间轴，返回最相关且可引用的片段。",
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
                    "description": "提交最终结构化报告。工具会执行 Critic 校验；未通过时应继续检索并修订。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "conclusions": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                            "evidence": {"type": "array", "items": citation_schema, "maxItems": 20},
                            "suggestions": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                        },
                        "required": ["title", "conclusions", "evidence", "suggestions"],
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

    def _get_video_metadata(self) -> dict[str, Any]:
        return {"ok": True, **self.metadata, "evidenceSegmentCount": len(self.segments)}

    def _search_timeline(
        self,
        query: str,
        top_k: int = 8,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_query = re.sub(r"\s+", " ", str(query)).strip()
        if not normalized_query:
            raise ValueError("检索词不能为空")
        top_k = max(1, min(int(top_k), 20))
        allowed_sources = {str(item).upper() for item in sources or []}
        candidates = [
            item for item in self.segments
            if not allowed_sources or item["source"] in allowed_sources
        ]
        ranked = [
            {**item, "score": self._relevance(normalized_query, item["content"])}
            for item in candidates
        ]
        ranked.sort(key=lambda item: (-item["score"], item["startMs"], str(item["segmentId"])))
        positive = [item for item in ranked if item["score"] > 0]
        fallback = not positive
        matches = (positive or ranked)[:top_k]
        return {
            "ok": True,
            "query": normalized_query,
            "matches": matches,
            "matchedCount": len(positive),
            "fallbackToTimelineStart": fallback,
        }

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
        **_: Any,
    ) -> dict[str, Any]:
        report = self.normalize_report({
            "title": title,
            "conclusions": conclusions,
            "evidence": evidence,
            "suggestions": suggestions,
        })
        evaluation = self.evaluate_report(report, self.segments)
        return {
            "ok": True,
            "report": report,
            "evaluation": evaluation,
            "accepted": evaluation["criticPassed"],
            "message": "Critic 校验通过" if evaluation["criticPassed"] else "引用或结构未通过，请继续检索并修订",
        }
