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
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

load_dotenv()


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


DATABASE_URL = env("DATABASE_URL", "sqlite:///./seeit.db")
UPLOAD_ROOT = Path(env("UPLOAD_ROOT", "./data/uploads")).resolve()
MAX_CHUNK_BYTES = int(env("MAX_CHUNK_BYTES", str(5 * 1024 * 1024)))
JWT_SECRET = env("JWT_SECRET", "development-only-change-me")
JWT_ALGORITHM = "HS256"
log = logging.getLogger("seeit")

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
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media_files.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    goal: Mapped[str] = mapped_column(String(500))
    state: Mapped[str] = mapped_column(String(20), default="QUEUED")
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class AuthRequest(BaseModel):
    username: str
    password: str
    nickname: str = ""


class AnalysisRequest(BaseModel):
    goal: str = Field(default="理解视频核心内容并生成结构化分析报告", max_length=500)


class FeedbackRequest(BaseModel):
    mediaId: int
    goal: str
    rating: Optional[int] = None
    comment: Optional[str] = None


class Provider:
    def analyze(self, transcript: str, goal: str) -> dict[str, Any]:
        raise NotImplementedError

    def follow_up(self, report: str, question: str) -> str:
        raise NotImplementedError


class MockProvider(Provider):
    def analyze(self, transcript: str, goal: str) -> dict[str, Any]:
        text = transcript.strip() or "未检测到可用转写内容。"
        evidence = text[:180]
        return {
            "title": "视频内容分析报告",
            "conclusions": [f"围绕“{goal}”提取到一条可核验内容。", text[:300]],
            "evidence": [{"timestampMs": 0, "source": "ASR", "content": evidence}],
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
            f"\n目标：{goal}\n转写：{transcript[:12000]}"
        )
        raw = self._chat(prompt).replace("```json", "").replace("```", "").strip()
        return json.loads(raw[raw.find("{") : raw.rfind("}") + 1])

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
    payload = {"sub": str(user.id), "role": user.role, "exp": datetime.now(timezone.utc) + timedelta(days=7)}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def current_user(authorization: Optional[str] = Header(default=None)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(authorization[7:], JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = int(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
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


def transcribe(path: Path) -> str:
    sidecar = path.with_suffix(path.suffix + ".txt")
    if sidecar.exists():
        return sidecar.read_text(encoding="utf-8")
    asr_base_url = env("ASR_BASE_URL")
    asr_api_key = env("ASR_API_KEY")
    if asr_base_url and asr_api_key:
        temp_dir = UPLOAD_ROOT / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        audio_path = temp_dir / f"{uuid.uuid4()}.mp3"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(audio_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=900,
            )
            with audio_path.open("rb") as audio:
                response = httpx.post(
                    asr_base_url.rstrip("/") + "/audio/transcriptions",
                    headers={"Authorization": f"Bearer {asr_api_key}"},
                    data={"model": env("ASR_MODEL", "TeleAI/TeleSpeechASR")},
                    files={"file": (audio_path.name, audio, "audio/mpeg")},
                    timeout=180,
                )
            response.raise_for_status()
            text = response.json().get("text", "").strip()
            if text:
                return text
        finally:
            audio_path.unlink(missing_ok=True)
    return f"文件 {path.name} 已上传。请配置 ASR 服务，或在同目录提供 .txt 转写文件以生成真实内容。"


def format_report(result: dict[str, Any]) -> str:
    lines = [f"## {result.get('title', '视频分析报告')}", "", "### 核心结论"]
    lines.extend(f"- {item}" for item in result.get("conclusions", []))
    lines.extend(["", "### 证据"])
    for item in result.get("evidence", []):
        lines.append(f"- [{item.get('timestampMs', 0)}ms] {item.get('source', 'ASR')}: {item.get('content', '')}")
    lines.extend(["", "### 建议"])
    lines.extend(f"- {item}" for item in result.get("suggestions", []))
    return "\n".join(lines)


executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="analysis")
task_lock = threading.Lock()
_redis_client: Optional[redis.Redis] = None
_rocketmq_producer: Any = None


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


def active_task_key(media_id: int, goal: str) -> str:
    digest = hashlib.sha256(goal.encode("utf-8")).hexdigest()[:24]
    return f"analysis:active:{media_id}:{digest}"


def publish_analysis(task_id: str) -> None:
    """配置 RocketMQ 时投递消息，否则使用本地执行器处理任务。"""
    global _rocketmq_producer
    nameserver = env("ROCKETMQ_NAMESERVER")
    if not nameserver:
        executor.submit(process_analysis, task_id)
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
        executor.submit(process_analysis, task_id)


def process_analysis(task_id: str) -> None:
    with SessionLocal() as db:
        task = db.get(AnalysisTask, task_id)
        if not task:
            return
        task.state = "PROCESSING"
        task.updated_at = datetime.now(timezone.utc)
        db.commit()
        try:
            media = db.get(Media, task.media_id)
            if not media:
                raise RuntimeError("媒体不存在")
            text = media.transcript_text or transcribe(Path(media.file_path))
            media.transcript_text = text
            result = provider().analyze(text, task.goal)
            task.result = format_report(result)
            task.state = "COMPLETED"
            task.error = None
            media.ai_summary = task.result
            media.status = "COMPLETED"
            db.commit()
        except Exception as exc:
            task.state = "FAILED"
            task.error = str(exc)
            media = db.get(Media, task.media_id)
            if media:
                media.status = "COMPLETED"
            task.updated_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            client = redis_client()
            if client:
                try:
                    client.delete(active_task_key(task.media_id, task.goal))
                except redis.RedisError:
                    log.warning("redis_task_lock_cleanup_failed task_id=%s", task.id)


@asynccontextmanager
async def lifespan(_: FastAPI):
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="SeeIt AI API", version="0.1.0", lifespan=lifespan)
origins = [item.strip() for item in env("CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "redis": "up" if redis_client() else "optional-unavailable"}


@app.post("/user/register")
def register(request: AuthRequest) -> JSONResponse:
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
def login(request: AuthRequest) -> JSONResponse:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == request.username.strip()))
        if not user or not verify_password(request.password, user.password_hash):
            return JSONResponse(status_code=401, content={"code": 401, "message": "账号或密码错误"})
        return {"code": 200, "message": "登录成功", "token": issue_token(user), "userInfo": {"id": user.id, "username": user.username, "nickname": user.nickname, "role": user.role}}


@app.post("/user/logout")
def logout(_: User = Depends(current_user)) -> dict[str, Any]:
    return {"code": 200, "message": "已退出登录"}


@app.post("/media/init-upload")
def init_upload(filename: str, totalChunks: int, user: User = Depends(current_user)) -> str:
    if totalChunks <= 0 or totalChunks > 10000:
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
async def upload_chunk(uploadId: str, chunkIndex: int, totalChunks: int, file: UploadFile = File(...), user: User = Depends(current_user)) -> str:
    with SessionLocal() as db:
        session = db.get(UploadSession, uploadId)
        if not session or session.user_id != user.id:
            raise HTTPException(status_code=404, detail="上传任务不存在")
        if session.total_chunks != totalChunks or not 0 <= chunkIndex < totalChunks:
            raise HTTPException(status_code=400, detail="分片参数不合法")
    target = chunk_dir(uploadId) / f"part-{chunkIndex}"
    size = 0
    with target.open("wb") as output:
        while block := await file.read(1024 * 1024):
            size += len(block)
            if size > MAX_CHUNK_BYTES:
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="分片超过 5MB")
            output.write(block)
    return "Chunk uploaded"


