"""Run the annotated real-video QA set against a running SeeIt API.

The script deliberately keeps answer judging conservative and reproducible:
required concept groups are OR alternatives, all groups must be present, and
forbidden patterns must be absent. Semantic equivalence still needs a manual
review pass and is reported separately from the rule score.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import httpx


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


def group_hit(answer: str, alternatives: list[str]) -> bool:
    normalized = normalize_text(answer)
    return any(normalize_text(item) and normalize_text(item) in normalized for item in alternatives)


def evaluate_answer(case: dict[str, Any], final_answer: str) -> dict[str, Any]:
    groups = [group_hit(final_answer, group) for group in case.get("requiredAnswerGroups", [])]
    forbidden = [
        pattern for pattern in case.get("forbiddenAnswerPatterns", [])
        if normalize_text(pattern) and normalize_text(pattern) in normalize_text(final_answer)
    ]
    return {
        "requiredGroupHits": groups,
        "requiredGroupsPassed": all(groups),
        "forbiddenMatches": forbidden,
        "forbiddenPassed": not forbidden,
        "rulePassed": all(groups) and not forbidden,
    }


def evaluate_status_answer(case: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    return evaluate_answer(case, str(status.get("finalAnswer") or ""))


def classify_failure(error: Any, exc: Exception | None = None) -> str | None:
    if not error and exc is None:
        return None
    if isinstance(exc, (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.RemoteProtocolError,
    )):
        return "INFRASTRUCTURE"
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
        return "INFRASTRUCTURE"
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        return "RATE_LIMIT"
    normalized = normalize_text(error or exc)
    infrastructure_markers = (
        "connectionrefused", "connecterror", "connecttimeout", "readtimeout",
        "connectionreset", "networkisunreachable", "temporaryfailure", "serverdisconnected",
        "502badgateway", "503serviceunavailable", "504gatewaytimeout",
        "连接被拒绝", "连接超时", "网络不可达",
    )
    if any(marker in normalized for marker in infrastructure_markers):
        return "INFRASTRUCTURE"
    if any(marker in normalized for marker in ("步骤预算", "未提交有效报告", "未通过critic", "recursionlimit")):
        return "AGENT_NOT_FINISHED"
    if exc is not None:
        return "EVALUATOR_ERROR"
    return "TASK_FAILED"


def provider_usage_from_result(result: dict[str, Any]) -> dict[str, Any] | None:
    direct = result.get("providerUsage")
    if isinstance(direct, dict):
        return direct
    trace = result.get("trace")
    usage = trace.get("providerUsage") if isinstance(trace, dict) else None
    return usage if isinstance(usage, dict) else None


def aggregate_provider_usage(results: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [usage for item in results if (usage := provider_usage_from_result(item)) is not None]
    request_events: list[dict[str, Any]] = []
    for item in results:
        trace = item.get("trace")
        if not isinstance(trace, dict):
            continue
        events = trace.get("providerRequests")
        if not isinstance(events, list):
            continue
        request_events.extend(event for event in events if isinstance(event, dict))
    latencies = sorted(max(0, int(item.get("latencyMs", 0) or 0)) for item in request_events)

    def percentile(percent: float) -> int:
        if not latencies:
            return 0
        index = max(0, min(len(latencies) - 1, int((len(latencies) - 1) * percent + 0.999999)))
        return latencies[index]

    costs = [
        float(item["estimatedCostUsd"])
        for item in summaries
        if item.get("estimatedCostUsd") is not None
    ]
    by_phase: dict[str, dict[str, int]] = {}
    for summary in summaries:
        for phase, values in (summary.get("byPhase") or {}).items():
            if not isinstance(values, dict):
                continue
            target = by_phase.setdefault(str(phase), {
                "requestCount": 0,
                "successCount": 0,
                "failureCount": 0,
                "promptTokens": 0,
                "completionTokens": 0,
                "totalTokens": 0,
                "cacheHitTokens": 0,
                "cacheMissTokens": 0,
                "toolCallCount": 0,
            })
            for key in target:
                target[key] += int(values.get(key, 0) or 0)
    return {
        "caseWithUsageCount": len(summaries),
        "requestCount": sum(int(item.get("requestCount", 0) or 0) for item in summaries),
        "successCount": sum(int(item.get("successCount", 0) or 0) for item in summaries),
        "failureCount": sum(int(item.get("failureCount", 0) or 0) for item in summaries),
        "usageReportedCount": sum(int(item.get("usageReportedCount", 0) or 0) for item in summaries),
        "promptTokens": sum(int(item.get("promptTokens", 0) or 0) for item in summaries),
        "completionTokens": sum(int(item.get("completionTokens", 0) or 0) for item in summaries),
        "totalTokens": sum(int(item.get("totalTokens", 0) or 0) for item in summaries),
        "cacheHitTokens": sum(int(item.get("cacheHitTokens", 0) or 0) for item in summaries),
        "cacheMissTokens": sum(int(item.get("cacheMissTokens", 0) or 0) for item in summaries),
        "toolCallCount": sum(int(item.get("toolCallCount", 0) or 0) for item in summaries),
        "estimatedCostUsd": round(sum(costs), 8) if costs else None,
        "costConfigured": bool(costs),
        "latencyMsTotal": sum(latencies),
        "latencyMsP50": percentile(0.50),
        "latencyMsP95": percentile(0.95),
        "byPhase": dict(sorted(by_phase.items())),
    }


def budget_stop_reason(
    results: list[dict[str, Any]],
    *,
    max_provider_calls: int = 0,
    max_total_tokens: int = 0,
    max_estimated_cost_usd: float = 0.0,
    reserve_next_case: bool = False,
) -> dict[str, Any] | None:
    if not results:
        return None
    usage = aggregate_provider_usage(results)
    if (max_provider_calls > 0 or max_total_tokens > 0 or max_estimated_cost_usd > 0) and (
        usage["caseWithUsageCount"] < len(results)
    ):
        return {"reason": "PROVIDER_USAGE_UNAVAILABLE", "observed": usage}
    if max_provider_calls > 0 and usage["requestCount"] >= max_provider_calls:
        return {
            "reason": "MAX_PROVIDER_CALLS",
            "limit": max_provider_calls,
            "observed": usage["requestCount"],
        }
    if max_provider_calls > 0 and reserve_next_case:
        reserve = max(
            (
                int((provider_usage_from_result(item) or {}).get("requestCount", 0) or 0)
                for item in results
            ),
            default=0,
        )
        if reserve and usage["requestCount"] + reserve > max_provider_calls:
            return {
                "reason": "MAX_PROVIDER_CALLS_RESERVE",
                "limit": max_provider_calls,
                "observed": usage["requestCount"],
                "reservedForNextCase": reserve,
                "projected": usage["requestCount"] + reserve,
            }
    if max_total_tokens > 0:
        if usage["requestCount"] > usage["usageReportedCount"]:
            return {"reason": "TOKEN_USAGE_UNAVAILABLE", "observed": usage}
        if usage["totalTokens"] >= max_total_tokens:
            return {
                "reason": "MAX_TOTAL_TOKENS",
                "limit": max_total_tokens,
                "observed": usage["totalTokens"],
            }
        if reserve_next_case:
            reserve = max(
                (
                    int((provider_usage_from_result(item) or {}).get("totalTokens", 0) or 0)
                    for item in results
                ),
                default=0,
            )
            if reserve and usage["totalTokens"] + reserve > max_total_tokens:
                return {
                    "reason": "MAX_TOTAL_TOKENS_RESERVE",
                    "limit": max_total_tokens,
                    "observed": usage["totalTokens"],
                    "reservedForNextCase": reserve,
                    "projected": usage["totalTokens"] + reserve,
                }
    if max_estimated_cost_usd > 0:
        if usage["requestCount"] and not usage["costConfigured"]:
            return {"reason": "COST_ESTIMATE_UNAVAILABLE", "observed": usage}
        if float(usage["estimatedCostUsd"] or 0) >= max_estimated_cost_usd:
            return {
                "reason": "MAX_ESTIMATED_COST_USD",
                "limit": max_estimated_cost_usd,
                "observed": usage["estimatedCostUsd"],
            }
    return None


def request_json(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()


def run_case(
    client: httpx.Client,
    base_url: str,
    media_id: int,
    case: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    response = client.post(
        f"{base_url}/analysis/ai",
        params={"id": media_id, "goal": case["question"]},
        timeout=30,
    )
    payload = response.json()
    if response.status_code != 409:
        response.raise_for_status()
    task_id = payload.get("taskId")
    if not task_id:
        raise RuntimeError(f"analysis task id missing for {case['caseId']}")
    status: dict[str, Any] = {}
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = request_json(
            client,
            "GET",
            f"{base_url}/analysis/task-status",
            params={"taskId": task_id},
            timeout=20,
        )
        if status.get("state") in {"COMPLETED", "FAILED"}:
            break
        time.sleep(2)
    if status.get("state") not in {"COMPLETED", "FAILED"}:
        raise TimeoutError(f"analysis timeout for {case['caseId']} task={task_id}")
    report = str(status.get("report") or "")
    final_answer = str(status.get("finalAnswer") or "")
    error = (status.get("error") or status.get("message")) if status.get("state") == "FAILED" else None
    answer_check = evaluate_status_answer(case, status)
    return {
        "caseId": case["caseId"],
        "videoId": case["videoId"],
        "type": case["type"],
        "answerable": bool(case["answerable"]),
        "taskId": task_id,
        "state": status.get("state"),
        "error": error,
        "failureCategory": classify_failure(error),
        "stage": status.get("stage"),
        "progressPercent": status.get("progressPercent", 0),
        "answerableResult": status.get("answerable"),
        "finalAnswer": final_answer,
        "report": report,
        "evaluation": status.get("evaluation"),
        "trace": status.get("trace"),
        "providerUsage": (
            status.get("trace", {}).get("providerUsage")
            if isinstance(status.get("trace"), dict)
            else None
        ),
        "answerCheck": answer_check,
        "elapsedMs": int((time.perf_counter() - started) * 1000),
    }


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [item for item in results if item.get("state") == "COMPLETED"]
    infrastructure_failures = [item for item in results if item.get("failureCategory") == "INFRASTRUCTURE"]
    model_evaluable = [
        item for item in results
        if item.get("failureCategory") not in {"INFRASTRUCTURE", "RATE_LIMIT"}
    ]
    model_completed = [item for item in model_evaluable if item.get("state") == "COMPLETED"]
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        by_type[str(item.get("type"))].append(item)

    def group_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
        passed = [item for item in items if item.get("answerCheck", {}).get("rulePassed")]
        return {
            "caseCount": len(items),
            "completedCount": sum(item.get("state") == "COMPLETED" for item in items),
            "rulePassCount": len(passed),
            "rulePassRate": round(len(passed) / max(1, len(items)), 4),
        }

    citation_rates = [
        float(item["evaluation"]["evidenceSupportRate"])
        for item in completed
        if isinstance(item.get("evaluation"), dict)
        and item["evaluation"].get("evidenceSupportRate") is not None
    ]
    return {
        "caseCount": len(results),
        "completedCount": len(completed),
        "taskSuccessRate": round(len(completed) / max(1, len(results)), 4),
        "infrastructureFailureCount": len(infrastructure_failures),
        "rateLimitFailureCount": sum(item.get("failureCategory") == "RATE_LIMIT" for item in results),
        "agentNotFinishedCount": sum(
            item.get("failureCategory") == "AGENT_NOT_FINISHED" for item in results
        ),
        "modelEvaluableCaseCount": len(model_evaluable),
        "modelTaskSuccessRate": round(len(model_completed) / max(1, len(model_evaluable)), 4),
        "answerRulePassRate": round(
            sum(item.get("answerCheck", {}).get("rulePassed", False) for item in completed)
            / max(1, len(completed)),
            4,
        ),
        "citationSupportRate": round(sum(citation_rates) / max(1, len(citation_rates)), 4),
        "byType": {key: group_summary(value) for key, value in sorted(by_type.items())},
        "stateCounts": dict(Counter(str(item.get("state")) for item in results)),
        "providerUsage": aggregate_provider_usage(results),
    }


def write_run_output(
    output_path: Path,
    *,
    run_id: str,
    dataset_id: Any,
    media_map: dict[str, Any],
    selected_cases: list[dict[str, Any]],
    result_by_case: dict[str, dict[str, Any]],
    requested_case_count: int,
    budget_config: dict[str, Any],
    budget_stop: dict[str, Any] | None,
) -> dict[str, Any]:
    results = [
        result_by_case[case["caseId"]]
        for case in selected_cases
        if case["caseId"] in result_by_case
    ]
    output = {
        "runId": run_id,
        "datasetId": dataset_id,
        "mediaMap": media_map,
        "requestedCaseCount": requested_case_count,
        "plannedCaseCount": len(selected_cases),
        "remainingCaseIds": [
            case["caseId"] for case in selected_cases if case["caseId"] not in result_by_case
        ],
        "budget": {"config": budget_config, "stop": budget_stop},
        "aggregate": aggregate(results),
        "results": results,
        "note": "答案规则只检查结构化 finalAnswer，是保守自动筛查，不等于人工语义准确率。",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 SeeIt AI 真实视频问答评测集")
    parser.add_argument("--dataset", type=Path, default=Path(__file__).parents[1] / "evals" / "real_video_eval.json")
    parser.add_argument("--media-map", type=Path, required=True, help="JSON 文件，例如 {\"video-001\": 11, \"video-002\": 12}")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", default=os.getenv("SEEIT_API_URL", "http://127.0.0.1:9090"))
    parser.add_argument("--token", default=os.getenv("SEEIT_EVAL_TOKEN"))
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--resume", action="store_true", help="跳过输出文件中已经完成的用例")
    parser.add_argument("--case-id", action="append", default=[], help="只运行指定 caseId，可重复传入")
    parser.add_argument("--max-cases", type=int, default=0, help="本次最多计划运行的 case 数；0 表示不限")
    parser.add_argument("--max-provider-calls", type=int, default=0, help="达到 Provider 调用数后停止；0 表示不限")
    parser.add_argument("--max-total-tokens", type=int, default=0, help="达到总 Token 后停止；0 表示不限")
    parser.add_argument("--max-estimated-cost-usd", type=float, default=0, help="达到估算美元费用后停止；0 表示不限")
    args = parser.parse_args()
    if not args.token:
        raise SystemExit("请通过 --token 或 SEEIT_EVAL_TOKEN 提供登录 Token")

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    media_map = json.loads(args.media_map.read_text(encoding="utf-8"))
    selected_cases = dataset["cases"]
    if args.case_id:
        selected = set(args.case_id)
        selected_cases = [case for case in dataset["cases"] if case["caseId"] in selected]
        missing = selected - {case["caseId"] for case in selected_cases}
        if missing:
            raise SystemExit(f"评测集不存在 caseId：{', '.join(sorted(missing))}")
    requested_case_count = len(selected_cases)
    if args.max_cases > 0:
        selected_cases = selected_cases[:args.max_cases]
    base_url = args.base_url.rstrip("/")
    result_by_case: dict[str, dict[str, Any]] = {}
    run_id = time.strftime("%Y%m%dT%H%M%S%z")
    if args.resume and args.output.exists():
        previous = json.loads(args.output.read_text(encoding="utf-8"))
        run_id = str(previous.get("runId") or run_id)
        result_by_case = {
            str(item["caseId"]): item
            for item in previous.get("results", [])
            if item.get("caseId")
        }
    budget_config = {
        "maxCases": max(0, args.max_cases),
        "maxProviderCalls": max(0, args.max_provider_calls),
        "maxTotalTokens": max(0, args.max_total_tokens),
        "maxEstimatedCostUsd": max(0.0, args.max_estimated_cost_usd),
    }
    budget_stop: dict[str, Any] | None = None
    write_run_output(
        args.output,
        run_id=run_id,
        dataset_id=dataset.get("datasetId"),
        media_map=media_map,
        selected_cases=selected_cases,
        result_by_case=result_by_case,
        requested_case_count=requested_case_count,
        budget_config=budget_config,
        budget_stop=None,
    )
    with httpx.Client(headers={"Authorization": f"Bearer {args.token}"}) as client:
        for case in selected_cases:
            current_results = [
                result_by_case[item["caseId"]]
                for item in selected_cases
                if item["caseId"] in result_by_case
            ]
            budget_stop = budget_stop_reason(
                current_results,
                max_provider_calls=budget_config["maxProviderCalls"],
                max_total_tokens=budget_config["maxTotalTokens"],
                max_estimated_cost_usd=budget_config["maxEstimatedCostUsd"],
                reserve_next_case=True,
            )
            if budget_stop:
                print(f"STOP budget={budget_stop['reason']}", flush=True)
                break
            previous_result = result_by_case.get(case["caseId"])
            if previous_result and previous_result.get("state") == "COMPLETED":
                print(f"SKIP {case['caseId']} state=COMPLETED", flush=True)
                continue
            media_id = media_map.get(case["videoId"])
            if media_id is None:
                raise SystemExit(f"缺少 {case['videoId']} 的 mediaId")
            print(f"RUN {case['caseId']}", flush=True)
            try:
                result = run_case(client, base_url, int(media_id), case, args.timeout_seconds)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                result = {
                    "caseId": case["caseId"],
                    "videoId": case["videoId"],
                    "type": case["type"],
                    "answerable": bool(case["answerable"]),
                    "state": "SCRIPT_ERROR",
                    "error": error,
                    "failureCategory": classify_failure(error, exc),
                    "elapsedMs": 0,
                }
            result_by_case[case["caseId"]] = result
            print(
                f"DONE {case['caseId']} state={result.get('state')} "
                f"rule={result.get('answerCheck', {}).get('rulePassed')}",
                flush=True,
            )
            write_run_output(
                args.output,
                run_id=run_id,
                dataset_id=dataset.get("datasetId"),
                media_map=media_map,
                selected_cases=selected_cases,
                result_by_case=result_by_case,
                requested_case_count=requested_case_count,
                budget_config=budget_config,
                budget_stop=None,
            )

    final_results = [
        result_by_case[case["caseId"]]
        for case in selected_cases
        if case["caseId"] in result_by_case
    ]
    budget_stop = budget_stop or budget_stop_reason(
        final_results,
        max_provider_calls=budget_config["maxProviderCalls"],
        max_total_tokens=budget_config["maxTotalTokens"],
        max_estimated_cost_usd=budget_config["maxEstimatedCostUsd"],
    )
    output = write_run_output(
        args.output,
        run_id=run_id,
        dataset_id=dataset.get("datasetId"),
        media_map=media_map,
        selected_cases=selected_cases,
        result_by_case=result_by_case,
        requested_case_count=requested_case_count,
        budget_config=budget_config,
        budget_stop=budget_stop,
    )
    print(json.dumps(output["aggregate"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
