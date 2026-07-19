# SeeIt AI 后端

本目录是 SeeIt AI 的 Python/FastAPI 后端，负责用户认证、本地视频上传、BV 号导入、异步分析任务和证据报告生成。核心流程如下：

`本地上传/BV 导入 -> MySQL/SQLite 持久化 -> RocketMQ Worker -> FFmpeg/ASR/PaddleOCR -> LangGraph Agent -> Evidence RAG -> Critic -> 证据报告`

## 本地启动

推荐使用 Python 3.11 或更高版本。

```bash
cd backend
python -m venv .venv
# Windows 激活虚拟环境
.venv\Scripts\activate
# Linux/macOS 激活虚拟环境
source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env
python -m alembic upgrade head
uvicorn seeit.main:app --reload --port 9090
```

服务启动后可访问：

- API 地址：`http://localhost:9090`
- OpenAPI 文档：`http://localhost:9090/docs`
- MCP 地址：`http://localhost:8001/mcp`（Docker）

默认开发数据库为 SQLite，默认 AI Provider 为结果稳定的本地抽取式 Mock。Docker Compose 的 Worker 默认启用 `faster-whisper base/int8` 与 PaddleOCR，直接运行 Python 时可通过 `LOCAL_ASR_ENABLED=true` 和 `OCR_ENABLED=true` 开启；模型缓存目录由 `LOCAL_ASR_MODEL_ROOT` 与 `PADDLEOCR_MODEL_ROOT` 控制。DeepSeek 默认配置为 `https://api.deepseek.com` 和 `deepseek-v4-flash`，手动填写新生成的 `AI_API_KEY` 后启用真实 Provider。

RocketMQ Python 客户端依赖本地动态库，本项目建议通过 Docker/Linux 运行 RocketMQ 模式。需要单独运行 API 和 Worker 时，还需安装 `requirements-rocketmq.txt`。

开发演示不需要真实模型密钥：可以给已上传视频放置同名的 `.segments.json` 旁车文件，格式为 `{"segments": [{"start": 1.2, "end": 3.4, "source": "ASR", "text": "示例内容"}]}`，系统会按秒读取并生成时间轴证据。

## 已实现能力

- 使用 FastAPI 提供 REST API，通过 JWT 完成登录认证和用户资源归属校验。
- 支持视频分片上传、缺失分片查询、原子合并、MD5 内容指纹和用户级内容去重。
- 从经过格式校验的 BV 号调用 B 站官方公开 API 读取元数据，并通过持久化导入任务和 RocketMQ Worker 异步下载；下载优先使用 `yt-dlp`，失败时回退 `playurl` DASH 音视频流 + FFmpeg，不接受任意 URL、登录 Cookie、付费或私密内容。
- 使用 SQLAlchemy + Alembic 管理 MySQL/SQLite 中的用户、视频、上传会话、证据片段和分析任务。
- 持久化分析任务状态，并通过 RocketMQ Producer/Consumer 解耦 API 与后台 Worker；重复消息通过原子抢占避免重复执行。
- 使用 Redis `SET NX EX` 和数据库唯一约束限制相同视频和分析目标重复提交。
- 支持 Worker 内懒加载和预热 `faster-whisper base/int8`，生成真实 ASR 时间戳片段；远程 ASR 失败且本地 ASR 已开启时自动回退。
- 使用 PaddlePaddle `3.2.2` 与 PaddleOCR `3.7.0` 的 `PP-OCRv5_mobile_det/rec` 抽取关键帧文字；ASR 后释放 Whisper 模型，并通过独立 OCR 子进程规避 CPU oneDNN 的非主线程限制，将 ASR/OCR 统一写入 EvidenceSegment 时间轴。
- 发现历史媒体只有 SYSTEM 占位证据时，在 ASR 可用后自动重新构建时间轴；抽取式报告按型号、推理强度、价格、适用场景和建议等目标维度检索，并保留全片时间轴锚点。
- 抽象 OpenAI 兼容 AI Provider，同时提供离线 Mock，方便无密钥演示。
- 内置 LangGraph 有状态 Agent 图，复用元数据、时间轴检索、证据窗口、引用校验和报告生成工具；真实模型通过 Function Calling 自主选择工具，Mock 模式执行确定性工具流水线。
- 独立 Evidence Retriever 提供关键词/字符片段混合基线，输出分数明细；`backend/evals/evidence_rag_eval.json` 和脚本统计 Recall@K、MRR、Hit Rate。
- 输出带时间戳证据的 Markdown 报告，并持久化动态计划、逐工具 Trace、图执行元数据、阶段耗时、引用支持率、继续追问和用户反馈；MySQL 使用 `LONGTEXT` 保存真实多轮工具调用 Trace。
- 按用户、视频和分析目标持久化 AgentSession/AgentMessage；模型追问上下文默认取最近 12 条，历史查看每会话默认返回最近 200 条并标记是否截断。`GET /analysis/agent-memory` 同时返回最新会话和该视频的全部会话列表，`GET /media/list` 返回会话/消息计数与最近回复摘要，支持前端关闭后恢复和主页历史展示。
- 运行独立 SeeIt MCP Server，通过用户 Bearer Token 暴露 14 个工具与 4 个资源模板，不直接访问数据库或绕过 FastAPI 权限边界。
- 对模型异常提供最多 3 次有限重试；服务重启时会回收超时的 `PROCESSING` 任务。
- 生产模式校验 JWT、MySQL、CORS 与 Alembic 配置，支持 Token 注销撤销和 Redis/内存双层接口限流。
- 可在生产环境启用 FFprobe 视频轨道、格式和时长校验，并自动清理过期的上传会话与临时分片。

## 当前验证边界

项目当前以完整业务流程和面试演示为目标，尚未提供生产吞吐量、消息零丢失或大规模并发数据。Evidence RAG 当前是确定性的关键词/字符片段基线，9 条合成用例的指标不能代表真实视频召回率；向量 Embedding 和 Qdrant 属于下一阶段。PaddleOCR 只负责画面文字识别，不等同于通用视觉语义理解；RocketMQ 需要 Linux/Docker 动态库，B 站导入依赖对方公开 API、CDN 与格式，平台策略变化时仍可能需要升级 `yt-dlp` 或接口适配。对外描述时应以代码和测试能够验证的能力为准。

服务器部署使用根目录的 `docker-compose.prod.yml`，详细步骤见 [`deploy/README.md`](../deploy/README.md)。

## MCP 本地运行

先启动 API 并登录获得用户 Token，再执行：

```bash
$env:SEEIT_API_URL="http://127.0.0.1:9090"
$env:SEEIT_MCP_TOKEN="你的用户令牌"
python -m seeit.mcp_server --transport stdio
```

需要 HTTP 服务时使用：

```bash
python -m seeit.mcp_server --transport streamable-http --host 0.0.0.0 --port 8001
```

HTTP 客户端必须在 `Authorization: Bearer <token>` 请求头中携带网站登录 Token。不要把真实 Token 写进仓库、Skill 或 MCP 配置示例。

## 后续改进顺序

1. 在当前 Retriever 接口上增加可选 Embedding 检索和 Qdrant Profile，与关键词基线比较 Recall@K、MRR 和延迟。
2. 为 Prompt、模型、工具策略和 Agent 图版本建立评测记录，增加真实视频人工标注集和 Provider 失败场景回归。
3. 在 GitHub Actions 中启动 MySQL、Redis 和 RocketMQ，增加真实组件集成测试与 Agent 评测门禁。
4. 增加 Token、成本、模型延迟、工具失败、记忆命中和队列积压等可观测指标。
