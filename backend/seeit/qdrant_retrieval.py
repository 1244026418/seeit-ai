"""Qdrant-backed dense and hybrid evidence retrieval."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any

import numpy as np
from qdrant_client import QdrantClient, models

from seeit.embedding_retrieval import EmbeddingBackend
from seeit.retrieval import EvidenceRetriever


DEFAULT_COLLECTION = "seeit_evidence_v1"
POINT_NAMESPACE = uuid.UUID("0f5e7379-50d0-4d9c-aa25-e018f25caaf8")


def evidence_snapshot_hash(segments: list[dict[str, Any]]) -> str:
    canonical = [
        {
            "segmentId": str(item.get("segmentId", "")),
            "source": str(item.get("source", "")).upper(),
            "startMs": int(item.get("startMs", 0)),
            "endMs": int(item.get("endMs", 0)),
            "content": str(item.get("content", "")),
        }
        for item in segments
    ]
    rendered = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _point_id(video_id: str, segment_id: str) -> str:
    return str(uuid.uuid5(POINT_NAMESPACE, f"{video_id}:{segment_id}"))


def _keyword_filter(video_id: str, sources: list[str] | None = None) -> models.Filter:
    must = [
        models.FieldCondition(key="videoId", match=models.MatchValue(value=str(video_id))),
    ]
    normalized_sources = sorted({str(item).upper() for item in sources or []})
    if normalized_sources:
        must.append(models.FieldCondition(
            key="source",
            match=models.MatchAny(any=normalized_sources),
        ))
    return models.Filter(must=must)


class QdrantEvidenceStore:
    def __init__(
        self,
        client: QdrantClient,
        *,
        collection_name: str = DEFAULT_COLLECTION,
    ) -> None:
        self.client = client
        self.collection_name = collection_name

    def ensure_collection(self, vector_size: int) -> None:
        vector_size = int(vector_size)
        if vector_size <= 0:
            raise ValueError("Qdrant 向量维度必须大于 0")
        if self.client.collection_exists(self.collection_name):
            info = self.client.get_collection(self.collection_name)
            params = info.config.params.vectors
            existing_size = int(params.size) if hasattr(params, "size") else None
            if existing_size is not None and existing_size != vector_size:
                raise RuntimeError(
                    f"Qdrant collection 向量维度不一致：{existing_size} != {vector_size}"
                )
            return
        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
            on_disk_payload=True,
        )
        self.client.create_payload_index(
            collection_name=self.collection_name,
            field_name="videoId",
            field_schema=models.PayloadSchemaType.KEYWORD,
            wait=True,
        )
        self.client.create_payload_index(
            collection_name=self.collection_name,
            field_name="source",
            field_schema=models.PayloadSchemaType.KEYWORD,
            wait=True,
        )

    def video_state(self, video_id: str, snapshot_hash: str, segment_count: int) -> str:
        if not self.client.collection_exists(self.collection_name):
            return "MISSING_COLLECTION"
        query_filter = _keyword_filter(video_id)
        count = int(self.client.count(
            collection_name=self.collection_name,
            count_filter=query_filter,
            exact=True,
        ).count)
        if count != int(segment_count):
            return "STALE_COUNT"
        records, _ = self.client.scroll(
            collection_name=self.collection_name,
            scroll_filter=query_filter,
            limit=1,
            with_payload=["snapshotHash"],
            with_vectors=False,
        )
        if not records or str((records[0].payload or {}).get("snapshotHash")) != snapshot_hash:
            return "STALE_HASH"
        return "CURRENT"

    def delete_video(self, video_id: str) -> None:
        if not self.client.collection_exists(self.collection_name):
            return
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=models.FilterSelector(filter=_keyword_filter(video_id)),
            wait=True,
        )

    def rebuild_video(
        self,
        video_id: str,
        segments: list[dict[str, Any]],
        vectors: np.ndarray,
        snapshot_hash: str,
    ) -> None:
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(segments):
            raise ValueError("Qdrant 向量矩阵与证据片段数量不一致")
        self.ensure_collection(matrix.shape[1])
        self.delete_video(video_id)
        points = []
        for index, (item, vector) in enumerate(zip(segments, matrix, strict=True)):
            segment_id = str(item.get("segmentId") or f"segment-{index + 1}")
            points.append(models.PointStruct(
                id=_point_id(str(video_id), segment_id),
                vector=vector.tolist(),
                payload={
                    "videoId": str(video_id),
                    "segmentId": segment_id,
                    "source": str(item.get("source", "ASR")).upper(),
                    "startMs": int(item.get("startMs", 0)),
                    "endMs": int(item.get("endMs", item.get("startMs", 0))),
                    "content": str(item.get("content", ""))[:2000],
                    "snapshotHash": snapshot_hash,
                },
            ))
        for start in range(0, len(points), 128):
            self.client.upsert(
                collection_name=self.collection_name,
                points=points[start : start + 128],
                wait=True,
            )

    def ensure_video(
        self,
        video_id: str,
        segments: list[dict[str, Any]],
        vector_builder: Callable[[], np.ndarray],
    ) -> dict[str, Any]:
        snapshot_hash = evidence_snapshot_hash(segments)
        state = self.video_state(video_id, snapshot_hash, len(segments))
        if state == "CURRENT":
            return {"reused": True, "snapshotHash": snapshot_hash, "segmentCount": len(segments)}
        vectors = vector_builder()
        self.rebuild_video(video_id, segments, vectors, snapshot_hash)
        return {
            "reused": False,
            "previousState": state,
            "snapshotHash": snapshot_hash,
            "segmentCount": len(segments),
        }

    def search(
        self,
        video_id: str,
        query_vector: np.ndarray,
        *,
        top_k: int,
        sources: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        vector = np.asarray(query_vector, dtype=np.float32).reshape(-1)
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=vector.tolist(),
            query_filter=_keyword_filter(video_id, sources),
            limit=max(1, int(top_k)),
            with_payload=True,
            with_vectors=False,
        )
        matches = []
        for point in response.points:
            payload = dict(point.payload or {})
            matches.append({
                "segmentId": payload.get("segmentId"),
                "source": payload.get("source"),
                "startMs": payload.get("startMs"),
                "endMs": payload.get("endMs"),
                "content": payload.get("content"),
                "score": round(float(point.score), 6),
                "scoreDetails": {"denseCosine": round(float(point.score), 6)},
            })
        return matches


class QdrantDenseRetriever:
    def __init__(
        self,
        video_id: str,
        segments: list[dict[str, Any]],
        backend: EmbeddingBackend,
        store: QdrantEvidenceStore,
    ) -> None:
        self.video_id = str(video_id)
        self.segments = [dict(item) for item in segments]
        self.backend = backend
        self.store = store
        self.index_state = store.ensure_video(
            self.video_id,
            self.segments,
            lambda: backend.encode(
                [str(item.get("content", "")) for item in self.segments],
                query=False,
            ),
        )

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
        query_vector = self.backend.encode([normalized_query], query=True)[0]
        matches = self.store.search(
            self.video_id,
            query_vector,
            top_k=max(1, min(int(top_k), 100)),
            sources=sources,
        )
        return {
            "ok": True,
            "query": normalized_query,
            "retrievalMode": "QDRANT_DENSE_EMBEDDING",
            "matches": matches,
            "matchedCount": len(matches),
            "fallbackToTimelineStart": False,
            "indexReused": bool(self.index_state["reused"]),
        }


class QdrantHybridRetriever:
    def __init__(
        self,
        video_id: str,
        segments: list[dict[str, Any]],
        backend: EmbeddingBackend,
        store: QdrantEvidenceStore,
        *,
        candidate_depth: int = 20,
        rrf_k: int = 60,
    ) -> None:
        self.lexical = EvidenceRetriever(segments)
        self.dense = QdrantDenseRetriever(video_id, segments, backend, store)
        self.candidate_depth = max(8, min(int(candidate_depth), 100))
        self.rrf_k = max(1, int(rrf_k))

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
        lexical = [] if lexical_result.get("fallbackToTimelineStart") else lexical_result["matches"]
        dense_result = self.dense.search(query, top_k=depth, sources=sources)
        dense = dense_result["matches"]
        candidates: dict[str, dict[str, Any]] = {}

        def add(items: list[dict[str, Any]], component: str) -> None:
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
                candidate["score"] += 0.5 / (self.rrf_k + rank)
                candidate["scoreDetails"][f"{component}Rank"] = rank
                candidate["scoreDetails"][f"{component}Score"] = item.get("score")

        add(lexical, "lexical")
        add(dense, "dense")
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
            "retrievalMode": "QDRANT_HYBRID_LEXICAL_DENSE_RRF",
            "matches": ranked[:top_k],
            "matchedCount": len(ranked),
            "fallbackToTimelineStart": False,
            "indexReused": bool(self.dense.index_state["reused"]),
        }
