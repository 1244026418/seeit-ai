"""Structured Planner -> Retriever -> Verifier -> Writer -> Critic Agent."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from seeit.agent import AgentToolbox, build_agent_plan, is_summary_goal
from seeit.agent_graph import extract_goal_timestamps_ms
from seeit.retrieval import plan_evidence_requirements


PROMPT_VERSION = "video-evidence-agent-v5.2-grounded-synthesis"
MAX_REQUIREMENTS = 6
MAX_LEDGER_EVIDENCE = 18
MAX_EVIDENCE_CONTENT = 360


class StructuredAgentState(TypedDict, total=False):
    goal: str
    plan: dict[str, Any]
    evidence_plan: dict[str, Any]
    planner_mode: str
    evidence_ledger: dict[str, Any]
    verification: dict[str, Any]
    draft_report: dict[str, Any]
    critic_review: dict[str, Any]
    last_report: dict[str, Any] | None
    accepted: bool
    revision_count: int
    model_calls: int
    context_chars: dict[str, int]
    acceptance_mode: str
    verification_passes: int


class AgentQualityGateError(RuntimeError):
    """The bounded Agent could not produce even a safe terminal report."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _tool_schema(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


PLAN_SCHEMA = _tool_schema(
    "submit_evidence_plan",
    "提交只依赖用户问题语义的证据槽位计划，不回答问题。",
    {
        "type": "object",
        "properties": {
            "answerMode": {
                "type": "string",
                "enum": ["SINGLE", "MULTI_PART", "EXHAUSTIVE_LIST", "COMPARISON", "SEQUENCE", "TEMPORAL", "SYNTHESIS"],
            },
            "requirements": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_REQUIREMENTS,
                "items": {
                    "type": "object",
                    "properties": {
                        "requirementId": {"type": "string"},
                        "subQuestion": {"type": "string"},
                        "retrievalQuery": {"type": "string"},
                        "evidenceRole": {
                            "type": "string",
                            "enum": ["DIRECT", "COMPARE_LEFT", "COMPARE_RIGHT", "ENUMERATION", "EXCLUSION", "TEMPORAL", "SEQUENCE_STEP", "SYNTHESIS"],
                        },
                        "completionPolicy": {
                            "type": "string",
                            "enum": ["DIRECT", "ALL_RELEVANT", "COMPARE_SIDE", "EXCLUSION_SET", "TEMPORAL_WINDOW", "ORDERED_STEP", "REPRESENTATIVE"],
                        },
                        "expectedSources": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["ASR", "OCR"]},
                            "maxItems": 2,
                        },
                        "required": {"type": "boolean"},
                    },
                    "required": [
                        "requirementId",
                        "subQuestion",
                        "retrievalQuery",
                        "evidenceRole",
                        "completionPolicy",
                        "expectedSources",
                        "required",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["answerMode", "requirements"],
        "additionalProperties": False,
    },
)


VERIFICATION_SCHEMA = _tool_schema(
    "submit_evidence_verification",
    "逐槽位判断证据是否直接支持且足够完整，不使用外部知识。",
    {
        "type": "object",
        "properties": {
            "requirements": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_REQUIREMENTS,
                "items": {
                    "type": "object",
                    "properties": {
                        "requirementId": {"type": "string"},
                        "supported": {"type": "boolean"},
                        "complete": {"type": "boolean"},
                        "supportLevel": {
                            "type": "string",
                            "enum": ["DIRECT", "SYNTHESIS", "GROUNDED_INFERENCE", "NONE"],
                        },
                        "evidenceIds": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 10,
                        },
                        "missingInformation": {"type": "string"},
                        "contradictionEvidenceIds": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 6,
                        },
                    },
                    "required": [
                        "requirementId",
                        "supported",
                        "complete",
                        "supportLevel",
                        "evidenceIds",
                        "missingInformation",
                        "contradictionEvidenceIds",
                    ],
                    "additionalProperties": False,
                },
            },
            "overallSufficient": {"type": "boolean"},
            "shouldRefuse": {"type": "boolean"},
            "refusalReason": {"type": "string"},
        },
        "required": ["requirements", "overallSufficient", "shouldRefuse", "refusalReason"],
        "additionalProperties": False,
    },
)


REPORT_SCHEMA = _tool_schema(
    "submit_grounded_report",
    "使用 Evidence Ledger 中的 evidenceId 提交最终回答。",
    {
        "type": "object",
        "properties": {
            "answerable": {"type": "boolean"},
            "finalAnswer": {"type": "string", "minLength": 1, "maxLength": 2000},
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
            "conclusions": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 10,
            },
            "evidenceIds": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 18,
            },
            "suggestions": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 6,
            },
        },
        "required": ["answerable", "finalAnswer", "title", "conclusions", "evidenceIds", "suggestions"],
        "additionalProperties": False,
    },
)


CRITIC_SCHEMA = _tool_schema(
    "submit_critic_review",
    "审查最终回答是否完整、无矛盾、无外部推断且引用覆盖全部必要槽位。",
    {
        "type": "object",
        "properties": {
            "approved": {"type": "boolean"},
            "answerabilityCorrect": {"type": "boolean"},
            "allRequiredSlotsCovered": {"type": "boolean"},
            "contradictionFree": {"type": "boolean"},
            "externalKnowledgeFree": {"type": "boolean"},
            "citationIdsValid": {"type": "boolean"},
            "missingRequirementIds": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_REQUIREMENTS,
            },
            "unsupportedClaims": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
            },
            "contradictions": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
            },
            "revisionInstruction": {"type": "string"},
        },
        "required": [
            "approved",
            "answerabilityCorrect",
            "allRequiredSlotsCovered",
            "contradictionFree",
            "externalKnowledgeFree",
            "citationIdsValid",
            "missingRequirementIds",
            "unsupportedClaims",
            "contradictions",
            "revisionInstruction",
        ],
        "additionalProperties": False,
    },
)


