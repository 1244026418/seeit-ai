from __future__ import annotations

import asyncio

from starlette.testclient import TestClient

from seeit import mcp_server


def test_mcp_server_exposes_video_tools_and_resources() -> None:
    tools = asyncio.run(mcp_server.mcp.list_tools())
    templates = asyncio.run(mcp_server.mcp.list_resource_templates())

    tool_names = {item.name for item in tools}
    assert {
        "list_media",
        "get_video_metadata",
        "search_video_evidence",
        "get_evidence_window",
        "verify_video_citations",
        "start_video_analysis",
        "get_analysis_status",
        "get_analysis_report",
        "ask_video",
    } <= tool_names
    assert {str(item.uriTemplate) for item in templates} == {
        "seeit://media/{media_id}/metadata",
        "seeit://media/{media_id}/timeline",
        "seeit://media/{media_id}/transcript",
        "seeit://media/{media_id}/report",
    }


def test_mcp_stdio_uses_user_token_without_exposing_it(monkeypatch) -> None:
    monkeypatch.setenv("SEEIT_MCP_TOKEN", "test-user-token")
    assert mcp_server._request_token() == "test-user-token"


def test_mcp_http_rejects_anonymous_requests() -> None:
    with TestClient(mcp_server.mcp.streamable_http_app()) as client:
        response = client.post("/mcp", json={}, headers={"Host": "127.0.0.1:8001"})
    assert response.status_code == 401
    security = mcp_server.mcp.settings.transport_security
    assert security.enable_dns_rebinding_protection is True
    assert "127.0.0.1:*" in security.allowed_hosts
    assert "attacker.invalid" not in security.allowed_hosts


def test_mcp_tool_forwards_structured_arguments(monkeypatch) -> None:
    captured = {}

    def fake_request(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return {"ok": True, "matches": []}

    monkeypatch.setattr(mcp_server, "_api_request", fake_request)
    result = mcp_server.search_video_evidence(7, "索引优化", top_k=5, sources=["ASR"])

    assert result["ok"] is True
    assert captured["method"] == "POST"
    assert captured["path"] == "/agent/tools/search-timeline"
    assert captured["json_body"] == {
        "mediaId": 7,
        "query": "索引优化",
        "topK": 5,
        "sources": ["ASR"],
    }
