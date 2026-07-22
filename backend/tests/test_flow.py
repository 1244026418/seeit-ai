from __future__ import annotations

import json
import os
import shutil
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
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
import seeit.ocr_runner as ocr_runner
from seeit.bilibili import DownloadedVideo


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
def reset_database(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("AI_BASE_URL", "AI_API_KEY", "AI_MODEL"):
        monkeypatch.delenv(variable, raising=False)
    main.Base.metadata.drop_all(main.engine)
    main.Base.metadata.create_all(main.engine)
    shutil.rmtree(main.UPLOAD_ROOT, ignore_errors=True)
    main.UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    with main._rate_limit_lock:
        main._local_rate_limits.clear()


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
        assert status["stage"] == "COMPLETED"
        assert status["progressCurrent"] == 1
        assert status["progressTotal"] == 1
        assert status["progressPercent"] == 100
        assert "[00:01] ASR" in status["result"]

        plan = client.get("/analysis/agent-plan", params={"id": media_id, "goal": goal}, headers=headers).json()
        trace = client.get("/analysis/agent-trace", params={"id": media_id}, headers=headers).json()
        evaluation = client.get("/analysis/agent-evaluation", params={"id": media_id, "goal": goal}, headers=headers).json()
        assert len(plan["tasks"]) == 4
        assert plan["intent"] == "STRUCTURED_SUMMARY"
        assert "EXECUTOR" in trace["stageDurationMs"]
        assert trace["agentMode"] == "DETERMINISTIC_TOOL_PIPELINE"
        assert trace["toolCallCount"] >= 4
        tool_names = [item["tool"] for item in trace["toolCalls"]]
        assert tool_names[0:2] == ["get_video_metadata", "search_timeline"]
        assert "get_evidence_window" in tool_names
        assert tool_names[-1] == "generate_report"
        assert evaluation["structuredValid"] is True
        assert evaluation["evidenceSupportRate"] == 1.0

        task_status = client.get("/analysis/task-status", params={"taskId": task_id}, headers=headers).json()
        report = client.get("/analysis/report", params={"id": media_id, "goal": goal}, headers=headers).json()
        assert task_status["state"] == "COMPLETED"
        assert task_status["answerable"] is True
        assert task_status["finalAnswer"]
        assert report["report"] == status["result"]
        assert report["finalAnswer"] == task_status["finalAnswer"]
        assert task_status["trace"]["providerUsage"]["requestCount"] == 0
        assert task_status["trace"]["providerRequests"] == []

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


def test_agent_tool_api_searches_windows_and_verifies_owned_evidence() -> None:
    with TestClient(main.app) as client:
        owner = register(client, "toolowner")
        other = register(client, "toolother")
        media_id = upload(client, owner, b"agent-tool-video")
        with main.SessionLocal() as db:
            media = db.get(main.Media, media_id)
            Path(media.file_path + ".segments.json").write_text(json.dumps({"segments": [
                {"start": 5, "end": 9, "source": "ASR", "text": "首先创建数据库索引。"},
                {"start": 10, "end": 14, "source": "OCR", "text": "EXPLAIN 查询执行计划"},
            ]}, ensure_ascii=False), encoding="utf-8")

        metadata = client.get(
            "/agent/tools/video-metadata",
            params={"id": media_id},
            headers=owner,
        ).json()
        assert metadata["evidenceSegmentCount"] == 0

        search = client.post(
            "/agent/tools/search-timeline",
            json={"mediaId": media_id, "query": "数据库索引", "topK": 3, "sources": ["ASR"]},
            headers=owner,
        ).json()
        assert search["matches"][0]["content"] == "首先创建数据库索引。"
        assert search["matches"][0]["startMs"] == 5000
        metadata = client.get(
            "/agent/tools/video-metadata",
            params={"id": media_id},
            headers=owner,
        ).json()
        assert metadata["evidenceSegmentCount"] == 2

        window = client.post(
            "/agent/tools/evidence-window",
            json={"mediaId": media_id, "timestampMs": 10000, "beforeMs": 6000, "afterMs": 6000},
            headers=owner,
        ).json()
        assert len(window["segments"]) == 2

        verification = client.post(
            "/agent/tools/verify-citations",
            json={"mediaId": media_id, "citations": [{
                "timestampMs": 5000,
                "source": "ASR",
                "content": "首先创建数据库索引。",
            }]},
            headers=owner,
        ).json()
        assert verification["evidenceSupportRate"] == 1.0
        assert verification["citations"][0]["supported"] is True

        denied = client.post(
            "/agent/tools/search-timeline",
            json={"mediaId": media_id, "query": "数据库", "topK": 3, "sources": []},
            headers=other,
        )
        assert denied.status_code == 404


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


def test_stale_queued_analysis_is_discovered_for_worker_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(main.app) as client:
        headers = register(client, "qfallback")
        media_id = upload(client, headers, b"queue-fallback-video")
        monkeypatch.setattr(main, "publish_analysis", lambda task_id: None)
        task_id = client.post(
            "/analysis/ai",
            params={"id": media_id, "goal": "验证排队任务兜底"},
            headers=headers,
        ).json()["taskId"]

        with main.SessionLocal() as db:
            task = db.get(main.AnalysisTask, task_id)
            task.updated_at = datetime.now(timezone.utc) - timedelta(
                seconds=main.QUEUED_TASK_FALLBACK_SECONDS + 1,
            )
            db.commit()

        assert task_id in main.stale_queued_analysis_task_ids()
        assert main.process_analysis(task_id) == "COMPLETED"
        assert task_id not in main.stale_queued_analysis_task_ids()


def test_local_asr_returns_timestamped_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    class FakeSegment:
        def __init__(self, start: float, end: float, text: str) -> None:
            self.start = start
            self.end = end
            self.text = text

    class FakeModel:
        def transcribe(self, path: str, **kwargs: object):
            calls["path"] = path
            calls["kwargs"] = kwargs
            return iter([
                FakeSegment(1.25, 3.5, " GPT-5.6 提供三个型号。 "),
                FakeSegment(65.0, 69.25, "应该选择能够稳定完成任务的最低推理强度。"),
            ]), types.SimpleNamespace(language="zh", duration=70.0)

    monkeypatch.setattr(main, "local_asr_model", lambda: FakeModel())
    monkeypatch.setenv("LOCAL_ASR_LANGUAGE", "zh")
    monkeypatch.setenv("LOCAL_ASR_BEAM_SIZE", "3")
    monkeypatch.setenv("LOCAL_ASR_VAD_FILTER", "true")
    monkeypatch.setenv("LOCAL_ASR_HOTWORDS", "GPT Sol Terra Luna")

    segments = main.request_local_asr(Path("demo.mp4"))

    assert segments == [
        {
            "source": "ASR",
            "startMs": 1250,
            "endMs": 3500,
            "content": "GPT-5.6 提供三个型号。",
        },
        {
            "source": "ASR",
            "startMs": 65000,
            "endMs": 69250,
            "content": "应该选择能够稳定完成任务的最低推理强度。",
        },
    ]
    assert calls["path"] == "demo.mp4"
    assert calls["kwargs"]["beam_size"] == 3
    assert calls["kwargs"]["hotwords"] == "GPT Sol Terra Luna"


def test_paddle_ocr_content_filters_low_confidence_and_short_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PADDLEOCR_MIN_TEXT_LENGTH", "2")
    payload = {
        "res": {
            "rec_texts": [" GPT-5.6 Sol ", "x", "低置信度", "每100万输入Token 5美元"],
            "rec_scores": [0.98, 0.99, 0.42, 0.91],
        }
    }

    assert ocr_runner.paddle_ocr_content(payload, 0.65, min_length=2) == "GPT-5.6 Sol 每100万输入Token 5美元"


def test_paddle_ocr_content_can_preserve_single_chinese_characters() -> None:
    payload = {
        "res": {
            "rec_texts": ["我喜欢", "唱", "跳", "Rap和", "篮球"],
            "rec_scores": [0.99, 0.98, 0.97, 0.99, 0.99],
        }
    }

    assert ocr_runner.paddle_ocr_content(payload, 0.65) == "我喜欢 唱 跳 Rap和 篮球"


def test_ocr_runner_reads_png_and_jpeg_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    frame_dir = TEST_ROOT / "ocr-frame-formats"
    shutil.rmtree(frame_dir, ignore_errors=True)
    frame_dir.mkdir(parents=True)
    (frame_dir / "frame-000001.png").write_bytes(b"png")
    (frame_dir / "frame-000002.jpg").write_bytes(b"jpg")
    (frame_dir / "ignored.txt").write_text("ignored", encoding="utf-8")

    class FakePaddleOCR:
        def __init__(self, **_: object) -> None:
            pass

        def predict(self, path: str, **_: object) -> list[dict]:
            return [{"res": {"rec_texts": [Path(path).name], "rec_scores": [0.99]}}]

    fake_module = types.ModuleType("paddleocr")
    fake_module.PaddleOCR = FakePaddleOCR
    monkeypatch.setitem(sys.modules, "paddleocr", fake_module)
    monkeypatch.setenv("PADDLEOCR_MODEL_ROOT", str(TEST_ROOT / "paddlex-models"))

    progress_file = frame_dir / "progress.json"
    payload = ocr_runner.run(frame_dir, progress_file)

    assert payload["frameCount"] == 2
    assert [item["frame"] for item in payload["results"]] == ["frame-000001.png", "frame-000002.jpg"]
    assert json.loads(progress_file.read_text(encoding="utf-8")) == {"current": 2, "total": 2}


def test_paddle_ocr_runs_in_isolated_process(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> types.SimpleNamespace:
        captured["command"] = command
        captured["kwargs"] = kwargs
        output = Path(command[command.index("--output") + 1])
        output.write_text(json.dumps({
            "frameCount": 1,
            "results": [{"index": 0, "frame": "frame-000001.jpg", "content": "SeeIt AI"}],
            "errors": [],
            "modelLoadMs": 100,
            "elapsedMs": 200,
        }), encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    payload = main.run_paddle_ocr_frames(TEST_ROOT)

    assert captured["command"][1:3] == ["-m", "seeit.ocr_runner"]
    assert captured["kwargs"]["timeout"] == 600
    assert payload["results"][0]["content"] == "SeeIt AI"


def test_ocr_sampling_always_selects_the_first_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **_: object) -> types.SimpleNamespace:
        captured["command"] = command
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("OCR_ENABLED", "true")
    monkeypatch.setenv("OCR_INTERVAL_SECONDS", "30")
    monkeypatch.setattr(main.subprocess, "run", fake_run)
    monkeypatch.setattr(main, "release_local_asr_model", lambda: None)
    monkeypatch.setattr(main, "run_paddle_ocr_frames", lambda _directory: {
        "frameCount": 1,
        "results": [{"index": 0, "content": "SeeIt AI"}],
        "errors": [],
    })

    segments = main.extract_ocr_evidence(TEST_ROOT / "short-video.mp4")
    command = captured["command"]

    assert "select='isnan(prev_selected_t)+gte(t-prev_selected_t,30)'" in command[command.index("-vf") + 1]
    assert command[command.index("-fps_mode") + 1] == "vfr"
    assert command[-1].endswith(".png")
    assert segments[0]["content"] == "SeeIt AI"


def test_deepseek_payload_uses_thinking_mode_and_standard_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        headers = {"x-request-id": "request-usage-1"}

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {
                "choices": [{"message": {"content": None, "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "get_video_metadata", "arguments": "{}"},
                }]}}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                    "total_tokens": 1500,
                    "prompt_cache_hit_tokens": 200,
                    "prompt_cache_miss_tokens": 800,
                },
            }

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setenv("AI_THINKING_MODE", "disabled")
    monkeypatch.setenv("AI_REQUEST_TIMEOUT_SECONDS", "180")
    monkeypatch.setenv("AI_INPUT_COST_PER_MILLION_TOKENS", "2")
    monkeypatch.setenv("AI_OUTPUT_COST_PER_MILLION_TOKENS", "8")
    monkeypatch.setenv("AI_CACHE_HIT_INPUT_COST_PER_MILLION_TOKENS", "1")
    monkeypatch.setattr(main.httpx, "post", fake_post)
    provider = main.OpenAICompatibleProvider(
        "https://api.deepseek.com",
        "test-key",
        "deepseek-v4-flash",
    )

    message = provider._completion(
        [{"role": "user", "content": "读取视频元数据"}],
        [{"type": "function", "function": {
            "name": "get_video_metadata",
            "description": "读取视频元数据",
            "parameters": {"type": "object", "properties": {}},
        }}],
    )

    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["json"]["model"] == "deepseek-v4-flash"
    assert captured["json"]["thinking"] == {"type": "disabled"}
    assert captured["json"]["tool_choice"] == "auto"
    assert message["tool_calls"][0]["function"]["name"] == "get_video_metadata"
    usage = provider.provider_usage_summary()
    assert usage["requestCount"] == 1
    assert usage["promptTokens"] == 1000
    assert usage["completionTokens"] == 500
    assert usage["totalTokens"] == 1500
    assert usage["cacheHitTokens"] == 200
    assert usage["estimatedCostUsd"] == 0.0058
    assert provider.provider_usage_events()[0]["requestId"] == "request-usage-1"
    assert provider.provider_usage_events()[0]["phase"] == "UNSPECIFIED"
    assert usage["byPhase"]["UNSPECIFIED"]["totalTokens"] == 1500

    nested_cache_tokens = provider._usage_tokens({
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "prompt_tokens_details": {"cached_tokens": 20},
    })
    assert nested_cache_tokens["cacheHitTokens"] == 20
    assert nested_cache_tokens["cacheMissTokens"] == 100
    assert provider._usage_tokens({"unexpected": 1})["usageReported"] is False
    monkeypatch.setenv("AI_OUTPUT_COST_PER_MILLION_TOKENS", "0")
    assert provider._token_cost(nested_cache_tokens) is None


