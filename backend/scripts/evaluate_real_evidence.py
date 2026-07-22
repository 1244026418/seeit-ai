"""Evaluate ASR/OCR retrieval profiles against annotated evidence spans."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from seeit.retrieval import (  # noqa: E402
    LEXICAL_PROFILE,
    RETRIEVER_PROFILES,
    RetrieverProfile,
    SearchRetriever,
    get_retriever_profile,
)


def overlaps(segment: dict[str, Any], gold: dict[str, Any]) -> bool:
    return int(segment.get("endMs", 0)) >= int(gold["startMs"]) and int(
        segment.get("startMs", 0)
    ) <= int(gold["endMs"])


def requirements(case: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (str(evidence["evidenceId"]), str(source).upper(), evidence)
        for evidence in case.get("goldEvidence", [])
        for source in evidence.get("sources", [])
    ]


def matched_requirements(
    segments: list[dict[str, Any]],
    required: list[tuple[str, str, dict[str, Any]]],
) -> set[tuple[str, str]]:
    matched: set[tuple[str, str]] = set()
    for evidence_id, source, gold in required:
        if any(
            str(item.get("source", "")).upper() == source and overlaps(item, gold)
            for item in segments
        ):
            matched.add((evidence_id, source))
    return matched


def expand_windows(
    matches: list[dict[str, Any]],
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Approximate AgentToolbox.get_evidence_window for each retrieved anchor."""
    expanded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in matches:
        start = max(0, int(anchor.get("startMs", 0)) - 15000)
        end = int(anchor.get("startMs", 0)) + 15000
        for segment in segments:
            segment_id = str(segment.get("segmentId"))
            if segment_id in seen:
                continue
            if int(segment.get("endMs", 0)) >= start and int(segment.get("startMs", 0)) <= end:
                expanded.append(segment)
                seen.add(segment_id)
    return expanded


def retrieval_metrics(
    matches: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    required: list[tuple[str, str, dict[str, Any]]],
    top_ks: list[int],
) -> dict[str, Any]:
    required_keys = {(item[0], item[1]) for item in required}
    available = matched_requirements(segments, required)
    first_rank = 0
    for index, item in enumerate(matches, start=1):
        if matched_requirements([item], required):
            first_rank = index
            break
    metrics: dict[str, Any] = {
        "requiredEvidenceCount": len(required_keys),
        "availableEvidenceCount": len(available),
        "extractionCoverage": round(len(available) / max(1, len(required_keys)), 4),
        "reciprocalRank": round(1 / first_rank, 4) if first_rank else 0.0,
    }
    for top in top_ks:
        hits = matched_requirements(matches[:top], required)
        window_hits = matched_requirements(expand_windows(matches[:top], segments), required)
        metrics[f"recallAt{top}"] = round(len(hits) / len(required_keys), 4)
        metrics[f"hitAt{top}"] = bool(hits)
        metrics[f"completeHitAt{top}"] = hits == required_keys
        metrics[f"windowRecallAt{top}"] = round(len(window_hits) / len(required_keys), 4)
        metrics[f"windowHitAt{top}"] = bool(window_hits)
        metrics[f"windowCompleteHitAt{top}"] = window_hits == required_keys
    return metrics


