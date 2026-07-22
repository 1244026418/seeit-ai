from __future__ import annotations

import os
import uuid
from pathlib import Path

import numpy as np
import pytest
from qdrant_client import QdrantClient

from seeit.qdrant_retrieval import QdrantEvidenceStore
from seeit import runtime_retrieval


QDRANT_TEST_URL = os.getenv("QDRANT_TEST_URL", "").strip()
SEEIT_TEST_MODEL_DIR = os.getenv("SEEIT_TEST_MODEL_DIR", "").strip()
pytestmark = pytest.mark.skipif(not QDRANT_TEST_URL, reason="未配置 QDRANT_TEST_URL")


def test_remote_qdrant_collection_upsert_filter_search_and_delete() -> None:
    collection = f"seeit_test_{uuid.uuid4().hex[:12]}"
    client = QdrantClient(url=QDRANT_TEST_URL, timeout=10)
    store = QdrantEvidenceStore(client, collection_name=collection)
    segments = [
        {
            "segmentId": "asr-1",
            "source": "ASR",
            "startMs": 0,
            "endMs": 1000,
            "content": "alpha",
        },
        {
            "segmentId": "ocr-1",
            "source": "OCR",
            "startMs": 2000,
            "endMs": 3000,
            "content": "beta",
        },
    ]
    try:
        state = store.ensure_video(
            "integration-video",
            segments,
            lambda: np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
        )
        matches = store.search(
            "integration-video",
            np.asarray([1.0, 0.0], dtype=np.float32),
            top_k=2,
            sources=["ASR"],
        )

        assert state["reused"] is False
        assert [item["segmentId"] for item in matches] == ["asr-1"]
        assert client.get_collection(collection).points_count == 2

        store.delete_video("integration-video")
        assert client.count(collection, exact=True).count == 0
    finally:
        if client.collection_exists(collection):
            client.delete_collection(collection)


@pytest.mark.skipif(
    not SEEIT_TEST_MODEL_DIR or not Path(SEEIT_TEST_MODEL_DIR).is_dir(),
    reason="未配置本地 BGE 模型目录",
)
def test_local_bge_to_qdrant_contextual_hybrid_runtime(monkeypatch) -> None:
    collection = f"seeit_test_{uuid.uuid4().hex[:12]}"
    client = QdrantClient(url=QDRANT_TEST_URL, timeout=10)
    monkeypatch.setenv("EVIDENCE_RETRIEVER_PROFILE", "contextual-qdrant-hybrid-v1")
    monkeypatch.setenv("EVIDENCE_EMBEDDING_MODEL_DIR", SEEIT_TEST_MODEL_DIR)
    monkeypatch.setenv("EVIDENCE_EMBEDDING_ALLOW_DOWNLOAD", "false")
    monkeypatch.setenv("QDRANT_URL", QDRANT_TEST_URL)
    monkeypatch.setenv("QDRANT_COLLECTION", collection)
    monkeypatch.setattr(runtime_retrieval, "_embedding_backend", None)
    monkeypatch.setattr(runtime_retrieval, "_embedding_key", None)
    monkeypatch.setattr(runtime_retrieval, "_qdrant_store", None)
    monkeypatch.setattr(runtime_retrieval, "_qdrant_key", None)
    segments = [
        {
            "segmentId": "answer",
            "source": "ASR",
            "startMs": 10000,
            "endMs": 15000,
            "content": "大脑把有含义的词语作为整体处理，是为了节省脑力。",
        },
        {
            "segmentId": "noise",
            "source": "ASR",
            "startMs": 30000,
            "endMs": 35000,
            "content": "服务器部署需要配置反向代理和 HTTPS 证书。",
        },
    ]
    try:
        retriever = runtime_retrieval.build_runtime_retriever({"mediaId": 99}, segments)
        result = retriever.search("大脑为什么把有含义的词语当成整体？", top_k=2)

        assert result["retrievalMode"] == "CONTEXTUAL_QDRANT_HYBRID_LEXICAL_DENSE_RRF"
        assert result["matches"][0]["segmentId"] == "answer"
        assert result["abstention"]["abstained"] is False
    finally:
        if client.collection_exists(collection):
            client.delete_collection(collection)
