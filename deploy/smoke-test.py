from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx


BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9090"
USERNAME = os.getenv("SMOKE_USERNAME", f"smoke_{int(time.time())}_{uuid.uuid4().hex[:6]}")
PASSWORD = os.getenv("SMOKE_PASSWORD", f"Smoke_{uuid.uuid4().hex[:12]}")
GOAL = "验证异步视频分析、证据引用和结构化报告链路"
VIDEO_PATH = Path("/tmp/seeit-production-smoke.mp4")


def require(response: httpx.Response, expected: int | tuple[int, ...]) -> object:
    allowed = (expected,) if isinstance(expected, int) else expected
    if response.status_code not in allowed:
        raise RuntimeError(
            f"{response.request.method} {response.request.url} -> "
            f"{response.status_code}: {response.text[:1000]}"
        )
    return response.json() if response.content else None


def create_video() -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=16000",
            "-t",
            "2",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-c:a",
            "aac",
            "-shortest",
            str(VIDEO_PATH),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )


def main() -> None:
    started = time.perf_counter()
    create_video()
    video = VIDEO_PATH.read_bytes()
    media_id: int | None = None
    token: str | None = None

    result: dict[str, object] = {
        "username": USERNAME,
        "videoBytes": len(video),
        "chunkCount": 3,
    }

    with httpx.Client(base_url=BASE_URL, timeout=30) as client:
        try:
            registration = require(
                client.post(
                    "/user/register",
                    json={"username": USERNAME, "password": PASSWORD, "nickname": "生产冒烟测试"},
                ),
                200,
            )
            require(
                client.post("/user/login", json={"username": USERNAME, "password": PASSWORD}),
                200,
            )
            token = registration["token"]
            headers = {"Authorization": f"Bearer {token}"}

            upload_id = require(
                client.post(
                    "/media/init-upload",
                    params={"filename": "server-smoke.mp4", "totalChunks": 3},
                    headers=headers,
                ),
                200,
            )
            boundaries = [0, len(video) // 3, 2 * len(video) // 3, len(video)]

            for index in (0, 2):
                require(
                    client.post(
                        "/media/upload-chunk",
                        data={"uploadId": upload_id, "chunkIndex": str(index), "totalChunks": "3"},
                        files={"file": (f"part-{index}", video[boundaries[index] : boundaries[index + 1]])},
                        headers=headers,
                    ),
                    200,
                )
            resume_status = require(
                client.get("/media/upload-status", params={"uploadId": upload_id}, headers=headers),
                200,
            )
            if resume_status != [0, 2]:
                raise RuntimeError(f"断点状态不符合预期: {resume_status}")

            require(
                client.post(
                    "/media/upload-chunk",
                    data={"uploadId": upload_id, "chunkIndex": "1", "totalChunks": "3"},
                    files={"file": ("part-1", video[boundaries[1] : boundaries[2]])},
                    headers=headers,
                ),
                200,
            )
            completed_upload = require(
                client.post("/media/complete-upload", params={"uploadId": upload_id}, headers=headers),
                200,
            )
            media_id = int(completed_upload["mediaId"])
            result.update(
                resumeStatus=resume_status,
                mediaId=media_id,
                deduplicated=completed_upload["deduplicated"],
            )

            submitted = require(
                client.post("/analysis/ai", params={"id": media_id, "goal": GOAL}, headers=headers),
                202,
            )
            result["taskId"] = submitted["taskId"]

            deadline = time.monotonic() + 120
            while True:
                status = require(
                    client.get(
                        "/analysis/analysis-status",
                        params={"id": media_id, "goal": GOAL},
                        headers=headers,
                    ),
                    200,
                )
                if status["state"] in {"COMPLETED", "FAILED"}:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"分析任务超时: {status}")
                time.sleep(1)

            result.update(
                state=status["state"],
                attemptCount=status["attemptCount"],
                reportChars=len(status.get("result") or ""),
            )
            if status["state"] != "COMPLETED":
                raise RuntimeError(f"分析任务失败: {status}")

            plan = require(
                client.get("/analysis/agent-plan", params={"id": media_id, "goal": GOAL}, headers=headers),
                200,
            )
            trace = require(
                client.get("/analysis/agent-trace", params={"id": media_id}, headers=headers),
                200,
            )
            evaluation = require(
                client.get(
                    "/analysis/agent-evaluation",
                    params={"id": media_id, "goal": GOAL},
                    headers=headers,
                ),
                200,
            )
            feedback = require(
                client.post(
                    "/analysis/agent-feedback",
                    json={
                        "mediaId": media_id,
                        "goal": GOAL,
                        "rating": 1,
                        "comment": "自动化生产冒烟验证通过",
                    },
                    headers=headers,
                ),
                200,
            )
            follow_up = require(
                client.post(
                    "/analysis/follow-up",
                    params={"id": media_id, "question": "本次分析引用了什么证据？"},
                    headers=headers,
                ),
                200,
            )
            memory = require(
                client.get("/analysis/agent-memory", params={"id": media_id}, headers=headers),
                200,
            )
            if int(memory.get("messageCount", 0)) < 2:
                raise RuntimeError(f"Agent 短期记忆未持久化追问: {memory}")
            media_list = require(client.get("/media/list", headers=headers), 200)

            result.update(
                planTaskCount=len(plan.get("tasks", [])) if isinstance(plan, dict) else 0,
                trace=trace,
                evaluation=evaluation,
                feedback=feedback,
                followUpChars=len(follow_up),
                memoryMessageCount=memory.get("messageCount", 0),
                mediaVisible=any(item["id"] == media_id for item in media_list),
            )
        finally:
            if token:
                headers = {"Authorization": f"Bearer {token}"}
                if media_id is not None:
                    try:
                        client.delete("/media/delete", params={"id": media_id}, headers=headers)
                    except httpx.HTTPError:
                        pass
                try:
                    client.post("/user/logout", headers=headers)
                except httpx.HTTPError:
                    pass

    result["elapsedSeconds"] = round(time.perf_counter() - started, 3)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    finally:
        VIDEO_PATH.unlink(missing_ok=True)