def evaluate_case(
    case: dict[str, Any],
    segments: list[dict[str, Any]],
    top_ks: list[int],
    *,
    retriever: SearchRetriever | None = None,
    profile: RetrieverProfile = LEXICAL_PROFILE,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    normalized_top_ks = sorted({max(1, int(item)) for item in top_ks})
    active_retriever = retriever or profile.create(segments)
    started = clock()
    result = active_retriever.search(str(case["question"]), top_k=max(normalized_top_ks))
    search_latency_ms = max(0.0, (clock() - started) * 1000)
    matches = result["matches"]
    required = requirements(case)
    detail: dict[str, Any] = {
        "caseId": case["caseId"],
        "videoId": case["videoId"],
        "type": case["type"],
        "answerable": bool(case["answerable"]),
        "profileId": profile.profile_id,
        "retrievalMode": str(result.get("retrievalMode") or profile.retrieval_mode),
        "searchLatencyMs": round(search_latency_ms, 4),
        "requiredEvidenceCount": len(required),
        "availableEvidenceCount": len(matched_requirements(segments, required)),
        "extractionCoverage": (
            round(len(matched_requirements(segments, required)) / max(1, len(required)), 4)
            if required
            else None
        ),
        "requiredSources": sorted({item[1] for item in required}),
        "fallbackToTimelineStart": bool(result.get("fallbackToTimelineStart")),
        "positiveMatchCount": int(result.get("matchedCount", 0)),
        "topScore": float(matches[0].get("score", 0)) if matches else 0.0,
        "indexReused": result.get("indexReused"),
        "abstention": result.get("abstention"),
        "coveragePlan": result.get("coveragePlan"),
        "evidenceSufficiency": result.get("evidenceSufficiency"),
        "predictedAbstained": bool((result.get("abstention") or {}).get("abstained")),
        "retrieved": [
            {
                "segmentId": item.get("segmentId"),
                "source": item.get("source"),
                "startMs": item.get("startMs"),
                "endMs": item.get("endMs"),
                "score": item.get("score"),
                "content": item.get("content"),
                "coverageRequirementIds": item.get("coverageRequirementIds"),
                "coverageRequirementRanks": item.get("coverageRequirementRanks"),
                "coverageSelectionReasons": item.get("coverageSelectionReasons"),
            }
            for item in matches
        ],
    }
    if not required:
        detail["unanswerableReturnedPositive"] = bool(result.get("matchedCount", 0))
        detail["outcome"] = "UNANSWERABLE"
        detail["sourceMetrics"] = {}
        return detail

    detail.update(retrieval_metrics(matches, segments, required, normalized_top_ks))
    detail["sourceMetrics"] = {
        source: retrieval_metrics(
            matches,
            segments,
            [item for item in required if item[1] == source],
            normalized_top_ks,
        )
        for source in detail["requiredSources"]
    }
    if detail["availableEvidenceCount"] < detail["requiredEvidenceCount"]:
        detail["outcome"] = "EVIDENCE_EXTRACTION_MISSING"
    elif detail[f"completeHitAt{max(normalized_top_ks)}"]:
        detail["outcome"] = "HIT"
    else:
        detail["outcome"] = "RETRIEVAL_MISS"
    return detail


def latency_summary(values: list[float]) -> dict[str, Any]:
    ordered = sorted(max(0.0, float(value)) for value in values)

    def percentile(percent: float) -> float:
        if not ordered:
            return 0.0
        index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percent + 0.999999)))
        return round(ordered[index], 4)

    return {
        "sampleCount": len(ordered),
        "totalMs": round(sum(ordered), 4),
        "meanMs": round(sum(ordered) / max(1, len(ordered)), 4),
        "p50Ms": percentile(0.50),
        "p95Ms": percentile(0.95),
        "maxMs": round(ordered[-1], 4) if ordered else 0.0,
    }


def summarize_metrics(items: list[dict[str, Any]], top_ks: list[int]) -> dict[str, Any]:
    summary: dict[str, Any] = {"caseCount": len(items)}
    if not items:
        return summary
    summary["extractionCoverage"] = round(
        sum(float(item.get("extractionCoverage") or 0) for item in items) / len(items), 4
    )
    summary["mrr"] = round(
        sum(float(item.get("reciprocalRank", 0)) for item in items) / len(items), 4
    )
    for top in top_ks:
        summary[f"recallAt{top}"] = round(
            sum(float(item[f"recallAt{top}"]) for item in items) / len(items), 4
        )
        summary[f"hitRateAt{top}"] = round(
            sum(bool(item[f"hitAt{top}"]) for item in items) / len(items), 4
        )
        summary[f"completeHitRateAt{top}"] = round(
            sum(bool(item[f"completeHitAt{top}"]) for item in items) / len(items), 4
        )
        summary[f"windowRecallAt{top}"] = round(
            sum(float(item[f"windowRecallAt{top}"]) for item in items) / len(items), 4
        )
        summary[f"windowCompleteHitRateAt{top}"] = round(
            sum(bool(item[f"windowCompleteHitAt{top}"]) for item in items) / len(items), 4
        )
    return summary


