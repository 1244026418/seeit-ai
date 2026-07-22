from __future__ import annotations

import hashlib
import gc
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import jwt
import redis
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, create_engine, delete, func, select, update
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from seeit.agent import AgentToolbox, build_agent_plan, is_summary_goal
from seeit.agent_graph import run_langgraph_agent
from seeit.agent_structured import AgentQualityGateError, run_structured_evidence_agent
from seeit.bilibili import (
    BilibiliImportError,
    download_bilibili_video,
    fetch_bilibili_metadata,
    normalize_bvid,
)
from seeit.runtime_retrieval import build_runtime_retriever, delete_runtime_media_index

load_dotenv()


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


DATABASE_URL = env("DATABASE_URL", "sqlite:///./seeit.db")
UPLOAD_ROOT = Path(env("UPLOAD_ROOT", "./data/uploads")).resolve()
MAX_CHUNK_BYTES = int(env("MAX_CHUNK_BYTES", str(5 * 1024 * 1024)))
MAX_UPLOAD_BYTES = int(env("MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))
MAX_ANALYSIS_ATTEMPTS = int(env("MAX_ANALYSIS_ATTEMPTS", "3"))
MAX_IMPORT_ATTEMPTS = int(env("BILIBILI_IMPORT_ATTEMPTS", "3"))
BILIBILI_MAX_DURATION_SECONDS = int(env("BILIBILI_MAX_DURATION_SECONDS", "600"))
BILIBILI_MAX_FILE_BYTES = int(env("BILIBILI_MAX_FILE_BYTES", str(512 * 1024 * 1024)))
STALE_TASK_SECONDS = int(env("STALE_TASK_SECONDS", "1800"))
QUEUED_TASK_FALLBACK_SECONDS = max(1, int(env("QUEUED_TASK_FALLBACK_SECONDS", "10")))
UPLOAD_SESSION_TTL_HOURS = int(env("UPLOAD_SESSION_TTL_HOURS", "24"))
OCR_INTERVAL_SECONDS = max(1, int(env("OCR_INTERVAL_SECONDS", "15")))
JWT_SECRET = env("JWT_SECRET", "development-only-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(env("JWT_EXPIRE_HOURS", "24"))
log = logging.getLogger("seeit")

logging.basicConfig(
    level=getattr(logging, env("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

_local_asr_model: Any = None
_local_asr_model_key: tuple[Any, ...] | None = None
_local_asr_lock = threading.Lock()

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

ProgressCallback = Callable[[str, int, int, str], None]


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    nickname: Mapped[str] = mapped_column(String(50), default="用户")
    role: Mapped[str] = mapped_column(String(20), default="USER")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class Media(Base):
    __tablename__ = "media_files"
    __table_args__ = (
        UniqueConstraint("user_id", "content_hash", name="uq_media_user_content_hash"),
        Index("ix_media_user_upload_time", "user_id", "upload_time"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(1024))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="COMPLETED")
    source_type: Mapped[str] = mapped_column(String(20), default="UPLOAD")
    source_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    cover_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    transcript_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ai_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    upload_time: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class UploadSession(Base):
    __tablename__ = "upload_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    total_chunks: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class MediaImportTask(Base):
    __tablename__ = "media_import_tasks"
    __table_args__ = (
        UniqueConstraint("active_key", name="uq_media_import_tasks_active_key"),
        Index("ix_import_task_state_updated", "state", "updated_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    media_id: Mapped[Optional[int]] = mapped_column(ForeignKey("media_files.id"), nullable=True, index=True)
    bvid: Mapped[str] = mapped_column(String(12), index=True)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    state: Mapped[str] = mapped_column(String(20), default="QUEUED")
    active_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=MAX_IMPORT_ATTEMPTS)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class AnalysisTask(Base):
    __tablename__ = "analysis_tasks"
    __table_args__ = (
        UniqueConstraint("active_key", name="uq_analysis_tasks_active_key"),
        Index("ix_task_media_goal_created", "media_id", "goal_hash", "created_at"),
        Index("ix_task_state_updated", "state", "updated_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media_files.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    goal: Mapped[str] = mapped_column(String(500))
    goal_hash: Mapped[str] = mapped_column(String(64))
    active_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    state: Mapped[str] = mapped_column(String(20), default="QUEUED")
    stage: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    progress_message: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=MAX_ANALYSIS_ATTEMPTS)
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    answerable: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    final_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    plan_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trace_json: Mapped[Optional[str]] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"), nullable=True)
    evaluation_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class EvidenceSegment(Base):
    __tablename__ = "evidence_segments"
    __table_args__ = (Index("ix_evidence_media_timeline", "media_id", "start_ms"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media_files.id"), index=True)
    source: Mapped[str] = mapped_column(String(20))
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class AnalysisFeedback(Base):
    __tablename__ = "analysis_feedback"
    __table_args__ = (UniqueConstraint("task_id", "user_id", name="uq_feedback_task_user"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("analysis_tasks.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    rating: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (
        UniqueConstraint("user_id", "media_id", "goal_hash", name="uq_agent_session_scope"),
        Index("ix_agent_session_user_updated", "user_id", "updated_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media_files.id"), index=True)
    goal: Mapped[str] = mapped_column(String(500))
    goal_hash: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class AgentMessage(Base):
    __tablename__ = "agent_messages"
    __table_args__ = (Index("ix_agent_message_session_created", "session_id", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


def emit_progress(
    callback: ProgressCallback | None,
    stage: str,
    current: int = 0,
    total: int = 0,
    message: str = "",
) -> None:
    if callback is None:
        return
    try:
        callback(stage, max(0, int(current)), max(0, int(total)), message[:255])
    except Exception:
        log.exception("analysis_progress_callback_failed stage=%s", stage)


def persist_analysis_progress(
    task_id: str,
    stage: str,
    current: int = 0,
    total: int = 0,
    message: str = "",
) -> None:
    with SessionLocal() as db:
        db.execute(
            update(AnalysisTask)
            .where(AnalysisTask.id == task_id)
            .values(
                stage=stage[:32],
                progress_current=max(0, int(current)),
                progress_total=max(0, int(total)),
                progress_message=message[:255] or None,
                updated_at=datetime.now(timezone.utc),
            )
        )
        db.commit()


class AuthRequest(BaseModel):
    username: str
    password: str
    nickname: str = ""


class AnalysisRequest(BaseModel):
    goal: str = Field(default="概括视频主要内容和核心观点，并引用有代表性的时间戳证据", max_length=500)


class BilibiliRequest(BaseModel):
    bvid: str = Field(min_length=3, max_length=200)


class FeedbackRequest(BaseModel):
    mediaId: int
    goal: str = Field(min_length=1, max_length=500)
    rating: Optional[int] = Field(default=None, ge=-1, le=1)
    comment: Optional[str] = Field(default=None, max_length=1000)


class TimelineSearchRequest(BaseModel):
    mediaId: int
    query: str = Field(min_length=1, max_length=500)
    topK: int = Field(default=8, ge=1, le=20)
    sources: list[str] = Field(default_factory=list)


class EvidenceWindowRequest(BaseModel):
    mediaId: int
    timestampMs: int = Field(ge=0)
    beforeMs: int = Field(default=15000, ge=0, le=120000)
    afterMs: int = Field(default=15000, ge=0, le=120000)


class CitationRequest(BaseModel):
    timestampMs: int = Field(ge=0)
    source: str = Field(min_length=1, max_length=20)
    content: str = Field(min_length=1, max_length=2000)


class CitationVerificationRequest(BaseModel):
    mediaId: int
    citations: list[CitationRequest] = Field(min_length=1, max_length=20)


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _usage_percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.999999)))
    return ordered[index]


def summarize_provider_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    costs = [
        _safe_float(item.get("estimatedCostUsd"))
        for item in events
        if item.get("estimatedCostUsd") is not None
    ]
    latencies = [_safe_int(item.get("latencyMs")) for item in events]
    by_phase: dict[str, dict[str, int]] = {}
    for item in events:
        phase = str(item.get("phase") or "UNSPECIFIED")
        summary = by_phase.setdefault(phase, {
            "requestCount": 0,
            "successCount": 0,
            "failureCount": 0,
            "promptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
            "cacheHitTokens": 0,
            "cacheMissTokens": 0,
            "toolCallCount": 0,
        })
        summary["requestCount"] += 1
        summary["successCount"] += int(bool(item.get("success")))
        summary["failureCount"] += int(not bool(item.get("success")))
        for key in (
            "promptTokens",
            "completionTokens",
            "totalTokens",
            "cacheHitTokens",
            "cacheMissTokens",
            "toolCallCount",
        ):
            summary[key] += _safe_int(item.get(key))
    return {
        "requestCount": len(events),
        "successCount": sum(bool(item.get("success")) for item in events),
        "failureCount": sum(not bool(item.get("success")) for item in events),
        "usageReportedCount": sum(bool(item.get("usageReported")) for item in events),
        "promptTokens": sum(_safe_int(item.get("promptTokens")) for item in events),
        "completionTokens": sum(_safe_int(item.get("completionTokens")) for item in events),
        "totalTokens": sum(_safe_int(item.get("totalTokens")) for item in events),
        "cacheHitTokens": sum(_safe_int(item.get("cacheHitTokens")) for item in events),
        "cacheMissTokens": sum(_safe_int(item.get("cacheMissTokens")) for item in events),
        "toolCallCount": sum(_safe_int(item.get("toolCallCount")) for item in events),
        "estimatedCostUsd": round(sum(costs), 8) if costs else None,
        "costConfigured": bool(costs),
        "latencyMsTotal": sum(latencies),
        "latencyMsP50": _usage_percentile(latencies, 0.50),
        "latencyMsP95": _usage_percentile(latencies, 0.95),
        "byPhase": dict(sorted(by_phase.items())),
    }


class Provider:
    agent_mode = "DETERMINISTIC_TOOL_PIPELINE"

    def _record_provider_usage(self, event: dict[str, Any]) -> None:
        events = self.__dict__.setdefault("_provider_usage_events", [])
        events.append({"requestIndex": len(events) + 1, **event})

    def provider_usage_events(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.__dict__.get("_provider_usage_events", [])]

    def provider_usage_summary(self) -> dict[str, Any]:
        return summarize_provider_usage(self.provider_usage_events())

    def analyze(self, transcript: str, goal: str) -> dict[str, Any]:
        raise NotImplementedError

    def follow_up(
        self,
        report: str,
        question: str,
        *,
        history: Optional[list[dict[str, str]]] = None,
        memory_summary: str = "",
        evidence_timeline: str = "",
    ) -> str:
        raise NotImplementedError

    def run_agent(self, toolbox: AgentToolbox, goal: str) -> dict[str, Any]:
        toolbox.execute("get_video_metadata")
        if is_summary_goal(goal):
            overview = toolbox.execute("get_timeline_overview", {"max_segments": 18})
            search = {"fallbackToTimelineStart": False}
            matches = overview.get("segments", []) if overview.get("ok") else []
        else:
            search = toolbox.execute("search_timeline", {"query": goal, "top_k": 8})
            matches = search.get("matches", []) if search.get("ok") else []
        timeline = [item for item in toolbox.segments if item.get("source") != "SYSTEM"]
        anchor_matches: list[dict[str, Any]] = []
        if timeline:
            positions = sorted({
                0,
                len(timeline) // 4,
                len(timeline) // 2,
                (len(timeline) * 3) // 4,
                len(timeline) - 1,
            })
            anchor_matches = [timeline[index] for index in positions]
        if search.get("fallbackToTimelineStart"):
            matches = anchor_matches
        selected_segments: list[dict[str, Any]] = []
        seen: set[str] = set()
        for match in [*matches[:8], *anchor_matches]:
            window = toolbox.execute("get_evidence_window", {
                "timestamp_ms": int(match.get("startMs", 0)),
                "before_ms": 15000,
                "after_ms": 15000,
            })
            for segment in window.get("segments", []):
                segment_id = str(segment.get("segmentId"))
                if segment_id not in seen:
                    selected_segments.append(segment)
                    seen.add(segment_id)
        if not selected_segments:
            selected_segments = matches
        selected_segments.sort(key=lambda item: (int(item.get("startMs", 0)), str(item.get("segmentId", ""))))
        transcript = evidence_context(selected_segments)
        candidate = self.analyze(transcript, goal)
        return toolbox.execute("generate_report", candidate)


class MockProvider(Provider):
    _topic_expansions = {
        "型号": "型号 模型 版本 Sol Terra Luna",
        "推理强度": "推理强度 Reasoning Effort None Low Medium High X-High Max",
        "价格": "价格 定价 费用 成本 美元 输入 输出 百万 Token",
        "适用场景": "适用场景 适合 任务 简单 通用 复杂 专业 编程",
        "建议": "建议 最低 稳定 完成 推理强度 不要 默认 拉满 None Low Medium High X-High Max",
    }

    def analyze(self, transcript: str, goal: str) -> dict[str, Any]:
        text = transcript.strip() or "未检测到可用转写内容。"
        parsed: list[dict[str, Any]] = []
        for line in text.splitlines():
            match = re.match(r"\[(\d+)-(\d+)ms]\[([^]]+)]\s*(.*)", line.strip())
            if not match or not match.group(4).strip():
                continue
            parsed.append({
                "timestampMs": int(match.group(1)),
                "source": match.group(3),
                "content": match.group(4).strip()[:1800],
            })
        if not parsed:
            first_line = next((line for line in text.splitlines() if line.strip()), text)
            parsed = [{"timestampMs": 0, "source": "ASR", "content": first_line[:1800]}]

        topic_matches: list[tuple[str, dict[str, Any]]] = []
        for label, expanded_query in self._topic_expansions.items():
            if label not in goal and not (label == "建议" and re.search(r"结论|推荐|最终", goal)):
                continue
            scored = [
                (AgentToolbox._relevance(expanded_query, item["content"]), item)
                for item in parsed
            ]
            best_score = max(score for score, _ in scored)
            if label == "建议" and best_score > 0:
                near_best = [item for score, item in scored if score >= best_score * 0.75]
                best = max(near_best, key=lambda item: item["timestampMs"])
            else:
                best = max(scored, key=lambda pair: pair[0])[1]
            if best_score > 0:
                topic_matches.append((label, best))

        positions = sorted({
            0,
            len(parsed) // 4,
            len(parsed) // 2,
            (len(parsed) * 3) // 4,
            len(parsed) - 1,
        })
        candidates = [item for _, item in topic_matches] + [parsed[index] for index in positions]
        evidence: list[dict[str, Any]] = []
        seen: set[tuple[int, str, str]] = set()
        for item in candidates:
            key = (item["timestampMs"], item["source"], item["content"])
            if key not in seen:
                evidence.append(item)
                seen.add(key)
            if len(evidence) >= 8:
                break
        evidence.sort(key=lambda item: item["timestampMs"])
        conclusions = [
            f"围绕“{goal}”提取了 {len(evidence)} 组可核验证据，并覆盖视频开头、中段和结尾。",
            *[f"{label}：{item['content'][:320]}" for label, item in topic_matches[:6]],
        ]
        if len(conclusions) == 1:
            conclusions.extend(item["content"][:320] for item in evidence[:4])
        return {
            "answerable": bool(evidence),
            "finalAnswer": (
                conclusions[0]
                if evidence
                else "视频未明确说明该问题，无法从视频确定答案。"
            ),
            "title": "视频内容分析报告",
            "conclusions": conclusions,
            "evidence": evidence,
            "suggestions": ["若需更强的归纳与追问能力，请配置真实 LLM Provider；当前报告为可复现的抽取式摘要。"],
        }

    def follow_up(
        self,
        report: str,
        question: str,
        *,
        history: Optional[list[dict[str, str]]] = None,
        memory_summary: str = "",
        evidence_timeline: str = "",
    ) -> str:
        turn_count = len(history or []) // 2
        memory_note = f"，并参考了此前 {turn_count} 轮追问" if turn_count else ""
        evidence_note = f"\n\n原始视频证据：\n{evidence_timeline[:3000]}" if evidence_timeline else ""
        return f"## 追问结果\n\n问题：{question}\n\n基于当前报告{memory_note}：\n\n{report[:3000]}{evidence_note}"


class OpenAICompatibleProvider(Provider):
    agent_mode = "MODEL_TOOL_CALLING_LANGGRAPH"

    def __init__(self, base_url: str, api_key: str, model: str):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self._agent_phase = "UNSPECIFIED"

    @staticmethod
    def _usage_tokens(usage: dict[str, Any]) -> dict[str, Any]:
        prompt_tokens = _safe_int(usage.get("prompt_tokens"))
        completion_tokens = _safe_int(usage.get("completion_tokens"))
        total_tokens = _safe_int(usage.get("total_tokens"))
        prompt_details = usage.get("prompt_tokens_details")
        prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
        cache_hit_raw = usage.get("prompt_cache_hit_tokens")
        if cache_hit_raw is None:
            cache_hit_raw = prompt_details.get("cached_tokens")
        cache_hit_tokens = _safe_int(cache_hit_raw)
        cache_miss_raw = usage.get("prompt_cache_miss_tokens")
        cache_miss_tokens = (
            _safe_int(cache_miss_raw)
            if cache_miss_raw is not None
            else max(0, prompt_tokens - cache_hit_tokens)
        )
        usage_reported = any(
            key in usage and usage.get(key) is not None
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        )
        return {
            "promptTokens": prompt_tokens,
            "completionTokens": completion_tokens,
            "totalTokens": total_tokens,
            "cacheHitTokens": cache_hit_tokens,
            "cacheMissTokens": cache_miss_tokens,
            "usageReported": usage_reported,
        }

    @staticmethod
    def _token_cost(tokens: dict[str, Any]) -> float | None:
        input_rate = max(0.0, _safe_float(env("AI_INPUT_COST_PER_MILLION_TOKENS", "0")))
        output_rate = max(0.0, _safe_float(env("AI_OUTPUT_COST_PER_MILLION_TOKENS", "0")))
        cache_hit_rate = max(0.0, _safe_float(env("AI_CACHE_HIT_INPUT_COST_PER_MILLION_TOKENS", "0")))
        if input_rate <= 0 or output_rate <= 0 or not tokens.get("usageReported"):
            return None
        prompt_tokens = _safe_int(tokens.get("promptTokens"))
        completion_tokens = _safe_int(tokens.get("completionTokens"))
        cache_hit_tokens = _safe_int(tokens.get("cacheHitTokens"))
        uncached_tokens = max(0, prompt_tokens - cache_hit_tokens)
        effective_cache_rate = cache_hit_rate if cache_hit_rate > 0 else input_rate
        cost = (
            uncached_tokens * input_rate
            + cache_hit_tokens * effective_cache_rate
            + completion_tokens * output_rate
        ) / 1_000_000
        return round(cost, 8)

    def _completion(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Any = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.1,
            "messages": messages,
        }
        thinking_mode = env("AI_THINKING_MODE").strip().lower()
        if thinking_mode in {"enabled", "disabled"}:
            payload["thinking"] = {"type": thinking_mode}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        started = time.perf_counter()
        response: Any = None
        try:
            response = httpx.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=max(30, int(env("AI_REQUEST_TIMEOUT_SECONDS", "180"))),
            )
            response.raise_for_status()
            body = response.json()
            message = body["choices"][0]["message"]
            usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
            tokens = self._usage_tokens(usage)
            headers = getattr(response, "headers", {}) or {}
            self._record_provider_usage({
                "provider": self.__class__.__name__,
                "model": self.model,
                "phase": self._agent_phase,
                "success": True,
                "statusCode": int(getattr(response, "status_code", 200)),
                "latencyMs": max(0, int((time.perf_counter() - started) * 1000)),
                **tokens,
                "estimatedCostUsd": self._token_cost(tokens),
                "toolCallCount": len(message.get("tool_calls") or []),
                "requestId": str(headers.get("x-request-id") or headers.get("x-requestid") or "")[:200] or None,
                "errorType": None,
            })
            return message
        except Exception as exc:
            self._record_provider_usage({
                "provider": self.__class__.__name__,
                "model": self.model,
                "phase": self._agent_phase,
                "success": False,
                "statusCode": int(getattr(response, "status_code", 0) or 0),
                "latencyMs": max(0, int((time.perf_counter() - started) * 1000)),
                "promptTokens": 0,
                "completionTokens": 0,
                "totalTokens": 0,
                "cacheHitTokens": 0,
                "cacheMissTokens": 0,
                "usageReported": False,
                "estimatedCostUsd": None,
                "toolCallCount": 0,
                "requestId": None,
                "errorType": type(exc).__name__,
            })
            raise

    def _chat(self, prompt: str) -> str:
        message = self._completion([{"role": "user", "content": prompt}])
        return str(message.get("content") or "")

    def analyze(self, transcript: str, goal: str) -> dict[str, Any]:
        prompt = (
            "只返回 JSON，字段为 answerable、finalAnswer、title、conclusions、evidence、suggestions。"
            "如果视频证据无法回答，answerable=false，finalAnswer 明确拒答，evidence 为空，且不得补充外部知识。"
            "evidence 必须包含 timestampMs、source、content。"
            "只能引用证据时间轴中存在的内容和时间戳，不得编造。"
            f"\n目标：{goal}\n证据时间轴：\n{transcript[:16000]}"
        )
        raw = self._chat(prompt).replace("```json", "").replace("```", "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型未返回合法 JSON 对象")
        return json.loads(raw[start : end + 1])

    def follow_up(
        self,
        report: str,
        question: str,
        *,
        history: Optional[list[dict[str, str]]] = None,
        memory_summary: str = "",
        evidence_timeline: str = "",
    ) -> str:
        messages: list[dict[str, Any]] = [{
            "role": "system",
            "content": (
                "你是 SeeIt AI 的多轮视频问答 Agent。只能根据已有报告和对话中出现的证据回答；"
                "同时优先使用本轮提供的原始 ASR/OCR 时间轴。"
                "对于‘主要讲了什么、总结视频’等概括题，应综合跨时间位置的代表性证据直接回答。"
                "如果视频没有逐字说明，但答案能完全由给定视频证据合理推出，可以回答，"
                "并在结尾明确写‘此答案为基于视频证据的推测，视频没有明确说明。’"
                "无法由证据推出时才拒答，不得使用视频外知识或编造时间戳。"
            ),
        }, {
            "role": "user",
            "content": (
                f"视频分析报告：\n{report[:10000]}\n\n"
                f"本轮检索到的原始视频证据：\n{evidence_timeline[:10000]}\n\n"
                f"历史记忆摘要：\n{memory_summary[:3000]}"
            ),
        }]
        for item in (history or [])[-20:]:
            role = str(item.get("role") or "").lower()
            if role in {"user", "assistant"}:
                messages.append({"role": role, "content": str(item.get("content") or "")[:4000]})
        messages.append({"role": "user", "content": question})
        follow_up_schema = {
            "type": "function",
            "function": {
                "name": "submit_follow_up_answer",
                "description": "提交带证据基础分类的视频追问回答。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "answerBasis": {
                            "type": "string",
                            "enum": ["EXPLICIT", "SYNTHESIS", "GROUNDED_INFERENCE", "UNANSWERABLE"],
                        },
                        "answer": {"type": "string", "minLength": 1, "maxLength": 4000},
                    },
                    "required": ["answerBasis", "answer"],
                    "additionalProperties": False,
                },
            },
        }
        messages[0]["content"] += (
            "回答前必须判断 answerBasis：视频直接陈述答案用 EXPLICIT；概括多条证据用 SYNTHESIS；"
            "结论并非视频原话、而是由视频证据推导出来时必须用 GROUNDED_INFERENCE；"
            "证据无法支持时用 UNANSWERABLE。条件假设、偏好选择和‘会更推荐’通常属于 GROUNDED_INFERENCE。"
        )
        message = self._completion(
            messages,
            [follow_up_schema],
            tool_choice={"type": "function", "function": {"name": "submit_follow_up_answer"}},
        )
        arguments: dict[str, Any] = {}
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name") != "submit_follow_up_answer":
                continue
            raw = function.get("arguments") or "{}"
            try:
                arguments = raw if isinstance(raw, dict) else json.loads(str(raw))
            except (TypeError, ValueError, json.JSONDecodeError):
                arguments = {}
            break
        answer = str(arguments.get("answer") or message.get("content") or "").strip()
        basis = str(arguments.get("answerBasis") or "UNANSWERABLE").upper()
        inference_cues = re.search(
            r"如果|假如|意味着|可以推断|由此可见|更适合|会更|可能|推测|推断",
            question,
        )
        disclaimer = "此答案为基于视频证据的推测，视频没有明确说明。"
        if answer and (basis == "GROUNDED_INFERENCE" or inference_cues) and disclaimer not in answer:
            answer = answer.rstrip("。") + "。\n\n" + disclaimer
        if not answer:
            return "视频证据不足，无法回答这个问题。"
        return answer

    def run_agent(self, toolbox: AgentToolbox, goal: str) -> dict[str, Any]:
        pipeline = env("AGENT_PIPELINE_VERSION", "structured-v5").strip().lower()
        if pipeline in {"structured-v5", "v5", "structured"}:
            return run_structured_evidence_agent(self, toolbox, goal)
        max_steps = max(3, min(int(env("AGENT_MAX_TOOL_STEPS", "8")), 12))
        return run_langgraph_agent(self, toolbox, goal, max_steps=max_steps)


def provider() -> Provider:
    if env("AI_BASE_URL") and env("AI_API_KEY") and env("AI_MODEL"):
        return OpenAICompatibleProvider(env("AI_BASE_URL"), env("AI_API_KEY"), env("AI_MODEL"))
    return MockProvider()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 180_000)
    return f"pbkdf2_sha256$180000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, rounds, salt_hex, digest_hex = encoded.split("$")
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
        return secrets.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def issue_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if not payload.get("jti"):
            raise jwt.InvalidTokenError("missing jti")
        client = redis_client()
        if client and client.get(f"jwt:revoked:{payload['jti']}"):
            raise jwt.InvalidTokenError("revoked token")
        return payload
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="登录已失效") from exc


def current_user(authorization: Optional[str] = Header(default=None)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = decode_token(authorization[7:])
        user_id = int(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=401, detail="登录已失效")
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="用户不存在或已禁用")
        return user


def owned_media(db: Session, media_id: int, user: User) -> Media:
    media = db.get(Media, media_id)
    if not media or media.user_id != user.id:
        raise HTTPException(status_code=404, detail="媒体不存在")
    return media


def chunk_dir(upload_id: str) -> Path:
    return UPLOAD_ROOT / "chunks" / upload_id


def media_dir() -> Path:
    path = UPLOAD_ROOT / "media"
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_filename(filename: str) -> str:
    value = Path(filename or "video.bin").name
    return re.sub(r"[^\w.()\-\u4e00-\u9fff ]", "_", value)[:255] or "video.bin"


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_video_file(
    path: Path,
    *,
    max_duration_seconds: int | None = None,
) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=60,
    )
    payload = json.loads(completed.stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise ValueError("文件中未检测到视频轨道")
    duration = float((payload.get("format") or {}).get("duration") or 0)
    max_duration = max(
        1,
        int(
            max_duration_seconds
            if max_duration_seconds is not None
            else env("MAX_VIDEO_DURATION_SECONDS", "900")
        ),
    )
    if duration <= 0:
        raise ValueError("无法读取视频时长")
    if duration > max_duration:
        raise ValueError(f"视频时长不能超过 {max_duration // 60} 分钟")
    stream = streams[0]
    return {
        "codec": stream.get("codec_name"),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "durationSeconds": round(duration, 3),
    }


def _milliseconds(value: Any, *, seconds: bool = False) -> int:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, int(number * 1000 if seconds else number))


def normalize_segment(item: dict[str, Any], default_source: str = "ASR") -> Optional[dict[str, Any]]:
    content = re.sub(r"\s+", " ", str(item.get("content") or item.get("text") or "")).strip()
    if not content:
        return None
    has_milliseconds = "startMs" in item or "endMs" in item or "timestampMs" in item
    start_value = item.get("startMs", item.get("timestampMs")) if has_milliseconds else item.get("start")
    start_ms = _milliseconds(start_value, seconds=not has_milliseconds)
    end_ms = _milliseconds(item.get("endMs") if has_milliseconds else item.get("end"), seconds=not has_milliseconds)
    return {
        "source": str(item.get("source") or default_source).upper()[:20],
        "startMs": start_ms,
        "endMs": max(start_ms, end_ms),
        "content": content,
    }


def parse_segment_payload(payload: Any, default_source: str = "ASR") -> list[dict[str, Any]]:
    items = payload.get("segments", []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    segments = [normalize_segment(item, default_source) for item in items if isinstance(item, dict)]
    return [item for item in segments if item]


def read_sidecar_evidence(path: Path) -> list[dict[str, Any]]:
    segment_sidecar = path.with_suffix(path.suffix + ".segments.json")
    if segment_sidecar.exists():
        return parse_segment_payload(json.loads(segment_sidecar.read_text(encoding="utf-8")))
    text_sidecar = path.with_suffix(path.suffix + ".txt")
    if not text_sidecar.exists():
        return []
    text = text_sidecar.read_text(encoding="utf-8").strip()
    return [{"source": "ASR", "startMs": 0, "endMs": 0, "content": text}] if text else []


def extract_audio_file(video_path: Path, audio_path: Path) -> Path:
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(audio_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=900,
    )
    return audio_path


def request_asr(audio_path: Path) -> list[dict[str, Any]]:
    asr_base_url = env("ASR_BASE_URL")
    asr_api_key = env("ASR_API_KEY")
    if not asr_base_url:
        return []
    headers = {"Authorization": f"Bearer {asr_api_key}"} if asr_api_key else {}
    with audio_path.open("rb") as audio:
        response = httpx.post(
            asr_base_url.rstrip("/") + "/audio/transcriptions",
            headers=headers,
            data={"model": env("ASR_MODEL", "TeleAI/TeleSpeechASR"), "response_format": "verbose_json"},
            files={"file": (audio_path.name, audio, "audio/mpeg")},
            timeout=180,
        )
    response.raise_for_status()
    payload = response.json()
    segments = parse_segment_payload(payload)
    if segments:
        return segments
    text = str(payload.get("text", "")).strip() if isinstance(payload, dict) else ""
    return [{"source": "ASR", "startMs": 0, "endMs": 0, "content": text}] if text else []


def local_asr_enabled() -> bool:
    return env("LOCAL_ASR_ENABLED", "false").lower() in {"1", "true", "yes"}


def local_asr_model() -> Any:
    global _local_asr_model, _local_asr_model_key
    model_name = env("LOCAL_ASR_MODEL", "base")
    model_root = Path(env("LOCAL_ASR_MODEL_ROOT", str(UPLOAD_ROOT / "models"))).resolve()
    device = env("LOCAL_ASR_DEVICE", "cpu")
    compute_type = env("LOCAL_ASR_COMPUTE_TYPE", "int8")
    cpu_threads = max(1, int(env("LOCAL_ASR_CPU_THREADS", "4")))
    model_key = (model_name, str(model_root), device, compute_type, cpu_threads)
    with _local_asr_lock:
        if _local_asr_model is not None and _local_asr_model_key == model_key:
            return _local_asr_model
        from faster_whisper import WhisperModel

        model_root.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        _local_asr_model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
            download_root=str(model_root),
        )
        _local_asr_model_key = model_key
        log.info(
            "local_asr_model_loaded model=%s device=%s compute_type=%s elapsed_ms=%s",
            model_name,
            device,
            compute_type,
            int((time.perf_counter() - started) * 1000),
        )
        return _local_asr_model


def warm_local_asr_model() -> None:
    if local_asr_enabled() and env("LOCAL_ASR_PRELOAD", "true").lower() in {"1", "true", "yes"}:
        local_asr_model()


def release_local_asr_model() -> None:
    global _local_asr_model, _local_asr_model_key
    with _local_asr_lock:
        if _local_asr_model is None:
            return
        _local_asr_model = None
        _local_asr_model_key = None
    gc.collect()
    log.info("local_asr_model_released")


def request_local_asr(
    media_path: Path,
    progress_callback: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    model = local_asr_model()
    started = time.perf_counter()
    language = env("LOCAL_ASR_LANGUAGE", "zh").strip() or None
    segments_iter, info = model.transcribe(
        str(media_path),
        language=language,
        beam_size=max(1, int(env("LOCAL_ASR_BEAM_SIZE", "5"))),
        vad_filter=env("LOCAL_ASR_VAD_FILTER", "true").lower() in {"1", "true", "yes"},
        condition_on_previous_text=True,
        initial_prompt=env("LOCAL_ASR_INITIAL_PROMPT") or None,
        hotwords=env("LOCAL_ASR_HOTWORDS") or None,
    )
    duration_seconds = max(1, int(float(getattr(info, "duration", 0) or 0) + 0.999))
    segments: list[dict[str, Any]] = []
    last_reported = -5
    for item in segments_iter:
        if item.text.strip():
            segments.append({
            "source": "ASR",
            "startMs": max(0, int(item.start * 1000)),
            "endMs": max(0, int(item.end * 1000)),
            "content": item.text.strip(),
            })
        current_seconds = min(duration_seconds, max(0, int(float(item.end) + 0.999)))
        if current_seconds >= duration_seconds or current_seconds - last_reported >= 5:
            emit_progress(
                progress_callback,
                "ASR",
                current_seconds,
                duration_seconds,
                f"ASR 处理中（{current_seconds}/{duration_seconds} 秒）",
            )
            last_reported = current_seconds
    emit_progress(
        progress_callback,
        "ASR",
        duration_seconds,
        duration_seconds,
        "ASR 处理完成",
    )
    log.info(
        "local_asr_completed path=%s language=%s duration_seconds=%.3f segments=%s elapsed_ms=%s",
        media_path.name,
        getattr(info, "language", language),
        float(getattr(info, "duration", 0) or 0),
        len(segments),
        int((time.perf_counter() - started) * 1000),
    )
    return segments


def run_paddle_ocr_frames(
    directory: Path,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    output_path = directory / "paddle-results.json"
    progress_path = directory / "paddle-progress.json"
    command = [
            sys.executable,
            "-m",
            "seeit.ocr_runner",
            "--input-dir",
            str(directory),
            "--output",
            str(output_path),
            "--progress-file",
            str(progress_path),
        ]
    stop_polling = threading.Event()

    def forward_progress() -> None:
        last_progress: tuple[int, int] | None = None
        while not stop_polling.wait(0.25):
            if not progress_path.exists():
                continue
            try:
                progress = json.loads(progress_path.read_text(encoding="utf-8"))
                current = max(0, int(progress.get("current", 0)))
                total = max(0, int(progress.get("total", 0)))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if (current, total) == last_progress:
                continue
            emit_progress(
                progress_callback,
                "OCR",
                current,
                total,
                f"OCR 处理中（{current}/{total} 帧）" if total else "OCR 处理中",
            )
            last_progress = (current, total)

    progress_thread = threading.Thread(target=forward_progress, name="ocr-progress", daemon=True)
    if progress_callback is not None:
        progress_thread.start()
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(60, int(env("PADDLEOCR_PROCESS_TIMEOUT_SECONDS", "600"))),
        )
    finally:
        stop_polling.set()
        if progress_thread.is_alive():
            progress_thread.join(timeout=1)
    if progress_path.exists():
        try:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            current = max(0, int(progress.get("current", 0)))
            total = max(0, int(progress.get("total", 0)))
            emit_progress(progress_callback, "OCR", current, total, f"OCR 处理中（{current}/{total} 帧）")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    if not output_path.exists():
        log.error(
            "paddle_ocr_process_failed returncode=%s stderr=%s",
            completed.returncode,
            completed.stderr[-4000:],
        )
        return {"frameCount": 0, "results": [], "errors": []}
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    if completed.returncode != 0 or payload.get("fatalError"):
        log.error(
            "paddle_ocr_process_failed returncode=%s fatal=%s stderr=%s",
            completed.returncode,
            payload.get("fatalError"),
            completed.stderr[-4000:],
        )
    return payload


def extract_ocr_evidence(
    path: Path,
    progress_callback: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    if env("OCR_ENABLED", "false").lower() not in {"1", "true", "yes"}:
        return []
    interval_seconds = max(1, int(env("OCR_INTERVAL_SECONDS", "15")))
    max_frames = max(1, int(env("PADDLEOCR_MAX_FRAMES", "40")))
    max_width = max(320, int(env("PADDLEOCR_FRAME_MAX_WIDTH", "960")))
    dedup_threshold = min(1.0, max(0.0, float(env("PADDLEOCR_DEDUP_THRESHOLD", "0.88"))))
    started = time.perf_counter()
    temp_root = UPLOAD_ROOT / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ocr-", dir=temp_root) as directory:
        frame_pattern = str(Path(directory) / "frame-%06d.png")
        frame_filter = (
            f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{interval_seconds})',"
            f"scale='min({max_width},iw)':-2"
        )
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(path),
                "-vf",
                frame_filter,
                "-fps_mode",
                "vfr",
                "-frames:v",
                str(max_frames),
                frame_pattern,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=900,
        )
        frame_count = len(list(Path(directory).glob("frame-*.png")))
        emit_progress(
            progress_callback,
            "OCR",
            0,
            frame_count,
            f"OCR 处理中（0/{frame_count} 帧）" if frame_count else "OCR 未抽取到可处理画面",
        )
        if env("LOCAL_ASR_RELEASE_BEFORE_OCR", "true").lower() in {"1", "true", "yes"}:
            release_local_asr_model()
        payload = (
            run_paddle_ocr_frames(Path(directory), progress_callback)
            if progress_callback is not None
            else run_paddle_ocr_frames(Path(directory))
        )
        for error in payload.get("errors", []):
            log.warning("paddle_ocr_frame_failed frame=%s error=%s", error.get("frame"), error.get("error"))

        segments: list[dict[str, Any]] = []
        previous_content = ""
        for item in payload.get("results", []):
            index = max(0, int(item.get("index", 0)))
            content = str(item.get("content", "")).strip()
            if previous_content and _content_similarity(previous_content, content) >= dedup_threshold:
                continue
            if content:
                start_ms = index * interval_seconds * 1000
                segments.append({
                    "source": "OCR",
                    "startMs": start_ms,
                    "endMs": start_ms + interval_seconds * 1000,
                    "content": content,
                })
                previous_content = content
        log.info(
            "paddle_ocr_completed path=%s frames=%s evidence=%s model_load_ms=%s runner_elapsed_ms=%s elapsed_ms=%s",
            path.name,
            int(payload.get("frameCount", 0)),
            len(segments),
            int(payload.get("modelLoadMs", 0)),
            int(payload.get("elapsedMs", 0)),
            int((time.perf_counter() - started) * 1000),
        )
        emit_progress(
            progress_callback,
            "OCR",
            int(payload.get("frameCount", 0)),
            int(payload.get("frameCount", 0)),
            "OCR 处理完成",
        )
        return segments


def collect_evidence(
    path: Path,
    progress_callback: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    segments = read_sidecar_evidence(path)
    if not segments and env("ASR_BASE_URL"):
        emit_progress(progress_callback, "ASR", 0, 1, "ASR 处理中")
        temp_dir = UPLOAD_ROOT / "tmp"
        audio_path = temp_dir / f"{uuid.uuid4()}.mp3"
        try:
            extract_audio_file(path, audio_path)
            try:
                segments = request_asr(audio_path)
                emit_progress(progress_callback, "ASR", 1, 1, "ASR 处理完成")
            except Exception:
                if not local_asr_enabled():
                    raise
                log.exception("remote_asr_failed path=%s; falling back to local ASR", path.name)
        finally:
            audio_path.unlink(missing_ok=True)
    if not segments and local_asr_enabled():
        emit_progress(progress_callback, "ASR", 0, 0, "ASR 处理中")
        segments = (
            request_local_asr(path, progress_callback)
            if progress_callback is not None
            else request_local_asr(path)
        )
    segments.extend(
        extract_ocr_evidence(path, progress_callback)
        if progress_callback is not None
        else extract_ocr_evidence(path)
    )
    if not segments:
        segments.append({
            "source": "SYSTEM",
            "startMs": 0,
            "endMs": 0,
            "content": f"文件 {path.name} 已上传；配置 ASR/OCR 后可生成真实时间轴证据。",
        })
    return sorted(segments, key=lambda item: (item["startMs"], item["source"]))


def evidence_context(segments: list[dict[str, Any]]) -> str:
    lines = [
        f"[{item['startMs']}-{item['endMs']}ms][{item['source']}] {item['content']}"
        for item in segments[:200]
    ]
    return "\n".join(lines)[:20000]


def replace_evidence(db: Session, media_id: int, segments: list[dict[str, Any]]) -> None:
    db.execute(delete(EvidenceSegment).where(EvidenceSegment.media_id == media_id))
    db.add_all([
        EvidenceSegment(
            media_id=media_id,
            source=item["source"],
            start_ms=item["startMs"],
            end_ms=item["endMs"],
            content=item["content"],
        )
        for item in segments
    ])


def stored_evidence(db: Session, media_id: int) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(EvidenceSegment).where(EvidenceSegment.media_id == media_id).order_by(EvidenceSegment.start_ms, EvidenceSegment.id)
    ).all()
    return [
        {
            "segmentId": row.id,
            "source": row.source,
            "startMs": row.start_ms,
            "endMs": row.end_ms,
            "content": row.content,
        }
        for row in rows
    ]


def ensure_media_evidence(
    db: Session,
    media: Media,
    progress_callback: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    segments = stored_evidence(db, media.id)
    asr_available = bool(env("ASR_BASE_URL")) or local_asr_enabled()
    has_asr = any(item["source"] == "ASR" for item in segments)
    if segments and (not asr_available or has_asr):
        return segments
    collected = collect_evidence(Path(media.file_path), progress_callback)
    replace_evidence(db, media.id, collected)
    db.flush()
    segments = stored_evidence(db, media.id)
    text_segments = [item["content"] for item in segments if item["source"] == "ASR"]
    media.transcript_text = "\n".join(text_segments) or evidence_context(segments)
    return segments


def agent_toolbox(
    db: Session,
    media: Media,
    *,
    ensure_evidence: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> AgentToolbox:
    segments = (
        ensure_media_evidence(db, media, progress_callback)
        if ensure_evidence
        else stored_evidence(db, media.id)
    )
    metadata = {
        "mediaId": media.id,
        "filename": media.filename,
        "sourceType": media.source_type,
        "sourceRef": media.source_ref,
        "status": media.status,
        "uploadedAt": media.upload_time.isoformat() if media.upload_time else None,
        "hasAnalysisReport": bool(media.ai_summary),
    }
    return AgentToolbox(
        metadata=metadata,
        segments=segments,
        normalize_report=normalize_analysis_result,
        evaluate_report=evaluate_result,
        retriever=build_runtime_retriever(metadata, segments),
    )


def _normalize_text_items(value: Any, limit: int) -> list[str]:
    if isinstance(value, str):
        candidates: list[Any] = value.splitlines() or [value]
    elif isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        candidates = []

    normalized: list[str] = []
    for item in candidates:
        text = re.sub(r"\s+", " ", str(item)).strip()
        text = re.sub(r"^(?:[-*]|\d+[.)、])\s*", "", text).strip()
        if text:
            normalized.append(text[:1000])
        if len(normalized) >= limit:
            break
    return normalized


def normalize_analysis_result(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("模型分析结果不是 JSON 对象")
    conclusions = _normalize_text_items(result.get("conclusions"), 10)
    suggestions = _normalize_text_items(result.get("suggestions"), 10)
    evidence_items = result.get("evidence") or []
    if isinstance(evidence_items, dict):
        evidence_items = [evidence_items]
    if not isinstance(evidence_items, (list, tuple)):
        evidence_items = []
    evidence = []
    for item in evidence_items[:20]:
        if not isinstance(item, dict):
            continue
        normalized = normalize_segment(item)
        if normalized:
            citation = {
                "timestampMs": normalized["startMs"],
                "source": normalized["source"],
                "content": normalized["content"],
            }
            evidence_id = str(item.get("evidenceId") or "").strip()[:64]
            if evidence_id:
                citation["evidenceId"] = evidence_id
            evidence.append(citation)
    raw_answerable = result.get("answerable")
    if isinstance(raw_answerable, bool):
        answerable = raw_answerable
    elif isinstance(raw_answerable, str):
        answerable = raw_answerable.strip().lower() not in {"false", "0", "no", "否"}
    else:
        answerable = bool(evidence)
    final_answer = re.sub(r"\s+", " ", str(result.get("finalAnswer") or "")).strip()[:2000]
    if not final_answer and conclusions:
        final_answer = conclusions[0]
    if not final_answer and not answerable:
        final_answer = "视频未明确说明该问题，无法从视频确定答案。"
    if not conclusions and final_answer:
        conclusions = [final_answer]
    return {
        "answerable": answerable,
        "finalAnswer": final_answer,
        "title": str(result.get("title") or "视频分析报告").strip()[:120],
        "conclusions": conclusions,
        "evidence": evidence,
        "suggestions": suggestions,
    }


def _content_similarity(left: str, right: str) -> float:
    left = re.sub(r"\s+", "", left.lower())
    right = re.sub(r"\s+", "", right.lower())
    if not left or not right:
        return 0.0
    if left in right or right in left:
        return 1.0
    left_pairs = {left[index : index + 2] for index in range(max(1, len(left) - 1))}
    right_pairs = {right[index : index + 2] for index in range(max(1, len(right) - 1))}
    return len(left_pairs & right_pairs) / max(1, len(left_pairs | right_pairs))


def evaluate_result(result: dict[str, Any], segments: list[dict[str, Any]]) -> dict[str, Any]:
    evidence = result.get("evidence", [])
    supported = 0
    for citation in evidence:
        for segment in segments:
            timestamp_matches = abs(int(citation.get("timestampMs", 0)) - segment["startMs"]) <= 5000
            source_matches = str(citation.get("source", "")).upper() == segment["source"]
            if timestamp_matches and source_matches and _content_similarity(str(citation.get("content", "")), segment["content"]) >= 0.2:
                supported += 1
                break
    answerable = bool(result.get("answerable", True))
    final_answer = str(result.get("finalAnswer") or "").strip()
    refusal_markers = (
        "视频未", "视频中未", "视频没有", "视频中没有", "视频并未",
        "无法从视频", "无法从当前视频", "无法根据视频", "现有视频证据不足",
        "当前视频证据不足", "未找到足够的视频证据",
    )
    external_fact_markers = ("但是", "但", "不过", "然而", "通常", "一般来说", "实际上", "是指", "定义为")
    explicit_refusal = any(marker in final_answer for marker in refusal_markers)
    refusal_only = (
        explicit_refusal
        and len(final_answer) <= 200
        and not any(marker in final_answer for marker in external_fact_markers)
    )
    support_rate = supported / len(evidence) if evidence else (1.0 if not answerable else 0.0)
    if answerable:
        structured_valid = bool(result.get("title") and final_answer and result.get("conclusions") and evidence)
        critic_passed = structured_valid and support_rate >= 0.8
    else:
        structured_valid = bool(result.get("title") and final_answer and result.get("conclusions") and refusal_only)
        critic_passed = structured_valid and not evidence
    return {
        "answerable": answerable,
        "explicitRefusal": explicit_refusal,
        "structuredValid": structured_valid,
        "evidenceSupportRate": round(support_rate, 4),
        "criticPassed": critic_passed,
        "citationCount": len(evidence),
        "supportedCitationCount": supported,
    }


def build_plan(goal: str) -> dict[str, Any]:
    return build_agent_plan(goal)


def format_timestamp(milliseconds: int) -> str:
    seconds = max(0, milliseconds) // 1000
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def format_report(result: dict[str, Any]) -> str:
    lines = [
        f"## {result.get('title', '视频分析报告')}",
        "",
        "### 最终回答",
        str(result.get("finalAnswer") or "未生成最终回答"),
        "",
        "### 核心结论",
    ]
    lines.extend(f"- {item}" for item in result.get("conclusions", []))
    lines.extend(["", "### 视频证据"])
    evidence = result.get("evidence", [])
    for item in evidence:
        timestamp = format_timestamp(int(item.get("timestampMs", 0)))
        lines.append(f"- [{timestamp}] {item.get('source', 'ASR')}：{item.get('content', '')}")
    if not evidence:
        lines.append("- 未发现可支持该问题答案的视频证据。")
    lines.extend(["", "### 行动建议"])
    lines.extend(f"- {item}" for item in result.get("suggestions", []))
    return "\n".join(lines)


executor = ThreadPoolExecutor(
    max_workers=max(1, int(env("LOCAL_EXECUTOR_WORKERS", "2"))),
    thread_name_prefix="analysis",
)
task_lock = threading.Lock()
_redis_client: Optional[redis.Redis] = None
_rocketmq_producer: Any = None
_rate_limit_lock = threading.Lock()
_local_rate_limits: dict[str, tuple[int, float]] = {}


def redis_client() -> Optional[redis.Redis]:
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        client = redis.Redis.from_url(
            env("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        )
        client.ping()
        _redis_client = client
        return client
    except redis.RedisError:
        return None


def enforce_rate_limit(key: str, limit: int, window_seconds: int) -> None:
    """优先使用 Redis 计数；Redis 不可用时退化为单进程内存计数。"""
    redis_key = f"rate:{key}"
    client = redis_client()
    if client:
        try:
            count = int(client.incr(redis_key))
            if count == 1:
                client.expire(redis_key, window_seconds)
            if count > limit:
                raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试", headers={"Retry-After": str(window_seconds)})
            return
        except redis.RedisError:
            log.warning("redis_rate_limit_unavailable key=%s", key)

    now = time.monotonic()
    with _rate_limit_lock:
        count, expires_at = _local_rate_limits.get(key, (0, now + window_seconds))
        if now >= expires_at:
            count, expires_at = 0, now + window_seconds
        count += 1
        _local_rate_limits[key] = (count, expires_at)
        if count > limit:
            retry_after = max(1, int(expires_at - now))
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试", headers={"Retry-After": str(retry_after)})


def active_task_key(media_id: int, goal: str) -> str:
    digest = hashlib.sha256(goal.encode("utf-8")).hexdigest()[:24]
    return f"analysis:active:{media_id}:{digest}"


def release_active_lock(media_id: int, goal: str, task_id: str) -> None:
    client = redis_client()
    if not client:
        return
    key = active_task_key(media_id, goal)
    try:
        if client.get(key) == task_id:
            client.delete(key)
    except redis.RedisError:
        log.warning("redis_task_lock_cleanup_failed task_id=%s", task_id)


def run_analysis_locally(task_id: str) -> None:
    while True:
        outcome = process_analysis(task_id)
        if outcome != "RETRYING":
            return
        with SessionLocal() as db:
            task = db.get(AnalysisTask, task_id)
            attempt = task.attempt_count if task else MAX_ANALYSIS_ATTEMPTS
        time.sleep(min(2 ** max(0, attempt - 1), 8))


def publish_task_message(task_id: str, task_type: str, tag: str, local_runner: Any) -> None:
    global _rocketmq_producer
    nameserver = env("ROCKETMQ_NAMESERVER")
    if not nameserver:
        executor.submit(local_runner, task_id)
        return
    try:
        from rocketmq.client import Message, Producer

        with task_lock:
            if _rocketmq_producer is None:
                producer = Producer(env("ROCKETMQ_PRODUCER_GROUP", "seeit-python-producer"))
                producer.set_name_server_address(nameserver)
                producer.start()
                _rocketmq_producer = producer
        message = Message(env("ROCKETMQ_TOPIC", "video-analysis-topic"))
        message.set_keys(task_id)
        message.set_tags(tag)
        message.set_body(json.dumps({"type": task_type, "taskId": task_id}).encode("utf-8"))
        _rocketmq_producer.send_sync(message)
    except Exception:
        log.exception("rocketmq_publish_failed task_type=%s task_id=%s; using local executor", task_type, task_id)
        executor.submit(local_runner, task_id)


def publish_analysis(task_id: str) -> None:
    """配置 RocketMQ 时投递消息，否则使用本地执行器处理任务。"""
    publish_task_message(task_id, "analysis", "analysis", run_analysis_locally)


def process_analysis(task_id: str) -> str:
    with SessionLocal() as db:
        now = datetime.now(timezone.utc)
        claimed = db.execute(
            update(AnalysisTask)
            .where(AnalysisTask.id == task_id, AnalysisTask.state.in_(["QUEUED", "RETRYING"]))
            .values(
                state="PROCESSING",
                stage="ASR",
                progress_current=0,
                progress_total=0,
                progress_message="正在准备视频证据",
                attempt_count=AnalysisTask.attempt_count + 1,
                started_at=now,
                updated_at=now,
            )
        )
        db.commit()
        if claimed.rowcount != 1:
            return "SKIPPED"
        task = db.get(AnalysisTask, task_id)
        if not task:
            return "SKIPPED"
        previous_trace: dict[str, Any] = {}
        if task.trace_json:
            try:
                parsed_trace = json.loads(task.trace_json)
                previous_trace = parsed_trace if isinstance(parsed_trace, dict) else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                previous_trace = {}
        previous_provider_requests = list(previous_trace.get("providerRequests") or [])
        analysis_provider: Provider | None = None
        toolbox: AgentToolbox | None = None
        try:
            def report_progress(stage: str, current: int, total: int, message: str) -> None:
                try:
                    persist_analysis_progress(task_id, stage, current, total, message)
                except Exception:
                    log.exception("analysis_progress_persist_failed task_id=%s stage=%s", task_id, stage)

            media = db.get(Media, task.media_id)
            if not media:
                raise RuntimeError("媒体不存在")

            planner_started = time.perf_counter()
            plan = build_plan(task.goal)
            task.plan_json = json.dumps(plan, ensure_ascii=False)
            planner_duration = int((time.perf_counter() - planner_started) * 1000)
            task.stage = "ASR"
            task.progress_current = 0
            task.progress_total = 0
            task.progress_message = "正在准备视频证据"
            task.updated_at = datetime.now(timezone.utc)
            db.commit()

            evidence_started = time.perf_counter()
            existing_segments = stored_evidence(db, media.id)
            asr_available = bool(env("ASR_BASE_URL")) or local_asr_enabled()
            evidence_reused = bool(existing_segments) and (not asr_available or any(
                item["source"] == "ASR" for item in existing_segments
            ))
            if evidence_reused:
                db.commit()
                report_progress("AGENT", 0, 0, "证据已就绪，Agent 生成中")
            toolbox = agent_toolbox(db, media, progress_callback=None if evidence_reused else report_progress)
            evidence_duration = int((time.perf_counter() - evidence_started) * 1000)
            db.commit()

            analysis_provider = provider()
            report_progress("AGENT", 0, 1, "Agent 生成中")
            analysis_started = time.perf_counter()
            generated = analysis_provider.run_agent(toolbox, task.goal)
            analysis_duration = int((time.perf_counter() - analysis_started) * 1000)
            if not generated.get("ok") or not generated.get("report"):
                raise ValueError(generated.get("error") or "Agent 未生成有效报告")
            result = generated["report"]
            evaluation = generated["evaluation"]
            evaluation_duration = toolbox.duration_for("verify_citations", "generate_report")
            current_provider_requests = [
                {**item, "attempt": task.attempt_count}
                for item in analysis_provider.provider_usage_events()
            ]
            provider_requests = [*previous_provider_requests, *current_provider_requests]
            trace = {
                "stageDurationMs": {
                    "VIDEO_CONTEXT": evidence_duration,
                    "PLANNER": planner_duration,
                    "EXECUTOR": analysis_duration,
                    "CRITIC": evaluation_duration,
                },
                "provider": analysis_provider.__class__.__name__,
                "agentMode": analysis_provider.agent_mode,
                "attempt": task.attempt_count,
                "evidenceSegmentCount": len(toolbox.segments),
                "toolCallCount": len(toolbox.trace()),
                "toolCalls": toolbox.trace(),
                "graph": generated.get("agentGraph"),
                "providerUsage": summarize_provider_usage(provider_requests),
                "providerRequests": provider_requests,
            }

            task.result = format_report(result)
            task.answerable = bool(result.get("answerable", True))
            task.final_answer = str(result.get("finalAnswer") or "").strip() or None
            task.state = "COMPLETED"
            task.stage = "COMPLETED"
            task.progress_current = 1
            task.progress_total = 1
            task.progress_message = "分析完成"
            task.error = None
            task.active_key = None
            task.trace_json = json.dumps(trace, ensure_ascii=False)
            task.evaluation_json = json.dumps(evaluation, ensure_ascii=False)
            task.updated_at = datetime.now(timezone.utc)
            task.finished_at = task.updated_at
            media.ai_summary = task.result
            media.status = "COMPLETED"
            db.commit()
            release_active_lock(task.media_id, task.goal, task.id)
            return "COMPLETED"
        except Exception as exc:
            db.rollback()
            task = db.get(AnalysisTask, task_id)
            if not task:
                return "FAILED"
            should_retry = (
                task.attempt_count < task.max_attempts
                and not isinstance(exc, AgentQualityGateError)
            )
            task.state = "RETRYING" if should_retry else "FAILED"
            task.stage = "RETRYING" if should_retry else "FAILED"
            task.progress_message = (
                f"本次执行失败，准备第 {task.attempt_count + 1} 次重试"
                if should_retry
                else "分析失败"
            )
            task.error = f"{exc.__class__.__name__}: {exc}"[:2000]
            task.updated_at = datetime.now(timezone.utc)
            current_provider_requests = [
                {**item, "attempt": task.attempt_count}
                for item in analysis_provider.provider_usage_events()
            ] if analysis_provider else []
            provider_requests = [*previous_provider_requests, *current_provider_requests]
            failure_trace = {
                "provider": analysis_provider.__class__.__name__ if analysis_provider else None,
                "agentMode": analysis_provider.agent_mode if analysis_provider else None,
                "attempt": task.attempt_count,
                "toolCallCount": len(toolbox.trace()) if toolbox else 0,
                "toolCalls": toolbox.trace() if toolbox else [],
                "providerUsage": summarize_provider_usage(provider_requests),
                "providerRequests": provider_requests,
                "lastErrorType": type(exc).__name__,
            }
            task.trace_json = json.dumps(failure_trace, ensure_ascii=False)
            if not should_retry:
                task.active_key = None
                task.finished_at = task.updated_at
            media = db.get(Media, task.media_id)
            if media:
                media.status = "PROCESSING" if should_retry else "FAILED"
            db.commit()
            if not should_retry:
                release_active_lock(task.media_id, task.goal, task.id)
            log.exception("analysis_failed task_id=%s attempt=%s", task.id, task.attempt_count)
            return task.state


def run_bilibili_import_locally(task_id: str) -> None:
    while True:
        outcome = process_bilibili_import(task_id)
        if outcome != "RETRYING":
            return
        with SessionLocal() as db:
            task = db.get(MediaImportTask, task_id)
            attempt = task.attempt_count if task else MAX_IMPORT_ATTEMPTS
        time.sleep(min(2 ** max(0, attempt - 1), 8))


def publish_bilibili_import(task_id: str) -> None:
    publish_task_message(task_id, "bilibili_import", "bilibili-import", run_bilibili_import_locally)


def process_bilibili_import(task_id: str) -> str:
    temporary_dir = UPLOAD_ROOT / "tmp" / f"bilibili-{task_id}"
    destination: Optional[Path] = None
    with SessionLocal() as db:
        now = datetime.now(timezone.utc)
        claimed = db.execute(
            update(MediaImportTask)
            .where(MediaImportTask.id == task_id, MediaImportTask.state.in_(["QUEUED", "RETRYING"]))
            .values(
                state="PROCESSING",
                attempt_count=MediaImportTask.attempt_count + 1,
                started_at=now,
                updated_at=now,
                error=None,
            )
        )
        db.commit()
        if claimed.rowcount != 1:
            return "SKIPPED"
        task = db.get(MediaImportTask, task_id)
        if not task:
            return "SKIPPED"
        user_id, bvid = task.user_id, task.bvid

    try:
        downloaded = download_bilibili_video(bvid, temporary_dir)
        if downloaded.duration_seconds > BILIBILI_MAX_DURATION_SECONDS:
            raise BilibiliImportError(f"视频时长不能超过 {BILIBILI_MAX_DURATION_SECONDS // 60} 分钟")
        if downloaded.path.stat().st_size > BILIBILI_MAX_FILE_BYTES:
            raise BilibiliImportError(f"视频文件不能超过 {BILIBILI_MAX_FILE_BYTES // (1024 * 1024)} MB")
        probe = validate_video_file(
            downloaded.path,
            max_duration_seconds=BILIBILI_MAX_DURATION_SECONDS,
        )
        content_hash = md5_file(downloaded.path)
        metadata = {
            "bvid": downloaded.bvid,
            "title": downloaded.title,
            "uploader": downloaded.uploader,
            "durationSeconds": downloaded.duration_seconds or int(probe["durationSeconds"]),
            "coverUrl": downloaded.cover_url,
        }

        with SessionLocal() as db:
            task = db.get(MediaImportTask, task_id)
            if not task or task.state != "PROCESSING":
                return "SKIPPED"
            media = db.scalar(select(Media).where(Media.user_id == user_id, Media.content_hash == content_hash))
            if media:
                downloaded.path.unlink(missing_ok=True)
            else:
                extension = downloaded.path.suffix.lower() or ".mp4"
                filename = safe_filename(f"{downloaded.title}{extension}")
                destination = media_dir() / f"{uuid.uuid4()}-{filename}"
                downloaded.path.replace(destination)
                media = Media(
                    user_id=user_id,
                    filename=filename,
                    file_path=str(destination),
                    content_hash=content_hash,
                    source_type="BILIBILI",
                    source_ref=downloaded.bvid,
                    cover_url=downloaded.cover_url or None,
                )
                db.add(media)
                db.flush()

            task.media_id = media.id
            task.title = downloaded.title
            task.metadata_json = json.dumps(metadata, ensure_ascii=False)
            task.state = "COMPLETED"
            task.active_key = None
            task.error = None
            task.updated_at = datetime.now(timezone.utc)
            task.finished_at = task.updated_at
            db.commit()
        return "COMPLETED"
    except Exception as exc:
        if destination:
            destination.unlink(missing_ok=True)
        with SessionLocal() as db:
            task = db.get(MediaImportTask, task_id)
            if not task:
                return "FAILED"
            should_retry = task.attempt_count < task.max_attempts
            task.state = "RETRYING" if should_retry else "FAILED"
            task.error = f"{exc.__class__.__name__}: {exc}"[:2000]
            task.updated_at = datetime.now(timezone.utc)
            if not should_retry:
                task.active_key = None
                task.finished_at = task.updated_at
            db.commit()
            log.exception("bilibili_import_failed task_id=%s attempt=%s", task.id, task.attempt_count)
            return task.state
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)


def recover_stale_tasks() -> None:
    threshold = datetime.now(timezone.utc) - timedelta(seconds=STALE_TASK_SECONDS)
    with SessionLocal() as db:
        stale_ids = list(db.scalars(select(AnalysisTask.id).where(
            AnalysisTask.state == "PROCESSING",
            AnalysisTask.updated_at < threshold,
            AnalysisTask.attempt_count < AnalysisTask.max_attempts,
        )).all())
        if not stale_ids:
            return
        db.execute(
            update(AnalysisTask)
            .where(AnalysisTask.id.in_(stale_ids))
            .values(state="RETRYING", error="服务重启后恢复未完成任务", updated_at=datetime.now(timezone.utc))
        )
        db.commit()
    for task_id in stale_ids:
        publish_analysis(task_id)


def stale_queued_analysis_task_ids(limit: int = 10) -> list[str]:
    """Return queued tasks old enough for the worker fallback to claim safely."""
    threshold = datetime.now(timezone.utc) - timedelta(seconds=QUEUED_TASK_FALLBACK_SECONDS)
    with SessionLocal() as db:
        return list(db.scalars(
            select(AnalysisTask.id)
            .where(
                AnalysisTask.state == "QUEUED",
                AnalysisTask.updated_at < threshold,
            )
            .order_by(AnalysisTask.updated_at.asc())
            .limit(max(1, limit))
        ).all())


def recover_stale_import_tasks() -> None:
    threshold = datetime.now(timezone.utc) - timedelta(seconds=STALE_TASK_SECONDS)
    with SessionLocal() as db:
        stale_ids = list(db.scalars(select(MediaImportTask.id).where(
            MediaImportTask.state == "PROCESSING",
            MediaImportTask.updated_at < threshold,
            MediaImportTask.attempt_count < MediaImportTask.max_attempts,
        )).all())
        if stale_ids:
            db.execute(
                update(MediaImportTask)
                .where(MediaImportTask.id.in_(stale_ids))
                .values(state="RETRYING", error="服务重启后恢复未完成导入", updated_at=datetime.now(timezone.utc))
            )
        db.execute(
            update(MediaImportTask)
            .where(
                MediaImportTask.state == "PROCESSING",
                MediaImportTask.updated_at < threshold,
                MediaImportTask.attempt_count >= MediaImportTask.max_attempts,
            )
            .values(
                state="FAILED",
                active_key=None,
                error="导入任务超过最大重试次数",
                updated_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    for task_id in stale_ids:
        publish_bilibili_import(task_id)


def cleanup_stale_uploads() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=UPLOAD_SESSION_TTL_HOURS)
    with SessionLocal() as db:
        sessions = db.scalars(select(UploadSession).where(UploadSession.created_at < cutoff)).all()
        if not sessions:
            return
        for session in sessions:
            shutil.rmtree(chunk_dir(session.id), ignore_errors=True)
            db.delete(session)
        db.commit()
        log.info("stale_upload_sessions_cleaned count=%s", len(sessions))


def validate_production_config() -> None:
    if env("APP_ENV", "development").lower() != "production":
        return
    problems = []
    if len(JWT_SECRET) < 32 or JWT_SECRET in {"development-only-change-me", "change-this-before-deploying"}:
        problems.append("JWT_SECRET 必须是至少 32 位随机值")
    if DATABASE_URL.startswith("sqlite"):
        problems.append("生产环境不能使用 SQLite")
    if env("AUTO_CREATE_SCHEMA", "true").lower() in {"1", "true", "yes"}:
        problems.append("生产环境必须关闭 AUTO_CREATE_SCHEMA 并执行 Alembic")
    if any(origin in {"*", "http://localhost:5173", "http://127.0.0.1:5173"} for origin in origins):
        problems.append("生产 CORS_ALLOWED_ORIGINS 不能使用本地地址或通配符")
    if problems:
        raise RuntimeError("生产配置不安全：" + "；".join(problems))


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_production_config()
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    if env("AUTO_CREATE_SCHEMA", "true").lower() in {"1", "true", "yes"}:
        Base.metadata.create_all(engine)
    cleanup_stale_uploads()
    recover_stale_tasks()
    recover_stale_import_tasks()
    yield


app = FastAPI(
    title="SeeIt AI API",
    version="0.5.0",
    root_path=env("ROOT_PATH", ""),
    lifespan=lifespan,
)
origins = [item.strip() for item in env("CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health() -> dict[str, str]:
    with SessionLocal() as db:
        db.execute(select(1))
    return {
        "status": "ok",
        "database": "up",
        "redis": "up" if redis_client() else "optional-unavailable",
        "version": app.version,
    }


@app.post("/user/register")
def register(request: AuthRequest, http_request: Request) -> JSONResponse:
    enforce_rate_limit(f"register:{http_request.client.host if http_request.client else 'unknown'}", 10, 60)
    username = request.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", username) or not 8 <= len(request.password) <= 128:
        return JSONResponse(status_code=400, content={"code": 400, "message": "账号或密码格式不正确"})
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.username == username)):
            return JSONResponse(status_code=409, content={"code": 409, "message": "该账号已存在"})
        user = User(username=username, password_hash=hash_password(request.password), nickname=request.nickname.strip() or f"用户{username}")
        db.add(user)
        db.commit()
        db.refresh(user)
        return {"code": 200, "message": "注册成功", "token": issue_token(user), "userInfo": {"id": user.id, "username": user.username, "nickname": user.nickname, "role": user.role}}


@app.post("/user/login")
def login(request: AuthRequest, http_request: Request) -> JSONResponse:
    enforce_rate_limit(f"login:{http_request.client.host if http_request.client else 'unknown'}", 10, 60)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == request.username.strip()))
        if not user or not verify_password(request.password, user.password_hash):
            return JSONResponse(status_code=401, content={"code": 401, "message": "账号或密码错误"})
        return {"code": 200, "message": "登录成功", "token": issue_token(user), "userInfo": {"id": user.id, "username": user.username, "nickname": user.nickname, "role": user.role}}


@app.post("/user/logout")
def logout(authorization: Optional[str] = Header(default=None), _: User = Depends(current_user)) -> dict[str, Any]:
    if authorization and authorization.lower().startswith("bearer "):
        try:
            payload = jwt.decode(authorization[7:], JWT_SECRET, algorithms=[JWT_ALGORITHM])
            expires_at = int(payload.get("exp", time.time()))
            ttl = max(1, expires_at - int(time.time()))
            client = redis_client()
            if client and payload.get("jti"):
                client.setex(f"jwt:revoked:{payload['jti']}", ttl, "1")
        except (jwt.InvalidTokenError, redis.RedisError):
            pass
    return {"code": 200, "message": "已退出登录"}


@app.get("/user/me")
def user_me(user: User = Depends(current_user)) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "nickname": user.nickname,
        "role": user.role,
    }


def serialize_import_task(task: MediaImportTask) -> dict[str, Any]:
    metadata = json.loads(task.metadata_json) if task.metadata_json else None
    messages = {
        "QUEUED": "等待导入",
        "PROCESSING": "正在下载并校验视频",
        "RETRYING": f"第 {task.attempt_count} 次导入失败，等待重试",
        "COMPLETED": "导入完成",
        "FAILED": task.error or "导入失败",
    }
    return {
        "taskId": task.id,
        "bvid": task.bvid,
        "title": task.title,
        "state": task.state,
        "attemptCount": task.attempt_count,
        "mediaId": task.media_id,
        "metadata": metadata,
        "message": messages.get(task.state, task.state),
        "createdAt": task.created_at.isoformat(),
        "updatedAt": task.updated_at.isoformat(),
    }


def ensure_bilibili_import_enabled() -> None:
    if env("BILIBILI_IMPORT_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        raise HTTPException(status_code=503, detail="B 站视频导入当前未启用")


@app.post("/media/bilibili/preview")
def preview_bilibili(request: BilibiliRequest, user: User = Depends(current_user)) -> dict[str, Any]:
    ensure_bilibili_import_enabled()
    enforce_rate_limit(f"bilibili-preview:{user.id}", 30, 3600)
    try:
        metadata = fetch_bilibili_metadata(request.bvid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BilibiliImportError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if metadata["durationSeconds"] > BILIBILI_MAX_DURATION_SECONDS:
        raise HTTPException(status_code=400, detail=f"视频时长不能超过 {BILIBILI_MAX_DURATION_SECONDS // 60} 分钟")
    return metadata


@app.post("/media/bilibili/import")
def start_bilibili_import(request: BilibiliRequest, user: User = Depends(current_user)) -> JSONResponse:
    ensure_bilibili_import_enabled()
    enforce_rate_limit(f"bilibili-import:{user.id}", 10, 3600)
    try:
        bvid = normalize_bvid(request.bvid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    active_key = f"{user.id}:{bvid}"
    task_id = str(uuid.uuid4())
    with SessionLocal() as db:
        existing_media = db.scalar(select(Media).where(
            Media.user_id == user.id,
            Media.source_type == "BILIBILI",
            Media.source_ref == bvid,
        ))
        if existing_media:
            return JSONResponse(
                status_code=200,
                content={"mediaId": existing_media.id, "message": "该 BV 视频已在媒体库中"},
            )
        completed = db.scalar(
            select(MediaImportTask)
            .where(
                MediaImportTask.user_id == user.id,
                MediaImportTask.bvid == bvid,
                MediaImportTask.state == "COMPLETED",
                MediaImportTask.media_id.is_not(None),
            )
            .order_by(MediaImportTask.created_at.desc())
        )
        if completed and db.get(Media, completed.media_id):
            return JSONResponse(
                status_code=200,
                content={"mediaId": completed.media_id, "message": "该 BV 视频已在媒体库中"},
            )
        active = db.scalar(select(MediaImportTask).where(
            MediaImportTask.user_id == user.id,
            MediaImportTask.bvid == bvid,
            MediaImportTask.state.in_(["QUEUED", "PROCESSING", "RETRYING"]),
        ))
        if active:
            return JSONResponse(status_code=409, content=serialize_import_task(active))
        task = MediaImportTask(
            id=task_id,
            user_id=user.id,
            bvid=bvid,
            active_key=active_key,
            max_attempts=MAX_IMPORT_ATTEMPTS,
        )
        db.add(task)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            active = db.scalar(select(MediaImportTask).where(MediaImportTask.active_key == active_key))
            if active:
                return JSONResponse(status_code=409, content=serialize_import_task(active))
            raise
    publish_bilibili_import(task_id)
    return JSONResponse(status_code=202, content={"taskId": task_id, "bvid": bvid, "message": "导入任务已提交"})


@app.get("/media/bilibili/import-status")
def bilibili_import_status(taskId: str, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        task = db.get(MediaImportTask, taskId)
        if not task or task.user_id != user.id:
            raise HTTPException(status_code=404, detail="导入任务不存在")
        return serialize_import_task(task)


@app.get("/media/bilibili/imports")
def bilibili_import_list(user: User = Depends(current_user)) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        tasks = db.scalars(
            select(MediaImportTask)
            .where(MediaImportTask.user_id == user.id)
            .order_by(MediaImportTask.created_at.desc())
            .limit(10)
        ).all()
        return [serialize_import_task(task) for task in tasks]


@app.post("/media/init-upload")
def init_upload(filename: str, totalChunks: int, user: User = Depends(current_user)) -> str:
    enforce_rate_limit(f"upload-init:{user.id}", 20, 3600)
    max_chunks = max(1, MAX_UPLOAD_BYTES // MAX_CHUNK_BYTES)
    if totalChunks <= 0 or totalChunks > max_chunks:
        raise HTTPException(status_code=400, detail="totalChunks 不合法")
    upload_id = str(uuid.uuid4())
    chunk_dir(upload_id).mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        db.add(UploadSession(id=upload_id, user_id=user.id, filename=safe_filename(filename), total_chunks=totalChunks))
        db.commit()
    return upload_id


@app.get("/media/upload-status")
def upload_status(uploadId: str, user: User = Depends(current_user)) -> list[int]:
    with SessionLocal() as db:
        session = db.get(UploadSession, uploadId)
        if not session or session.user_id != user.id:
            raise HTTPException(status_code=404, detail="上传任务不存在")
    result = []
    for path in chunk_dir(uploadId).glob("part-*"):
        try:
            result.append(int(path.name.split("-")[1]))
        except (IndexError, ValueError):
            continue
    return sorted(result)


@app.post("/media/upload-chunk")
async def upload_chunk(
    uploadId: Optional[str] = Form(None),
    chunkIndex: Optional[int] = Form(None),
    totalChunks: Optional[int] = Form(None),
    file: UploadFile = File(...),
    uploadIdQuery: Optional[str] = Query(default=None, alias="uploadId"),
    chunkIndexQuery: Optional[int] = Query(default=None, alias="chunkIndex"),
    totalChunksQuery: Optional[int] = Query(default=None, alias="totalChunks"),
    user: User = Depends(current_user),
) -> str:
    uploadId = uploadId or uploadIdQuery
    chunkIndex = chunkIndex if chunkIndex is not None else chunkIndexQuery
    totalChunks = totalChunks if totalChunks is not None else totalChunksQuery
    if uploadId is None or chunkIndex is None or totalChunks is None:
        raise HTTPException(status_code=400, detail="分片参数不完整")
    with SessionLocal() as db:
        session = db.get(UploadSession, uploadId)
        if not session or session.user_id != user.id:
            raise HTTPException(status_code=404, detail="上传任务不存在")
        if session.total_chunks != totalChunks or not 0 <= chunkIndex < totalChunks:
            raise HTTPException(status_code=400, detail="分片参数不合法")
    target = chunk_dir(uploadId) / f"part-{chunkIndex}"
    temporary_target = target.with_suffix(".uploading")
    size = 0
    too_large = False
    with temporary_target.open("wb") as output:
        while block := await file.read(1024 * 1024):
            size += len(block)
            if size > MAX_CHUNK_BYTES:
                too_large = True
                break
            output.write(block)
    if too_large:
        temporary_target.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail="分片超过大小限制")
    temporary_target.replace(target)
    return "分片上传成功"


@app.post("/media/complete-upload")
def complete_upload(uploadId: str, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        session = db.get(UploadSession, uploadId)
        if not session or session.user_id != user.id:
            raise HTTPException(status_code=404, detail="上传任务不存在")
        directory = chunk_dir(uploadId)
        parts = [directory / f"part-{index}" for index in range(session.total_chunks)]
        if not all(path.is_file() for path in parts):
            raise HTTPException(status_code=400, detail="还有分片未上传")
        final_path = media_dir() / f"{uuid.uuid4()}-{session.filename}"
        temporary_path = final_path.with_suffix(final_path.suffix + ".assembling")
        with temporary_path.open("wb") as output:
            for part in parts:
                with part.open("rb") as source:
                    shutil.copyfileobj(source, output)
        if env("VALIDATE_VIDEO_CONTENT", "false").lower() in {"1", "true", "yes"}:
            try:
                validate_video_file(temporary_path)
            except (subprocess.SubprocessError, OSError, ValueError, json.JSONDecodeError) as exc:
                temporary_path.unlink(missing_ok=True)
                db.delete(session)
                db.commit()
                shutil.rmtree(directory, ignore_errors=True)
                raise HTTPException(status_code=400, detail=f"视频校验失败：{exc}") from exc
        content_hash = md5_file(temporary_path)
        existing = db.scalar(select(Media).where(Media.user_id == user.id, Media.content_hash == content_hash))
        if existing:
            temporary_path.unlink(missing_ok=True)
            db.delete(session)
            db.commit()
            shutil.rmtree(directory, ignore_errors=True)
            return {"mediaId": existing.id, "deduplicated": True}
        temporary_path.replace(final_path)
        media = Media(user_id=user.id, filename=session.filename, file_path=str(final_path), content_hash=content_hash)
        db.add(media)
        db.delete(session)
        db.commit()
        db.refresh(media)
        shutil.rmtree(directory, ignore_errors=True)
    return {"mediaId": media.id, "deduplicated": False}


@app.get("/media/list")
def media_list(user: User = Depends(current_user)) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        rows = db.scalars(select(Media).where(Media.user_id == user.id).order_by(Media.id.desc())).all()
        tasks = db.scalars(
            select(AnalysisTask)
            .where(AnalysisTask.user_id == user.id)
            .order_by(AnalysisTask.created_at.desc())
        ).all()
        latest_tasks: dict[int, AnalysisTask] = {}
        for task in tasks:
            latest_tasks.setdefault(task.media_id, task)

        sessions = db.scalars(
            select(AgentSession)
            .where(AgentSession.user_id == user.id)
            .order_by(AgentSession.updated_at.desc())
        ).all()
        agent_stats: dict[int, dict[str, Any]] = {}
        for session in sessions:
            stats = agent_stats.setdefault(session.media_id, {
                "sessionCount": 0,
                "messageCount": 0,
                "lastMessage": "",
                "updatedAt": None,
            })
            stats["sessionCount"] += 1
            if stats["updatedAt"] is None:
                stats["updatedAt"] = session.updated_at.isoformat()
        messages = db.execute(
            select(AgentMessage, AgentSession.media_id)
            .join(AgentSession, AgentMessage.session_id == AgentSession.id)
            .where(AgentSession.user_id == user.id)
            .order_by(AgentMessage.id.desc())
        ).all()
        for message, media_id in messages:
            stats = agent_stats.get(media_id)
            if not stats:
                continue
            stats["messageCount"] += 1
            if not stats["lastMessage"]:
                stats["lastMessage"] = message.content[:180]

        return [{
            "id": row.id,
            "filename": row.filename,
            "status": row.status,
            "uploadTime": row.upload_time.isoformat(),
            "coverUrl": row.cover_url,
            "sourceType": row.source_type,
            "sourceRef": row.source_ref,
            "hasAnalysisReport": bool(row.ai_summary),
            "analysisState": latest_tasks[row.id].state if row.id in latest_tasks else None,
            "analysisGoal": latest_tasks[row.id].goal if row.id in latest_tasks else None,
            "agentSessionCount": agent_stats.get(row.id, {}).get("sessionCount", 0),
            "agentMessageCount": agent_stats.get(row.id, {}).get("messageCount", 0),
            "agentLastMessage": agent_stats.get(row.id, {}).get("lastMessage", ""),
            "agentUpdatedAt": agent_stats.get(row.id, {}).get("updatedAt"),
        } for row in rows]


@app.delete("/media/delete")
def delete_media(id: int, user: User = Depends(current_user)) -> str:
    with SessionLocal() as db:
        media = owned_media(db, id, user)
        task_ids = list(db.scalars(select(AnalysisTask.id).where(AnalysisTask.media_id == id)).all())
        session_ids = list(db.scalars(select(AgentSession.id).where(AgentSession.media_id == id)).all())
        if session_ids:
            db.execute(delete(AgentMessage).where(AgentMessage.session_id.in_(session_ids)))
        db.execute(delete(AgentSession).where(AgentSession.media_id == id))
        if task_ids:
            db.execute(delete(AnalysisFeedback).where(AnalysisFeedback.task_id.in_(task_ids)))
        db.execute(delete(AnalysisTask).where(AnalysisTask.media_id == id))
        db.execute(delete(EvidenceSegment).where(EvidenceSegment.media_id == id))
        db.execute(delete(MediaImportTask).where(MediaImportTask.media_id == id))
        Path(media.file_path).unlink(missing_ok=True)
        Path(media.file_path + ".txt").unlink(missing_ok=True)
        Path(media.file_path + ".segments.json").unlink(missing_ok=True)
        (UPLOAD_ROOT / "audio" / f"{media.id}.mp3").unlink(missing_ok=True)
        db.delete(media)
        db.commit()
    delete_runtime_media_index(id)
    return "删除成功"


@app.post("/analysis/ai")
def start_analysis(id: int, goal: str = Query(default="概括视频主要内容和核心观点，并引用有代表性的时间戳证据", max_length=500), user: User = Depends(current_user)) -> JSONResponse:
    enforce_rate_limit(f"analysis:{user.id}", 20, 3600)
    normalized_goal = goal.strip()
    if not normalized_goal:
        raise HTTPException(status_code=400, detail="分析目标不能为空")
    goal_hash = hashlib.sha256(normalized_goal.encode("utf-8")).hexdigest()
    task_id = str(uuid.uuid4())
    lock_key = active_task_key(id, normalized_goal)
    with SessionLocal() as db:
        media = owned_media(db, id, user)
        active = db.scalar(select(AnalysisTask).where(
            AnalysisTask.media_id == id,
            AnalysisTask.goal_hash == goal_hash,
            AnalysisTask.state.in_(["QUEUED", "PROCESSING", "RETRYING"]),
        ))
        if active:
            return JSONResponse(status_code=409, content={"taskId": active.id, "message": "相同任务正在处理中"})
        client = redis_client()
        if client:
            try:
                if not client.set(lock_key, task_id, ex=7200, nx=True):
                    return JSONResponse(status_code=409, content={"message": "相同任务正在处理中"})
            except redis.RedisError:
                log.warning("redis_task_lock_failed media_id=%s", id)
        task = AnalysisTask(
            id=task_id,
            media_id=id,
            user_id=user.id,
            goal=normalized_goal,
            goal_hash=goal_hash,
            active_key=f"{id}:{goal_hash}",
            max_attempts=MAX_ANALYSIS_ATTEMPTS,
            stage="QUEUED",
            progress_current=0,
            progress_total=0,
            progress_message="任务已排队，等待 Worker 接收",
            plan_json=json.dumps(build_plan(normalized_goal), ensure_ascii=False),
        )
        db.add(task)
        media.status = "PROCESSING"
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            release_active_lock(id, normalized_goal, task_id)
            return JSONResponse(status_code=409, content={"message": "相同任务正在处理中"})
    publish_analysis(task_id)
    return JSONResponse(status_code=202, content={"taskId": task_id, "message": "任务已提交"})


@app.get("/analysis/analysis-status")
def analysis_status(id: int, goal: str, user: User = Depends(current_user)) -> dict[str, Any]:
    normalized_goal = goal.strip()
    goal_hash = hashlib.sha256(normalized_goal.encode("utf-8")).hexdigest()
    with SessionLocal() as db:
        owned_media(db, id, user)
        task = db.scalar(select(AnalysisTask).where(
            AnalysisTask.media_id == id,
            AnalysisTask.goal_hash == goal_hash,
        ).order_by(AnalysisTask.created_at.desc()))
        if not task:
            return {"state": "NOT_STARTED", "message": "尚未提交分析任务"}
        messages = {
            "QUEUED": "任务已排队",
            "PROCESSING": "正在分析",
            "RETRYING": f"第 {task.attempt_count} 次执行失败，等待重试",
            "COMPLETED": "分析完成",
            "FAILED": task.error or "分析失败",
        }
        progress_total = max(0, int(task.progress_total or 0))
        progress_current = max(0, int(task.progress_current or 0))
        progress_percent = (
            min(100, round(progress_current * 100 / progress_total))
            if progress_total else 0
        )
        return {
            "taskId": task.id,
            "state": task.state,
            "attemptCount": task.attempt_count,
            "error": task.error,
            "result": task.result,
            "answerable": task.answerable,
            "finalAnswer": task.final_answer,
            "stage": task.stage,
            "progressCurrent": progress_current,
            "progressTotal": progress_total,
            "progressPercent": progress_percent,
            "message": task.progress_message or messages.get(task.state, task.error or task.state),
        }


@app.post("/analysis/transcribe")
def start_transcription(id: int, user: User = Depends(current_user)) -> JSONResponse:
    enforce_rate_limit(f"transcription:{user.id}", 20, 3600)
    with SessionLocal() as db:
        media = owned_media(db, id, user)
        segments = collect_evidence(Path(media.file_path))
        replace_evidence(db, media.id, segments)
        media.transcript_text = "\n".join(item["content"] for item in segments if item["source"] == "ASR") or evidence_context(segments)
        db.commit()
    return JSONResponse(status_code=200, content={"message": "文字提取完成", "segmentCount": len(segments)})


@app.get("/analysis/transcription-status")
def transcription_status(id: int, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        media = owned_media(db, id, user)
        segments = stored_evidence(db, media.id)
        return {
            "state": "COMPLETED" if media.transcript_text else "NOT_STARTED",
            "result": media.transcript_text or "",
            "segments": segments,
        }


@app.get("/agent/tools/video-metadata")
def agent_video_metadata(id: int, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        media = owned_media(db, id, user)
        toolbox = agent_toolbox(db, media, ensure_evidence=False)
        result = toolbox.execute("get_video_metadata")
        return result


@app.post("/agent/tools/search-timeline")
def agent_search_timeline(
    request: TimelineSearchRequest,
    user: User = Depends(current_user),
) -> dict[str, Any]:
    with SessionLocal() as db:
        media = owned_media(db, request.mediaId, user)
        toolbox = agent_toolbox(db, media)
        result = toolbox.execute("search_timeline", {
            "query": request.query,
            "top_k": request.topK,
            "sources": request.sources,
        })
        db.commit()
        return result


@app.post("/agent/tools/evidence-window")
def agent_evidence_window(
    request: EvidenceWindowRequest,
    user: User = Depends(current_user),
) -> dict[str, Any]:
    with SessionLocal() as db:
        media = owned_media(db, request.mediaId, user)
        toolbox = agent_toolbox(db, media)
        result = toolbox.execute("get_evidence_window", {
            "timestamp_ms": request.timestampMs,
            "before_ms": request.beforeMs,
            "after_ms": request.afterMs,
        })
        db.commit()
        return result


@app.post("/agent/tools/verify-citations")
def agent_verify_citations(
    request: CitationVerificationRequest,
    user: User = Depends(current_user),
) -> dict[str, Any]:
    with SessionLocal() as db:
        media = owned_media(db, request.mediaId, user)
        toolbox = agent_toolbox(db, media)
        result = toolbox.execute(
            "verify_citations",
            {"citations": [item.model_dump() for item in request.citations]},
        )
        db.commit()
        return result


def latest_task(db: Session, media_id: int, goal: Optional[str] = None) -> Optional[AnalysisTask]:
    statement = select(AnalysisTask).where(AnalysisTask.media_id == media_id)
    if goal is not None:
        goal_hash = hashlib.sha256(goal.strip().encode("utf-8")).hexdigest()
        statement = statement.where(AnalysisTask.goal_hash == goal_hash)
    return db.scalar(statement.order_by(AnalysisTask.created_at.desc()))


def get_or_create_agent_session(
    db: Session,
    *,
    user_id: int,
    media_id: int,
    goal: str,
) -> AgentSession:
    normalized_goal = re.sub(r"\s+", " ", goal).strip() or "基于最新报告继续追问"
    goal_hash = hashlib.sha256(normalized_goal.encode("utf-8")).hexdigest()
    session = db.scalar(select(AgentSession).where(
        AgentSession.user_id == user_id,
        AgentSession.media_id == media_id,
        AgentSession.goal_hash == goal_hash,
    ))
    if session:
        return session
    session = AgentSession(
        id=str(uuid.uuid4()),
        user_id=user_id,
        media_id=media_id,
        goal=normalized_goal,
        goal_hash=goal_hash,
    )
    db.add(session)
    db.flush()
    return session


def recent_agent_messages(db: Session, session_id: str) -> list[dict[str, str]]:
    limit = max(2, min(int(env("AGENT_MEMORY_MAX_MESSAGES", "12")), 30))
    rows = list(db.scalars(
        select(AgentMessage)
        .where(AgentMessage.session_id == session_id)
        .order_by(AgentMessage.id.desc())
        .limit(limit)
    ).all())
    rows.reverse()
    return [{"role": row.role, "content": row.content} for row in rows]


def agent_history_messages(db: Session, session_id: str) -> list[dict[str, str]]:
    limit = max(20, min(int(env("AGENT_HISTORY_MAX_MESSAGES", "200")), 500))
    rows = list(db.scalars(
        select(AgentMessage)
        .where(AgentMessage.session_id == session_id)
        .order_by(AgentMessage.id.desc())
        .limit(limit)
    ).all())
    rows.reverse()
    return [{"role": row.role, "content": row.content} for row in rows]


def update_agent_memory_summary(session: AgentSession, messages: list[dict[str, str]]) -> None:
    rendered = "\n".join(
        f"{item['role']}: {str(item['content'])[:1200]}"
        for item in messages[-12:]
    )
    session.summary = rendered[-6000:]
    session.updated_at = datetime.now(timezone.utc)


@app.post("/analysis/follow-up")
def follow_up(id: int, question: str = Query(min_length=1, max_length=500), user: User = Depends(current_user)) -> str:
    with SessionLocal() as db:
        media = owned_media(db, id, user)
        if not media.ai_summary:
            raise HTTPException(status_code=409, detail="请先完成视频分析")
        task = latest_task(db, id)
        session = get_or_create_agent_session(
            db,
            user_id=user.id,
            media_id=id,
            goal=task.goal if task else "基于最新报告继续追问",
        )
        history = recent_agent_messages(db, session.id)
        question_text = question.strip()
        toolbox = agent_toolbox(db, media, ensure_evidence=False)
        follow_up_evidence = toolbox.evidence_for_question(question_text, max_segments=18)
        answer = provider().follow_up(
            media.ai_summary,
            question_text,
            history=history,
            memory_summary=session.summary or "",
            evidence_timeline=evidence_context(follow_up_evidence),
        )
        db.add_all([
            AgentMessage(session_id=session.id, user_id=user.id, role="user", content=question_text),
            AgentMessage(session_id=session.id, user_id=user.id, role="assistant", content=answer),
        ])
        update_agent_memory_summary(session, [
            *history,
            {"role": "user", "content": question_text},
            {"role": "assistant", "content": answer},
        ])
        db.commit()
        return answer


@app.get("/analysis/agent-memory")
def agent_memory(id: int, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        owned_media(db, id, user)
        sessions = list(db.scalars(
            select(AgentSession)
            .where(AgentSession.media_id == id, AgentSession.user_id == user.id)
            .order_by(AgentSession.updated_at.desc())
        ).all())
        serialized_sessions: list[dict[str, Any]] = []
        for session in sessions:
            message_count = int(db.scalar(
                select(func.count(AgentMessage.id)).where(AgentMessage.session_id == session.id)
            ) or 0)
            messages = agent_history_messages(db, session.id)
            serialized_sessions.append({
                "sessionId": session.id,
                "goal": session.goal,
                "summary": session.summary or "",
                "messageCount": message_count,
                "messages": messages,
                "historyTruncated": message_count > len(messages),
                "createdAt": session.created_at.isoformat(),
                "updatedAt": session.updated_at.isoformat(),
            })
        latest = serialized_sessions[0] if serialized_sessions else {
            "sessionId": None,
            "goal": None,
            "summary": "",
            "messageCount": 0,
            "messages": [],
            "createdAt": None,
            "updatedAt": None,
        }
        return {
            **latest,
            "sessionCount": len(serialized_sessions),
            "sessions": serialized_sessions,
        }


def analysis_task_payload(task: AnalysisTask) -> dict[str, Any]:
    messages = {
        "QUEUED": "任务已排队",
        "PROCESSING": "正在分析",
        "RETRYING": f"第 {task.attempt_count} 次执行失败，等待重试",
        "COMPLETED": "分析完成",
        "FAILED": task.error or "分析失败",
    }
    progress_total = max(0, int(task.progress_total or 0))
    progress_current = max(0, int(task.progress_current or 0))
    progress_percent = (
        min(100, round(progress_current * 100 / progress_total))
        if progress_total else 0
    )
    return {
        "taskId": task.id,
        "mediaId": task.media_id,
        "goal": task.goal,
        "state": task.state,
        "attemptCount": task.attempt_count,
        "error": task.error,
        "stage": task.stage,
        "progressCurrent": progress_current,
        "progressTotal": progress_total,
        "progressPercent": progress_percent,
        "report": task.result,
        "answerable": task.answerable,
        "finalAnswer": task.final_answer,
        "evaluation": json.loads(task.evaluation_json) if task.evaluation_json else None,
        "trace": json.loads(task.trace_json) if task.trace_json else None,
        "message": task.progress_message or messages.get(task.state, task.error or task.state),
    }


@app.get("/analysis/task-status")
def analysis_task_status(taskId: str, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        task = db.get(AnalysisTask, taskId)
        if not task or task.user_id != user.id:
            raise HTTPException(status_code=404, detail="分析任务不存在")
        return analysis_task_payload(task)


@app.get("/analysis/report")
def analysis_report(
    id: int,
    goal: Optional[str] = Query(default=None, max_length=500),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    with SessionLocal() as db:
        media = owned_media(db, id, user)
        task = latest_task(db, id, goal)
        if task:
            return analysis_task_payload(task)
        if media.ai_summary:
            return {
                "taskId": None,
                "mediaId": media.id,
                "goal": goal,
                "state": "COMPLETED",
                "attemptCount": 0,
                "report": media.ai_summary,
                "answerable": None,
                "finalAnswer": None,
                "evaluation": None,
                "trace": None,
                "message": "分析完成",
            }
        raise HTTPException(status_code=404, detail="尚无分析报告")


@app.get("/analysis/agent-plan")
def agent_plan(id: int, goal: str, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        owned_media(db, id, user)
        task = latest_task(db, id, goal)
        if task and task.plan_json:
            return json.loads(task.plan_json)
    return build_plan(goal.strip())


@app.get("/analysis/agent-trace")
def agent_trace(id: int, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        owned_media(db, id, user)
        task = latest_task(db, id)
        if task and task.trace_json:
            return json.loads(task.trace_json)
    return {"stageDurationMs": {}, "provider": None, "attempt": 0, "evidenceSegmentCount": 0}


@app.get("/analysis/agent-evaluation")
def agent_evaluation(id: int, goal: str, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        owned_media(db, id, user)
        task = latest_task(db, id, goal)
        if task and task.evaluation_json:
            return json.loads(task.evaluation_json)
    return {"structuredValid": False, "evidenceSupportRate": 0.0, "criticPassed": False, "citationCount": 0, "supportedCitationCount": 0}


@app.post("/analysis/agent-feedback")
def agent_feedback(request: FeedbackRequest, user: User = Depends(current_user)) -> dict[str, str]:
    with SessionLocal() as db:
        owned_media(db, request.mediaId, user)
        task = latest_task(db, request.mediaId, request.goal)
        if not task or task.state != "COMPLETED":
            raise HTTPException(status_code=409, detail="请先完成视频分析")
        feedback = db.scalar(select(AnalysisFeedback).where(
            AnalysisFeedback.task_id == task.id,
            AnalysisFeedback.user_id == user.id,
        ))
        if feedback:
            feedback.rating = request.rating
            feedback.comment = request.comment
        else:
            db.add(AnalysisFeedback(
                task_id=task.id,
                user_id=user.id,
                rating=request.rating,
                comment=request.comment,
            ))
        db.commit()
    return {"message": "反馈已接收"}


@app.get("/analysis/download")
def download(id: int, user: User = Depends(current_user)) -> FileResponse:
    with SessionLocal() as db:
        media = owned_media(db, id, user)
        path = Path(media.file_path)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        audio_path = UPLOAD_ROOT / "audio" / f"{media.id}.mp3"
        if not audio_path.is_file():
            try:
                extract_audio_file(path, audio_path)
            except (subprocess.SubprocessError, OSError) as exc:
                raise HTTPException(status_code=500, detail="音频提取失败，请确认视频格式与 FFmpeg 环境") from exc
        return FileResponse(audio_path, filename=f"{Path(media.filename).stem}.mp3", media_type="audio/mpeg")
