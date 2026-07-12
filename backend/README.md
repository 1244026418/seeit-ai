# SeeIt AI backend

This directory contains the focused Python implementation of the SeeIt AI workflow. It keeps the HTTP contract used by the Vue client while making the core path easy to run and explain:

`upload -> MySQL/SQLite -> background task -> FFmpeg/ASR -> evidence report -> status polling`

The Python backend, task model, and provider layer are implemented as the SeeIt AI rewrite. The repository root contains the applicable license notice.

## Run locally

Python 3.11+ is recommended. The current machine's Python 3.8 is too old for the pinned FastAPI toolchain.

```bash
cd backend
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env  # Windows
uvicorn seeit.main:app --reload --port 9090
```

The RocketMQ Python client depends on native libraries and is intended to run in Docker/Linux for this project. Install `requirements-rocketmq.txt` when running the API and worker with RocketMQ.

The default development database is SQLite and the default AI provider is a deterministic local mock. Set `DATABASE_URL`, `AI_BASE_URL`, and `AI_API_KEY` in `.env` to use MySQL and an OpenAI-compatible provider.

The Vue client can keep using `http://localhost:9090` as its API base URL.

## Interview-level features

- FastAPI REST API with JWT authentication and per-user ownership checks.
- Resumable chunk upload with Redis-compatible metadata semantics and MD5 deduplication.
- MySQL/SQLite persistence through SQLAlchemy models.
- Background analysis jobs with durable task status; the message payload is designed for a RocketMQ producer/consumer adapter.
- FFmpeg audio extraction and optional Tesseract OCR integration.
- OpenAI-compatible model provider plus deterministic mock provider for offline demos.
- Structured Markdown reports with timestamped evidence, plan, trace, and evaluation endpoints.

The implementation intentionally avoids claiming production throughput. Add tests, metrics, migrations, and a real RocketMQ worker before presenting those as production capabilities.

## Improvement order

1. Add Alembic migrations and explicit MySQL indexes.
2. Store completed media and evidence frames in MinIO instead of the shared Docker volume.
3. Split ASR by time window and persist real timestamped segments.
4. Add OCR key-frame extraction and merge ASR/OCR evidence by timestamp.
5. Add RocketMQ retry, dead-letter handling, and consumer idempotency tests.
6. Build an offline evaluation set for evidence coverage and hallucination rate.
7. Add API integration tests for authorization, duplicate chunks, retries, and task recovery.
