from __future__ import annotations

import argparse
import json
import os
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse


API_URL = os.getenv("SEEIT_API_URL", "http://127.0.0.1:9090").rstrip("/")
MCP_PUBLIC_URL = os.getenv("SEEIT_MCP_PUBLIC_URL", "http://127.0.0.1:8001/mcp")
MCP_ISSUER_URL = os.getenv("SEEIT_MCP_ISSUER_URL", API_URL)


def _transport_security() -> TransportSecuritySettings:
    public_url = urlsplit(MCP_PUBLIC_URL)
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "testserver"]
    allowed_origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if public_url.netloc:
        allowed_hosts.append(public_url.netloc)
        allowed_origins.append(f"{public_url.scheme}://{public_url.netloc}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(allowed_hosts)),
        allowed_origins=list(dict.fromkeys(allowed_origins)),
    )


class VideoCitation(BaseModel):
    timestampMs: int = Field(ge=0)
    source: Literal["ASR", "OCR", "SYSTEM"]
    content: str = Field(min_length=1, max_length=2000)


class SeeItTokenVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            async with httpx.AsyncClient(base_url=API_URL, timeout=10) as client:
                response = await client.get(
                    "/user/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if response.status_code != 200:
                return None
            user = response.json()
            return AccessToken(
                token=token,
                client_id=f"seeit-user-{user['id']}",
                subject=str(user["id"]),
                scopes=["seeit"],
                claims={"username": user["username"], "role": user["role"]},
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return None


mcp = FastMCP(
    name="SeeIt AI",
    instructions=(
        "访问用户自己的 SeeIt AI 视频库，检索 ASR/OCR 时间轴证据，"
        "启动视频分析并读取带时间戳的报告。回答视频事实时必须先检索证据。"
    ),
    host=os.getenv("SEEIT_MCP_HOST", "127.0.0.1"),
    port=int(os.getenv("SEEIT_MCP_PORT", "8001")),
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    token_verifier=SeeItTokenVerifier(),
    auth=AuthSettings(
        issuer_url=MCP_ISSUER_URL,
        resource_server_url=MCP_PUBLIC_URL,
        required_scopes=["seeit"],
    ),
    transport_security=_transport_security(),
)


def _request_token() -> str:
    access_token = get_access_token()
    if access_token:
        return access_token.token
    token = os.getenv("SEEIT_MCP_TOKEN", "").strip()
    if token:
        return token
    raise RuntimeError("未提供 SeeIt 用户令牌；stdio 模式请设置 SEEIT_MCP_TOKEN")


def _api_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 130,
    allowed_statuses: set[int] | None = None,
) -> Any:
    headers = {"Authorization": f"Bearer {token or _request_token()}"}
    try:
        with httpx.Client(base_url=API_URL, timeout=timeout) as client:
            response = client.request(method, path, params=params, json=json_body, headers=headers)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"SeeIt API 连接失败：{exc.__class__.__name__}") from exc
    if response.is_error and response.status_code not in (allowed_statuses or set()):
        try:
            payload = response.json()
            detail = payload.get("detail") or payload.get("message") or str(payload)
        except (ValueError, AttributeError):
            detail = response.text[:500]
        raise RuntimeError(f"SeeIt API 返回 {response.status_code}：{detail}")
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    try:
        async with httpx.AsyncClient(base_url=API_URL, timeout=5) as client:
            response = await client.get("/health")
        api_state = "up" if response.status_code == 200 else "down"
    except httpx.HTTPError:
        api_state = "down"
    status_code = 200 if api_state == "up" else 503
    return JSONResponse({"status": "ok" if api_state == "up" else "degraded", "api": api_state}, status_code=status_code)


@mcp.tool(title="列出视频")
def list_media(limit: int = 50) -> dict[str, Any]:
    """列出当前用户的视频媒体库。"""
    items = _api_request("GET", "/media/list")
    limit = max(1, min(int(limit), 100))
    return {"items": items[:limit], "count": min(len(items), limit), "total": len(items)}


@mcp.tool(title="读取视频元数据")
def get_video_metadata(media_id: int) -> dict[str, Any]:
    """读取一个视频的来源、状态和证据数量。"""
    return _api_request("GET", "/agent/tools/video-metadata", params={"id": media_id})


@mcp.tool(title="检索视频证据")
def search_video_evidence(
    media_id: int,
    query: str,
    top_k: int = 8,
    sources: list[Literal["ASR", "OCR", "SYSTEM"]] | None = None,
) -> dict[str, Any]:
    """按问题或主题检索视频时间轴，返回带时间戳的证据片段。"""
    return _api_request(
        "POST",
        "/agent/tools/search-timeline",
        json_body={"mediaId": media_id, "query": query, "topK": top_k, "sources": sources or []},
    )


@mcp.tool(title="展开证据上下文")
def get_evidence_window(
    media_id: int,
    timestamp_ms: int,
    before_ms: int = 15000,
    after_ms: int = 15000,
) -> dict[str, Any]:
    """读取指定时间戳前后的连续 ASR/OCR 证据。"""
    return _api_request(
        "POST",
        "/agent/tools/evidence-window",
        json_body={
            "mediaId": media_id,
            "timestampMs": timestamp_ms,
            "beforeMs": before_ms,
            "afterMs": after_ms,
        },
    )


