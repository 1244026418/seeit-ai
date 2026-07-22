from __future__ import annotations

import json
from typing import Any

from seeit.agent import AgentToolbox
from seeit.agent_structured import (
    _normalize_evidence_plan,
    _normalize_verification,
    _retrieve_node,
    _verification_needs_deictic_audit,
    run_structured_evidence_agent,
)


def tool_response(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": None,
        "tool_calls": [{
            "id": f"call-{name}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        }],
    }


def toolbox(segments: list[dict[str, Any]]) -> AgentToolbox:
    return AgentToolbox(
        metadata={"mediaId": 1, "filename": "comparison.mp4", "status": "COMPLETED"},
        segments=segments,
        normalize_report=lambda value: value,
        evaluate_report=lambda report, _: {
            "structuredValid": bool(report.get("finalAnswer")),
            "evidenceSupportRate": 1.0,
            "criticPassed": bool(report.get("finalAnswer")),
            "citationCount": len(report.get("evidence", [])),
            "supportedCitationCount": len(report.get("evidence", [])),
        },
    )


class ComparisonProvider:
    def __init__(self, *, reject_first_critic: bool = False) -> None:
        self.reject_first_critic = reject_first_critic
        self.critic_calls = 0
        self.prompt_lengths: list[int] = []

    def _completion(self, messages, tools=None, tool_choice=None):
        self.prompt_lengths.append(sum(len(str(item.get("content") or "")) for item in messages))
        name = tool_choice["function"]["name"]
        if name == "submit_evidence_plan":
            return tool_response(name, {
                "answerMode": "COMPARISON",
                "requirements": [
                    {
                        "requirementId": "python-side",
                        "subQuestion": "Python 适合什么场景？",
                        "retrievalQuery": "Python 数据处理 场景",
                        "evidenceRole": "COMPARE_LEFT",
                        "completionPolicy": "COMPARE_SIDE",
                        "expectedSources": ["ASR"],
                        "required": True,
                    },
                    {
                        "requirementId": "java-side",
                        "subQuestion": "Java 适合什么场景？",
                        "retrievalQuery": "Java 企业后端 场景",
                        "evidenceRole": "COMPARE_RIGHT",
                        "completionPolicy": "COMPARE_SIDE",
                        "expectedSources": ["ASR"],
                        "required": True,
                    },
                ],
            })
        payload = json.loads(messages[-1]["content"])
        if name == "submit_evidence_verification":
            evidence = payload["ledger"]["evidence"]
            python_id = next(item["evidenceId"] for item in evidence if "Python" in item["content"])
            java_id = next(item["evidenceId"] for item in evidence if "Java" in item["content"])
            return tool_response(name, {
                "requirements": [
                    {
                        "requirementId": "python-side",
                        "supported": True,
                        "complete": True,
                        "evidenceIds": [python_id],
                        "missingInformation": "",
                        "contradictionEvidenceIds": [],
                    },
                    {
                        "requirementId": "java-side",
                        "supported": True,
                        "complete": True,
                        "evidenceIds": [java_id],
                        "missingInformation": "",
                        "contradictionEvidenceIds": [],
                    },
                ],
                "overallSufficient": True,
                "shouldRefuse": False,
                "refusalReason": "",
            })
        if name == "submit_grounded_report":
            verification = payload["verification"]
            evidence_ids = [
                evidence_id
                for item in verification["requirements"]
                for evidence_id in item["evidenceIds"]
            ]
            revised = "criticReview" in payload
            return tool_response(name, {
                "answerable": True,
                "finalAnswer": (
                    "Python 适合数据处理，Java 适合企业后端。"
                    if revised or not self.reject_first_critic
                    else "两者都适合所有场景。"
                ),
                "title": "语言场景比较",
                "conclusions": ["分别覆盖 Python 和 Java。"],
                "evidenceIds": evidence_ids,
                "suggestions": [],
            })
        if name == "submit_critic_review":
            self.critic_calls += 1
            rejected = self.reject_first_critic and self.critic_calls == 1
            return tool_response(name, {
                "approved": not rejected,
                "answerabilityCorrect": True,
                "allRequiredSlotsCovered": not rejected,
                "contradictionFree": not rejected,
                "externalKnowledgeFree": not rejected,
                "citationIdsValid": True,
                "missingRequirementIds": ["python-side", "java-side"] if rejected else [],
                "unsupportedClaims": ["所有场景"] if rejected else [],
                "contradictions": [],
                "revisionInstruction": "分别回答两个槽位，不要扩大为所有场景。" if rejected else "",
            })
        raise AssertionError(name)


def comparison_segments() -> list[dict[str, Any]]:
    return [
        {
            "segmentId": "python",
            "source": "ASR",
            "startMs": 10000,
            "endMs": 15000,
            "content": "Python 更适合数据处理和快速脚本。",
        },
        {
            "segmentId": "java",
            "source": "ASR",
            "startMs": 30000,
            "endMs": 35000,
            "content": "Java 更适合大型企业后端服务。",
        },
    ]


def test_enumeration_retrieval_expands_a_wider_context_window() -> None:
    current_toolbox = toolbox([
        {
            "segmentId": "definition-1",
            "source": "ASR",
            "startMs": 10000,
            "endMs": 15000,
            "content": "第一种是参数量大。",
        },
        {
            "segmentId": "summary",
            "source": "ASR",
            "startMs": 60000,
            "endMs": 65000,
            "content": "这三种大都具备。",
        },
    ])

    result = _retrieve_node(current_toolbox, {
        "goal": "三种大分别是什么？",
        "evidence_plan": {
            "answerMode": "EXHAUSTIVE_LIST",
            "requirements": [{
                "requirementId": "R1",
                "subQuestion": "三种大分别是什么？",
                "retrievalQuery": "三种 大",
                "evidenceRole": "ENUMERATION",
                "completionPolicy": "ALL_RELEVANT",
                "expectedSources": ["ASR"],
                "required": True,
            }],
        },
    })

    window = next(item for item in current_toolbox.trace() if item["tool"] == "get_evidence_window")
    assert '"before_ms":60000' in window["arguments"]
    assert '"after_ms":30000' in window["arguments"]
    assert result["evidence_ledger"]["statistics"]["compactEvidenceCount"] == 2


def test_direct_deictic_citation_requires_a_second_verifier_audit() -> None:
    plan = {
        "requirements": [{
            "requirementId": "R1",
            "completionPolicy": "DIRECT",
            "candidateEvidenceIds": ["E001", "E002"],
        }],
    }
    ledger = {
        "evidence": [
            {"evidenceId": "E001", "content": "K3 使用了这篇论文中的技术。"},
            {"evidenceId": "E002", "content": "这个技术叫注意力残差。"},
        ],
    }
    verification = {
        "requirements": [{
            "requirementId": "R1",
            "complete": True,
            "evidenceIds": ["E001"],
        }],
    }

    assert _verification_needs_deictic_audit(plan, ledger, verification) is True


def test_structured_agent_covers_each_comparison_slot_with_evidence_ids() -> None:
    provider = ComparisonProvider()

    result = run_structured_evidence_agent(
        provider,
        toolbox(comparison_segments()),
        "Python 与 Java 分别适合什么场景？",
    )

    graph = result["agentGraph"]
    assert result["accepted"] is True
    assert graph["promptVersion"] == "video-evidence-agent-v5.1-bounded-closeout"
    assert graph["evidencePlan"]["answerMode"] == "COMPARISON"
    assert graph["evidenceVerification"]["fullyCovered"] is True
    assert len(graph["evidenceVerification"]["completeRequirementIds"]) == 2
    assert graph["modelCalls"] == 4
    assert graph["revisionCount"] == 0
    assert {item["evidenceId"] for item in result["report"]["evidence"]} == {"E001", "E002"}
    assert max(provider.prompt_lengths) < 20000


def test_structured_agent_allows_one_critic_driven_revision() -> None:
    provider = ComparisonProvider(reject_first_critic=True)

    result = run_structured_evidence_agent(
        provider,
        toolbox(comparison_segments()),
        "Python 与 Java 分别适合什么场景？",
    )

    assert result["accepted"] is True
    assert result["agentGraph"]["revisionCount"] == 1
    assert result["agentGraph"]["modelCalls"] == 6
    assert provider.critic_calls == 2
    assert result["report"]["finalAnswer"] == "Python 适合数据处理，Java 适合企业后端。"


class AlwaysRejectCriticProvider(ComparisonProvider):
    def _completion(self, messages, tools=None, tool_choice=None):
        name = tool_choice["function"]["name"]
        if name != "submit_critic_review":
            return super()._completion(messages, tools, tool_choice)
        self.critic_calls += 1
        return tool_response(name, {
            "approved": False,
            "answerabilityCorrect": True,
            "allRequiredSlotsCovered": True,
            "contradictionFree": True,
            "externalKnowledgeFree": True,
            "citationIdsValid": True,
            "missingRequirementIds": [],
            "unsupportedClaims": [],
            "contradictions": [],
            "revisionInstruction": "再次确认措辞。",
        })


def test_structured_agent_accepts_deterministic_gate_after_bounded_revision() -> None:
    provider = AlwaysRejectCriticProvider()

    result = run_structured_evidence_agent(
        provider,
        toolbox(comparison_segments()),
        "Python 与 Java 分别适合什么场景？",
    )

    assert result["accepted"] is True
    assert result["agentGraph"]["revisionCount"] == 1
    assert result["agentGraph"]["modelCalls"] == 6
    assert result["agentGraph"]["acceptanceMode"] == "DETERMINISTIC_AFTER_CRITIC_REVISION"


def test_single_direct_plan_cannot_be_upgraded_to_all_relevant() -> None:
    plan = _normalize_evidence_plan({
        "answerMode": "SINGLE",
        "requirements": [{
            "requirementId": "R1",
            "subQuestion": "大模型的大字是什么意思？",
            "retrievalQuery": "大模型 大字 含义",
            "evidenceRole": "DIRECT",
            "completionPolicy": "ALL_RELEVANT",
            "expectedSources": ["ASR", "OCR"],
            "required": True,
        }],
    }, "大模型的大字是什么意思？")

    assert plan is not None
    assert plan["requirements"][0]["completionPolicy"] == "DIRECT"
    assert plan["requirements"][0]["subQuestion"] == "大模型的大字是什么意思？"


def test_single_target_contrast_is_not_expanded_into_comparison() -> None:
    plan = _normalize_evidence_plan({
        "answerMode": "COMPARISON",
        "requirements": [
            {
                "requirementId": "R1",
                "subQuestion": "其他模型使用什么路径？",
                "retrievalQuery": "其他模型 残差 路径",
                "evidenceRole": "COMPARE_LEFT",
                "completionPolicy": "COMPARE_SIDE",
                "expectedSources": ["ASR"],
                "required": True,
            },
            {
                "requirementId": "R2",
                "subQuestion": "Kimi 选择的另一条路径是什么？",
                "retrievalQuery": "Kimi 另一条路径",
                "evidenceRole": "COMPARE_RIGHT",
                "completionPolicy": "COMPARE_SIDE",
                "expectedSources": ["ASR"],
                "required": True,
            },
        ],
    }, "Kimi 没有与其他模型一样，而选择的另一条路径是什么？")

    assert plan is not None
    assert plan["answerMode"] == "SINGLE"
    assert len(plan["requirements"]) == 1
    assert plan["requirements"][0]["completionPolicy"] == "DIRECT"
    assert plan["requirements"][0]["subQuestion"] == "Kimi 没有与其他模型一样，而选择的另一条路径是什么？"


def test_single_target_sequence_is_not_expanded_into_full_enumeration() -> None:
    plan = _normalize_evidence_plan({
        "answerMode": "SEQUENCE",
        "requirements": [
            {
                "requirementId": "R1",
                "subQuestion": "四个任务是什么？",
                "retrievalQuery": "四个任务",
                "evidenceRole": "ENUMERATION",
                "completionPolicy": "ALL_RELEVANT",
                "expectedSources": ["ASR", "OCR"],
                "required": True,
            },
            {
                "requirementId": "R2",
                "subQuestion": "工作流之后的下一个任务是什么？",
                "retrievalQuery": "工作流 之后 下一个任务",
                "evidenceRole": "SEQUENCE_STEP",
                "completionPolicy": "ORDERED_STEP",
                "expectedSources": ["ASR", "OCR"],
                "required": True,
            },
        ],
    }, "“文生图生视频的工作流”之后的任务是什么？")

    assert plan is not None
    assert plan["answerMode"] == "SINGLE"
    assert len(plan["requirements"]) == 1
    assert plan["requirements"][0]["completionPolicy"] == "DIRECT"


def test_verifier_repairs_self_contradictory_structured_output() -> None:
    plan = {
        "requirements": [{
            "requirementId": "R1",
            "required": True,
        }],
    }
    ledger = {
        "evidence": [{"evidenceId": "E001", "content": "前几天我老婆这么问我。"}],
    }
    raw = {
        "requirements": [{
            "requirementId": "R1",
            "supported": False,
            "complete": False,
            "evidenceIds": [],
            "missingInformation": "E001 说‘我老婆这么问我’，这直接回答了问题，因此证据直接支持了答案。",
            "contradictionEvidenceIds": [],
        }],
        "overallSufficient": False,
        "shouldRefuse": True,
        "refusalReason": "自相矛盾",
    }

    verification = _normalize_verification(raw, plan, ledger)

    assert verification["overallSufficient"] is True
    assert verification["requirements"][0]["evidenceIds"] == ["E001"]
    assert verification["requirements"][0]["consistencyRepaired"] is True


class RefusalProvider:
    def _completion(self, messages, tools=None, tool_choice=None):
        name = tool_choice["function"]["name"]
        if name == "submit_evidence_plan":
            return tool_response(name, {
                "answerMode": "SINGLE",
                "requirements": [{
                    "requirementId": "owner",
                    "subQuestion": "负责人是谁？",
                    "retrievalQuery": "日志系统 负责人",
                    "evidenceRole": "DIRECT",
                    "completionPolicy": "DIRECT",
                    "expectedSources": ["ASR", "OCR"],
                    "required": True,
                }],
            })
        if name == "submit_evidence_verification":
            return tool_response(name, {
                "requirements": [{
                    "requirementId": "owner",
                    "supported": False,
                    "complete": False,
                    "evidenceIds": [],
                    "missingInformation": "标题只说明主题，没有负责人姓名。",
                    "contradictionEvidenceIds": [],
                }],
                "overallSufficient": False,
                "shouldRefuse": True,
                "refusalReason": "视频未说明负责人。",
            })
        if name == "submit_grounded_report":
            return tool_response(name, {
                "answerable": False,
                "finalAnswer": "根据现有证据，无法确定日志系统负责人。",
                "title": "视频证据不足",
                "conclusions": ["缺少负责人姓名证据。"],
                "evidenceIds": [],
                "suggestions": [],
            })
        if name == "submit_critic_review":
            return tool_response(name, {
                "approved": True,
                "answerabilityCorrect": True,
                "allRequiredSlotsCovered": False,
                "contradictionFree": True,
                "externalKnowledgeFree": True,
                "citationIdsValid": True,
                "missingRequirementIds": ["owner"],
                "unsupportedClaims": [],
                "contradictions": [],
                "revisionInstruction": "",
            })
        raise AssertionError(name)


class VerifierFalseNegativeProvider(ComparisonProvider):
    def __init__(self) -> None:
        super().__init__()
        self.verifier_calls = 0

    def _completion(self, messages, tools=None, tool_choice=None):
        name = tool_choice["function"]["name"]
        if name != "submit_evidence_verification":
            return super()._completion(messages, tools, tool_choice)
        self.verifier_calls += 1
        if self.verifier_calls > 1:
            return super()._completion(messages, tools, tool_choice)
        return tool_response(name, {
            "requirements": [
                {
                    "requirementId": "python-side",
                    "supported": False,
                    "complete": False,
                    "evidenceIds": [],
                    "missingInformation": "首次误判",
                    "contradictionEvidenceIds": [],
                },
                {
                    "requirementId": "java-side",
                    "supported": False,
                    "complete": False,
                    "evidenceIds": [],
                    "missingInformation": "首次误判",
                    "contradictionEvidenceIds": [],
                },
            ],
            "overallSufficient": False,
            "shouldRefuse": True,
            "refusalReason": "首次误判",
        })


def test_structured_agent_audits_a_verifier_false_negative_once() -> None:
    provider = VerifierFalseNegativeProvider()

    result = run_structured_evidence_agent(
        provider,
        toolbox(comparison_segments()),
        "Python 与 Java 分别适合什么场景？",
    )

    assert result["accepted"] is True
    assert result["agentGraph"]["verificationPasses"] == 2
    assert result["agentGraph"]["modelCalls"] == 5
    assert result["agentGraph"]["evidenceVerification"]["overallSufficient"] is True
    assert provider.verifier_calls == 2


def test_structured_agent_refuses_when_title_is_related_but_answer_is_missing() -> None:
    related_title = [{
        "segmentId": "title",
        "source": "OCR",
        "startMs": 0,
        "endMs": 15000,
        "content": "日志系统事故复盘",
    }]

    result = run_structured_evidence_agent(
        RefusalProvider(),
        toolbox(related_title),
        "视频是否说明日志系统负责人是谁？",
    )

    assert result["accepted"] is True
    assert result["report"]["answerable"] is False
    assert result["report"]["evidence"] == []
    assert result["report"]["finalAnswer"] == "视频未提供足够证据，无法从视频确定答案。"
    assert "无法从视频确定答案" in result["report"]["finalAnswer"]
    assert result["agentGraph"]["evidenceVerification"]["shouldRefuse"] is True


class InvalidCitationProvider(ComparisonProvider):
    def _completion(self, messages, tools=None, tool_choice=None):
        name = tool_choice["function"]["name"]
        if name == "submit_grounded_report":
            return tool_response(name, {
                "answerable": True,
                "finalAnswer": "引用不存在。",
                "title": "无效引用",
                "conclusions": ["无效"],
                "evidenceIds": ["E999"],
                "suggestions": [],
            })
        return super()._completion(messages, tools, tool_choice)


def test_structured_agent_rejects_unknown_evidence_ids_even_when_model_critic_approves() -> None:
    result = run_structured_evidence_agent(
        InvalidCitationProvider(),
        toolbox(comparison_segments()),
        "Python 与 Java 分别适合什么场景？",
    )

    assert result["accepted"] is True
    assert result["report"]["answerable"] is False
    assert result["report"]["evidence"] == []
    assert result["agentGraph"]["acceptanceMode"] == "SAFE_REFUSAL_AFTER_CRITIC_REVISION"
