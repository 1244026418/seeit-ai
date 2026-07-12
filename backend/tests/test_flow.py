import os
import time
from pathlib import Path

TEST_ROOT = Path("F:/temp/codex-seeit-ai/test-flow")
TEST_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{(TEST_ROOT / 'test.db').as_posix()}"
os.environ["UPLOAD_ROOT"] = str(TEST_ROOT / "uploads")
os.environ["JWT_SECRET"] = "test-secret"
os.environ["ROCKETMQ_NAMESERVER"] = ""
os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/15"

from fastapi.testclient import TestClient

from seeit.main import app


def test_register_upload_and_analyze() -> None:
    with TestClient(app) as client:
        username = f"tester{int(time.time() * 1000)}"
        response = client.post(
            "/user/register",
            json={"username": username, "password": "password123", "nickname": "测试用户"},
        )
        assert response.status_code == 200
        headers = {"Authorization": f"Bearer {response.json()['token']}"}

        response = client.post(
            "/media/init-upload",
            params={"filename": "demo.mp4", "totalChunks": 1},
            headers=headers,
        )
        assert response.status_code == 200
        upload_id = response.json()

        response = client.post(
            "/media/upload-chunk",
            params={"uploadId": upload_id, "chunkIndex": 0, "totalChunks": 1},
            files={"file": ("part-0", b"fake-video-content", "application/octet-stream")},
            headers=headers,
        )
        assert response.status_code == 200
        assert client.post("/media/complete-upload", params={"uploadId": upload_id}, headers=headers).status_code == 200

        media = client.get("/media/list", headers=headers).json()[0]
        goal = "总结视频内容"
        assert client.post("/analysis/ai", params={"id": media["id"], "goal": goal}, headers=headers).status_code == 202

        for _ in range(30):
            status = client.get(
                "/analysis/analysis-status",
                params={"id": media["id"], "goal": goal},
                headers=headers,
            ).json()
            if status["state"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.1)
        assert status["state"] == "COMPLETED"
        assert "视频内容分析报告" in status["result"]
