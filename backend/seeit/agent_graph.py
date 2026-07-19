"""LangGraph orchestration for the model-driven video evidence Agent.

The business tools stay in ``AgentToolbox``. This module owns only workflow
state, model/tool messages, the step budget, and the stop condition so the
same tools can be tested independently from the orchestration framework.
"""

from __future__ import annotations

import json
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


PROMPT_VERSION = "video-evidence-agent-v2"
SYSTEM_PROMPT = (
    "你是 SeeIt AI 的视频证据 Agent。必须先调用工具读取和检索视频，"
    "只根据工具返回的 ASR/OCR 证据回答。不得编造时间戳或视频事实。"
    "完成后调用 generate_report；如果 Critic 未通过，继续检索并修订。"
)


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
    if state.get("accepted") or _budget_exhausted(state):
        return END
    return "model_step"


def _route_after_tools(state: AgentGraphState) -> str:
    if state.get("accepted") or _budget_exhausted(state):
        return END
    return "model_step"


def run_langgraph_agent(
    provider: Any,
    toolbox: AgentToolbox,
    goal: str,
    *,
    max_steps: int = 8,
) -> dict[str, Any]:
    """Run the existing model/tool loop as an explicit LangGraph state graph."""

    workflow = StateGraph(AgentGraphState)
    workflow.add_node("plan", _plan_node)
    workflow.add_node("model_step", lambda state: _model_step_node(provider, toolbox, state))
    workflow.add_node("tool_execution", lambda state: _tool_execution_node(toolbox, state))
    workflow.add_edge(START, "plan")
    workflow.add_edge("plan", "model_step")
    workflow.add_conditional_edges("model_step", _route_after_model)
    workflow.add_conditional_edges("tool_execution", _route_after_tools)
    graph = workflow.compile()

    final_state = graph.invoke({
        "goal": goal,
        "max_steps": max(3, min(int(max_steps), 12)),
    })
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
            "nodes": ["plan", "model_step", "tool_execution"],
            "steps": int(final_state.get("steps", 0)),
            "maxSteps": int(final_state.get("max_steps", max_steps)),
            "intent": (final_state.get("plan") or {}).get("intent"),
            "planStages": (final_state.get("plan") or {}).get("steps", []),
        },
    }