def test_provider_usage_records_failed_http_request(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("POST", "https://api.example.com/chat/completions")

    class FailedResponse:
        status_code = 503
        headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("service unavailable", request=request, response=response)

    monkeypatch.setattr(main.httpx, "post", lambda *args, **kwargs: FailedResponse())
    provider = main.OpenAICompatibleProvider("https://api.example.com", "test-key", "test-model")

    with pytest.raises(httpx.HTTPStatusError):
        provider._completion([{"role": "user", "content": "test"}])

    usage = provider.provider_usage_summary()
    assert usage["requestCount"] == 1
    assert usage["successCount"] == 0
    assert usage["failureCount"] == 1
    assert usage["totalTokens"] == 0
    assert provider.provider_usage_events()[0]["statusCode"] == 503


def test_report_normalization_does_not_split_string_fields_into_characters() -> None:
    result = main.normalize_analysis_result({
        "title": "模型总结",
        "conclusions": "GPT-5.6 包含多个型号。",
        "evidence": {
            "timestampMs": 5000,
            "source": "ASR",
            "content": "GPT-5.6 包含多个型号。",
        },
        "suggestions": "1. 简单任务使用较低推理强度。\n2. 复杂任务再逐步提高。",
    })

    assert result["conclusions"] == ["GPT-5.6 包含多个型号。"]
    assert result["suggestions"] == ["简单任务使用较低推理强度。", "复杂任务再逐步提高。"]
    assert result["evidence"][0]["timestampMs"] == 5000


def test_system_placeholder_is_refreshed_when_local_asr_becomes_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(main.app) as client:
        headers = register(client, "asrrefresh")
        media_id = upload(client, headers, b"video-needing-asr")

        monkeypatch.setenv("LOCAL_ASR_ENABLED", "false")
        with main.SessionLocal() as db:
            media = db.get(main.Media, media_id)
            placeholder = main.ensure_media_evidence(db, media)
            db.commit()
            assert [item["source"] for item in placeholder] == ["SYSTEM"]

        monkeypatch.setenv("LOCAL_ASR_ENABLED", "true")
        monkeypatch.setattr(main, "request_local_asr", lambda _path: [{
            "source": "ASR",
            "startMs": 1000,
            "endMs": 4000,
            "content": "真实语音转写证据",
        }])
        with main.SessionLocal() as db:
            media = db.get(main.Media, media_id)
            refreshed = main.ensure_media_evidence(db, media)
            db.commit()

        assert [item["source"] for item in refreshed] == ["ASR"]
        assert refreshed[0]["content"] == "真实语音转写证据"


def test_mock_provider_samples_evidence_across_the_full_timeline() -> None:
    lines = [
        "[0-5000ms][ASR] GPT-5.6 包含 Sol、Terra 和 Luna 三个型号。",
        "[60000-65000ms][ASR] 价格按每百万 Token 的输入和输出分别定价。",
        "[120000-125000ms][ASR] 推理强度包含 None、Low、Medium、High、X-High 和 Max。",
        "[180000-185000ms][ASR] 简单任务适合 Luna，复杂专业任务适合 Sol。",
        "[210000-215000ms][ASR] 轻量代码任务可以从 Luna 开始测试。",
        "[240000-245000ms][ASR] 最终建议是使用能够稳定完成任务的最低推理强度，不要默认拉满。",
    ]

    result = main.MockProvider().analyze(
        "\n".join(lines),
        "总结 GPT-5.6 的型号、推理强度、价格、适用场景和最终建议",
    )

    timestamps = [item["timestampMs"] for item in result["evidence"]]
    assert timestamps == [0, 60000, 120000, 180000, 210000, 240000]
    assert max(timestamps) - min(timestamps) == 240000
    conclusions = "\n".join(result["conclusions"])
    assert all(f"{label}：" in conclusions for label in ["型号", "推理强度", "价格", "适用场景", "建议"])
    assert "建议：最终建议是使用能够稳定完成任务的最低推理强度" in conclusions


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
            self._record_provider_usage({
                "provider": self.__class__.__name__,
                "model": "test-model",
                "success": False,
                "statusCode": 503,
                "latencyMs": 10,
                "promptTokens": 100,
                "completionTokens": 0,
                "totalTokens": 100,
                "cacheHitTokens": 0,
                "cacheMissTokens": 100,
                "usageReported": True,
                "estimatedCostUsd": 0.001,
                "toolCallCount": 0,
                "requestId": None,
                "errorType": "RuntimeError",
            })
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
            trace = json.loads(task.trace_json)
            assert trace["providerUsage"]["requestCount"] == 3
            assert trace["providerUsage"]["totalTokens"] == 300
            assert trace["providerUsage"]["estimatedCostUsd"] == 0.003
            assert [item["attempt"] for item in trace["providerRequests"]] == [1, 2, 3]


def test_agent_quality_gate_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    class QualityGateProvider(main.Provider):
        def analyze(self, transcript: str, goal: str) -> dict:
            return {}

        def follow_up(self, report: str, question: str) -> str:
            return ""

        def run_agent(self, toolbox: main.AgentToolbox, goal: str) -> dict:
            raise main.AgentQualityGateError("安全收尾失败")

    with TestClient(main.app) as client:
        headers = register(client, "quality_gate")
        media_id = upload(client, headers, b"quality-gate-video")
        monkeypatch.setattr(main, "publish_analysis", lambda task_id: None)
        monkeypatch.setattr(main, "provider", lambda: QualityGateProvider())
        task_id = client.post(
            "/analysis/ai",
            params={"id": media_id, "goal": "质量门禁"},
            headers=headers,
        ).json()["taskId"]

        assert main.process_analysis(task_id) == "FAILED"
        assert main.process_analysis(task_id) == "SKIPPED"
        with main.SessionLocal() as db:
            task = db.get(main.AnalysisTask, task_id)
            assert task is not None
            assert task.attempt_count == 1
            assert task.active_key is None


def test_openai_compatible_provider_executes_model_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_PIPELINE_VERSION", "legacy-v4")
    toolbox = main.AgentToolbox(
        metadata={"mediaId": 1, "filename": "course.mp4", "status": "COMPLETED"},
        segments=[{
            "segmentId": 1,
            "source": "ASR",
            "startMs": 5000,
            "endMs": 9000,
            "content": "首先创建数据库索引。",
        }],
        normalize_report=main.normalize_analysis_result,
        evaluate_report=main.evaluate_result,
    )
    responses = iter([
        {"content": None, "tool_calls": [
            {"id": "call-1", "function": {"name": "get_video_metadata", "arguments": "{}"}},
            {"id": "call-2", "function": {"name": "search_timeline", "arguments": json.dumps({"query": "数据库索引", "top_k": 3})}},
        ]},
        {"content": None, "tool_calls": [
            {"id": "call-3", "function": {"name": "get_evidence_window", "arguments": json.dumps({"timestamp_ms": 5000})}},
        ]},
        {"content": None, "tool_calls": [
            {"id": "call-4", "function": {"name": "generate_report", "arguments": json.dumps({
                "title": "索引课程笔记",
                "conclusions": ["视频首先要求创建数据库索引。"],
                "evidence": [{"timestampMs": 5000, "source": "ASR", "content": "首先创建数据库索引。"}],
                "suggestions": ["回看对应操作。"],
            }, ensure_ascii=False)}},
        ]},
    ])
    provider = main.OpenAICompatibleProvider("https://example.com/v1", "test-key", "test-model")
    monkeypatch.setattr(provider, "_completion", lambda messages, tools=None: next(responses))

    result = provider.run_agent(toolbox, "整理数据库索引操作")

    assert result["accepted"] is True
    assert result["agentGraph"]["framework"] == "LangGraph"
    assert result["agentGraph"]["intent"] == "OPERATION_GUIDE"
    assert result["agentGraph"]["steps"] == 3
    assert [item["tool"] for item in toolbox.trace()] == [
        "search_timeline",
        "get_video_metadata",
        "search_timeline",
        "get_evidence_window",
        "generate_report",
    ]


def test_langgraph_revises_report_after_critic_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_PIPELINE_VERSION", "legacy-v4")
    toolbox = main.AgentToolbox(
        metadata={"mediaId": 1, "filename": "course.mp4", "status": "COMPLETED"},
        segments=[{
            "segmentId": 1,
            "source": "ASR",
            "startMs": 5000,
            "endMs": 9000,
            "content": "首先创建数据库索引。",
        }],
        normalize_report=main.normalize_analysis_result,
        evaluate_report=main.evaluate_result,
    )
    reports = iter([
        {"content": None, "tool_calls": [{
            "id": "rejected-report",
            "function": {"name": "generate_report", "arguments": json.dumps({
                "title": "第一次报告",
                "conclusions": ["视频介绍数据库索引。"],
                "evidence": [{"timestampMs": 99999, "source": "ASR", "content": "不存在的证据"}],
                "suggestions": [],
            }, ensure_ascii=False)},
        }]},
        {"content": None, "tool_calls": [{
            "id": "accepted-report",
            "function": {"name": "generate_report", "arguments": json.dumps({
                "title": "修订后的报告",
                "conclusions": ["视频首先要求创建数据库索引。"],
                "evidence": [{"timestampMs": 5000, "source": "ASR", "content": "首先创建数据库索引。"}],
                "suggestions": [],
            }, ensure_ascii=False)},
        }]},
    ])
    provider = main.OpenAICompatibleProvider("https://example.com/v1", "test-key", "test-model")
    monkeypatch.setattr(provider, "_completion", lambda messages, tools=None: next(reports))

    result = provider.run_agent(toolbox, "整理数据库索引操作")

    assert result["accepted"] is True
    assert result["report"]["title"] == "修订后的报告"
    assert result["agentGraph"]["steps"] == 2
    report_traces = [item for item in toolbox.trace() if item["tool"] == "generate_report"]
    assert [
        item["resultPreview"].find('"accepted":false') >= 0
        for item in report_traces
    ] == [True, False]


def test_langgraph_budget_closeout_falls_back_to_explicit_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_PIPELINE_VERSION", "legacy-v4")
    toolbox = main.AgentToolbox(
        metadata={"mediaId": 1, "filename": "course.mp4", "status": "COMPLETED"},
        segments=[],
        normalize_report=main.normalize_analysis_result,
        evaluate_report=main.evaluate_result,
    )
    rejected = {
        "content": None,
        "tool_calls": [{
            "id": "rejected-report",
            "function": {"name": "generate_report", "arguments": json.dumps({
                "title": "无证据报告",
                "conclusions": ["缺少可验证证据。"],
                "evidence": [],
                "suggestions": [],
            }, ensure_ascii=False)},
        }],
    }
    provider = main.OpenAICompatibleProvider("https://example.com/v1", "test-key", "test-model")
    monkeypatch.setenv("AGENT_MAX_TOOL_STEPS", "3")
    monkeypatch.setattr(provider, "_completion", lambda messages, tools=None: rejected)

    result = provider.run_agent(toolbox, "总结视频")

    assert result["accepted"] is True
    assert result["report"]["answerable"] is False
    assert "无法从视频确定" in result["report"]["finalAnswer"]
    assert result["agentGraph"]["finalizeCalls"] == 2


def test_unanswerable_report_accepts_refusal_and_rejects_external_fact() -> None:
    refusal = main.normalize_analysis_result({
        "answerable": False,
        "finalAnswer": "视频中没有说明加密领域 token 的含义。",
        "title": "视频未说明",
        "conclusions": ["无法从视频确定该问题的答案。"],
        "evidence": [],
        "suggestions": [],
    })
    invented = main.normalize_analysis_result({
        **refusal,
        "finalAnswer": "视频中没有说明，不过加密领域的 token 通常是数字资产。",
    })
    cited_refusal = main.normalize_analysis_result({
        **refusal,
        "evidence": [{"timestampMs": 0, "source": "ASR", "content": "大模型 token"}],
    })

    assert main.evaluate_result(refusal, [])["criticPassed"] is True
    assert main.evaluate_result(refusal, [])["citationCount"] == 0
    assert main.evaluate_result(invented, [])["criticPassed"] is False
    assert main.evaluate_result(cited_refusal, [{
        "segmentId": "asr-1",
        "source": "ASR",
        "startMs": 0,
        "endMs": 1000,
        "content": "大模型 token",
    }])["criticPassed"] is False


def test_generate_report_prunes_unsupported_citations_before_rejecting() -> None:
    toolbox = main.AgentToolbox(
        metadata={"mediaId": 1},
        segments=[{
            "segmentId": "asr-1",
            "source": "ASR",
            "startMs": 5000,
            "endMs": 9000,
            "content": "公开测评中 K3 的表现相当亮眼。",
        }],
        normalize_report=main.normalize_analysis_result,
        evaluate_report=main.evaluate_result,
    )

    result = toolbox.execute("generate_report", {
        "answerable": True,
        "finalAnswer": "K3 的表现相当亮眼。",
        "title": "公开测评表现",
        "conclusions": ["K3 的表现相当亮眼。"],
        "evidence": [
            {"timestampMs": 5000, "source": "ASR", "content": "公开测评中 K3 的表现相当亮眼。"},
            {"timestampMs": 99000, "source": "OCR", "content": "不存在的排行榜"},
        ],
        "suggestions": [],
    })

    assert result["accepted"] is True
    assert len(result["report"]["evidence"]) == 1
    assert result["evaluation"]["citationRepairApplied"] is True
    assert result["evaluation"]["discardedCitationCount"] == 1


def test_qualitative_how_question_is_not_misclassified_as_operation_guide() -> None:
    plan = main.build_plan("Agent 能力公开测评中 K3 的表现怎么样？")

    assert plan["intent"] == "EVIDENCE_QA"


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


def test_follow_up_persists_user_scoped_short_term_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_history_lengths: list[int] = []

    class RememberingProvider(main.Provider):
        def analyze(self, transcript: str, goal: str) -> dict:
            return {}

        def follow_up(
            self,
            report: str,
            question: str,
            *,
            history: list[dict[str, str]] | None = None,
            memory_summary: str = "",
        ) -> str:
            captured_history_lengths.append(len(history or []))
            return f"回答：{question}；历史消息={len(history or [])}"

    monkeypatch.setattr(main, "provider", lambda: RememberingProvider())

    with TestClient(main.app) as client:
        owner = register(client, "memown")
        other = register(client, "memoth")
        media_id = upload(client, owner, b"memory-video")
        with main.SessionLocal() as db:
            media = db.get(main.Media, media_id)
            media.ai_summary = "报告说明数据库索引可以避免重复数据。"
            db.commit()

        first = client.post(
            "/analysis/follow-up",
            params={"id": media_id, "question": "索引有什么作用？"},
            headers=owner,
        )
        second = client.post(
            "/analysis/follow-up",
            params={"id": media_id, "question": "刚才的结论对应什么场景？"},
            headers=owner,
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert captured_history_lengths == [0, 2]
        memory = client.get("/analysis/agent-memory", params={"id": media_id}, headers=owner).json()
        assert memory["sessionId"]
        assert memory["messageCount"] == 4
        assert memory["sessionCount"] == 1
        assert len(memory["sessions"]) == 1
        assert memory["sessions"][0]["historyTruncated"] is False
        assert [item["role"] for item in memory["messages"]] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert "索引有什么作用" in memory["summary"]

        with main.SessionLocal() as db:
            second_goal = "从另一个分析目标继续追问"
            media = db.get(main.Media, media_id)
            second_session = main.AgentSession(
                id=str(main.uuid.uuid4()),
                user_id=media.user_id,
                media_id=media_id,
                goal=second_goal,
                goal_hash=main.hashlib.sha256(second_goal.encode("utf-8")).hexdigest(),
                updated_at=datetime.now(timezone.utc) + timedelta(seconds=1),
            )
            db.add(second_session)
            db.flush()
            db.add_all([
                main.AgentMessage(
                    session_id=second_session.id,
                    user_id=media.user_id,
                    role="user",
                    content="第二个会话的问题",
                ),
                main.AgentMessage(
                    session_id=second_session.id,
                    user_id=media.user_id,
                    role="assistant",
                    content="第二个会话的回答",
                ),
            ])
            db.commit()

        memory = client.get("/analysis/agent-memory", params={"id": media_id}, headers=owner).json()
        assert memory["sessionCount"] == 2
        assert memory["goal"] == "从另一个分析目标继续追问"
        assert sum(item["messageCount"] for item in memory["sessions"]) == 6
        media_card = client.get("/media/list", headers=owner).json()[0]
        assert media_card["hasAnalysisReport"] is True
        assert media_card["agentSessionCount"] == 2
        assert media_card["agentMessageCount"] == 6
        assert media_card["agentLastMessage"] == "第二个会话的回答"
        assert client.get("/analysis/agent-memory", params={"id": media_id}, headers=other).status_code == 404

        assert client.delete("/media/delete", params={"id": media_id}, headers=owner).status_code == 200
        with main.SessionLocal() as db:
            assert db.scalar(main.select(main.AgentSession)) is None
            assert db.scalar(main.select(main.AgentMessage)) is None


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

    overridden = main.validate_video_file(
        Path("bilibili-long.mp4"),
        max_duration_seconds=900,
    )
    assert overridden["durationSeconds"] == 601.0


def test_bilibili_preview_uses_validated_bvid_and_duration_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    assert main.normalize_bvid("https://www.bilibili.com/video/BV1xx411c7mD?p=1") == "BV1xx411c7mD"
    with pytest.raises(ValueError, match="BV"):
        main.normalize_bvid("https://example.com/video/123")

    metadata = {
        "bvid": "BV1xx411c7mD",
        "title": "公开课程视频",
        "uploader": "测试作者",
        "durationSeconds": 120,
        "coverUrl": "https://i0.hdslb.com/demo.jpg",
        "webpageUrl": "https://www.bilibili.com/video/BV1xx411c7mD",
    }
    monkeypatch.setattr(main, "fetch_bilibili_metadata", lambda _: metadata)

    with TestClient(main.app) as client:
        headers = register(client, "bilipreview")
        response = client.post("/media/bilibili/preview", json={"bvid": "BV1xx411c7mD"}, headers=headers)
        assert response.status_code == 200
        assert response.json()["title"] == "公开课程视频"

        monkeypatch.setattr(
            main,
            "fetch_bilibili_metadata",
            lambda _: {**metadata, "durationSeconds": main.BILIBILI_MAX_DURATION_SECONDS + 1},
        )
        response = client.post("/media/bilibili/preview", json={"bvid": "BV1xx411c7mD"}, headers=headers)
        assert response.status_code == 400
        assert "时长" in response.json()["detail"]


def test_bilibili_import_is_idempotent_and_enters_media_library(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "publish_bilibili_import", lambda task_id: None)
    validation_limits: list[int | None] = []

    def fake_validate(path: Path, *, max_duration_seconds: int | None = None) -> dict:
        validation_limits.append(max_duration_seconds)
        return {"codec": "h264", "durationSeconds": 30.0, "width": 1280, "height": 720}

    monkeypatch.setattr(main, "validate_video_file", fake_validate)

    def fake_download(value: str, directory: Path) -> DownloadedVideo:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "BV1xx411c7mD.mp4"
        path.write_bytes(b"downloaded-public-video")
        return DownloadedVideo(
            bvid="BV1xx411c7mD",
            title="数据结构公开课",
            uploader="测试作者",
            duration_seconds=30,
            cover_url="https://i0.hdslb.com/demo.jpg",
            path=path,
        )

    monkeypatch.setattr(main, "download_bilibili_video", fake_download)

    with TestClient(main.app) as client:
        owner = register(client, "biliowner")
        other = register(client, "biliother")
        created = client.post(
            "/media/bilibili/import",
            json={"bvid": "BV1xx411c7mD"},
            headers=owner,
        )
        assert created.status_code == 202
        task_id = created.json()["taskId"]

        duplicate = client.post(
            "/media/bilibili/import",
            json={"bvid": "BV1xx411c7mD"},
            headers=owner,
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["taskId"] == task_id

        assert main.process_bilibili_import(task_id) == "COMPLETED"
        assert validation_limits == [main.BILIBILI_MAX_DURATION_SECONDS]
        status = client.get(
            "/media/bilibili/import-status",
            params={"taskId": task_id},
            headers=owner,
        ).json()
        assert status["state"] == "COMPLETED"
        assert status["metadata"]["durationSeconds"] == 30
        assert client.get(
            "/media/bilibili/import-status",
            params={"taskId": task_id},
            headers=other,
        ).status_code == 404

        media = client.get("/media/list", headers=owner).json()
        assert len(media) == 1
        assert media[0]["sourceType"] == "BILIBILI"
        assert media[0]["sourceRef"] == "BV1xx411c7mD"
        assert media[0]["coverUrl"].endswith("demo.jpg")

        existing = client.post(
            "/media/bilibili/import",
            json={"bvid": "BV1xx411c7mD"},
            headers=owner,
        )
        assert existing.status_code == 200
        assert existing.json()["mediaId"] == media[0]["id"]
