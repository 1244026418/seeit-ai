"""Evidence retrieval primitives and deterministic evaluation metrics."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


class SearchRetriever(Protocol):
    def search(
        self,
        query: str,
        *,
        top_k: int = 8,
        sources: list[str] | None = None,
    ) -> dict[str, Any]: ...


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


def parse_time_hints(value: str) -> list[int]:
    """Extract explicit timestamps from a Chinese natural-language query."""
    text = str(value)
    hints: list[int] = []
    occupied: list[tuple[int, int]] = []

    def add(seconds: float, span: tuple[int, int]) -> None:
        milliseconds = max(0, int(round(seconds * 1000)))
        if milliseconds not in hints:
            hints.append(milliseconds)
        occupied.append(span)

    for match in re.finditer(r"(?<!\d)(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?!\d)", text):
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        add(hours * 3600 + minutes * 60 + seconds, match.span())

    minute_pattern = r"第?\s*(\d+(?:\.\d+)?)\s*(?:分钟|分)(?:\s*(\d+(?:\.\d+)?)\s*秒)?"
    for match in re.finditer(minute_pattern, text):
        if any(match.start() < end and match.end() > start for start, end in occupied):
            continue
        add(float(match.group(1)) * 60 + float(match.group(2) or 0), match.span())

    for match in re.finditer(r"第?\s*(\d+(?:\.\d+)?)\s*秒", text):
        if any(match.start() < end and match.end() > start for start, end in occupied):
            continue
        add(float(match.group(1)), match.span())
    return hints


def _interval_distance(timestamp_ms: int, segment: dict[str, Any]) -> int:
    start = int(segment.get("startMs", 0))
    end = max(start, int(segment.get("endMs", start)))
    if start <= timestamp_ms <= end:
        return 0
    return min(abs(timestamp_ms - start), abs(timestamp_ms - end))


_CHINESE_NUMBERS = {
    "两": 2,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
}
_NUMBER_LABELS = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六"}
_GENERIC_ASCII_ANCHORS = {
    "agent",
    "ai",
    "api",
    "asr",
    "llm",
    "ocr",
    "token",
    "video",
}


@dataclass(frozen=True)
class EvidenceRequirement:
    requirement_id: str
    query: str
    kind: str
    markers: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "requirementId": self.requirement_id,
            "query": self.query,
            "kind": self.kind,
            "markers": list(self.markers),
        }


def _question_clauses(value: str) -> list[str]:
    clauses = [
        re.sub(r"^[，,。.!！\s]+|[，,。.!！\s]+$", "", item).strip()
        for item in re.split(r"[？?；;]+", value)
    ]
    return [item for item in clauses if len(item) >= 4]


def _enumeration_marker(label: str) -> str:
    if "机制" in label:
        return "机制"
    if "局限" in label or "硬伤" in label:
        return "局限"
    if "任务" in label:
        return "任务"
    if "步骤" in label or "流程" in label:
        return "步骤"
    if "原因" in label:
        return "原因"
    return ""


def plan_evidence_requirements(query: str) -> dict[str, Any]:
    """Build a deterministic coverage plan without calling an LLM."""
    normalized = " ".join(str(query).split())
    if not normalized:
        raise ValueError("检索词不能为空")

    clauses = _question_clauses(normalized)
    if len(clauses) > 1:
        requirements = [
            EvidenceRequirement(f"requirement-{index}", clause, "CLAUSE")
            for index, clause in enumerate(clauses, start=1)
        ]
        strategy = "CLAUSE_DECOMPOSITION"
    else:
        comparison = re.search(
            r"(?P<left>[A-Za-z][A-Za-z0-9_.+-]*)\s*(?:和|与|、)\s*"
            r"(?P<right>[A-Za-z][A-Za-z0-9_.+-]*)\s*分别(?P<tail>.+)",
            normalized,
            re.IGNORECASE,
        )
        if comparison:
            tail = comparison.group("tail").strip("？?。 ")
            requirements = [
                EvidenceRequirement(
                    "requirement-1",
                    f"{comparison.group('left')} {tail}",
                    "COMPARISON_SIDE",
                ),
                EvidenceRequirement(
                    "requirement-2",
                    f"{comparison.group('right')} {tail}",
                    "COMPARISON_SIDE",
                ),
            ]
            strategy = "COMPARISON_DECOMPOSITION"
        else:
            enumeration = re.search(
                r"^(?P<subject>.+?)(?:有|包含|包括)哪\s*"
                r"(?P<count>[二两三四五六2-6])\s*(?:个|种|项|条|类)"
                r"(?P<label>[^？?。]{1,30})",
                normalized,
            )
            if not enumeration:
                enumeration = re.search(
                    r"^(?P<subject>.*?)哪\s*(?P<count>[二两三四五六2-6])\s*"
                    r"(?:个|种|项|条|类)(?P<label>[^？?。]{1,30})",
                    normalized,
                )
            if enumeration:
                raw_count = enumeration.group("count")
                count = int(raw_count) if raw_count.isdigit() else _CHINESE_NUMBERS[raw_count]
                subject = enumeration.group("subject").strip(" ，,：:")
                label = enumeration.group("label").strip(" ，,：:？?")
                marker_prefix = _enumeration_marker(label)
                requirements = []
                for index in range(1, count + 1):
                    chinese_index = _NUMBER_LABELS[index]
                    markers = tuple(dict.fromkeys(filter(None, (
                        f"{marker_prefix}{chinese_index}" if marker_prefix else "",
                        f"{marker_prefix}{index}" if marker_prefix else "",
                        f"第{chinese_index}个",
                        f"第{index}个",
                    ))))
                    requirements.append(EvidenceRequirement(
                        f"requirement-{index}",
                        " ".join(filter(None, (subject, label, *markers))),
                        "ENUMERATED_ITEM",
                        markers,
                    ))
                strategy = "ENUMERATION_DECOMPOSITION"
            else:
                requirements = [EvidenceRequirement("requirement-1", normalized, "PRIMARY")]
                strategy = "SINGLE_REQUIREMENT"

    return {
        "query": normalized,
        "strategy": strategy,
        "requirementCount": len(requirements),
        "requirements": [item.as_dict() for item in requirements],
    }


def required_query_anchors(query: str) -> list[dict[str, str]]:
    """Extract only high-precision anchors suitable for a conservative refusal gate."""
    anchors: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(value: str, kind: str) -> None:
        normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value.lower())
        if len(normalized) < 2 or normalized in seen:
            return
        seen.add(normalized)
        anchors.append({"text": value, "normalized": normalized, "kind": kind})

    for value in re.findall(r"[A-Za-z][A-Za-z0-9_.+-]{1,}", str(query)):
        if value.lower() not in _GENERIC_ASCII_ANCHORS:
            add(value, "ASCII_ENTITY")

    for match in re.finditer(r"([\u4e00-\u9fff]{2,10})领域", str(query)):
        value = match.group(1)
        for prefix in ("视频中", "视频里", "视频内", "关于", "对于", "在", "对", "从"):
            if value.startswith(prefix):
                value = value[len(prefix) :]
        add(value, "DOMAIN_MODIFIER")
    return anchors


class ContextualEvidenceRetriever:
    """Adds temporal anchors, adjacent evidence, and conservative abstention.

    This wrapper deliberately keeps the wrapped retriever's score scale intact.
    Temporal and adjacency candidates are selected deterministically instead of
    adding lexical, cosine, and RRF scores that are not directly comparable.
    """

    _VISUAL_HINT = re.compile(r"画面|屏幕|图中|图片|字幕|显示|截图")
    _CONTEXT_HINT = re.compile(
        r"这篇|这段|这几个|这些|这次|当时|之后|以前|以前的|之前|随后|接下来|"
        r"分别|哪些|哪几个|哪三|哪四|三种|四个任务|顺序"
    )

    def __init__(
        self,
        segments: list[dict[str, Any]],
        base: SearchRetriever,
        *,
        candidate_depth: int = 20,
        time_window_ms: int = 15000,
        neighbor_window_ms: int = 15000,
        max_neighbors: int = 3,
        enable_abstention: bool = False,
        min_dense_score: float = 0.45,
        min_lexical_score: float = 0.18,
    ) -> None:
        self.segments = [dict(item) for item in segments]
        self.base = base
        self.candidate_depth = max(8, min(int(candidate_depth), 100))
        self.time_window_ms = max(1000, int(time_window_ms))
        self.neighbor_window_ms = max(1000, int(neighbor_window_ms))
        self.max_neighbors = max(0, min(int(max_neighbors), 8))
        self.enable_abstention = bool(enable_abstention)
        self.min_dense_score = float(min_dense_score)
        self.min_lexical_score = float(min_lexical_score)

    @staticmethod
    def _allowed(item: dict[str, Any], allowed_sources: set[str]) -> bool:
        return not allowed_sources or str(item.get("source", "")).upper() in allowed_sources

    @staticmethod
    def _segment_key(item: dict[str, Any]) -> str:
        segment_id = item.get("segmentId")
        if segment_id is not None:
            return str(segment_id)
        return ":".join((
            str(item.get("source", "")),
            str(item.get("startMs", 0)),
            str(item.get("endMs", 0)),
        ))

    def _time_candidates(
        self,
        query: str,
        hints: list[int],
        allowed_sources: set[str],
    ) -> list[dict[str, Any]]:
        prefer_ocr = bool(self._VISUAL_HINT.search(query))
        candidates: list[tuple[int, int, int, str, dict[str, Any]]] = []
        for item in self.segments:
            if not self._allowed(item, allowed_sources):
                continue
            distances = [_interval_distance(timestamp, item) for timestamp in hints]
            distance = min(distances, default=self.time_window_ms + 1)
            if distance > self.time_window_ms:
                continue
            source = str(item.get("source", "")).upper()
            source_priority = 0 if prefer_ocr and source == "OCR" else 1
            candidate = dict(item)
            candidate["score"] = float(candidate.get("score", 0.0))
            candidate["scoreDetails"] = {
                **dict(candidate.get("scoreDetails") or {}),
                "timeDistanceMs": distance,
            }
            candidates.append((
                source_priority,
                distance,
                int(item.get("startMs", 0)),
                self._segment_key(item),
                candidate,
            ))
        candidates.sort(key=lambda item: item[:4])
        return [item[4] for item in candidates]

    def _neighbors(
        self,
        anchor: dict[str, Any],
        allowed_sources: set[str],
    ) -> list[dict[str, Any]]:
        anchor_start = int(anchor.get("startMs", 0))
        anchor_end = max(anchor_start, int(anchor.get("endMs", anchor_start)))
        anchor_source = str(anchor.get("source", "")).upper()
        anchor_key = self._segment_key(anchor)
        candidates: list[tuple[int, int, int, str, dict[str, Any]]] = []
        for item in self.segments:
            if self._segment_key(item) == anchor_key or not self._allowed(item, allowed_sources):
                continue
            start = int(item.get("startMs", 0))
            end = max(start, int(item.get("endMs", start)))
            gap = max(0, start - anchor_end, anchor_start - end)
            if gap > self.neighbor_window_ms:
                continue
            same_source = int(str(item.get("source", "")).upper() != anchor_source)
            candidate = dict(item)
            candidate["score"] = float(candidate.get("score", 0.0))
            candidate["scoreDetails"] = {
                **dict(candidate.get("scoreDetails") or {}),
                "neighborGapMs": gap,
            }
            candidates.append((
                same_source,
                gap,
                abs(start - anchor_start),
                self._segment_key(item),
                candidate,
            ))
        candidates.sort(key=lambda item: item[:4])
        return [item[4] for item in candidates[: self.max_neighbors]]

    def _abstention(self, matches: list[dict[str, Any]], has_time_hint: bool) -> dict[str, Any]:
        dense_scores = [
            float(details["denseScore"])
            for item in matches
            if isinstance((details := item.get("scoreDetails")), dict)
            and details.get("denseScore") is not None
        ]
        lexical_scores = [
            float(details["lexicalScore"])
            for item in matches
            if isinstance((details := item.get("scoreDetails")), dict)
            and details.get("lexicalScore") is not None
        ]
        max_dense = max(dense_scores, default=None)
        max_lexical = max(lexical_scores, default=None)
        abstained = bool(
            self.enable_abstention
            and not has_time_hint
            and max_dense is not None
            and max_lexical is not None
            and max_dense < self.min_dense_score
            and max_lexical < self.min_lexical_score
        )
        return {
            "abstained": abstained,
            "maxDenseScore": round(max_dense, 6) if max_dense is not None else None,
            "maxLexicalScore": round(max_lexical, 6) if max_lexical is not None else None,
            "denseThreshold": self.min_dense_score,
            "lexicalThreshold": self.min_lexical_score,
            "policy": "DENSE_AND_LEXICAL_FLOOR_V1" if self.enable_abstention else "DISABLED",
        }

    def search(
        self,
        query: str,
        *,
        top_k: int = 8,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_query = " ".join(str(query).split())
        if not normalized_query:
            raise ValueError("检索词不能为空")
        top_k = max(1, min(int(top_k), 20))
        depth = max(top_k, self.candidate_depth)
        allowed_sources = {str(item).upper() for item in sources or []}
        base_result = self.base.search(normalized_query, top_k=depth, sources=sources)
        base_matches = [dict(item) for item in base_result.get("matches", [])]
        hints = parse_time_hints(normalized_query)
        confidence = self._abstention(base_matches, bool(hints))
        if confidence["abstained"]:
            return {
                **base_result,
                "retrievalMode": f"CONTEXTUAL_{base_result.get('retrievalMode', 'UNKNOWN')}",
                "matches": [],
                "matchedCount": 0,
                "fallbackToTimelineStart": False,
                "candidateCountBeforeAbstention": len(base_matches),
                "timeHintsMs": hints,
                "abstention": confidence,
            }

        ranked: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(items: list[dict[str, Any]], reason: str) -> None:
            for item in items:
                key = self._segment_key(item)
                if key in seen:
                    continue
                enriched = dict(item)
                enriched["selectionReason"] = reason
                ranked.append(enriched)
                seen.add(key)

        if hints:
            add(self._time_candidates(normalized_query, hints, allowed_sources), "TIME_ANCHOR")
            add(base_matches, "BASE_RANK")
        elif base_matches and self._CONTEXT_HINT.search(normalized_query):
            add(base_matches[:1], "BASE_ANCHOR")
            add(self._neighbors(base_matches[0], allowed_sources), "ADJACENT_CONTEXT")
            add(base_matches[1:], "BASE_RANK")
        else:
            add(base_matches, "BASE_RANK")

        return {
            **base_result,
            "retrievalMode": f"CONTEXTUAL_{base_result.get('retrievalMode', 'UNKNOWN')}",
            "matches": ranked[:top_k],
            "matchedCount": len(ranked),
            "fallbackToTimelineStart": False,
            "timeHintsMs": hints,
            "abstention": confidence,
        }


class CoverageAwareEvidenceRetriever:
    """Decomposes multi-part questions and exposes evidence sufficiency state."""

    def __init__(
        self,
        segments: list[dict[str, Any]],
        base: SearchRetriever,
        *,
        candidate_depth: int = 12,
        context_window_ms: int = 15000,
        context_per_requirement: int = 1,
        enable_anchor_gate: bool = True,
    ) -> None:
        self.segments = [dict(item) for item in segments]
        self.base = base
        self.candidate_depth = max(8, min(int(candidate_depth), 40))
        self.context_window_ms = max(1000, int(context_window_ms))
        self.context_per_requirement = max(0, min(int(context_per_requirement), 3))
        self.enable_anchor_gate = bool(enable_anchor_gate)
        self._corpus = re.sub(
            r"[^a-z0-9\u4e00-\u9fff]+",
            "",
            " ".join(str(item.get("content", "")) for item in self.segments).lower(),
        )

    @staticmethod
    def _segment_key(item: dict[str, Any]) -> str:
        segment_id = item.get("segmentId")
        if segment_id is not None:
            return str(segment_id)
        return ":".join((
            str(item.get("source", "")),
            str(item.get("startMs", 0)),
            str(item.get("endMs", 0)),
        ))

    @staticmethod
    def _contains_marker(content: str, markers: list[str]) -> bool:
        compact = re.sub(r"\s+", "", str(content).lower())
        return any(re.sub(r"\s+", "", marker.lower()) in compact for marker in markers)

    def _missing_anchors(self, anchors: list[dict[str, str]]) -> list[dict[str, str]]:
        return [item for item in anchors if item["normalized"] not in self._corpus]

    @staticmethod
    def _strict_anchor_gate(
        query: str,
        plan: dict[str, Any],
        anchors: list[dict[str, str]],
        missing: list[dict[str, str]],
    ) -> bool:
        if not missing:
            return False
        if any(item.get("kind") == "DOMAIN_MODIFIER" for item in missing):
            return True
        if re.search(r"区别|差异|不同|分别", query) and len(anchors) >= 2:
            return True
        if len(anchors) == len(missing) and any(
            str(item.get("text", "")).isupper() for item in missing
        ):
            return True
        return False

    def _neighbor_candidates(
        self,
        anchor: dict[str, Any],
        query: str,
        sources: list[str] | None,
    ) -> list[dict[str, Any]]:
        anchor_key = self._segment_key(anchor)
        anchor_start = int(anchor.get("startMs", 0))
        anchor_end = max(anchor_start, int(anchor.get("endMs", anchor_start)))
        anchor_source = str(anchor.get("source", "")).upper()
        allowed_sources = {str(item).upper() for item in sources or []}
        candidates: list[tuple[int, int, int, float, str, dict[str, Any]]] = []
        for item in self.segments:
            if self._segment_key(item) == anchor_key:
                continue
            source = str(item.get("source", "")).upper()
            if allowed_sources and source not in allowed_sources:
                continue
            start = int(item.get("startMs", 0))
            end = max(start, int(item.get("endMs", start)))
            gap = max(0, start - anchor_end, anchor_start - end)
            if gap > self.context_window_ms:
                continue
            relevance = EvidenceRetriever.relevance(query, str(item.get("content", "")))
            candidates.append((
                int(source == anchor_source),
                gap,
                start,
                -relevance,
                self._segment_key(item),
                dict(item),
            ))
        candidates.sort(key=lambda item: item[:5])
        candidate_limit = max(self.context_per_requirement * 4, 4)
        return [item[5] for item in candidates[:candidate_limit]]

    @staticmethod
    def _requirement_status(
        requirement: dict[str, Any],
        matches: list[dict[str, Any]],
    ) -> dict[str, Any]:
        markers = [str(item) for item in requirement.get("markers", [])]
        marker_covered = not markers or any(
            CoverageAwareEvidenceRetriever._contains_marker(
                str(item.get("content", "")),
                markers,
            )
            for item in matches
        )
        max_term_coverage = max((
            EvidenceRetriever.score(
                str(requirement.get("query", "")),
                str(item.get("content", "")),
            )["termCoverage"]
            for item in matches[:5]
        ), default=0.0)
        satisfied = bool(matches) and marker_covered
        return {
            **requirement,
            "candidateCount": len(matches),
            "markerCovered": marker_covered,
            "maxTermCoverage": round(max_term_coverage, 4),
            "satisfied": satisfied,
            "status": "SATISFIED" if satisfied else "MISSING_COVERAGE",
        }

    @staticmethod
    def _merge_metadata(
        target: dict[str, Any],
        requirement_id: str,
        requirement_rank: int,
        reason: str,
    ) -> None:
        requirement_ids = target.setdefault("coverageRequirementIds", [])
        if requirement_id not in requirement_ids:
            requirement_ids.append(requirement_id)
        ranks = target.setdefault("coverageRequirementRanks", {})
        current = ranks.get(requirement_id)
        ranks[requirement_id] = requirement_rank if current is None else min(current, requirement_rank)
        reasons = target.setdefault("coverageSelectionReasons", [])
        if reason not in reasons:
            reasons.append(reason)

    def _gate_result(
        self,
        normalized_query: str,
        plan: dict[str, Any],
        anchors: list[dict[str, str]],
        missing_anchors: list[dict[str, str]],
    ) -> dict[str, Any]:
        requirements = [
            {
                **item,
                "candidateCount": 0,
                "markerCovered": False,
                "maxTermCoverage": 0.0,
                "satisfied": False,
                "status": "REQUIRED_ANCHOR_MISSING",
            }
            for item in plan["requirements"]
        ]
        sufficiency = {
            "decision": "INSUFFICIENT_EVIDENCE",
            "policy": "CURRENT_VIDEO_REQUIRED_ANCHORS_V2",
            "requiredAnchors": anchors,
            "missingRequiredAnchors": missing_anchors,
            "requirementCount": len(requirements),
            "satisfiedRequirementCount": 0,
            "fullyCovered": False,
            "requirements": requirements,
        }
        return {
            "ok": True,
            "query": normalized_query,
            "retrievalMode": "COVERAGE_AWARE_REQUIRED_ANCHOR_GATE",
            "matches": [],
            "matchedCount": 0,
            "fallbackToTimelineStart": False,
            "coveragePlan": plan,
            "evidenceSufficiency": sufficiency,
            "abstention": {
                "abstained": True,
                "policy": "CURRENT_VIDEO_REQUIRED_ANCHORS_V2",
                "missingRequiredAnchors": missing_anchors,
            },
        }

    def search(
        self,
        query: str,
        *,
        top_k: int = 8,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_query = " ".join(str(query).split())
        if not normalized_query:
            raise ValueError("检索词不能为空")
        top_k = max(1, min(int(top_k), 20))
        depth = max(top_k, self.candidate_depth)
        plan = plan_evidence_requirements(normalized_query)
        anchors = required_query_anchors(normalized_query)
        missing_anchors = self._missing_anchors(anchors)
        strict_anchor_gate = self._strict_anchor_gate(
            normalized_query,
            plan,
            anchors,
            missing_anchors,
        )
        if self.enable_anchor_gate and strict_anchor_gate:
            return self._gate_result(normalized_query, plan, anchors, missing_anchors)

        requirements = [dict(item) for item in plan["requirements"]]
        if len(requirements) == 1:
            base_result = self.base.search(normalized_query, top_k=depth, sources=sources)
            base_matches = [dict(item) for item in base_result.get("matches", [])]
            status = self._requirement_status(requirements[0], base_matches)
            sufficiency = {
                "decision": (
                    "SUFFICIENT_CANDIDATES_WITH_ANCHOR_VARIANCE"
                    if status["satisfied"] and missing_anchors
                    else "SUFFICIENT_CANDIDATES"
                    if status["satisfied"]
                    else "PARTIAL_EVIDENCE"
                ),
                "policy": "COVERAGE_REQUIREMENTS_AND_CURRENT_VIDEO_ANCHORS_V2",
                "requiredAnchors": anchors,
                "missingRequiredAnchors": missing_anchors,
                "requirementCount": 1,
                "satisfiedRequirementCount": int(status["satisfied"]),
                "fullyCovered": bool(status["satisfied"]),
                "requirements": [status],
            }
            return {
                **base_result,
                "retrievalMode": f"COVERAGE_AWARE_{base_result.get('retrievalMode', 'UNKNOWN')}",
                "matches": base_matches[:top_k],
                "coveragePlan": plan,
                "evidenceSufficiency": sufficiency,
                "abstention": {
                    "abstained": False,
                    "policy": "CURRENT_VIDEO_REQUIRED_ANCHORS_V2",
                    "missingRequiredAnchors": missing_anchors,
                },
            }

        requirement_results: list[tuple[dict[str, Any], dict[str, Any]]] = []
        statuses: list[dict[str, Any]] = []
        for requirement in requirements:
            result = self.base.search(str(requirement["query"]), top_k=depth, sources=sources)
            matches = [dict(item) for item in result.get("matches", [])]
            markers = [str(item) for item in requirement.get("markers", [])]
            if markers:
                allowed_sources = {str(item).upper() for item in sources or []}
                by_key = {self._segment_key(item): item for item in matches}
                explicit_markers: list[dict[str, Any]] = []
                for segment in self.segments:
                    if (
                        allowed_sources
                        and str(segment.get("source", "")).upper() not in allowed_sources
                    ):
                        continue
                    if not self._contains_marker(str(segment.get("content", "")), markers):
                        continue
                    candidate = dict(by_key.get(self._segment_key(segment)) or segment)
                    candidate.setdefault(
                        "score",
                        EvidenceRetriever.relevance(
                            str(requirement["query"]),
                            str(candidate.get("content", "")),
                        ),
                    )
                    explicit_markers.append(candidate)
                explicit_markers.sort(key=lambda item: (
                    int(str(item.get("source", "")).upper() != "ASR"),
                    int(item.get("startMs", 0)),
                    -float(item.get("score", 0.0)),
                ))
                explicit_keys = {self._segment_key(item) for item in explicit_markers}
                matches = [
                    *explicit_markers,
                    *(item for item in matches if self._segment_key(item) not in explicit_keys),
                ]
            requirement_results.append((requirement, {**result, "matches": matches}))
            statuses.append(self._requirement_status(requirement, matches))

        primary_result = self.base.search(normalized_query, top_k=depth, sources=sources)
        ranked: list[dict[str, Any]] = []
        by_key: dict[str, dict[str, Any]] = {}

        def add(item: dict[str, Any], requirement_id: str, rank: int, reason: str) -> None:
            key = self._segment_key(item)
            if key not in by_key:
                by_key[key] = dict(item)
                ranked.append(by_key[key])
            self._merge_metadata(by_key[key], requirement_id, rank, reason)

        for requirement, result in requirement_results:
            matches = result["matches"]
            if not matches:
                continue
            anchor = matches[0]
            requirement_id = str(requirement["requirementId"])
            add(anchor, requirement_id, 1, "REQUIREMENT_PRIMARY")
            added_context = 0
            for neighbor in self._neighbor_candidates(anchor, str(requirement["query"]), sources):
                if self._segment_key(neighbor) in by_key:
                    continue
                add(neighbor, requirement_id, 2, "REQUIREMENT_CONTEXT")
                added_context += 1
                if added_context >= self.context_per_requirement:
                    break

        for rank, item in enumerate(primary_result.get("matches", []), start=1):
            add(dict(item), "primary-query", rank, "PRIMARY_RANK")
        for requirement, result in requirement_results:
            requirement_id = str(requirement["requirementId"])
            for rank, item in enumerate(result["matches"][1:], start=2):
                add(item, requirement_id, rank, "REQUIREMENT_RANK")

        satisfied_count = sum(bool(item["satisfied"]) for item in statuses)
        fully_covered = satisfied_count == len(statuses)
        sufficiency = {
            "decision": "SUFFICIENT_CANDIDATES" if fully_covered else "PARTIAL_EVIDENCE",
            "policy": "COVERAGE_REQUIREMENTS_AND_CURRENT_VIDEO_ANCHORS_V2",
            "requiredAnchors": anchors,
            "missingRequiredAnchors": missing_anchors,
            "requirementCount": len(statuses),
            "satisfiedRequirementCount": satisfied_count,
            "fullyCovered": fully_covered,
            "requirements": statuses,
        }
        return {
            **primary_result,
            "retrievalMode": f"COVERAGE_AWARE_{primary_result.get('retrievalMode', 'UNKNOWN')}",
            "matches": ranked[:top_k],
            "matchedCount": len(ranked),
            "fallbackToTimelineStart": False,
            "coveragePlan": plan,
            "evidenceSufficiency": sufficiency,
            "abstention": {
                "abstained": False,
                "policy": "CURRENT_VIDEO_REQUIRED_ANCHORS_V2",
                "missingRequiredAnchors": missing_anchors,
            },
        }


@dataclass(frozen=True)
class RetrieverProfile:
    """Builds one retriever implementation for a fixed evidence snapshot."""

    profile_id: str
    retrieval_mode: str
    factory: Callable[[list[dict[str, Any]]], SearchRetriever]
    description: str
    settings: dict[str, Any] = field(default_factory=dict)

    def create(self, segments: list[dict[str, Any]]) -> SearchRetriever:
        return self.factory([dict(item) for item in segments])

    def metadata(self) -> dict[str, Any]:
        return {
            "profileId": self.profile_id,
            "retrievalMode": self.retrieval_mode,
            "description": self.description,
            **self.settings,
        }


LEXICAL_PROFILE = RetrieverProfile(
    profile_id="lexical-v1",
    retrieval_mode="HYBRID_LEXICAL_BASELINE",
    factory=EvidenceRetriever,
    description="精确短语、ASCII 词项与中文二元组的确定性词法基线",
)

CONTEXTUAL_LEXICAL_PROFILE = RetrieverProfile(
    profile_id="contextual-lexical-v2",
    retrieval_mode="CONTEXTUAL_HYBRID_LEXICAL_BASELINE",
    factory=lambda segments: ContextualEvidenceRetriever(
        segments,
        EvidenceRetriever(segments),
    ),
    description="在 lexical-v1 上增加显式时间锚点与按需邻接证据扩展",
    settings={
        "baseProfile": "lexical-v1",
        "timeWindowMs": 15000,
        "neighborWindowMs": 15000,
        "abstention": "disabled-without-dense-signal",
    },
)


def coverage_hybrid_profile(
    base_factory: Callable[[list[dict[str, Any]]], SearchRetriever],
    settings: dict[str, Any],
    *,
    profile_id: str = "coverage-aware-hybrid-v3",
    retrieval_mode: str = "COVERAGE_AWARE_HYBRID_LEXICAL_DENSE_RRF",
    description: str = "按证据需求拆分问题，并对多段证据做覆盖诊断与当前视频锚点拒答",
) -> RetrieverProfile:
    return RetrieverProfile(
        profile_id=profile_id,
        retrieval_mode=retrieval_mode,
        factory=lambda segments: CoverageAwareEvidenceRetriever(
            segments,
            ContextualEvidenceRetriever(
                segments,
                base_factory(segments),
                enable_abstention=False,
            ),
        ),
        description=description,
        settings={
            **settings,
            "baseProfile": "contextual-hybrid-bge-rrf-v2",
            "coverage": {
                "planner": "deterministic-clause-enumeration-comparison-v1",
                "candidateDepth": 12,
                "contextWindowMs": 15000,
                "contextPerRequirement": 1,
            },
            "abstention": {
                "policy": "CURRENT_VIDEO_REQUIRED_ANCHORS_V2",
                "status": "development-only",
            },
        },
    )

RETRIEVER_PROFILES: dict[str, RetrieverProfile] = {
    "lexical": LEXICAL_PROFILE,
    "contextual-lexical": CONTEXTUAL_LEXICAL_PROFILE,
}


def get_retriever_profile(name: str) -> RetrieverProfile:
    normalized = str(name).strip().lower()
    try:
        return RETRIEVER_PROFILES[normalized]
    except KeyError as exc:
        available = ", ".join(sorted(RETRIEVER_PROFILES))
        raise ValueError(f"未知 Retriever Profile：{name}；当前可用：{available}") from exc


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
