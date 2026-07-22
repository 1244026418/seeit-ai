"""Optional local ONNX embedding and hybrid retrieval profiles for offline evals."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from seeit.retrieval import (
    ContextualEvidenceRetriever,
    EvidenceRetriever,
    RetrieverProfile,
    coverage_hybrid_profile,
)


DEFAULT_EMBEDDING_REPO = "Xenova/bge-small-zh-v1.5"
DEFAULT_EMBEDDING_REVISION = "75c43b069aac4d136ba6bc1122f995fedcfd2781"
DEFAULT_EMBEDDING_MODEL_FILE = "onnx/model_quantized.onnx"
DEFAULT_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


class EmbeddingBackend(Protocol):
    def encode(self, texts: Sequence[str], *, query: bool = False) -> np.ndarray: ...


class LocalOnnxEmbeddingBackend:
    """Runs a pinned BGE model locally through ONNX Runtime."""

    def __init__(
        self,
        model_dir: Path,
        *,
        repo_id: str = DEFAULT_EMBEDDING_REPO,
        revision: str = DEFAULT_EMBEDDING_REVISION,
        model_file: str = DEFAULT_EMBEDDING_MODEL_FILE,
        query_prefix: str = DEFAULT_QUERY_PREFIX,
        allow_download: bool = False,
        cpu_threads: int = 4,
        batch_size: int = 32,
    ) -> None:
        self.model_dir = Path(model_dir).resolve()
        self.repo_id = repo_id
        self.revision = revision
        self.model_file = model_file
        self.query_prefix = query_prefix
        self.batch_size = max(1, int(batch_size))
        required_files = ("tokenizer.json", model_file)
        missing = [name for name in required_files if not (self.model_dir / name).is_file()]
        if missing and allow_download:
            self._download(required_files)
            missing = [name for name in required_files if not (self.model_dir / name).is_file()]
        if missing:
            raise FileNotFoundError(
                f"本地 Embedding 模型不完整：{', '.join(missing)}；"
                "请显式允许下载并把模型目录放在 F:\\temp"
            )

        import onnxruntime as ort
        from tokenizers import Tokenizer

        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, int(cpu_threads))
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(self.model_dir / model_file),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self.input_names = {item.name for item in self.session.get_inputs()}
        self.tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")

    def _download(self, required_files: Sequence[str]) -> None:
        from huggingface_hub import hf_hub_download

        self.model_dir.mkdir(parents=True, exist_ok=True)
        for filename in required_files:
            hf_hub_download(
                repo_id=self.repo_id,
                filename=filename,
                revision=self.revision,
                local_dir=self.model_dir,
            )

    def encode(self, texts: Sequence[str], *, query: bool = False) -> np.ndarray:
        values = [str(item).strip() for item in texts]
        if not values:
            return np.empty((0, 0), dtype=np.float32)
        if query:
            values = [self.query_prefix + item for item in values]
        batches: list[np.ndarray] = []
        for start in range(0, len(values), self.batch_size):
            encodings = self.tokenizer.encode_batch(values[start : start + self.batch_size])
            feeds: dict[str, np.ndarray] = {}
            if "input_ids" in self.input_names:
                feeds["input_ids"] = np.asarray([item.ids for item in encodings], dtype=np.int64)
            if "attention_mask" in self.input_names:
                feeds["attention_mask"] = np.asarray(
                    [item.attention_mask for item in encodings], dtype=np.int64
                )
            if "token_type_ids" in self.input_names:
                feeds["token_type_ids"] = np.asarray(
                    [item.type_ids for item in encodings], dtype=np.int64
                )
            output = np.asarray(self.session.run(None, feeds)[0], dtype=np.float32)
            vectors = output[:, 0, :] if output.ndim == 3 else output
            if vectors.ndim != 2:
                raise RuntimeError(f"Embedding 输出维度异常：{vectors.shape}")
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            batches.append(vectors / np.maximum(norms, 1e-12))
        return np.concatenate(batches, axis=0)


class DenseEmbeddingRetriever:
    def __init__(
        self,
        segments: list[dict[str, Any]],
        backend: EmbeddingBackend,
    ) -> None:
        self.segments = [dict(item) for item in segments]
        self.backend = backend
        self.vectors = backend.encode(
            [str(item.get("content", "")) for item in self.segments],
            query=False,
        )
        if len(self.vectors) != len(self.segments):
            raise RuntimeError("Embedding 数量与证据片段数量不一致")

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
        top_k = max(1, min(int(top_k), 100))
        allowed_sources = {str(item).upper() for item in sources or []}
        candidate_indices = [
            index
            for index, item in enumerate(self.segments)
            if not allowed_sources or str(item.get("source", "")).upper() in allowed_sources
        ]
        if not candidate_indices:
            return {
                "ok": True,
                "query": normalized_query,
                "retrievalMode": "LOCAL_DENSE_EMBEDDING",
                "matches": [],
                "matchedCount": 0,
                "fallbackToTimelineStart": False,
            }
        query_vector = self.backend.encode([normalized_query], query=True)[0]
        scores = self.vectors[candidate_indices] @ query_vector
        ranked = sorted(
            (
                {
                    **self.segments[index],
                    "score": round(float(score), 6),
                    "scoreDetails": {"denseCosine": round(float(score), 6)},
                }
                for index, score in zip(candidate_indices, scores, strict=True)
            ),
            key=lambda item: (
                -float(item["score"]),
                int(item.get("startMs", 0)),
                str(item.get("segmentId", "")),
            ),
        )
        return {
            "ok": True,
            "query": normalized_query,
            "retrievalMode": "LOCAL_DENSE_EMBEDDING",
            "matches": ranked[:top_k],
            "matchedCount": len(ranked),
            "fallbackToTimelineStart": False,
        }


class HybridRrfRetriever:
    def __init__(
        self,
        segments: list[dict[str, Any]],
        backend: EmbeddingBackend,
        *,
        candidate_depth: int = 20,
        rrf_k: int = 60,
        lexical_weight: float = 0.5,
        dense_weight: float = 0.5,
    ) -> None:
        self.lexical = EvidenceRetriever(segments)
        self.dense = DenseEmbeddingRetriever(segments, backend)
        self.candidate_depth = max(8, min(int(candidate_depth), 100))
        self.rrf_k = max(1, int(rrf_k))
        self.lexical_weight = max(0.0, float(lexical_weight))
        self.dense_weight = max(0.0, float(dense_weight))
        if self.lexical_weight + self.dense_weight <= 0:
            raise ValueError("Hybrid 至少需要一个正权重")

    def search(
        self,
        query: str,
        *,
        top_k: int = 8,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        top_k = max(1, min(int(top_k), 20))
        depth = max(top_k, self.candidate_depth)
        lexical_result = self.lexical.search(query, top_k=depth, sources=sources)
        lexical_matches = (
            [] if lexical_result.get("fallbackToTimelineStart") else lexical_result["matches"]
        )
        dense_matches = self.dense.search(query, top_k=depth, sources=sources)["matches"]
        candidates: dict[str, dict[str, Any]] = {}

        def add(items: list[dict[str, Any]], component: str, weight: float) -> None:
            for rank, item in enumerate(items, start=1):
                segment_id = str(item.get("segmentId"))
                candidate = candidates.setdefault(segment_id, {
                    **item,
                    "score": 0.0,
                    "scoreDetails": {
                        "lexicalRank": None,
                        "denseRank": None,
                        "lexicalScore": None,
                        "denseScore": None,
                    },
                })
                candidate["score"] += weight / (self.rrf_k + rank)
                candidate["scoreDetails"][f"{component}Rank"] = rank
                candidate["scoreDetails"][f"{component}Score"] = item.get("score")

        add(lexical_matches, "lexical", self.lexical_weight)
        add(dense_matches, "dense", self.dense_weight)
        ranked = sorted(
            candidates.values(),
            key=lambda item: (
                -float(item["score"]),
                int(item.get("startMs", 0)),
                str(item.get("segmentId", "")),
            ),
        )
        for item in ranked:
            item["score"] = round(float(item["score"]), 8)
        return {
            "ok": True,
            "query": " ".join(str(query).split()),
            "retrievalMode": "HYBRID_LEXICAL_DENSE_RRF",
            "matches": ranked[:top_k],
            "matchedCount": len(ranked),
            "fallbackToTimelineStart": False,
        }


def dense_profile(backend: EmbeddingBackend, settings: dict[str, Any]) -> RetrieverProfile:
    return RetrieverProfile(
        profile_id="dense-bge-small-zh-v1.5-onnx-int8-v1",
        retrieval_mode="LOCAL_DENSE_EMBEDDING",
        factory=lambda segments: DenseEmbeddingRetriever(segments, backend),
        description="本地 BGE 中文 INT8 ONNX 向量检索，归一化余弦相似度",
        settings=settings,
    )


def hybrid_profile(backend: EmbeddingBackend, settings: dict[str, Any]) -> RetrieverProfile:
    return RetrieverProfile(
        profile_id="hybrid-lexical-bge-rrf-v1",
        retrieval_mode="HYBRID_LEXICAL_DENSE_RRF",
        factory=lambda segments: HybridRrfRetriever(segments, backend),
        description="lexical 与本地 BGE dense Top-20 等权 RRF 融合",
        settings={
            **settings,
            "fusion": {"method": "RRF", "rrfK": 60, "candidateDepth": 20, "weights": [0.5, 0.5]},
        },
    )


def contextual_hybrid_profile(
    backend: EmbeddingBackend,
    settings: dict[str, Any],
    *,
    enable_abstention: bool = True,
    min_dense_score: float = 0.45,
    min_lexical_score: float = 0.18,
) -> RetrieverProfile:
    """Build the V1 development profile on top of equal-weight hybrid RRF."""
    return RetrieverProfile(
        profile_id="contextual-hybrid-bge-rrf-v2",
        retrieval_mode="CONTEXTUAL_HYBRID_LEXICAL_DENSE_RRF",
        factory=lambda segments: ContextualEvidenceRetriever(
            segments,
            HybridRrfRetriever(segments, backend),
            enable_abstention=enable_abstention,
            min_dense_score=min_dense_score,
            min_lexical_score=min_lexical_score,
        ),
        description="在 Hybrid RRF 上增加时间锚点、邻接证据扩展和双低置信度拒答",
        settings={
            **settings,
            "baseProfile": "hybrid-lexical-bge-rrf-v1",
            "fusion": {"method": "RRF", "rrfK": 60, "candidateDepth": 20, "weights": [0.5, 0.5]},
            "context": {"timeWindowMs": 15000, "neighborWindowMs": 15000, "maxNeighbors": 3},
            "abstention": {
                "enabled": enable_abstention,
                "policy": "DENSE_AND_LEXICAL_FLOOR_V1",
                "minDenseScore": min_dense_score,
                "minLexicalScore": min_lexical_score,
                "status": "experimental-single-unanswerable-dev-case",
            },
        },
    )


def coverage_aware_hybrid_profile(
    backend: EmbeddingBackend,
    settings: dict[str, Any],
) -> RetrieverProfile:
    return coverage_hybrid_profile(
        lambda segments: HybridRrfRetriever(segments, backend),
        {
            **settings,
            "fusion": {
                "method": "RRF",
                "rrfK": 60,
                "candidateDepth": 20,
                "weights": [0.5, 0.5],
            },
        },
        profile_id="coverage-aware-hybrid-bge-rrf-v3",
        retrieval_mode="COVERAGE_AWARE_CONTEXTUAL_HYBRID_LEXICAL_DENSE_RRF",
        description="本地 BGE Hybrid RRF 上的多需求证据覆盖与当前视频锚点拒答",
    )
