from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import jwt
import redis
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, create_engine, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

load_dotenv()


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


DATABASE_URL = env("DATABASE_URL", "sqlite:///./seeit.db")
UPLOAD_ROOT = Path(env("UPLOAD_ROOT", "./data/uploads")).resolve()
MAX_CHUNK_BYTES = int(env("MAX_CHUNK_BYTES", str(5 * 1024 * 1024)))
MAX_UPLOAD_BYTES = int(env("MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))
MAX_ANALYSIS_ATTEMPTS = int(env("MAX_ANALYSIS_ATTEMPTS", "3"))
STALE_TASK_SECONDS = int(env("STALE_TASK_SECONDS", "1800"))
UPLOAD_SESSION_TTL_HOURS = int(env("UPLOAD_SESSION_TTL_HOURS", "24"))
OCR_INTERVAL_SECONDS = max(1, int(env("OCR_INTERVAL_SECONDS", "10")))
JWT_SECRET = env("JWT_SECRET", "development-only-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(env("JWT_EXPIRE_HOURS", "24"))
log = logging.getLogger("seeit")

logging.basicConfig(
    level=getattr(logging, env("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


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
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=MAX_ANALYSIS_ATTEMPTS)
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    plan_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trace_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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


class AuthRequest(BaseModel):
    username: str
    password: str
    nickname: str = ""


class AnalysisRequest(BaseModel):
    goal: str = Field(default="理解视频核心内容并生成结构化分析报告", max_length=500)


class FeedbackRequest(BaseModel):
    mediaId: int
    goal: str = Field(min_length=1, max_length=500)
    rating: Optional[int] = Field(default=None, ge=-1, le=1)
    comment: Optional[str] = Field(default=None, max_length=1000)


class Provider:
    def analyze(self, transcript: str, goal: str) -> dict[str, Any]:
        raise NotImplementedError

    def follow_up(self, report: str, question: str) -> str:
        raise NotImplementedError


class MockProvider(Provider):
    def analyze(self, transcript: str, goal: str) -> dict[str, Any]:
        text = transcript.strip() or "未检测到可用转写内容。"
        first_line = next((line for line in text.splitlines() if line.strip()), text)
        match = re.match(r"\[(\d+)-(\d+)ms]\[([^]]+)]\s*(.*)", first_line)
        timestamp_ms = int(match.group(1)) if match else 0
        source = match.group(3) if match else "ASR"
        evidence = (match.group(4) if match else first_line)[:300]
        return {
            "title": "视频内容分析报告",
            "conclusions": [f"围绕“{goal}”提取到可核验的视频内容。", evidence],
            "evidence": [{"timestampMs": timestamp_ms, "source": source, "content": evidence}],
            "suggestions": ["补充更具体的分析目标，以获得更精确的证据检索结果。"],
        }

    def follow_up(self, report: str, question: str) -> str:
        return f"## 追问结果\n\n问题：{question}\n\n基于当前报告：\n\n{report[:3000]}"


class OpenAICompatibleProvider(Provider):
    def __init__(self, base_url: str, api_key: str, model: str):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model

    def _chat(self, prompt: str) -> str:
        response = httpx.post(
            self.url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "temperature": 0.1, "messages": [{"role": "user", "content": prompt}]},
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    def analyze(self, transcript: str, goal: str) -> dict[str, Any]:
        prompt = (
            "只返回 JSON，字段为 title、conclusions、evidence、suggestions。"
            "evidence 必须包含 timestampMs、source、content。"
            "只能引用证据时间轴中存在的内容和时间戳，不得编造。"
            f"\n目标：{goal}\n证据时间轴：\n{transcript[:16000]}"
        )
        raw = self._chat(prompt).replace("```json", "").replace("```", "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型未返回合法 JSON 对象")
        return json.loads(raw[start : end + 1])

    def follow_up(self, report: str, question: str) -> str:
        return self._chat(f"根据报告回答问题，不要编造事实。\n报告：{report}\n问题：{question}")


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


def validate_video_file(path: Path) -> dict[str, Any]:
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
    max_duration = int(env("MAX_VIDEO_DURATION_SECONDS", "600"))
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
    if not asr_base_url or not asr_api_key:
        return []
    with audio_path.open("rb") as audio:
        response = httpx.post(
            asr_base_url.rstrip("/") + "/audio/transcriptions",
            headers={"Authorization": f"Bearer {asr_api_key}"},
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


def extract_ocr_evidence(path: Path) -> list[dict[str, Any]]:
    if env("OCR_ENABLED", "false").lower() not in {"1", "true", "yes"}:
        return []
    temp_root = UPLOAD_ROOT / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ocr-", dir=temp_root) as directory:
        frame_pattern = str(Path(directory) / "frame-%06d.jpg")
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-vf", f"fps=1/{OCR_INTERVAL_SECONDS}", "-q:v", "3", frame_pattern],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=900,
        )
        segments = []
        for index, frame in enumerate(sorted(Path(directory).glob("frame-*.jpg"))):
            completed = subprocess.run(
                ["tesseract", str(frame), "stdout", "-l", env("OCR_LANG", "chi_sim+eng")],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=60,
            )
            content = re.sub(r"\s+", " ", completed.stdout).strip()
            if content:
                start_ms = index * OCR_INTERVAL_SECONDS * 1000
                segments.append({
                    "source": "OCR",
                    "startMs": start_ms,
                    "endMs": start_ms + OCR_INTERVAL_SECONDS * 1000,
                    "content": content,
                })
        return segments


def collect_evidence(path: Path) -> list[dict[str, Any]]:
    segments = read_sidecar_evidence(path)
    if not segments and env("ASR_BASE_URL") and env("ASR_API_KEY"):
        temp_dir = UPLOAD_ROOT / "tmp"
        audio_path = temp_dir / f"{uuid.uuid4()}.mp3"
        try:
            extract_audio_file(path, audio_path)
            segments = request_asr(audio_path)
        finally:
            audio_path.unlink(missing_ok=True)
    segments.extend(extract_ocr_evidence(path))
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
        {"source": row.source, "startMs": row.start_ms, "endMs": row.end_ms, "content": row.content}
        for row in rows
    ]


def normalize_analysis_result(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("模型分析结果不是 JSON 对象")
    conclusions = [str(item).strip() for item in result.get("conclusions", []) if str(item).strip()][:10]
    suggestions = [str(item).strip() for item in result.get("suggestions", []) if str(item).strip()][:10]
    evidence = []
    for item in result.get("evidence", [])[:20]:
        if not isinstance(item, dict):
            continue
        normalized = normalize_segment(item)
        if normalized:
            evidence.append({
                "timestampMs": normalized["startMs"],
                "source": normalized["source"],
                "content": normalized["content"],
            })
    return {
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
    support_rate = supported / len(evidence) if evidence else 0.0
    structured_valid = bool(result.get("title") and result.get("conclusions") and evidence)
    return {
        "structuredValid": structured_valid,
        "evidenceSupportRate": round(support_rate, 4),
        "criticPassed": structured_valid and support_rate >= 0.8,
        "citationCount": len(evidence),
        "supportedCitationCount": supported,
    }


def build_plan(goal: str) -> dict[str, Any]:
    return {
        "understoodGoal": goal,
        "tasks": ["提取 ASR/OCR 时间轴证据", "按目标组织可核验结论", "校验证据引用与结构完整性"],
    }


def format_timestamp(milliseconds: int) -> str:
    seconds = max(0, milliseconds) // 1000
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def format_report(result: dict[str, Any]) -> str:
    lines = [f"## {result.get('title', '视频分析报告')}", "", "### 核心结论"]
    lines.extend(f"- {item}" for item in result.get("conclusions", []))
    lines.extend(["", "### 视频证据"])
    for item in result.get("evidence", []):
        timestamp = format_timestamp(int(item.get("timestampMs", 0)))
        lines.append(f"- [{timestamp}] {item.get('source', 'ASR')}：{item.get('content', '')}")
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


def publish_analysis(task_id: str) -> None:
    """配置 RocketMQ 时投递消息，否则使用本地执行器处理任务。"""
    global _rocketmq_producer
    nameserver = env("ROCKETMQ_NAMESERVER")
    if not nameserver:
        executor.submit(run_analysis_locally, task_id)
        return
    try:
        from rocketmq.client import Message, Producer

        with task_lock:
            if _rocketmq_producer is None:
                producer = Producer(env("ROCKETMQ_PRODUCER_GROUP", "seeit-python-producer"))
                producer.set_namesrv_addr(nameserver)
                producer.start()
                _rocketmq_producer = producer
        message = Message(env("ROCKETMQ_TOPIC", "video-analysis-topic"))
        message.set_keys(task_id)
        message.set_tags("analysis")
        message.set_body(json.dumps({"taskId": task_id}).encode("utf-8"))
        _rocketmq_producer.send_sync(message)
    except Exception:
        log.exception("rocketmq_publish_failed task_id=%s; using local executor", task_id)
        executor.submit(run_analysis_locally, task_id)


def process_analysis(task_id: str) -> str:
    with SessionLocal() as db:
        now = datetime.now(timezone.utc)
        claimed = db.execute(
            update(AnalysisTask)
            .where(AnalysisTask.id == task_id, AnalysisTask.state.in_(["QUEUED", "RETRYING"]))
            .values(
                state="PROCESSING",
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
        try:
            media = db.get(Media, task.media_id)
            if not media:
                raise RuntimeError("媒体不存在")

            evidence_started = time.perf_counter()
            segments = stored_evidence(db, media.id)
            if not segments:
                segments = collect_evidence(Path(media.file_path))
                replace_evidence(db, media.id, segments)
            text_segments = [item["content"] for item in segments if item["source"] == "ASR"]
            media.transcript_text = "\n".join(text_segments) or evidence_context(segments)
            evidence_duration = int((time.perf_counter() - evidence_started) * 1000)

            analysis_provider = provider()
            analysis_started = time.perf_counter()
            result = normalize_analysis_result(analysis_provider.analyze(evidence_context(segments), task.goal))
            analysis_duration = int((time.perf_counter() - analysis_started) * 1000)

            evaluation_started = time.perf_counter()
            evaluation = evaluate_result(result, segments)
            evaluation_duration = int((time.perf_counter() - evaluation_started) * 1000)
            trace = {
                "stageDurationMs": {
                    "VIDEO_CONTEXT": evidence_duration,
                    "PLANNER": 0,
                    "EXECUTOR": analysis_duration,
                    "CRITIC": evaluation_duration,
                },
                "provider": analysis_provider.__class__.__name__,
                "attempt": task.attempt_count,
                "evidenceSegmentCount": len(segments),
            }

            task.result = format_report(result)
            task.state = "COMPLETED"
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
            should_retry = task.attempt_count < task.max_attempts
            task.state = "RETRYING" if should_retry else "FAILED"
            task.error = f"{exc.__class__.__name__}: {exc}"[:2000]
            task.updated_at = datetime.now(timezone.utc)
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
    yield


app = FastAPI(
    title="SeeIt AI API",
    version="0.3.0",
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
        return [{"id": row.id, "filename": row.filename, "status": row.status, "uploadTime": row.upload_time.isoformat(), "coverUrl": None} for row in rows]


@app.delete("/media/delete")
def delete_media(id: int, user: User = Depends(current_user)) -> str:
    with SessionLocal() as db:
        media = owned_media(db, id, user)
        task_ids = list(db.scalars(select(AnalysisTask.id).where(AnalysisTask.media_id == id)).all())
        if task_ids:
            db.execute(delete(AnalysisFeedback).where(AnalysisFeedback.task_id.in_(task_ids)))
        db.execute(delete(AnalysisTask).where(AnalysisTask.media_id == id))
        db.execute(delete(EvidenceSegment).where(EvidenceSegment.media_id == id))
        Path(media.file_path).unlink(missing_ok=True)
        Path(media.file_path + ".txt").unlink(missing_ok=True)
        Path(media.file_path + ".segments.json").unlink(missing_ok=True)
        (UPLOAD_ROOT / "audio" / f"{media.id}.mp3").unlink(missing_ok=True)
        db.delete(media)
        db.commit()
    return "删除成功"


@app.post("/analysis/ai")
def start_analysis(id: int, goal: str = Query(default="理解视频核心内容并生成结构化分析报告", max_length=500), user: User = Depends(current_user)) -> JSONResponse:
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
        return {
            "taskId": task.id,
            "state": task.state,
            "attemptCount": task.attempt_count,
            "result": task.result,
            "message": messages.get(task.state, task.error or task.state),
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


@app.post("/analysis/follow-up")
def follow_up(id: int, question: str = Query(min_length=1, max_length=500), user: User = Depends(current_user)) -> str:
    with SessionLocal() as db:
        media = owned_media(db, id, user)
        if not media.ai_summary:
            raise HTTPException(status_code=409, detail="请先完成视频分析")
        return provider().follow_up(media.ai_summary, question.strip())


def latest_task(db: Session, media_id: int, goal: Optional[str] = None) -> Optional[AnalysisTask]:
    statement = select(AnalysisTask).where(AnalysisTask.media_id == media_id)
    if goal is not None:
        goal_hash = hashlib.sha256(goal.strip().encode("utf-8")).hexdigest()
        statement = statement.where(AnalysisTask.goal_hash == goal_hash)
    return db.scalar(statement.order_by(AnalysisTask.created_at.desc()))


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
