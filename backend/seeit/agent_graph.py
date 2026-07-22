"""LangGraph orchestration for the model-driven video evidence Agent.

The business tools stay in ``AgentToolbox``. This module owns only workflow
state, model/tool messages, the step budget, and the stop condition so the
same tools can be tested independently from the orchestration framework.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from seeit.agent import AgentToolbox, build_agent_plan


class AgentGraphState(TypedDict, total=False):
    goal: str
    plan: dict[str, Any]
    messages: list[dict[str, Any]]
    steps: int
    max_steps: int
    last_report: dict[str, Any] | None
    accepted: bool
    error: str | None
    pending_tool_calls: list[dict[str, Any]]
    finalize_calls: int


PROMPT_VERSION = "video-evidence-agent-v4-coverage"
SYSTEM_PROMPT = (
    "你是 SeeIt AI 的视频证据 Agent。必须先调用工具读取和检索视频，"
    "只根据工具返回的 ASR/OCR 证据回答。不得编造时间戳或视频事实。"
    "完成后调用 generate_report；如果 Critic 未通过，继续检索并修订。"
    "若视频证据无法回答，停止重复检索，提交 answerable=false 的明确拒答，"
    "检索结果包含 evidenceSufficiency 时必须服从证据覆盖门禁；多需求问题只有 fullyCovered=true"
    "才能提交 answerable=true，否则继续按缺失 requirement 检索或明确拒答。"
    "不得使用外部知识补全答案。若问题询问总数或组合结果，而证据分别给出各组成部分，"
    "可以在 finalAnswer 中展示加法并计算结果，同时引用每个组成部分。"
    "若画面先展示孤立生僻字、后续又展示包含这些字的词语或成语，必须用后续上下文交叉校验"
    "形近字 OCR 结果，并在最终回答中采用上下文能够确认的字形。"
)


def extract_goal_timestamps_ms(goal: str) -> list[int]:
    """Extract explicit video positions while preserving their mention order."""

    candidates: list[tuple[int, int]] = []
    occupied: list[tuple[int, int]] = []

    def add(match: re.Match[str], milliseconds: int) -> None:
        span = match.span()
        if milliseconds < 0 or any(span[0] < end and span[1] > start for start, end in occupied):
            return
        occupied.append(span)
        candidates.append((span[0], milliseconds))

    for match in re.finditer(r"(?<!\d)(\d{1,3}):(\d{2})(?!\d)", goal):
        add(match, (int(match.group(1)) * 60 + int(match.group(2))) * 1000)
    for match in re.finditer(r"(?:第\s*)?(\d+(?:\.\d+)?)\s*分(?:钟)?\s*(\d+(?:\.\d+)?)?\s*秒?", goal):
        seconds = float(match.group(1)) * 60 + float(match.group(2) or 0)
        add(match, int(seconds * 1000))
    for match in re.finditer(r"(?:第\s*)?(\d+(?:\.\d+)?)\s*秒(?:钟)?(?:左右|附近|前后)?", goal):
        add(match, int(float(match.group(1)) * 1000))

    return [milliseconds for _, milliseconds in sorted(candidates)][:5]


def _parse_arguments(raw_arguments: Any) -> dict[str, Any]:
    if isinstance(raw_arguments, dict):
        return dict(raw_arguments)
    if not isinstance(raw_arguments, str):
        return {"invalid_arguments": str(raw_arguments)[:1000]}
    try:
        parsed = json.loads(raw_arguments or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"invalid_arguments": raw_arguments[:1000]}
    return parsed if isinstance(parsed, dict) else {"invalid_arguments": str(parsed)[:1000]}


def _plan_node(state: AgentGraphState) -> dict[str, Any]:
    goal = str(state.get("goal") or "").strip()
    plan = build_agent_plan(goal)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"分析目标：{goal}\n"
                f"当前执行计划：{json.dumps(plan, ensure_ascii=False)}\n"
                "请按计划调用证据工具，并最终提交可校验报告。"
            ),
        },
    ]
    return {
        "plan": plan,
        "messages": messages,
        "steps": 0,
        "accepted": False,
        "last_report": None,
        "pending_tool_calls": [],
        "finalize_calls": 0,
    }


def _prefetch_time_evidence_node(toolbox: AgentToolbox, state: AgentGraphState) -> dict[str, Any]:
    goal = str(state.get("goal") or "")
    timestamps = extract_goal_timestamps_ms(goal)
    if not timestamps:
        return {}
    windows = [
        {
            "anchorTimestampMs": timestamp_ms,
            "window": window,
            "likelyFollowingOcr": next((
                segment for segment in window.get("segments", [])
                if segment.get("source") == "OCR" and int(segment.get("startMs", 0)) >= timestamp_ms
            ), None),
        }
        for timestamp_ms in timestamps
        for window in [toolbox.execute("get_evidence_window", {
            "timestamp_ms": timestamp_ms,
            "before_ms": 15000,
            "after_ms": 15000,
        })]
    ]
    messages = list(state.get("messages", []))
    messages.append({
        "role": "user",
        "content": (
            "系统已根据问题中的明确时间指代预取附近证据。优先结合指代出现前后的 ASR/OCR，"
            "不要把提问时刻本身误当作答案。对于‘这段话/这几个字/这个画面’等指代，"
            "时间指代解析器将锚点之后首次出现的 likelyFollowingOcr 判定为主答案候选；"
            "最终回答应直接复述该候选的完整内容，锚点之前或当时仍停留的标题文字只作上下文，"
            "除非后续 ASR/OCR 明确否定该候选。问题原文："
            f"{goal}\n预取结果：\n"
            + json.dumps(windows, ensure_ascii=False)
        ),
    })
    return {"messages": messages}


def _prefetch_coverage_evidence_node(toolbox: AgentToolbox, state: AgentGraphState) -> dict[str, Any]:
    goal = str(state.get("goal") or "").strip()
    if not goal:
        return {}
    result = toolbox.prefetch_goal_evidence(goal, top_k=8)
    compact_result = {
        "coveragePlan": result.get("coveragePlan"),
        "evidenceSufficiency": result.get("evidenceSufficiency"),
        "abstention": result.get("abstention"),
        "matches": [
            {
                "segmentId": item.get("segmentId"),
                "source": item.get("source"),
                "startMs": item.get("startMs"),
                "endMs": item.get("endMs"),
                "content": item.get("content"),
                "coverageRequirementIds": item.get("coverageRequirementIds"),
                "coverageSelectionReasons": item.get("coverageSelectionReasons"),
            }
            for item in result.get("matches", [])
        ],
    }
    messages = list(state.get("messages", []))
    messages.append({
        "role": "user",
        "content": (
            "系统已按原问题执行证据覆盖规划和首次检索。先检查 evidenceSufficiency："
            "INSUFFICIENT_EVIDENCE 表示当前视频缺少关键实体，必须拒答且不得反复检索；"
            "PARTIAL_EVIDENCE 表示仍需针对未满足 requirement 检索；"
            "只有 SUFFICIENT_CANDIDATES 且 fullyCovered=true 才能据此生成完整答案。\n"
            + json.dumps(compact_result, ensure_ascii=False)
        ),
    })
    return {"messages": messages}


def _generate_report_schema(toolbox: AgentToolbox) -> dict[str, Any]:
    return next(
        schema for schema in toolbox.tool_schemas()
        if schema.get("function", {}).get("name") == "generate_report"
    )


def _completion_with_choice(
    provider: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_choice: dict[str, Any],
) -> dict[str, Any]:
    completion = getattr(provider, "_completion", None)
    if not callable(completion):
        raise TypeError("LangGraph 模式需要支持 _completion(messages, tools) 的 Provider")
    try:
        return completion(messages, tools, tool_choice=tool_choice)
    except TypeError as exc:
        if "tool_choice" not in str(exc):
            raise
        return completion(messages, tools)


def _report_call_from_message(message: dict[str, Any]) -> dict[str, Any] | None:
    for call in message.get("tool_calls") or []:
        if str((call.get("function") or {}).get("name") or "") == "generate_report":
            return call
    content = str(message.get("content") or "").replace("```json", "").replace("```", "").strip()
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return {
        "id": f"finalize-fallback-{uuid.uuid4()}",
        "type": "function",
        "function": {
            "name": "generate_report",
            "arguments": json.dumps(parsed, ensure_ascii=False),
        },
    }


def _finalize_node(provider: Any, toolbox: AgentToolbox, state: AgentGraphState) -> dict[str, Any]:
    messages = list(state.get("messages", []))
    coverage = toolbox.goal_evidence_sufficiency()
    messages.append({
        "role": "user",
        "content": (
            "现在必须结束检索并提交最终结果。只调用 generate_report。"
            "证据足够时 answerable=true，并用 finalAnswer 直接回答问题；"
            "证据不足时 answerable=false，明确说明无法从视频确定，evidence 为空，"
            "不要补充任何外部定义或常识。当前证据覆盖状态："
            + json.dumps(coverage, ensure_ascii=False)
        ),
    })
    schema = _generate_report_schema(toolbox)
    result: dict[str, Any] | None = None
    finalize_calls = 0
    for _ in range(2):
        message = _completion_with_choice(
            provider,
            messages,
            [schema],
            {"type": "function", "function": {"name": "generate_report"}},
        )
        finalize_calls += 1
        call = _report_call_from_message(message)
        if call:
            arguments = _parse_arguments((call.get("function") or {}).get("arguments") or "{}")
            result = toolbox.execute("generate_report", arguments)
        else:
            result = {"ok": False, "error": "模型未调用 generate_report 或未返回合法 JSON"}
        if result.get("accepted"):
            break
        messages.append({
            "role": "user",
            "content": (
                "上一次 generate_report 未通过。请修复 JSON 转义、结构或引用后再次调用；"
                "不要在字符串内部使用未转义的双引号。错误摘要："
                + json.dumps(result, ensure_ascii=False)[:2000]
            ),
        })

    if not result or not result.get("accepted"):
        result = toolbox.execute("generate_report", {
            "answerable": False,
            "finalAnswer": "视频未明确说明该问题，无法从视频确定答案。",
            "title": "视频证据不足",
            "conclusions": ["当前检索到的视频证据不足以支持确定答案。"],
            "evidence": [],
            "suggestions": ["可调整问题表述或补充更完整的视频内容后重试。"],
        })
    return {
        "messages": messages,
        "last_report": result,
        "accepted": bool(result.get("accepted")),
        "pending_tool_calls": [],
        "finalize_calls": int(state.get("finalize_calls", 0)) + finalize_calls,
    }


def _model_step_node(provider: Any, toolbox: AgentToolbox, state: AgentGraphState) -> dict[str, Any]:
    completion = getattr(provider, "_completion", None)
    if not callable(completion):
        raise TypeError("LangGraph 模式需要支持 _completion(messages, tools) 的 Provider")

    message = completion(state.get("messages", []), toolbox.tool_schemas())
    next_messages = list(state.get("messages", []))
    tool_calls = message.get("tool_calls") or []

    if not tool_calls:
        content = str(message.get("content") or "").replace("```json", "").replace("```", "").strip()
        start, end = content.find("{"), content.rfind("}")
        candidate: dict[str, Any] = {}
        if start >= 0 and end > start:
            try:
                parsed = json.loads(content[start : end + 1])
                candidate = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                pass
        if candidate:
            tool_calls = [{
                "id": f"fallback-{uuid.uuid4()}",
                "type": "function",
                "function": {
                    "name": "generate_report",
                    "arguments": json.dumps(candidate, ensure_ascii=False),
                },
            }]
            next_messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        else:
            next_messages.extend([
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": "请继续调用证据工具，并通过 generate_report 提交可校验的最终报告。",
                },
            ])
    else:
        next_messages.append({
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": tool_calls,
        })

    return {
        "messages": next_messages,
        "steps": int(state.get("steps", 0)) + 1,
        "pending_tool_calls": tool_calls,
    }


def _tool_execution_node(toolbox: AgentToolbox, state: AgentGraphState) -> dict[str, Any]:
    next_messages = list(state.get("messages", []))
    last_report = state.get("last_report")
    accepted = bool(state.get("accepted"))
    for call in state.get("pending_tool_calls", []):
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        arguments = _parse_arguments(function.get("arguments") or "{}")
        result = toolbox.execute(name, arguments)
        next_messages.append({
            "role": "tool",
            "tool_call_id": str(call.get("id") or uuid.uuid4()),
            "content": json.dumps(result, ensure_ascii=False),
        })
        if name == "generate_report" and result.get("ok"):
            last_report = result
            accepted = bool(result.get("accepted"))
    return {
        "messages": next_messages,
        "pending_tool_calls": [],
        "last_report": last_report,
        "accepted": accepted,
    }


def _budget_exhausted(state: AgentGraphState) -> bool:
    return int(state.get("steps", 0)) >= int(state.get("max_steps", 8))


def _route_after_model(state: AgentGraphState) -> str:
    if state.get("pending_tool_calls"):
        return "tool_execution"
    if state.get("accepted"):
        return END
    if _budget_exhausted(state):
        return "finalize"
    return "model_step"


def _route_after_tools(state: AgentGraphState) -> str:
    if state.get("accepted"):
        return END
    if _budget_exhausted(state):
        return "finalize"
    return "model_step"


def run_langgraph_agent(
    provider: Any,
    toolbox: AgentToolbox,
    goal: str,
    *,
    max_steps: int = 8,
) -> dict[str, Any]:
    """Run the existing model/tool loop as an explicit LangGraph state graph."""

    normalized_max_steps = max(3, min(int(max_steps), 12))

    workflow = StateGraph(AgentGraphState)
    workflow.add_node("plan", _plan_node)
    workflow.add_node("prefetch_time_evidence", lambda state: _prefetch_time_evidence_node(toolbox, state))
    workflow.add_node(
        "prefetch_coverage_evidence",
        lambda state: _prefetch_coverage_evidence_node(toolbox, state),
    )
    workflow.add_node("model_step", lambda state: _model_step_node(provider, toolbox, state))
    workflow.add_node("tool_execution", lambda state: _tool_execution_node(toolbox, state))
    workflow.add_node("finalize", lambda state: _finalize_node(provider, toolbox, state))
    workflow.add_edge(START, "plan")
    workflow.add_edge("plan", "prefetch_time_evidence")
    workflow.add_edge("prefetch_time_evidence", "prefetch_coverage_evidence")
    workflow.add_edge("prefetch_coverage_evidence", "model_step")
    workflow.add_conditional_edges("model_step", _route_after_model)
    workflow.add_conditional_edges("tool_execution", _route_after_tools)
    workflow.add_edge("finalize", END)
    graph = workflow.compile()

    final_state = graph.invoke(
        {"goal": goal, "max_steps": normalized_max_steps},
        config={"recursion_limit": normalized_max_steps * 2 + 8},
    )
    last_report = final_state.get("last_report")
    if not last_report:
        raise RuntimeError("LangGraph Agent 在步骤预算内未提交有效报告")
    if not final_state.get("accepted"):
        raise RuntimeError("LangGraph Agent 在步骤预算内未通过 Critic 校验")
    return {
        **last_report,
        "agentGraph": {
            "framework": "LangGraph",
            "promptVersion": PROMPT_VERSION,
            "nodes": [
                "plan",
                "prefetch_time_evidence",
                "prefetch_coverage_evidence",
                "model_step",
                "tool_execution",
                "finalize",
            ],
            "steps": int(final_state.get("steps", 0)),
            "maxSteps": int(final_state.get("max_steps", max_steps)),
            "finalizeCalls": int(final_state.get("finalize_calls", 0)),
            "intent": (final_state.get("plan") or {}).get("intent"),
            "planStages": (final_state.get("plan") or {}).get("steps", []),
        },
    }
