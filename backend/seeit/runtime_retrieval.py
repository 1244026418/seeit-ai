"""Runtime retriever selection with a Qdrant-to-lexical fallback."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from seeit.retrieval import (
    ContextualEvidenceRetriever,
    CoverageAwareEvidenceRetriever,
    EvidenceRetriever,
    SearchRetriever,
)


log = logging.getLogger("seeit.retrieval")
_lock = threading.Lock()
_embedding_backend: Any = None
_embedding_key: tuple[Any, ...] | None = None
_qdrant_store: Any = None
_qdrant_key: tuple[Any, ...] | None = None


def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _contextual_lexical(segments: list[dict[str, Any]]) -> SearchRetriever:
    return ContextualEvidenceRetriever(segments, EvidenceRetriever(segments))


def _coverage_lexical(segments: list[dict[str, Any]]) -> SearchRetriever:
    return CoverageAwareEvidenceRetriever(segments, _contextual_lexical(segments))


def _embedding() -> Any:
    from seeit.embedding_retrieval import LocalOnnxEmbeddingBackend

    global _embedding_backend, _embedding_key
    model_dir = Path(os.getenv(
        "EVIDENCE_EMBEDDING_MODEL_DIR",
        "/data/models/retrieval/bge-small-zh-v1.5",
    ))
    key = (
        str(model_dir.resolve()),
        _flag("EVIDENCE_EMBEDDING_ALLOW_DOWNLOAD", False),
        int(os.getenv("EVIDENCE_EMBEDDING_CPU_THREADS", "2")),
        int(os.getenv("EVIDENCE_EMBEDDING_BATCH_SIZE", "32")),
    )
    with _lock:
        if _embedding_backend is None or _embedding_key != key:
            _embedding_backend = LocalOnnxEmbeddingBackend(
                model_dir,
                allow_download=key[1],
                cpu_threads=key[2],
                batch_size=key[3],
            )
            _embedding_key = key
        return _embedding_backend


def _store() -> Any:
    from qdrant_client import QdrantClient
    from seeit.qdrant_retrieval import QdrantEvidenceStore

    global _qdrant_store, _qdrant_key
    url = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
    api_key = os.getenv("QDRANT_API_KEY", "").strip() or None
    collection = os.getenv("QDRANT_COLLECTION", "seeit_evidence_v1").strip()
    timeout = float(os.getenv("QDRANT_TIMEOUT_SECONDS", "10"))
    key = (url, api_key, collection, timeout)
    with _lock:
        if _qdrant_store is None or _qdrant_key != key:
            client = QdrantClient(url=url, api_key=api_key, timeout=timeout)
            _qdrant_store = QdrantEvidenceStore(client, collection_name=collection)
            _qdrant_key = key
        return _qdrant_store


def build_runtime_retriever(
    metadata: dict[str, Any],
    segments: list[dict[str, Any]],
) -> SearchRetriever:
    profile = os.getenv(
        "EVIDENCE_RETRIEVER_PROFILE",
        "coverage-aware-qdrant-hybrid-v2",
    ).strip().lower()
    fallback = _contextual_lexical(segments)
    if profile in {"lexical", "lexical-v1", "contextual-lexical", "contextual-lexical-v2"}:
        return fallback
    if profile in {"coverage-lexical", "coverage-aware-lexical-v3"}:
        return _coverage_lexical(segments)
    if profile not in {
        "qdrant",
        "qdrant-hybrid",
        "contextual-qdrant-hybrid-v1",
        "coverage-qdrant-hybrid-v2",
        "coverage-aware-qdrant-hybrid-v2",
    }:
        log.warning("unknown_retriever_profile profile=%s fallback=contextual-lexical-v2", profile)
        return fallback

    try:
        from seeit.qdrant_retrieval import QdrantHybridRetriever

        video_id = str(metadata.get("mediaId") or metadata.get("videoId") or "unknown")
        base = QdrantHybridRetriever(video_id, segments, _embedding(), _store())
        contextual = ContextualEvidenceRetriever(
            segments,
            base,
            enable_abstention=(
                _flag("EVIDENCE_ABSTENTION_ENABLED", True)
                and profile == "contextual-qdrant-hybrid-v1"
            ),
            min_dense_score=float(os.getenv("EVIDENCE_MIN_DENSE_SCORE", "0.45")),
            min_lexical_score=float(os.getenv("EVIDENCE_MIN_LEXICAL_SCORE", "0.18")),
        )
        if profile in {"coverage-qdrant-hybrid-v2", "coverage-aware-qdrant-hybrid-v2"}:
            return CoverageAwareEvidenceRetriever(segments, contextual)
        return contextual
    except Exception:
        if not _flag("EVIDENCE_RETRIEVAL_FALLBACK_ENABLED", True):
            raise
        log.exception(
            "qdrant_retriever_init_failed media_id=%s fallback=contextual-lexical-v2",
            metadata.get("mediaId"),
        )
        if profile in {"coverage-qdrant-hybrid-v2", "coverage-aware-qdrant-hybrid-v2"}:
            return _coverage_lexical(segments)
        return fallback


def delete_runtime_media_index(media_id: int | str) -> None:
    profile = os.getenv(
        "EVIDENCE_RETRIEVER_PROFILE",
        "coverage-aware-qdrant-hybrid-v2",
    ).strip().lower()
    if profile not in {
        "qdrant",
        "qdrant-hybrid",
        "contextual-qdrant-hybrid-v1",
        "coverage-qdrant-hybrid-v2",
        "coverage-aware-qdrant-hybrid-v2",
    }:
        return
    try:
        _store().delete_video(str(media_id))
    except Exception:
        log.exception("qdrant_media_delete_failed media_id=%s", media_id)