def _forced_completion(
    provider: Any,
    messages: list[dict[str, Any]],
    schema: dict[str, Any],
) -> dict[str, Any]:
    completion = getattr(provider, "_completion", None)
    if not callable(completion):
        raise TypeError("结构化 Agent 需要支持 _completion(messages, tools, tool_choice) 的 Provider")
    name = str(schema["function"]["name"])
    choice = {"type": "function", "function": {"name": name}}
    phase = {
        "submit_evidence_plan": "PLANNER",
        "submit_evidence_verification": "VERIFIER",
        "submit_grounded_report": "WRITER",
        "submit_critic_review": "CRITIC",
    }.get(name, "STRUCTURED_AGENT")
    previous_phase = getattr(provider, "_agent_phase", None)
    setattr(provider, "_agent_phase", phase)
    try:
        try:
            return completion(messages, [schema], tool_choice=choice)
        except TypeError as exc:
            if "tool_choice" not in str(exc):
                raise
            return completion(messages, [schema])
    finally:
        if previous_phase is None:
            try:
                delattr(provider, "_agent_phase")
            except AttributeError:
                pass
        else:
            setattr(provider, "_agent_phase", previous_phase)


def _tool_arguments(message: dict[str, Any], expected_name: str) -> dict[str, Any] | None:
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        if str(function.get("name") or "") != expected_name:
            continue
        raw = function.get("arguments") or "{}"
        if isinstance(raw, dict):
            return dict(raw)
        try:
            parsed = json.loads(str(raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _fallback_evidence_plan(goal: str) -> dict[str, Any]:
    if is_summary_goal(goal):
        return _summary_evidence_plan(goal)
    raw = plan_evidence_requirements(goal)
    answer_mode = "SINGLE"
    if raw.get("strategy") == "COMPARISON_DECOMPOSITION":
        answer_mode = "COMPARISON"
    elif len(raw.get("requirements", [])) > 1:
        answer_mode = "MULTI_PART"
    elif re.search(r"哪些|有哪些|全部|所有|列出|来源|包括|分别有", goal):
        answer_mode = "EXHAUSTIVE_LIST"
    elif extract_goal_timestamps_ms(goal):
        answer_mode = "TEMPORAL"
    requirements = []
    for index, item in enumerate(raw.get("requirements", [])[:MAX_REQUIREMENTS], start=1):
        requirements.append({
            "requirementId": f"R{index}",
            "subQuestion": str(item.get("query") or goal),
            "retrievalQuery": str(item.get("query") or goal),
            "evidenceRole": (
                "ENUMERATION" if answer_mode == "EXHAUSTIVE_LIST"
                else "TEMPORAL" if answer_mode == "TEMPORAL"
                else "COMPARE_LEFT" if answer_mode == "COMPARISON" and index == 1
                else "COMPARE_RIGHT" if answer_mode == "COMPARISON"
                else "DIRECT"
            ),
            "completionPolicy": (
                "ALL_RELEVANT" if answer_mode == "EXHAUSTIVE_LIST"
                else "TEMPORAL_WINDOW" if answer_mode == "TEMPORAL"
                else "COMPARE_SIDE" if answer_mode == "COMPARISON"
                else "DIRECT"
            ),
            "expectedSources": ["ASR", "OCR"],
            "required": True,
        })
    if not requirements:
        requirements = [{
            "requirementId": "R1",
            "subQuestion": goal,
            "retrievalQuery": goal,
            "evidenceRole": "DIRECT",
            "completionPolicy": "DIRECT",
            "expectedSources": ["ASR", "OCR"],
            "required": True,
        }]
    return {"answerMode": answer_mode, "requirements": requirements}


def _summary_evidence_plan(goal: str) -> dict[str, Any]:
    return {
        "answerMode": "SYNTHESIS",
        "requirements": [{
            "requirementId": "summary",
            "subQuestion": "概括视频主要内容、核心观点和关键示例",
            "retrievalQuery": "视频主题 核心观点 关键内容 示例 总结",
            "evidenceRole": "SYNTHESIS",
            "completionPolicy": "REPRESENTATIVE",
            "expectedSources": ["ASR", "OCR"],
            "required": True,
        }],
    }


def _normalize_evidence_plan(value: dict[str, Any] | None, goal: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    allowed_modes = {"SINGLE", "MULTI_PART", "EXHAUSTIVE_LIST", "COMPARISON", "SEQUENCE", "TEMPORAL", "SYNTHESIS"}
    answer_mode = str(value.get("answerMode") or "").upper()
    if answer_mode not in allowed_modes:
        return None
    raw_requirements = value.get("requirements")
    if not isinstance(raw_requirements, list) or not raw_requirements:
        return None
    single_target_contrast = bool(re.search(
        r"(?:另一条|不同的).{0,10}(?:路|路径|方案|方法).{0,10}(?:是什么|为何|哪)",
        goal,
    ))
    single_target_sequence = bool(re.search(
        r"之后(?:的)?(?:下一个)?任务.{0,8}(?:是什么|是哪)",
        goal,
    ))
    if single_target_contrast or single_target_sequence:
        answer_mode = "SINGLE"
        target_pattern = r"之后|下一个" if single_target_sequence else r"另一条|不同"
        targeted = [
            item for item in raw_requirements
            if isinstance(item, dict)
            and re.search(target_pattern, str(item.get("subQuestion") or item.get("retrievalQuery") or ""))
        ]
        raw_requirements = (targeted or raw_requirements[-1:])[:1]
    requirements = []
    seen: set[str] = set()
    allowed_roles = {"DIRECT", "COMPARE_LEFT", "COMPARE_RIGHT", "ENUMERATION", "EXCLUSION", "TEMPORAL", "SEQUENCE_STEP", "SYNTHESIS"}
    allowed_policies = {"DIRECT", "ALL_RELEVANT", "COMPARE_SIDE", "EXCLUSION_SET", "TEMPORAL_WINDOW", "ORDERED_STEP", "REPRESENTATIVE"}
    for index, item in enumerate(raw_requirements[:MAX_REQUIREMENTS], start=1):
        if not isinstance(item, dict):
            continue
        requirement_id = re.sub(r"[^A-Za-z0-9_-]", "", str(item.get("requirementId") or ""))[:32]
        if not requirement_id or requirement_id in seen:
            requirement_id = f"R{index}"
        seen.add(requirement_id)
        sub_question = " ".join(str(item.get("subQuestion") or "").split())[:300]
        retrieval_query = " ".join(str(item.get("retrievalQuery") or sub_question or goal).split())[:300]
        if not sub_question or not retrieval_query:
            continue
        role = str(item.get("evidenceRole") or "DIRECT").upper()
        policy = str(item.get("completionPolicy") or "DIRECT").upper()
        sources = [
            str(source).upper() for source in item.get("expectedSources", [])
            if str(source).upper() in {"ASR", "OCR"}
        ]
        normalized_role = role if role in allowed_roles else "DIRECT"
        normalized_policy = policy if policy in allowed_policies else "DIRECT"
        if answer_mode == "SINGLE" and len(raw_requirements) == 1:
            normalized_role = "DIRECT"
            normalized_policy = "DIRECT"
            sub_question = " ".join(goal.split())[:300]
        requirements.append({
            "requirementId": requirement_id,
            "subQuestion": sub_question,
            "retrievalQuery": retrieval_query,
            "evidenceRole": normalized_role,
            "completionPolicy": normalized_policy,
            "expectedSources": list(dict.fromkeys(sources)) or ["ASR", "OCR"],
            "required": bool(item.get("required", True)),
        })
    return {"answerMode": answer_mode, "requirements": requirements} if requirements else None


def _plan_node(provider: Any, state: StructuredAgentState) -> dict[str, Any]:
    goal = " ".join(str(state.get("goal") or "").split())
    system = (
        "你是视频证据规划器，只分析问题结构，不回答问题，也不使用外部知识。"
        "把每个必须独立验证的子问题拆成 requirement。比较题必须覆盖两侧；多问句逐问拆分；"
        "不知道枚举项数量时保留一个 ALL_RELEVANT 槽位，不能虚构条目。"
        "涉及‘有但没有/除了’时使用 EXCLUSION_SET；时间指代使用 TEMPORAL_WINDOW。"
    )
    prompt = system + "\n用户问题：" + goal
    if is_summary_goal(goal):
        evidence_plan = _summary_evidence_plan(goal)
        planner_mode = "DETERMINISTIC_SUMMARY"
        planner_calls = 0
    else:
        message = _forced_completion(
            provider,
            [{"role": "system", "content": system}, {"role": "user", "content": goal}],
            PLAN_SCHEMA,
        )
        planned = _normalize_evidence_plan(_tool_arguments(message, "submit_evidence_plan"), goal)
        planner_mode = "LLM_STRUCTURED" if planned else "DETERMINISTIC_FALLBACK"
        evidence_plan = planned or _fallback_evidence_plan(goal)
        planner_calls = 1
    return {
        "plan": build_agent_plan(goal),
        "evidence_plan": evidence_plan,
        "planner_mode": planner_mode,
        "model_calls": int(state.get("model_calls", 0)) + planner_calls,
        "context_chars": {**state.get("context_chars", {}), "planner": len(prompt)},
    }


def _segment_key(item: dict[str, Any]) -> str:
    return str(item.get("segmentId") or f"{item.get('source')}:{item.get('startMs')}:{item.get('endMs')}")


def _retrieve_node(toolbox: AgentToolbox, state: StructuredAgentState) -> dict[str, Any]:
    plan = state["evidence_plan"]
    goal = str(state.get("goal") or "")
    by_segment: dict[str, dict[str, Any]] = {}
    requirement_candidates: dict[str, list[str]] = {}
    raw_candidate_counts: dict[str, int] = {}

    def add_segment(item: dict[str, Any], requirement_id: str) -> None:
        key = _segment_key(item)
        if key not in by_segment and len(by_segment) < MAX_LEDGER_EVIDENCE:
            evidence_id = f"E{len(by_segment) + 1:03d}"
            by_segment[key] = {
                "evidenceId": evidence_id,
                "segmentId": item.get("segmentId"),
                "source": str(item.get("source", "ASR")).upper(),
                "startMs": max(0, int(item.get("startMs", 0))),
                "endMs": max(0, int(item.get("endMs", item.get("startMs", 0)))),
                "content": " ".join(str(item.get("content", "")).split())[:MAX_EVIDENCE_CONTENT],
                "requirementIds": [],
            }
        stored = by_segment.get(key)
        if not stored:
            return
        if requirement_id not in stored["requirementIds"]:
            stored["requirementIds"].append(requirement_id)
        ids = requirement_candidates.setdefault(requirement_id, [])
        if stored["evidenceId"] not in ids:
            ids.append(stored["evidenceId"])

    requirements_to_search = plan["requirements"]
    if plan.get("answerMode") == "SYNTHESIS":
        overview = toolbox.execute("get_timeline_overview", {"max_segments": MAX_LEDGER_EVIDENCE})
        overview_segments = [dict(item) for item in overview.get("segments", [])]
        for requirement in plan["requirements"]:
            requirement_id = str(requirement["requirementId"])
            raw_candidate_counts[requirement_id] = len(overview_segments)
            for item in overview_segments:
                add_segment(item, requirement_id)
        requirements_to_search = []

    for requirement in requirements_to_search:
        requirement_id = str(requirement["requirementId"])
        result = toolbox.execute("search_timeline", {
            "query": str(requirement["retrievalQuery"]),
            "top_k": 8,
            "sources": requirement.get("expectedSources", []),
        })
        matches = [dict(item) for item in result.get("matches", [])]
        raw_candidate_counts[requirement_id] = len(matches)
        policy = str(requirement.get("completionPolicy") or "DIRECT")
        match_limit = 8 if policy == "DIRECT" else 4
        for item in matches[:match_limit]:
            add_segment(item, requirement_id)
        if matches:
            if policy in {"ALL_RELEVANT", "EXCLUSION_SET"}:
                before_ms, after_ms, window_limit = 60000, 30000, 12
            else:
                before_ms, after_ms, window_limit = 30000, 15000, 8
            anchors = [matches[0]]
            if policy == "DIRECT":
                best_asr = next(
                    (item for item in matches[1:] if str(item.get("source")).upper() == "ASR"),
                    None,
                )
                if best_asr is not None:
                    anchors.append(best_asr)
            for anchor in anchors:
                anchor_ms = int(anchor.get("startMs", 0))
                window = toolbox.execute("get_evidence_window", {
                    "timestamp_ms": anchor_ms,
                    "before_ms": before_ms,
                    "after_ms": after_ms,
                })
                segments = [dict(item) for item in window.get("segments", [])]
                if policy == "DIRECT":
                    segments = sorted(
                        segments,
                        key=lambda item: (
                            abs(int(item.get("startMs", 0)) - anchor_ms),
                            int(item.get("startMs", 0)),
                        ),
                    )[:window_limit]
                    segments.sort(key=lambda item: (int(item.get("startMs", 0)), str(item.get("segmentId"))))
                else:
                    segments = segments[:window_limit]
                for item in segments:
                    add_segment(item, requirement_id)

    time_hints = extract_goal_timestamps_ms(goal)
    for timestamp_ms in time_hints:
        window = toolbox.execute("get_evidence_window", {
            "timestamp_ms": timestamp_ms,
            "before_ms": 15000,
            "after_ms": 15000,
        })
        temporal_ids = [
            str(item["requirementId"]) for item in plan["requirements"]
            if item.get("completionPolicy") == "TEMPORAL_WINDOW"
        ] or [str(plan["requirements"][0]["requirementId"])]
        for requirement_id in temporal_ids:
            for item in window.get("segments", [])[:8]:
                add_segment(dict(item), requirement_id)

    evidence = list(by_segment.values())
    ledger_requirements = [
        {
            **requirement,
            "candidateEvidenceIds": requirement_candidates.get(str(requirement["requirementId"]), []),
            "rawCandidateCount": raw_candidate_counts.get(str(requirement["requirementId"]), 0),
        }
        for requirement in plan["requirements"]
    ]
    ledger = {
        "question": goal,
        "answerMode": plan["answerMode"],
        "requirements": ledger_requirements,
        "evidence": evidence,
        "statistics": {
            "requirementCount": len(ledger_requirements),
            "rawCandidateCount": sum(raw_candidate_counts.values()),
            "compactEvidenceCount": len(evidence),
            "timeHintCount": len(time_hints),
        },
    }
    ledger["statistics"]["serializedChars"] = len(_json(ledger))
    return {"evidence_ledger": ledger}


def _normalize_verification(
    value: dict[str, Any] | None,
    plan: dict[str, Any],
    ledger: dict[str, Any],
) -> dict[str, Any]:
    known_ids = {str(item["evidenceId"]) for item in ledger.get("evidence", [])}
    submitted = {
        str(item.get("requirementId")): item
        for item in (value or {}).get("requirements", [])
        if isinstance(item, dict)
    }
    requirements = []
    for requirement in plan["requirements"]:
        requirement_id = str(requirement["requirementId"])
        raw = submitted.get(requirement_id, {})
        evidence_ids = [
            str(item) for item in raw.get("evidenceIds", [])
            if str(item) in known_ids
        ]
        contradiction_ids = [
            str(item) for item in raw.get("contradictionEvidenceIds", [])
            if str(item) in known_ids
        ]
        supported = bool(raw.get("supported")) and bool(evidence_ids)
        complete = bool(raw.get("complete")) and supported and not contradiction_ids
        raw_support_level = str(raw.get("supportLevel") or "").upper()
        allowed_support_levels = {"DIRECT", "SYNTHESIS", "GROUNDED_INFERENCE", "NONE"}
        if not supported:
            support_level = "NONE"
        elif raw_support_level in allowed_support_levels - {"NONE"}:
            support_level = raw_support_level
        elif plan.get("answerMode") == "SYNTHESIS":
            support_level = "SYNTHESIS"
        else:
            support_level = "DIRECT"
        explanation = str(raw.get("missingInformation") or "")[:500]
        consistency_repaired = False
        if not supported and not contradiction_ids and (
            "这直接回答了问题" in explanation
            or "证据直接支持" in explanation
            or "答案正确、证据充分" in explanation
        ):
            repaired_ids = [
                evidence_id
                for evidence_id in re.findall(r"\bE\d{3}\b", explanation)
                if evidence_id in known_ids
            ]
            if repaired_ids:
                evidence_ids = list(dict.fromkeys(repaired_ids))
                supported = True
                complete = True
                support_level = "DIRECT"
                explanation = ""
                consistency_repaired = True
        requirements.append({
            "requirementId": requirement_id,
            "supported": supported,
            "complete": complete,
            "supportLevel": support_level,
            "evidenceIds": list(dict.fromkeys(evidence_ids)),
            "missingInformation": explanation,
            "contradictionEvidenceIds": list(dict.fromkeys(contradiction_ids)),
            "consistencyRepaired": consistency_repaired,
        })
    required_ids = {
        str(item["requirementId"]) for item in plan["requirements"] if item.get("required", True)
    }
    complete_ids = {item["requirementId"] for item in requirements if item["complete"]}
    overall_sufficient = bool(required_ids) and required_ids <= complete_ids
    refusal_reason = "" if overall_sufficient else str(
        (value or {}).get("refusalReason") or "必要证据槽位不完整"
    )[:500]
    return {
        "policy": "LLM_SLOT_ENTAILMENT_WITH_DETERMINISTIC_ID_GATE_V1",
        "requirements": requirements,
        "requiredRequirementIds": sorted(required_ids),
        "completeRequirementIds": sorted(complete_ids),
        "overallSufficient": overall_sufficient,
        "fullyCovered": overall_sufficient,
        "shouldRefuse": not overall_sufficient,
        "refusalReason": refusal_reason,
    }


def _verification_needs_deictic_audit(
    plan: dict[str, Any],
    ledger: dict[str, Any],
    verification: dict[str, Any],
) -> bool:
    evidence_by_id = {
        str(item.get("evidenceId")): item for item in ledger.get("evidence", [])
    }
    requirements_by_id = {
        str(item.get("requirementId")): item for item in plan.get("requirements", [])
    }
    for item in verification.get("requirements", []):
        requirement = requirements_by_id.get(str(item.get("requirementId")), {})
        if requirement.get("completionPolicy") != "DIRECT" or not item.get("complete"):
            continue
        cited_ids = [str(value) for value in item.get("evidenceIds", [])]
        cited_content = " ".join(
            str(evidence_by_id.get(evidence_id, {}).get("content", "")) for evidence_id in cited_ids
        )
        candidate_ids = [str(value) for value in requirement.get("candidateEvidenceIds", [])]
        if (
            re.search(r"这(?:篇|个|项|种)|该(?:篇|项|技术|方法|论文)", cited_content)
            and any(evidence_id not in cited_ids for evidence_id in candidate_ids)
        ):
            return True
    return False


def _verify_node(provider: Any, state: StructuredAgentState) -> dict[str, Any]:
    plan = state["evidence_plan"]
    ledger = state["evidence_ledger"]
    system = (
        "你是视频证据 Verifier。只能判断给定 Evidence Ledger 是否直接支持每个 requirement。"
        "相关不等于支持；标题、主题词或常识不能代替答案。ALL_RELEVANT/EXCLUSION_SET 必须检查候选是否足以"
        "支撑完整枚举或差集，证据互相矛盾时 complete=false。不得创造 evidenceId。"
        "expectedSources 只是检索提示，不代表 ASR 与 OCR 必须同时命中。"
        "对 DIRECT 槽位，只要一条 Evidence 明确包含问题主体、关系和答案，就必须标记 supported=true、"
        "complete=true 并引用该 evidenceId；不能因为还有其他噪声候选或没有第二种来源而拒答。"
        "严格按用户实际提问判断完整性，不得把‘问什么技术’扩大为同时要求论文名称等未提问槽位。"
        "可以结合相邻上下文理解明显的 ASR/OCR 同音错字、音译和断句错误，但不得补充证据中完全不存在的名称或数值。"
        "当问题与相邻证据已建立同一对象时，品牌全称、简称和版本名（例如品牌名与其 K3 版本）属于同一实体，"
        "不能仅因一处使用简称、另一处使用版本名而判定无关联。"
        "SYNTHESIS/REPRESENTATIVE 是视频级概括：只要跨时间位置的多条代表性证据能建立连贯主题和主要观点，"
        "即可 complete=true、supportLevel=SYNTHESIS；不要求穷尽视频每句话，也不能凭空增加行动建议槽位。"
        "当证据没有逐字给出答案、但结论可以完全由给定证据合理推出时，可标记 supportLevel=GROUNDED_INFERENCE；"
        "这种推断不得依赖视频外常识。直接陈述使用 DIRECT，完全不支持使用 NONE。"
    )
    payload = {"plan": plan, "ledger": ledger}
    prompt = system + "\n" + _json(payload)
    message = _forced_completion(
        provider,
        [{"role": "system", "content": system}, {"role": "user", "content": _json(payload)}],
        VERIFICATION_SCHEMA,
    )
    raw = _tool_arguments(message, "submit_evidence_verification")
    verification = _normalize_verification(raw, plan, ledger)
    verification_passes = 1
    context_chars = {**state.get("context_chars", {}), "verifier1": len(prompt)}
    should_audit = (
        not verification["overallSufficient"]
        or _verification_needs_deictic_audit(plan, ledger, verification)
    )
    if should_audit and ledger.get("evidence"):
        audit_system = (
            "你是视频证据 Verifier 的独立审计员。首次审查可能存在假阴性或悬空引用，请从零重新核对，不能沿用首次结论。"
            "只按用户实际提问判断，不增加用户未要求的槽位。DIRECT 问题允许一条证据直接回答；"
            "允许组合时间相邻的 Evidence 解析‘这篇、这个、另一条路’等指代，也允许结合上下文理解明显的"
            "ASR/OCR 同音错字、音译和断句错误。相关不等于支持，仍不得使用外部知识或创造 evidenceId。"
            "当问题与相邻证据已建立同一对象时，品牌全称、简称和版本名属于可解析的同一实体。"
            "如果证据只说‘这篇论文的技术、这个方法、该技术’，必须继续组合相邻 Evidence 找到被指代的"
            "具体名称，并同时引用指代句与名称所在证据，不能只引用悬空指代句。"
            "如果候选确实包含主体、关系和答案，必须 supported=true、complete=true 并列出证据 ID。"
            "允许仅由现有证据推出的 GROUNDED_INFERENCE，但不能使用外部常识；视频级概括使用 SYNTHESIS，"
            "代表性证据足以覆盖主要主题时不应因无法穷尽全部内容而拒答。"
        )
        audit_payload = {
            "question": state.get("goal"),
            "plan": plan,
            "ledger": ledger,
            "firstVerification": verification,
        }
        audit_prompt = audit_system + "\n" + _json(audit_payload)
        audit_message = _forced_completion(
            provider,
            [
                {"role": "system", "content": audit_system},
                {"role": "user", "content": _json(audit_payload)},
            ],
            VERIFICATION_SCHEMA,
        )
        audited = _normalize_verification(
            _tool_arguments(audit_message, "submit_evidence_verification"),
            plan,
            ledger,
        )
        verification_passes = 2
        context_chars["verifier2"] = len(audit_prompt)
        if audited["overallSufficient"]:
            verification = audited
            verification["policy"] = "LLM_SLOT_ENTAILMENT_WITH_FALSE_NEGATIVE_AUDIT_V2"
        verification["auditPerformed"] = True
    return {
        "verification": verification,
        "verification_passes": verification_passes,
        "model_calls": int(state.get("model_calls", 0)) + verification_passes,
        "context_chars": context_chars,
    }


def _normalize_draft(value: dict[str, Any] | None) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    return {
        "answerable": bool(value.get("answerable")),
        "finalAnswer": " ".join(str(value.get("finalAnswer") or "").split())[:2000],
        "title": " ".join(str(value.get("title") or "视频分析报告").split())[:120],
        "conclusions": [
            " ".join(str(item).split())[:1000]
            for item in value.get("conclusions", [])[:10]
            if " ".join(str(item).split())
        ],
        "evidenceIds": list(dict.fromkeys(
            str(item) for item in value.get("evidenceIds", [])[:18] if str(item)
        )),
        "suggestions": [
            " ".join(str(item).split())[:1000]
            for item in value.get("suggestions", [])[:6]
            if " ".join(str(item).split())
        ],
    }


def _write_node(provider: Any, state: StructuredAgentState, *, revision: bool = False) -> dict[str, Any]:
    system = (
        "你是视频证据 Writer。只能使用 Verifier 认可的 evidenceId。"
        "overallSufficient=false 时必须 answerable=false、evidenceIds=[] 并明确拒答；"
        "overallSufficient=true 时必须逐项回答所有 required requirement，不能增加 Ledger 外事实。"
        "回答枚举/比较题时必须覆盖全部槽位，且不要把建议、标题或相关概念推断成事实。"
        "如果证据明确给出了技术、方法或模型的名称，finalAnswer 必须保留该规范名称，不能只改述工作原理。"
        "对上下文已能唯一确认的 ASR/OCR 同音错字或音译错误，应在答案中改写为规范术语"
        "（例如‘善象文’写为‘上下文’、‘模太/多摩太’写为‘模态/多模态’），引用仍保留原始证据。"
        "SYNTHESIS 是对多条代表性证据的概括，不等于无依据猜测；应直接总结视频主要内容并引用关键时间点。"
        "如果 Verifier 将任一必要槽位标为 GROUNDED_INFERENCE，可以回答，但必须明确说明"
        "‘此答案为基于视频证据的推测，视频没有明确说明’，不得伪装成视频原话。"
    )
    payload: dict[str, Any] = {
        "question": state.get("goal"),
        "plan": state["evidence_plan"],
        "verification": state["verification"],
        "ledger": state["evidence_ledger"],
    }
    if revision:
        payload["previousDraft"] = state.get("draft_report")
        payload["criticReview"] = state.get("critic_review")
    prompt = system + "\n" + _json(payload)
    message = _forced_completion(
        provider,
        [{"role": "system", "content": system}, {"role": "user", "content": _json(payload)}],
        REPORT_SCHEMA,
    )
    draft = _normalize_draft(_tool_arguments(message, "submit_grounded_report"))
    has_grounded_inference = any(
        item.get("complete") and item.get("supportLevel") == "GROUNDED_INFERENCE"
        for item in state["verification"].get("requirements", [])
    )
    inference_disclaimer = "此答案为基于视频证据的推测，视频没有明确说明。"
    if draft.get("answerable") and has_grounded_inference and inference_disclaimer not in draft["finalAnswer"]:
        draft["finalAnswer"] = (draft["finalAnswer"].rstrip("。") + "。" + inference_disclaimer)[:2000]
    if state["verification"].get("shouldRefuse") and not draft.get("answerable"):
        canonical_refusal = "视频未提供足够证据，无法从视频确定答案。"
        draft["finalAnswer"] = canonical_refusal
    key = "revisionWriter" if revision else "writer"
    return {
        "draft_report": draft,
        "revision_count": int(state.get("revision_count", 0)) + int(revision),
        "model_calls": int(state.get("model_calls", 0)) + 1,
        "context_chars": {**state.get("context_chars", {}), key: len(prompt)},
    }


def _resolved_citations(draft: dict[str, Any], ledger: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {str(item["evidenceId"]): item for item in ledger.get("evidence", [])}
    citations = []
    for evidence_id in draft.get("evidenceIds", []):
        item = by_id.get(str(evidence_id))
        if not item:
            continue
        citations.append({
            "evidenceId": str(evidence_id),
            "timestampMs": int(item.get("startMs", 0)),
            "source": str(item.get("source", "ASR")),
            "content": str(item.get("content", "")),
        })
    return citations


def _normalize_critic(value: dict[str, Any] | None) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    return {
        "approved": bool(value.get("approved")),
        "answerabilityCorrect": bool(value.get("answerabilityCorrect")),
        "allRequiredSlotsCovered": bool(value.get("allRequiredSlotsCovered")),
        "contradictionFree": bool(value.get("contradictionFree")),
        "externalKnowledgeFree": bool(value.get("externalKnowledgeFree")),
        "citationIdsValid": bool(value.get("citationIdsValid")),
        "missingRequirementIds": [str(item) for item in value.get("missingRequirementIds", [])[:MAX_REQUIREMENTS]],
        "unsupportedClaims": [str(item)[:500] for item in value.get("unsupportedClaims", [])[:8]],
        "contradictions": [str(item)[:500] for item in value.get("contradictions", [])[:8]],
        "revisionInstruction": str(value.get("revisionInstruction") or "")[:1000],
    }


def _critic_node(provider: Any, toolbox: AgentToolbox, state: StructuredAgentState) -> dict[str, Any]:
    draft = state["draft_report"]
    plan = state["evidence_plan"]
    ledger = state["evidence_ledger"]
    verification = state["verification"]
    system = (
        "你是独立视频答案 Critic。逐项检查：回答性是否正确、所有必要槽位是否回答、"
        "是否与证据或排除条件矛盾、是否加入外部知识、evidenceId 是否真正支持对应结论。"
        "引用存在但不蕴含答案时必须拒绝。不可回答题必须明确拒答且不能引用证据；"
        "此时槽位没有被证据覆盖是正确拒答的原因，allRequiredSlotsCovered 可以为 false，"
        "但 approved 和 answerabilityCorrect 应在拒答合规时为 true。"
        "把上下文中可唯一确认的 ASR/OCR 同音错字改成规范术语不属于外部知识，不能仅因此拒绝答案。"
        "SYNTHESIS 可以基于跨时间位置的代表性证据概括主题；GROUNDED_INFERENCE 只有在答案明确标注"
        "视频未直接说明且推断完全可由引用证据推出时才允许通过。"
    )
    payload = {
        "question": state.get("goal"),
        "plan": plan,
        "verification": verification,
        "draft": draft,
        "citedEvidence": _resolved_citations(draft, ledger),
    }
    prompt = system + "\n" + _json(payload)
    message = _forced_completion(
        provider,
        [{"role": "system", "content": system}, {"role": "user", "content": _json(payload)}],
        CRITIC_SCHEMA,
    )
    review = _normalize_critic(_tool_arguments(message, "submit_critic_review"))

    known_ids = {str(item["evidenceId"]) for item in ledger.get("evidence", [])}
    cited_ids = set(draft.get("evidenceIds", []))
    ids_valid = cited_ids <= known_ids
    slot_coverage = True
    for item in verification.get("requirements", []):
        if item["requirementId"] not in verification.get("requiredRequirementIds", []):
            continue
        if not set(item.get("evidenceIds", [])) & cited_ids:
            slot_coverage = False
            break
    if draft.get("answerable"):
        deterministic_gate = (
            bool(verification.get("overallSufficient"))
            and ids_valid
            and bool(cited_ids)
            and slot_coverage
            and bool(draft.get("finalAnswer"))
        )
    else:
        deterministic_gate = (
            bool(verification.get("shouldRefuse"))
            and not cited_ids
            and bool(draft.get("finalAnswer"))
        )
    model_gate_fields = [
        review["approved"],
        review["answerabilityCorrect"],
        review["contradictionFree"],
        review["externalKnowledgeFree"],
        review["citationIdsValid"],
    ]
    if draft.get("answerable"):
        model_gate_fields.append(review["allRequiredSlotsCovered"])
    model_gate = all(model_gate_fields)
    review["deterministicGatePassed"] = deterministic_gate
    review["slotCitationCoveragePassed"] = slot_coverage
    review["knownEvidenceIdsPassed"] = ids_valid

    accepted = False
    acceptance_mode = "MODEL_CRITIC"
    result: dict[str, Any] | None = None
    bounded_deterministic_accept = deterministic_gate and int(state.get("revision_count", 0)) >= 1
    if deterministic_gate and (model_gate or bounded_deterministic_accept):
        result = toolbox.execute("generate_report", {
            "answerable": draft["answerable"],
            "finalAnswer": draft["finalAnswer"],
            "title": draft["title"],
            "conclusions": draft["conclusions"] or [draft["finalAnswer"]],
            "evidence": _resolved_citations(draft, ledger),
            "suggestions": draft["suggestions"],
        })
        accepted = bool(result.get("accepted"))
        if bounded_deterministic_accept and not model_gate:
            acceptance_mode = "DETERMINISTIC_AFTER_CRITIC_REVISION"
    return {
        "critic_review": review,
        "last_report": result,
        "accepted": accepted,
        "acceptance_mode": acceptance_mode if accepted else "PENDING",
        "model_calls": int(state.get("model_calls", 0)) + 1,
        "context_chars": {**state.get("context_chars", {}), f"critic{int(state.get('revision_count', 0))}": len(prompt)},
    }


def _route_after_critic(state: StructuredAgentState) -> str:
    if state.get("accepted"):
        return END
    if int(state.get("revision_count", 0)) < 1:
        return "revise_report"
    return END


def run_structured_evidence_agent(
    provider: Any,
    toolbox: AgentToolbox,
    goal: str,
) -> dict[str, Any]:
    workflow = StateGraph(StructuredAgentState)
    workflow.add_node("structured_planner", lambda state: _plan_node(provider, state))
    workflow.add_node("retrieve_evidence_slots", lambda state: _retrieve_node(toolbox, state))
    workflow.add_node("verify_evidence_ledger", lambda state: _verify_node(provider, state))
    workflow.add_node("write_grounded_report", lambda state: _write_node(provider, state))
    workflow.add_node("critic_review", lambda state: _critic_node(provider, toolbox, state))
    workflow.add_node("revise_report", lambda state: _write_node(provider, state, revision=True))
    workflow.add_edge(START, "structured_planner")
    workflow.add_edge("structured_planner", "retrieve_evidence_slots")
    workflow.add_edge("retrieve_evidence_slots", "verify_evidence_ledger")
    workflow.add_edge("verify_evidence_ledger", "write_grounded_report")
    workflow.add_edge("write_grounded_report", "critic_review")
    workflow.add_conditional_edges("critic_review", _route_after_critic)
    workflow.add_edge("revise_report", "critic_review")
    graph = workflow.compile()

    final_state = graph.invoke(
        {"goal": " ".join(str(goal).split()), "revision_count": 0, "model_calls": 0},
        config={"recursion_limit": 12},
    )
    last_report = final_state.get("last_report")
    if not last_report or not final_state.get("accepted"):
        fallback_answer = "视频未提供足够证据，无法从视频确定答案。"
        fallback_report = toolbox.execute("generate_report", {
            "answerable": False,
            "finalAnswer": fallback_answer,
            "title": "视频证据校验未通过",
            "conclusions": [fallback_answer],
            "evidence": [],
            "suggestions": [],
        })
        if not fallback_report.get("accepted"):
            raise AgentQualityGateError("结构化视频证据 Agent 未通过槽位完整性与 Critic 校验")
        final_state["last_report"] = fallback_report
        final_state["accepted"] = True
        final_state["acceptance_mode"] = "SAFE_REFUSAL_AFTER_CRITIC_REVISION"
        last_report = fallback_report
    ledger = final_state.get("evidence_ledger") or {}
    return {
        **last_report,
        "agentGraph": {
            "framework": "LangGraph",
            "promptVersion": PROMPT_VERSION,
            "nodes": [
                "structured_planner",
                "retrieve_evidence_slots",
                "verify_evidence_ledger",
                "write_grounded_report",
                "critic_review",
                "revise_report",
            ],
            "plannerMode": final_state.get("planner_mode"),
            "intent": (final_state.get("plan") or {}).get("intent"),
            "evidencePlan": final_state.get("evidence_plan"),
            "evidenceLedger": {
                "requirements": ledger.get("requirements", []),
                "statistics": ledger.get("statistics", {}),
            },
            "evidenceVerification": final_state.get("verification"),
            "verificationPasses": int(final_state.get("verification_passes", 1)),
            "criticReview": final_state.get("critic_review"),
            "acceptanceMode": final_state.get("acceptance_mode", "MODEL_CRITIC"),
            "revisionCount": int(final_state.get("revision_count", 0)),
            "modelCalls": int(final_state.get("model_calls", 0)),
            "contextChars": final_state.get("context_chars", {}),
        },
    }