@app.post("/media/complete-upload")
def complete_upload(uploadId: str, user: User = Depends(current_user)) -> str:
    with SessionLocal() as db:
        session = db.get(UploadSession, uploadId)
        if not session or session.user_id != user.id:
            raise HTTPException(status_code=404, detail="上传任务不存在")
        directory = chunk_dir(uploadId)
        parts = [directory / f"part-{index}" for index in range(session.total_chunks)]
        if not all(path.is_file() for path in parts):
            raise HTTPException(status_code=400, detail="还有分片未上传")
        final_path = media_dir() / f"{uuid.uuid4()}-{session.filename}"
        with final_path.open("wb") as output:
            for part in parts:
                with part.open("rb") as source:
                    shutil.copyfileobj(source, output)
        media = Media(user_id=user.id, filename=session.filename, file_path=str(final_path), content_hash=md5_file(final_path))
        db.add(media)
        db.delete(session)
        db.commit()
        shutil.rmtree(directory, ignore_errors=True)
    return "Upload success"


@app.post("/media/upload-url")
def upload_url(url: str, user: User = Depends(current_user)) -> str:
    raise HTTPException(status_code=501, detail="URL 下载将在下一阶段接入 yt-dlp，请先上传本地视频")


@app.get("/media/list")
def media_list(user: User = Depends(current_user)) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        rows = db.scalars(select(Media).where(Media.user_id == user.id).order_by(Media.id.desc())).all()
        return [{"id": row.id, "filename": row.filename, "status": row.status, "uploadTime": row.upload_time.isoformat(), "coverUrl": None} for row in rows]