def aggregate(details: list[dict[str, Any]], top_ks: list[int]) -> dict[str, Any]:
    normalized_top_ks = sorted({max(1, int(item)) for item in top_ks})
    answerable = [item for item in details if item["answerable"]]
    unanswerable = [item for item in details if not item["answerable"]]
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in answerable:
        by_type[item["type"]].append(item)
    sources = sorted({
        "ASR",
        "OCR",
        *(
            source
            for item in answerable
            for source in (item.get("sourceMetrics") or {})
        ),
    })
    by_source = {
        source: summarize_metrics(
            [
                item["sourceMetrics"][source]
                for item in answerable
                if source in (item.get("sourceMetrics") or {})
            ],
            normalized_top_ks,
        )
        for source in sources
    }
    true_abstentions = sum(bool(item.get("predictedAbstained")) for item in unanswerable)
    false_abstentions = sum(bool(item.get("predictedAbstained")) for item in answerable)
    predicted_abstentions = true_abstentions + false_abstentions
    abstention_precision = true_abstentions / max(1, predicted_abstentions)
    abstention_recall = true_abstentions / max(1, len(unanswerable))
    abstention_f1 = (
        2 * abstention_precision * abstention_recall
        / (abstention_precision + abstention_recall)
        if abstention_precision + abstention_recall
        else 0.0
    )
    coverage_states = [
        item["evidenceSufficiency"]
        for item in answerable
        if isinstance(item.get("evidenceSufficiency"), dict)
    ]
    requirement_count = sum(
        int(item.get("requirementCount", 0)) for item in coverage_states
    )
    satisfied_requirement_count = sum(
        int(item.get("satisfiedRequirementCount", 0)) for item in coverage_states
    )
    return {
        **summarize_metrics(answerable, normalized_top_ks),
        "answerableCaseCount": len(answerable),
        "unanswerableCaseCount": len(unanswerable),
        "unanswerablePositiveRetrievalRate": round(
            sum(bool(item.get("unanswerableReturnedPositive")) for item in unanswerable)
            / max(1, len(unanswerable)),
            4,
        ),
        "abstention": {
            "predictedCount": predicted_abstentions,
            "truePositiveCount": true_abstentions,
            "falsePositiveCount": false_abstentions,
            "precision": round(abstention_precision, 4),
            "recall": round(abstention_recall, 4),
            "f1": round(abstention_f1, 4),
            "answerableFalseAbstentionRate": round(
                false_abstentions / max(1, len(answerable)),
                4,
            ),
        },
        "requirementCoverage": {
            "caseCount": len(coverage_states),
            "requirementCount": requirement_count,
            "satisfiedRequirementCount": satisfied_requirement_count,
            "satisfiedRate": round(
                satisfied_requirement_count / max(1, requirement_count),
                4,
            ),
            "fullyCoveredCaseRate": round(
                sum(bool(item.get("fullyCovered")) for item in coverage_states)
                / max(1, len(coverage_states)),
                4,
            ),
        },
        "searchLatency": latency_summary([
            float(item["searchLatencyMs"])
            for item in details
            if item.get("searchLatencyMs") is not None
        ]),
        "byType": {
            key: summarize_metrics(items, normalized_top_ks)
            for key, items in sorted(by_type.items())
        },
        "bySource": by_source,
    }


