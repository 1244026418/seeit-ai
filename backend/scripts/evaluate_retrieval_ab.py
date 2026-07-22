"""Run lexical, local dense, and hybrid retrieval on one frozen snapshot."""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.evaluate_real_evidence import (  # noqa: E402
    aggregate,
    evaluate_case,
    evaluate_profile,
    latency_summary,
    load_snapshots,
    snapshot_readiness,
    snapshot_statistics,
)
from seeit.embedding_retrieval import (  # noqa: E402
    DEFAULT_EMBEDDING_MODEL_FILE,
    DEFAULT_EMBEDDING_REPO,
    DEFAULT_EMBEDDING_REVISION,
    DEFAULT_QUERY_PREFIX,
    LocalOnnxEmbeddingBackend,
    coverage_aware_hybrid_profile,
    contextual_hybrid_profile,
    dense_profile,
    hybrid_profile,
)
from seeit.retrieval import LEXICAL_PROFILE  # noqa: E402
from seeit.retrieval import (  # noqa: E402
    ContextualEvidenceRetriever,
    CoverageAwareEvidenceRetriever,
    EvidenceRetriever,
    RetrieverProfile,
)


METRIC_KEYS = (
    "mrr",
    "recallAt1",
    "recallAt3",
    "recallAt8",
    "completeHitRateAt8",
    "windowRecallAt8",
    "windowCompleteHitRateAt8",
    "unanswerablePositiveRetrievalRate",
)


def metric_summary(evaluation: dict[str, Any]) -> dict[str, Any]:
    aggregate = evaluation["aggregate"]
    return {
        key: aggregate.get(key)
        for key in METRIC_KEYS
    } | {
        "searchLatency": aggregate.get("searchLatency"),
        "abstention": aggregate.get("abstention"),
        "requirementCoverage": aggregate.get("requirementCoverage"),
        "indexBuildLatency": evaluation.get("indexBuildLatency"),
        "modelLoadMs": evaluation.get("modelLoadMs", 0),
    }


def comparison(profiles: dict[str, dict[str, Any]]) -> dict[str, Any]:
    lexical = profiles["lexical"]["aggregate"]
    summaries = {name: metric_summary(value) for name, value in profiles.items()}
    deltas: dict[str, Any] = {}
    for name in (item for item in profiles if item != "lexical"):
        aggregate = profiles[name]["aggregate"]
        deltas[name] = {
            key: round(float(aggregate.get(key) or 0) - float(lexical.get(key) or 0), 4)
            for key in METRIC_KEYS
        }
    case_ids = [item["caseId"] for item in profiles["lexical"]["cases"]]
    by_profile = {
        name: {item["caseId"]: item for item in result["cases"]}
        for name, result in profiles.items()
    }
    case_matrix = [
        {
            "caseId": case_id,
            **{
                name: {
                    "outcome": by_profile[name][case_id]["outcome"],
                    "recallAt8": by_profile[name][case_id].get("recallAt8"),
                    "windowRecallAt8": by_profile[name][case_id].get("windowRecallAt8"),
                    "predictedAbstained": by_profile[name][case_id].get("predictedAbstained"),
                    "fullyCovered": (
                        by_profile[name][case_id].get("evidenceSufficiency") or {}
                    ).get("fullyCovered"),
                }
                for name in profiles
            },
        }
        for case_id in case_ids
    ]
    return {"summaries": summaries, "deltaVsLexical": deltas, "cases": case_matrix}


def build_embedding_backend(args: argparse.Namespace) -> tuple[LocalOnnxEmbeddingBackend, float]:
    started = time.perf_counter()
    backend = LocalOnnxEmbeddingBackend(
        args.model_dir,
        repo_id=args.model_repo,
        revision=args.model_revision,
        model_file=args.model_file,
        allow_download=args.allow_model_download,
        cpu_threads=args.cpu_threads,
        batch_size=args.batch_size,
    )
    return backend, round((time.perf_counter() - started) * 1000, 4)


def evaluate_qdrant_profile(
    dataset: dict[str, Any],
    snapshots: dict[str, list[dict[str, Any]]],
    backend: Any,
    store: Any,
    *,
    coverage_aware: bool = False,
    clock: Any = time.perf_counter,
) -> dict[str, Any]:
    """Evaluate the production Qdrant hybrid path with the same frozen policy."""
    from seeit.qdrant_retrieval import QdrantHybridRetriever

    profile = RetrieverProfile(
        profile_id=(
            "coverage-aware-qdrant-hybrid-v2"
            if coverage_aware
            else "contextual-qdrant-hybrid-v1"
        ),
        retrieval_mode=(
            "COVERAGE_AWARE_CONTEXTUAL_QDRANT_HYBRID_LEXICAL_DENSE_RRF"
            if coverage_aware
            else "CONTEXTUAL_QDRANT_HYBRID_LEXICAL_DENSE_RRF"
        ),
        factory=EvidenceRetriever,
        description=(
            "Qdrant Contextual Hybrid 上增加证据需求规划与当前视频锚点拒答"
            if coverage_aware
            else "Qdrant dense + lexical 等权 RRF，并复用 Contextual v2 与双低拒答"
        ),
        settings={
            "storage": "Qdrant",
            "baseProfile": "hybrid-lexical-bge-rrf-v1",
            "abstention": (
                "CURRENT_VIDEO_REQUIRED_ANCHORS_V2"
                if coverage_aware
                else "DENSE_AND_LEXICAL_FLOOR_V1"
            ),
        },
    )
    top_ks = sorted({max(1, int(item)) for item in dataset.get("topK", [1, 3, 8])})
    video_ids = sorted({str(case["videoId"]) for case in dataset.get("cases", [])})
    retrievers = {}
    build_latencies = []
    for video_id in video_ids:
        started = clock()
        segments = snapshots[video_id]
        contextual = ContextualEvidenceRetriever(
            segments,
            QdrantHybridRetriever(video_id, segments, backend, store),
            enable_abstention=not coverage_aware,
        )
        retrievers[video_id] = (
            CoverageAwareEvidenceRetriever(segments, contextual)
            if coverage_aware
            else contextual
        )
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