@mcp.tool(title="读取视频时间轴")
def get_video_timeline(
    media_id: int,
    start_ms: int = 0,
    end_ms: int | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """按时间范围读取视频证据时间轴。"""
    payload = _api_request("GET", "/analysis/transcription-status", params={"id": media_id})
    limit = max(1, min(int(limit), 200))
    upper = int(end_ms) if end_ms is not None else None
    segments = [
        item for item in payload.get("segments", [])
        if int(item.get("endMs", 0)) >= max(0, int(start_ms))
        and (upper is None or int(item.get("startMs", 0)) <= upper)
    ][:limit]
    return {"state": payload.get("state"), "segments": segments, "count": len(segments)}


@mcp.tool(title="校验视频引用")
def verify_video_citations(media_id: int, citations: list[VideoCitation]) -> dict[str, Any]:
    """校验候选时间戳、来源和引用文本是否受到原始视频证据支持。"""
    return _api_request(
        "POST",
        "/agent/tools/verify-citations",
        json_body={"mediaId": media_id, "citations": [item.model_dump() for item in citations]},
    )


@mcp.tool(title="启动视频分析")
def start_video_analysis(media_id: int, goal: str) -> dict[str, Any]:
    """异步启动一次指定目标的视频 Agent 分析。"""
    return _api_request(
        "POST",
        "/analysis/ai",
        params={"id": media_id, "goal": goal},
        allowed_statuses={409},
    )


@mcp.tool(title="查询分析状态")
def get_analysis_status(task_id: str) -> dict[str, Any]:
    """根据任务 ID 查询排队、执行、重试或完成状态。"""
    return _api_request("GET", "/analysis/task-status", params={"taskId": task_id})


@mcp.tool(title="读取分析报告")
def get_analysis_report(media_id: int, goal: str | None = None) -> dict[str, Any]:
    """读取一个视频最新或指定目标的结构化分析报告。"""
    params: dict[str, Any] = {"id": media_id}
    if goal:
        params["goal"] = goal
    return _api_request("GET", "/analysis/report", params=params)


@mcp.tool(title="基于视频追问")
def ask_video(media_id: int, question: str) -> dict[str, Any]:
    """基于已有视频报告继续追问，答案不得超出报告证据。"""
    answer = _api_request(
        "POST",
        "/analysis/follow-up",
        params={"id": media_id, "question": question},
    )
    return {"answer": answer}


@mcp.tool(title="读取 Agent 短期记忆")
def get_agent_memory(media_id: int) -> dict[str, Any]:
    """读取当前用户在指定视频上的最近追问会话和记忆摘要。"""
    return _api_request("GET", "/analysis/agent-memory", params={"id": media_id})


@mcp.tool(title="预览哔哩哔哩视频")
def preview_bilibili_video(bvid: str) -> dict[str, Any]:
    """校验 BV 号并读取公开视频的标题、作者和时长。"""
    return _api_request("POST", "/media/bilibili/preview", json_body={"bvid": bvid})


@mcp.tool(title="导入哔哩哔哩视频")
def import_bilibili_video(bvid: str) -> dict[str, Any]:
    """异步导入一个符合时长限制的哔哩哔哩公开视频。"""
    return _api_request(
        "POST",
        "/media/bilibili/import",
        json_body={"bvid": bvid},
        allowed_statuses={409},
    )


@mcp.tool(title="查询视频导入状态")
def get_bilibili_import_status(task_id: str) -> dict[str, Any]:
    """查询哔哩哔哩视频导入任务状态。"""
    return _api_request("GET", "/media/bilibili/import-status", params={"taskId": task_id})


@mcp.resource(
    "seeit://media/{media_id}/metadata",
    title="视频元数据",
    mime_type="application/json",
)
def media_metadata_resource(media_id: int) -> str:
    return json.dumps(get_video_metadata(media_id), ensure_ascii=False, indent=2)


@mcp.resource(
    "seeit://media/{media_id}/timeline",
    title="视频证据时间轴",
    mime_type="application/json",
)
def media_timeline_resource(media_id: int) -> str:
    return json.dumps(get_video_timeline(media_id, limit=200), ensure_ascii=False, indent=2)


@mcp.resource(
    "seeit://media/{media_id}/transcript",
    title="视频转写文本",
    mime_type="text/plain",
)
def media_transcript_resource(media_id: int) -> str:
    timeline = get_video_timeline(media_id, limit=200)
    return "\n".join(
        f"[{int(item['startMs']) // 60000:02d}:{int(item['startMs']) // 1000 % 60:02d}] "
        f"[{item['source']}] {item['content']}"
        for item in timeline["segments"]
    )


@mcp.resource(
    "seeit://media/{media_id}/report",
    title="视频分析报告",
    mime_type="text/markdown",
)
def media_report_resource(media_id: int) -> str:
    payload = get_analysis_report(media_id)
    return payload.get("report") or f"分析状态：{payload.get('state', 'UNKNOWN')}"


def main() -> None:
    parser = argparse.ArgumentParser(description="SeeIt AI MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=os.getenv("SEEIT_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument("--host", default=os.getenv("SEEIT_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("SEEIT_MCP_PORT", "8001")))
    args = parser.parse_args()
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