def evaluate_profile(
    dataset: dict[str, Any],
    snapshots: dict[str, list[dict[str, Any]]],
    profile: RetrieverProfile = LEXICAL_PROFILE,
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    top_ks = sorted({max(1, int(item)) for item in dataset.get("topK", [1, 3, 8])})
    video_ids = sorted({str(case["videoId"]) for case in dataset.get("cases", [])})
    missing = [video_id for video_id in video_ids if video_id not in snapshots]
    if missing:
        raise ValueError(f"证据快照缺少视频：{', '.join(missing)}")

    retrievers: dict[str, SearchRetriever] = {}
    build_latencies: list[float] = []
    for video_id in video_ids:
        started = clock()
        retrievers[video_id] = profile.create(snapshots[video_id])
        build_latencies.append(max(0.0, (clock() - started) * 1000))

    details = [
        evaluate_case(
            case,
            snapshots[str(case["videoId"])],
            top_ks,
            retriever=retrievers[str(case["videoId"])],
            profile=profile,
            clock=clock,
        )
        for case in dataset.get("cases", [])
    ]
    return {
        "profile": profile.metadata(),
        "topK": top_ks,
        "indexBuildLatency": latency_summary(build_latencies),
        "aggregate": aggregate(details, top_ks),
        "cases": details,
    }


def snapshot_statistics(
    snapshots: dict[str, list[dict[str, Any]]],
    media_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    for video_id, segments in snapshots.items():
        canonical = json.dumps(segments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        item: dict[str, Any] = {
            "segmentCount": len(segments),
            "sourceCounts": dict(Counter(str(segment.get("source")) for segment in segments)),
            "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        }
        if media_map and video_id in media_map:
            item["mediaId"] = int(media_map[video_id])
        stats[video_id] = item
    return stats


def snapshot_readiness(
    dataset: dict[str, Any],
    snapshots: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    videos: dict[str, Any] = {}
    for video in dataset.get("videos", []):
        video_id = str(video["videoId"])
        segments = snapshots.get(video_id)
        source_counts = Counter(
            str(item.get("source", "")).upper() for item in (segments or [])
        )
        missing_conditions: list[str] = []
        if segments is None:
            missing_conditions.append("MEDIA_SNAPSHOT_MISSING")
        if source_counts["ASR"] <= 0:
            missing_conditions.append("ASR_MISSING")
        if source_counts["OCR"] <= 0:
            missing_conditions.append("OCR_MISSING")
        videos[video_id] = {
            "ready": not missing_conditions,
            "segmentCount": len(segments or []),
            "asrCount": source_counts["ASR"],
            "ocrCount": source_counts["OCR"],
            "missingConditions": missing_conditions,
        }
    return {
        "ready": bool(videos) and all(item["ready"] for item in videos.values()),
        "videos": videos,
    }


def load_snapshots(path: Path) -> dict[str, list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict) and isinstance(payload.get("segmentSnapshots"), dict):
        payload = payload["segmentSnapshots"]
    if not isinstance(payload, dict):
        raise ValueError("快照文件必须是 videoId 到 segments 数组的 JSON 对象")
    snapshots: dict[str, list[dict[str, Any]]] = {}
    for video_id, segments in payload.items():
        if not isinstance(segments, list) or not all(isinstance(item, dict) for item in segments):
            raise ValueError(f"{video_id} 的证据快照必须是对象数组")
        snapshots[str(video_id)] = [dict(item) for item in segments]
    return snapshots


def fetch_snapshots(
    dataset: dict[str, Any],
    media_map: dict[str, Any],
    base_url: str,
    token: str,
) -> dict[str, list[dict[str, Any]]]:
    snapshots: dict[str, list[dict[str, Any]]] = {}
    with httpx.Client(headers={"Authorization": f"Bearer {token}"}) as client:
        for video in dataset["videos"]:
            video_id = str(video["videoId"])
            response = client.get(
                f"{base_url.rstrip('/')}/analysis/transcription-status",
                params={"id": int(media_map[video_id])},
                timeout=30,
            )
            response.raise_for_status()
            snapshots[video_id] = response.json().get("segments", [])
    return snapshots


def main() -> None:
    parser = argparse.ArgumentParser(description="离线比较 ASR/OCR Evidence Retriever Profile")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=BACKEND_ROOT / "evals" / "real_video_eval.json",
    )
    parser.add_argument("--snapshot", type=Path, help="离线证据快照 JSON；提供后不会请求 SeeIt API")
    parser.add_argument("--media-map", type=Path, help="未提供 --snapshot 时用于从 SeeIt API 获取证据")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=sorted(RETRIEVER_PROFILES), default="lexical")
    parser.add_argument("--base-url", default=os.getenv("SEEIT_API_URL", "http://127.0.0.1:9090"))
    parser.add_argument("--token", default=os.getenv("SEEIT_EVAL_TOKEN"))
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text(encoding="utf-8-sig"))
    media_map: dict[str, Any] | None = None
    if args.snapshot:
        snapshots = load_snapshots(args.snapshot)
    else:
        if not args.media_map or not args.token:
            raise SystemExit("未提供 --snapshot 时，必须同时提供 --media-map 和 --token")
        media_map = json.loads(args.media_map.read_text(encoding="utf-8-sig"))
        snapshots = fetch_snapshots(dataset, media_map, args.base_url, args.token)

    readiness = snapshot_readiness(dataset, snapshots)
    if not readiness["ready"]:
        raise SystemExit(
            "证据快照未就绪，拒绝运行基线："
            + json.dumps(readiness["videos"], ensure_ascii=False)
        )
    profile = get_retriever_profile(args.profile)
    evaluation = evaluate_profile(dataset, snapshots, profile)
    output = {
        "runId": time.strftime("%Y%m%dT%H%M%S%z"),
        "datasetId": dataset.get("datasetId"),
        **evaluation,
        "videos": snapshot_statistics(snapshots, media_map),
        "segmentSnapshots": snapshots,
        "note": (
            "V1 真实视频集只作为回归基线，不能单独证明新 Retriever 有效；"
            "时间窗命中不等于 ASR/OCR 文本逐字准确率，无答案题不混入 Recall/MRR。"
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"profile": output["profile"], "aggregate": output["aggregate"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
