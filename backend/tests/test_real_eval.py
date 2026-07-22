import json
from pathlib import Path

import httpx

from scripts.evaluate_real_evidence import aggregate as aggregate_evidence
from scripts.evaluate_real_evidence import evaluate_case
from scripts.evaluate_real_video import aggregate as aggregate_video
from scripts.evaluate_real_video import (
    aggregate_provider_usage,
    budget_stop_reason,
    classify_failure,
    evaluate_answer,
    evaluate_status_answer,
    write_run_output,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_real_dataset_has_unique_cases_and_valid_video_references() -> None:
    dataset = json.loads((BACKEND_ROOT / "evals" / "real_video_eval.json").read_text(encoding="utf-8"))
    video_ids = {item["videoId"] for item in dataset["videos"]}
    case_ids = [item["caseId"] for item in dataset["cases"]]

    assert len(dataset["videos"]) == 2
    assert len(case_ids) == 16
    assert len(set(case_ids)) == len(case_ids)
    assert {item["videoId"] for item in dataset["cases"]} == video_ids


def test_answer_rules_require_every_group_and_reject_forbidden_patterns() -> None:
    case = {
        "requiredAnswerGroups": [["拆解", "分解"], ["token", "词元"]],
        "forbiddenAnswerPatterns": ["加密货币"],
    }

    assert evaluate_answer(case, "分词器把文字分解成一个个 token")["rulePassed"] is True
    assert evaluate_answer(case, "分词器可以拆解文字")["rulePassed"] is False
    assert evaluate_answer(case, "分词器把文字拆解成 token，用于加密货币")["rulePassed"] is False


def test_answer_rules_score_only_structured_final_answer() -> None:
    case = {
        "requiredAnswerGroups": [["一個 token", "一个 token"]],
        "forbiddenAnswerPatterns": ["两个 token"],
    }
    status = {
        "finalAnswer": "在 Qwen 模型里是一个 token。",
        "report": "证据对比中提到了错误反例：两个 token。",
    }

    assert evaluate_status_answer(case, status)["rulePassed"] is True


def test_canonical_refusal_does_not_echo_forbidden_question_text() -> None:
    case = {
        "requiredAnswerGroups": [["视频未明确说明", "视频没有说明", "无法从视频确定"]],
        "forbiddenAnswerPatterns": ["加密领域的 token 是"],
    }

    result = evaluate_answer(case, "视频未提供足够证据，无法从视频确定答案。")

    assert result["rulePassed"] is True


def test_infrastructure_failures_are_excluded_from_model_success_rate() -> None:
    infrastructure = {
        "caseId": "network",
        "type": "unanswerable",
        "state": "SCRIPT_ERROR",
        "failureCategory": classify_failure("ConnectError: [WinError 10061] Connection refused"),
    }
    completed = {
        "caseId": "ok",
        "type": "direct_fact",
        "state": "COMPLETED",
        "failureCategory": None,
        "answerCheck": {"rulePassed": True},
        "evaluation": {"evidenceSupportRate": 1.0},
    }

    summary = aggregate_video([completed, infrastructure])

    assert infrastructure["failureCategory"] == "INFRASTRUCTURE"
    assert summary["taskSuccessRate"] == 0.5
    assert summary["modelEvaluableCaseCount"] == 1
    assert summary["modelTaskSuccessRate"] == 1.0


def test_http_429_is_classified_as_rate_limit() -> None:
    request = httpx.Request("POST", "http://localhost/analysis/ai")
    response = httpx.Response(429, request=request)
    error = httpx.HTTPStatusError("rate limited", request=request, response=response)

    assert classify_failure(error, error) == "RATE_LIMIT"

    summary = aggregate_video([{
        "caseId": "rate-limited",
        "type": "direct_fact",
        "state": "SCRIPT_ERROR",
        "failureCategory": "RATE_LIMIT",
    }])
    assert summary["rateLimitFailureCount"] == 1
    assert summary["modelEvaluableCaseCount"] == 0


def test_provider_usage_aggregate_and_budget_gates() -> None:
    results = [{
        "caseId": "usage-1",
        "trace": {
            "providerUsage": {
                "requestCount": 3,
                "successCount": 3,
                "failureCount": 0,
                "usageReportedCount": 3,
                "promptTokens": 1200,
                "completionTokens": 300,
                "totalTokens": 1500,
                "cacheHitTokens": 200,
                "cacheMissTokens": 1000,
                "toolCallCount": 4,
                "estimatedCostUsd": 0.006,
                "costConfigured": True,
            },
            "providerRequests": [
                {"latencyMs": 100},
                {"latencyMs": 200},
                {"latencyMs": 500},
            ],
        },
    }]

    usage = aggregate_provider_usage(results)

    assert usage["requestCount"] == 3
    assert usage["totalTokens"] == 1500
    assert usage["estimatedCostUsd"] == 0.006
    assert usage["latencyMsP50"] == 200
    assert usage["latencyMsP95"] == 500
    assert budget_stop_reason(results, max_provider_calls=3)["reason"] == "MAX_PROVIDER_CALLS"
    assert budget_stop_reason(results, max_total_tokens=1500)["reason"] == "MAX_TOTAL_TOKENS"
    assert budget_stop_reason(results, max_estimated_cost_usd=0.006)["reason"] == "MAX_ESTIMATED_COST_USD"


def test_budget_gate_stops_when_usage_or_cost_is_unavailable() -> None:
    missing_usage = [{"caseId": "legacy", "state": "COMPLETED", "trace": "legacy-json"}]
    no_cost = [{
        "caseId": "no-cost",
        "providerUsage": {
            "requestCount": 1,
            "successCount": 1,
            "failureCount": 0,
            "usageReportedCount": 1,
            "totalTokens": 100,
            "estimatedCostUsd": None,
            "costConfigured": False,
        },
    }]

    assert budget_stop_reason(missing_usage, max_provider_calls=5)["reason"] == "PROVIDER_USAGE_UNAVAILABLE"
    assert budget_stop_reason(no_cost, max_estimated_cost_usd=1)["reason"] == "COST_ESTIMATE_UNAVAILABLE"


def test_budget_gate_reserves_largest_observed_case_before_starting_next_case() -> None:
    results = [
        {
            "caseId": "small",
            "providerUsage": {
                "requestCount": 2,
                "usageReportedCount": 2,
                "totalTokens": 100,
            },
        },
        {
            "caseId": "large",
            "providerUsage": {
                "requestCount": 5,
                "usageReportedCount": 5,
                "totalTokens": 300,
            },
        },
    ]

    call_stop = budget_stop_reason(
        results,
        max_provider_calls=11,
        reserve_next_case=True,
    )
    token_stop = budget_stop_reason(
        results,
        max_total_tokens=699,
        reserve_next_case=True,
    )

    assert call_stop == {
        "reason": "MAX_PROVIDER_CALLS_RESERVE",
        "limit": 11,
        "observed": 7,
        "reservedForNextCase": 5,
        "projected": 12,
    }
    assert token_stop == {
        "reason": "MAX_TOTAL_TOKENS_RESERVE",
        "limit": 699,
        "observed": 400,
        "reservedForNextCase": 300,
        "projected": 700,
    }
    assert budget_stop_reason(
        results,
        max_total_tokens=700,
        reserve_next_case=True,
    ) is None


def test_incremental_eval_output_preserves_partial_results(tmp_path: Path) -> None:
    output_path = tmp_path / "partial.json"
    cases = [
        {"caseId": "case-1"},
        {"caseId": "case-2"},
    ]
    first = {
        "caseId": "case-1",
        "state": "COMPLETED",
        "type": "direct_fact",
        "answerCheck": {"rulePassed": True},
    }

    output = write_run_output(
        output_path,
        run_id="run-budget",
        dataset_id="dataset-1",
        media_map={"video-001": 1},
        selected_cases=cases,
        result_by_case={"case-1": first},
        requested_case_count=2,
        budget_config={"maxCases": 2, "maxProviderCalls": 3},
        budget_stop={"reason": "MAX_PROVIDER_CALLS", "limit": 3, "observed": 3},
    )

    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert output["remainingCaseIds"] == ["case-2"]
    assert saved["results"] == [first]
    assert saved["budget"]["stop"]["reason"] == "MAX_PROVIDER_CALLS"


def test_evidence_window_completes_asr_and_ocr_requirement() -> None:
    case = {
        "caseId": "asr-ocr-window",
        "videoId": "video-001",
        "type": "asr_ocr",
        "answerable": True,
        "question": "请念一下这几个字",
        "goldEvidence": [{
            "evidenceId": "gold-1",
            "startMs": 59000,
            "endMs": 75000,
            "sources": ["ASR", "OCR"],
        }],
    }
    segments = [
        {"segmentId": "asr-1", "source": "ASR", "startMs": 58000, "endMs": 66000, "content": "请念一下这几个字"},
        {"segmentId": "ocr-1", "source": "OCR", "startMs": 60000, "endMs": 75000, "content": "旯妁圳侈邯"},
    ]

    result = evaluate_case(case, segments, [1])

    assert result["recallAt1"] == 0.5
    assert result["completeHitAt1"] is False
    assert result["windowRecallAt1"] == 1.0
    assert result["windowCompleteHitAt1"] is True

    unanswerable = {
        "caseId": "none",
        "videoId": "video-001",
        "type": "unanswerable",
        "answerable": False,
        "requiredEvidenceCount": 0,
        "availableEvidenceCount": 0,
        "extractionCoverage": None,
        "positiveMatchCount": 1,
        "unanswerableReturnedPositive": True,
    }
    summary = aggregate_evidence([result, unanswerable], [1])
    assert summary["answerableCaseCount"] == 1
    assert summary["unanswerableCaseCount"] == 1
    assert summary["unanswerablePositiveRetrievalRate"] == 1.0