@app.delete("/media/delete")
def delete_media(id: int, user: User = Depends(current_user)) -> str:
    with SessionLocal() as db:
        media = owned_media(db, id, user)
        Path(media.file_path).unlink(missing_ok=True)
        db.delete(media)
        db.commit()
    return "删除成功"


@app.post("/analysis/ai")
def start_analysis(id: int, goal: str = Query(default="理解视频核心内容并生成结构化分析报告", max_length=500), user: User = Depends(current_user)) -> JSONResponse:
    with SessionLocal() as db:
        media = owned_media(db, id, user)
        active = db.scalar(select(AnalysisTask).where(AnalysisTask.media_id == id, AnalysisTask.goal == goal, AnalysisTask.state.in_(["QUEUED", "PROCESSING"])))
        if active:
            return JSONResponse(status_code=409, content={"message": "相同任务正在处理中"})
        client = redis_client()
        lock_key = active_task_key(id, goal)
        if client:
            try:
                if not client.set(lock_key, str(user.id), ex=7200, nx=True):
                    return JSONResponse(status_code=409, content={"message": "相同任务正在处理中"})
            except redis.RedisError:
                log.warning("redis_task_lock_failed media_id=%s", id)
        task = AnalysisTask(id=str(uuid.uuid4()), media_id=id, user_id=user.id, goal=goal.strip())
        db.add(task)
        media.status = "PROCESSING"
        db.commit()
        publish_analysis(task.id)
    return JSONResponse(status_code=202, content={"taskId": task.id, "message": "任务已提交"})


@app.get("/analysis/analysis-status")
def analysis_status(id: int, goal: str, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        owned_media(db, id, user)
        task = db.scalar(select(AnalysisTask).where(AnalysisTask.media_id == id, AnalysisTask.goal == goal).order_by(AnalysisTask.created_at.desc()))
        if not task:
            return {"state": "NOT_STARTED", "message": "尚未提交分析任务"}
        return {"state": task.state, "result": task.result, "message": task.error or ("正在分析" if task.state == "PROCESSING" else "任务已排队")}


@app.post("/analysis/transcribe")
def start_transcription(id: int, user: User = Depends(current_user)) -> JSONResponse:
    with SessionLocal() as db:
        media = owned_media(db, id, user)
        media.transcript_text = transcribe(Path(media.file_path))
        db.commit()
    return JSONResponse(status_code=202, content={"message": "提取任务已提交"})


@app.get("/analysis/transcription-status")
def transcription_status(id: int, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        media = owned_media(db, id, user)
        return {"state": "COMPLETED" if media.transcript_text else "NOT_STARTED", "result": media.transcript_text or ""}


@app.post("/analysis/follow-up")
def follow_up(id: int, question: str, user: User = Depends(current_user)) -> str:
    with SessionLocal() as db:
        media = owned_media(db, id, user)
        if not media.ai_summary:
            raise HTTPException(status_code=409, detail="请先完成视频分析")
        return provider().follow_up(media.ai_summary, question.strip())


@app.get("/analysis/agent-plan")
def agent_plan(id: int, goal: str, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        owned_media(db, id, user)
    return {"understoodGoal": goal, "tasks": ["提取视频内容", "检索相关证据", "生成并校验结构化报告"]}


@app.get("/analysis/agent-trace")
def agent_trace(id: int, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        owned_media(db, id, user)
    return {"stages": [["UPLOAD", 0], ["ANALYSIS", 0]], "provider": provider().__class__.__name__}


@app.get("/analysis/agent-evaluation")
def agent_evaluation(id: int, goal: str, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        owned_media(db, id, user)
    return {"goal": goal, "evidenceCoverage": 1.0, "criticPassed": True, "notes": "基础评测占位，后续应接入离线样本集。"}


@app.post("/analysis/agent-feedback")
def agent_feedback(request: FeedbackRequest, user: User = Depends(current_user)) -> dict[str, str]:
    with SessionLocal() as db:
        owned_media(db, request.mediaId, user)
    return {"message": "反馈已接收"}


@app.get("/analysis/download")
def download(id: int, user: User = Depends(current_user)) -> FileResponse:
    with SessionLocal() as db:
        media = owned_media(db, id, user)
        path = Path(media.file_path)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        return FileResponse(path, filename=media.filename, media_type="application/octet-stream")
