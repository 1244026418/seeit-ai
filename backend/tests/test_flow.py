from __future__ import annotations

import json
import os
import shutil
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DEFAULT_TEST_ROOT = "F:/temp/codex-seeit-ai/test-flow" if os.name == "nt" else "/tmp/codex-seeit-ai/test-flow"
TEST_ROOT = Path(os.environ.get("SEEIT_TEST_ROOT", DEFAULT_TEST_ROOT))
TEST_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{(TEST_ROOT / 'test.db').as_posix()}"
os.environ["UPLOAD_ROOT"] = str(TEST_ROOT / "uploads")
os.environ["JWT_SECRET"] = "test-secret"
os.environ["ROCKETMQ_NAMESERVER"] = ""
os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/15"

import seeit.main as main


def register(client: TestClient, suffix: str | None = None) -> dict:
    username = f"tester{suffix or int(time.time() * 1000000)}"
    response = client.post(
        "/user/register",
        json={"username": username, "password": "password123", "nickname": "测试用户"},
    )
    assert response.status_code == 200
    data = response.json()
    return {"Authorization": f"Bearer {data['token']}"}


def upload(client: TestClient, headers: dict[str, str], content: bytes, filename: str = "demo.mp4") -> int:
    response = client.post(
        "/media/init-upload",
        params={"filename": filename, "totalChunks": 1},
        headers=headers,
    )
    assert response.status_code == 200
    upload_id = response.json()
    response = client.post(
        "/media/upload-chunk",
        data={"uploadId": upload_id, "chunkIndex": "0", "totalChunks": "1"},
        files={"file": (filename, content, "video/mp4")},
        headers=headers,
    )
    assert response.status_code == 200
    response = client.post("/media/complete-upload", params={"uploadId": upload_id}, headers=headers)
    assert response.status_code == 200
    return response.json()["mediaId"]


@pytest.fixture(autouse=True)
def reset_database() -> None:
    main.Base.metadata.drop_all(main.engine)
    main.Base.metadata.create_all(main.engine)
    shutil.rmtree(main.UPLOAD_ROOT, ignore_errors=True)
    main.UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)


def test_register_upload_analyze_evaluate_and_feedback() -> None:
    with TestClient(main.app) as client:
        headers = register(client, "e2e")
        assert client.get("/health").json()["database"] == "up"
        media_id = upload(client, headers, b"fake-video-content")

        with main.SessionLocal() as db:
            media = db.get(main.Media, media_id)
            sidecar = Path(media.file_path + ".segments.json")
            sidecar.write_text(json.dumps({"segments": [{
                "start": 1.25,
                "end": 3.5,
                "source": "ASR",
                "text": "课程介绍二叉树的前序遍历。",
            }]}, ensure_ascii=False), encoding="utf-8")

        goal = "总结视频内容"
        response = client.post("/analysis/ai", params={"id": media_id, "goal": goal}, headers=headers)
        assert response.status_code == 202
        task_id = response.json()["taskId"]

        for _ in range(50):
            status = client.get("/analysis/analysis-status", params={"id": media_id, "goal": goal}, headers=headers).json()
            if status["state"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.05)
        assert status["state"] == "COMPLETED"
        assert status["taskId"] == task_id
        assert "[00:01] ASR" in status["result"]

        plan = client.get("/analysis/agent-plan", params={"id": media_id, "goal": goal}, headers=headers).json()
        trace = client.get("/analysis/agent-trace", params={"id": media_id}, headers=headers).json()
        evaluation = client.get("/analysis/agent-evaluation", params={"id": media_id, "goal": goal}, headers=headers).json()
        assert len(plan["tasks"]) == 3
        assert "EXECUTOR" in trace["stageDurationMs"]
        assert evaluation["structuredValid"] is True
        assert evaluation["evidenceSupportRate"] == 1.0

        transcription = client.get("/analysis/transcription-status", params={"id": media_id}, headers=headers).json()
        assert transcription["state"] == "COMPLETED"
        assert transcription["segments"][0]["startMs"] == 1250
        feedback = client.post(
            "/analysis/agent-feedback",
            json={"mediaId": media_id, "goal": goal, "rating": 1, "comment": "证据时间戳清晰"},
            headers=headers,
        )
        assert feedback.status_code == 200
        with main.SessionLocal() as db:
            saved = db.scalar(main.select(main.AnalysisFeedback).where(main.AnalysisFeedback.task_id == task_id))
            assert saved.rating == 1


def test_resume_upload_and_content_deduplication() -> None:
    with TestClient(main.app) as client:
        headers = register(client, "dedup")
        chunks = [b"first-", b"second"]
        response = client.post("/media/init-upload", params={"filename": "clip.mp4", "totalChunks": 2}, headers=headers)
        upload_id = response.json()
        client.post("/media/upload-chunk", data={"uploadId": upload_id, "chunkIndex": "0", "totalChunks": "2"}, files={"file": ("0", chunks[0])}, headers=headers)
        assert client.get("/media/upload-status", params={"uploadId": upload_id}, headers=headers).json() == [0]
        client.post("/media/upload-chunk", data={"uploadId": upload_id, "chunkIndex": "1", "totalChunks": "2"}, files={"file": ("1", chunks[1])}, headers=headers)
        first = client.post("/media/complete-upload", params={"uploadId": upload_id}, headers=headers).json()
        assert first["deduplicated"] is False

        second = upload(client, headers, b"first-second", "copy.mp4")
        assert second == first["mediaId"]
        assert len(client.get("/media/list", headers=headers).json()) == 1