def main() -> None:
    parser = argparse.ArgumentParser(description="运行冻结 held-out 的 lexical/dense/hybrid 离线 A/B")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--model-repo", default=DEFAULT_EMBEDDING_REPO)
    parser.add_argument("--model-revision", default=DEFAULT_EMBEDDING_REVISION)
    parser.add_argument("--model-file", default=DEFAULT_EMBEDDING_MODEL_FILE)
    parser.add_argument("--allow-model-download", action="store_true")
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument(
        "--scope",
        default="OFFLINE_RETRIEVAL_AB",
        help="写入结果的评测范围标签，例如 V1_DEVELOPMENT_RETRIEVAL_AB",
    )
    parser.add_argument("--qdrant-url", help="可选：同时评测生产 Qdrant Hybrid 路径")
    parser.add_argument("--qdrant-api-key", default="")
    parser.add_argument("--qdrant-collection", default="seeit_retrieval_eval")
    parser.add_argument("--qdrant-cleanup", action="store_true")
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text(encoding="utf-8-sig"))
    snapshots = load_snapshots(args.snapshot)
    readiness = snapshot_readiness(dataset, snapshots)
    if not readiness["ready"]:
        raise SystemExit("证据快照未就绪：" + json.dumps(readiness["videos"], ensure_ascii=False))

    profiles: dict[str, dict[str, Any]] = {
        "lexical": evaluate_profile(dataset, snapshots, LEXICAL_PROFILE),
    }
    model_settings = {
        "embeddingModel": args.model_repo,
        "modelRevision": args.model_revision,
        "modelFile": args.model_file,
        "queryPrefix": DEFAULT_QUERY_PREFIX,
        "runtime": "ONNXRuntime CPU",
    }

    dense_backend, dense_load_ms = build_embedding_backend(args)
    profiles["dense"] = evaluate_profile(
        dataset,
        snapshots,
        dense_profile(dense_backend, model_settings),
    )
    profiles["dense"]["modelLoadMs"] = dense_load_ms
    del dense_backend
    gc.collect()

    hybrid_backend, hybrid_load_ms = build_embedding_backend(args)
    profiles["hybrid"] = evaluate_profile(
        dataset,
        snapshots,
        hybrid_profile(hybrid_backend, model_settings),
    )
    profiles["hybrid"]["modelLoadMs"] = hybrid_load_ms
    profiles["contextual"] = evaluate_profile(
        dataset,
        snapshots,
        contextual_hybrid_profile(hybrid_backend, model_settings),
    )
    profiles["contextual"]["modelLoadMs"] = hybrid_load_ms
    profiles["coverage"] = evaluate_profile(
        dataset,
        snapshots,
        coverage_aware_hybrid_profile(hybrid_backend, model_settings),
    )
    profiles["coverage"]["modelLoadMs"] = hybrid_load_ms
    if args.qdrant_url:
        from qdrant_client import QdrantClient
        from seeit.qdrant_retrieval import QdrantEvidenceStore

        qdrant_client = QdrantClient(
            url=args.qdrant_url,
            api_key=args.qdrant_api_key or None,
            timeout=10,
        )
        qdrant_store = QdrantEvidenceStore(
            qdrant_client,
            collection_name=args.qdrant_collection,
        )
        profiles["qdrant"] = evaluate_qdrant_profile(
            dataset,
            snapshots,
            hybrid_backend,
            qdrant_store,
        )
        profiles["qdrant"]["modelLoadMs"] = hybrid_load_ms
        profiles["qdrantCoverage"] = evaluate_qdrant_profile(
            dataset,
            snapshots,
            hybrid_backend,
            qdrant_store,
            coverage_aware=True,
        )
        profiles["qdrantCoverage"]["modelLoadMs"] = hybrid_load_ms

    output = {
        "runId": time.strftime("%Y%m%dT%H%M%S%z"),
        "datasetId": dataset.get("datasetId"),
        "scope": args.scope,
        "videos": snapshot_statistics(snapshots),
        "profiles": profiles,
        "comparison": comparison(profiles),
        "note": (
            "全程只使用人工金标与本地证据快照；referenceAnswer 不参与评分。"
            "Contextual 的旧阈值和 Coverage-Aware 锚点策略都只在开发集上调试，"
            "不能视为泛化结论；最终未见集不得用于本脚本调参。"
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.qdrant_url and args.qdrant_cleanup and qdrant_client.collection_exists(
        args.qdrant_collection
    ):
        qdrant_client.delete_collection(args.qdrant_collection)
    print(json.dumps(output["comparison"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