def test_user_resources_are_isolated() -> None:
    with TestClient(main.app) as client:
        owner = register(client, "owner")
        other = register(client, "other")
        media_id = upload(client, owner, b"private-video")
        assert client.get("/media/list", headers=other).json() == []
        assert client.post("/analysis/ai", params={"id": media_id}, headers=other).status_code == 404
        assert client.delete("/media/delete", params={"id": media_id}, headers=other).status_code == 404


def test_duplicate_active_analysis_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(main.app) as client:
        headers = register(client, "idempotent")
        media_id = upload(client, headers, b"idempotent-video")
        monkeypatch.setattr(main, "publish_analysis", lambda task_id: None)
        first = client.post("/analysis/ai", params={"id": media_id, "goal": "同一个目标"}, headers=headers)
        second = client.post("/analysis/ai", params={"id": media_id, "goal": "同一个目标"}, headers=headers)
        assert first.status_code == 202
        assert second.status_code == 409
        assert second.json()["taskId"] == first.json()["taskId"]


def test_rocketmq_producer_uses_supported_nameserver_api(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    class FakeProducer:
        def __init__(self, group: str) -> None:
            calls.append(("group", group))

        def set_name_server_address(self, address: str) -> None:
            calls.append(("nameserver", address))

        def start(self) -> None:
            calls.append(("state", "started"))

        def send_sync(self, message: object) -> None:
            calls.append(("state", "sent"))

    class FakeMessage:
        def __init__(self, topic: str) -> None:
            calls.append(("topic", topic))

        def set_keys(self, value: str) -> None:
            pass

        def set_tags(self, value: str) -> None:
            pass

        def set_body(self, value: bytes) -> None:
            pass

    client_module = types.ModuleType("rocketmq.client")
    client_module.Producer = FakeProducer
    client_module.Message = FakeMessage
    package_module = types.ModuleType("rocketmq")
    package_module.client = client_module
    monkeypatch.setitem(sys.modules, "rocketmq", package_module)
    monkeypatch.setitem(sys.modules, "rocketmq.client", client_module)
    monkeypatch.setenv("ROCKETMQ_NAMESERVER", "rmqnamesrv:9876")
    monkeypatch.setattr(main, "_rocketmq_producer", None)

    main.publish_analysis("task-contract")

    assert ("nameserver", "rmqnamesrv:9876") in calls
    assert ("state", "started") in calls
    assert ("state", "sent") in calls


def test_failed_analysis_retries_then_becomes_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingProvider(main.Provider):
        def analyze(self, transcript: str, goal: str) -> dict:
            raise RuntimeError("模型暂时不可用")

        def follow_up(self, report: str, question: str) -> str:
            return ""

    with TestClient(main.app) as client:
        headers = register(client, "retry")
        media_id = upload(client, headers, b"retry-video")
        monkeypatch.setattr(main, "publish_analysis", lambda task_id: None)
        monkeypatch.setattr(main, "provider", lambda: FailingProvider())
        task_id = client.post("/analysis/ai", params={"id": media_id, "goal": "重试目标"}, headers=headers).json()["taskId"]
        assert main.process_analysis(task_id) == "RETRYING"
        assert main.process_analysis(task_id) == "RETRYING"
        assert main.process_analysis(task_id) == "FAILED"
        assert main.process_analysis(task_id) == "SKIPPED"
        with main.SessionLocal() as db:
            task = db.get(main.AnalysisTask, task_id)
            assert task.state == "FAILED"
            assert task.attempt_count == 3
            assert task.active_key is None


def test_chunk_size_and_authentication_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(main.app) as client:
        headers = register(client, "limits")
        monkeypatch.setattr(main, "MAX_CHUNK_BYTES", 4)
        response = client.post("/media/init-upload", params={"filename": "large.mp4", "totalChunks": 1}, headers=headers)
        upload_id = response.json()
        response = client.post("/media/upload-chunk", data={"uploadId": upload_id, "chunkIndex": "0", "totalChunks": "1"}, files={"file": ("large", b"12345")}, headers=headers)
        assert response.status_code == 413
        assert client.get("/media/list").status_code == 401


def test_logout_revokes_token_when_redis_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        def get(self, key: str) -> str | None:
            return self.values.get(key)

        def setex(self, key: str, ttl: int, value: str) -> None:
            assert ttl > 0
            self.values[key] = value

    with TestClient(main.app) as client:
        headers = register(client, "logout")
        fake_redis = FakeRedis()
        monkeypatch.setattr(main, "redis_client", lambda: fake_redis)
        assert client.post("/user/logout", headers=headers).status_code == 200
        assert client.get("/media/list", headers=headers).status_code == 401


def test_production_mode_rejects_unsafe_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTO_CREATE_SCHEMA", "true")
    monkeypatch.setattr(main, "JWT_SECRET", "short")
    monkeypatch.setattr(main, "DATABASE_URL", "sqlite:///unsafe.db")
    monkeypatch.setattr(main, "origins", ["*"])
    with pytest.raises(RuntimeError, match="生产配置不安全"):
        main.validate_production_config()


def test_ffprobe_video_validation_and_duration_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    class Completed:
        stdout = json.dumps({
            "streams": [{"codec_name": "h264", "width": 1920, "height": 1080}],
            "format": {"duration": "120.5"},
        })

    monkeypatch.setattr(main.subprocess, "run", lambda *args, **kwargs: Completed())
    monkeypatch.setenv("MAX_VIDEO_DURATION_SECONDS", "600")
    result = main.validate_video_file(Path("demo.mp4"))
    assert result["codec"] == "h264"
    assert result["durationSeconds"] == 120.5

    Completed.stdout = json.dumps({
        "streams": [{"codec_name": "h264", "width": 1920, "height": 1080}],
        "format": {"duration": "601"},
    })
    with pytest.raises(ValueError, match="视频时长不能超过"):
        main.validate_video_file(Path("too-long.mp4"))
