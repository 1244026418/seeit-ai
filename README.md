# SeeIt AI

面向长视频内容理解的 Video Agent 平台。SeeIt AI 将视频转写、关键证据和用户目标组织成可追溯的结构化分析报告。

- 在线体验：[https://seeitai.online](https://seeitai.online)
- API 健康检查：[https://seeitai.online/api/health](https://seeitai.online/api/health)
- MCP Streamable HTTP：`https://seeitai.online/mcp`（需要网站用户 Bearer Token）

公网演示环境未配置真实模型和 ASR 密钥时使用 Mock Provider，用于验证完整业务、异步任务和工具调用链路，不代表真实模型准确率。

## 核心能力

- JWT 登录与用户资源隔离
- 大文件分片上传、断点查询与 MD5 内容指纹
- BV 号元数据预览、公开 B 站视频异步导入与来源追踪；预览优先使用官方公开 API，下载保留 `yt-dlp` 并提供 DASH 音视频回退
- 分片原子写入、用户级内容去重和上传异常恢复
- MySQL 持久化与 Redis 重复任务锁
- RocketMQ 异步分析任务与独立 Worker
- FFmpeg 音频提取、`faster-whisper base/int8` 本地 ASR、远程 ASR 兼容和 PaddleOCR 关键帧证据
- 可替换的 AI Provider 与离线 Mock 演示
- LangGraph 结构化 Agent 图：LLM Planner 生成证据槽位，批量 Retriever 构建 Evidence Ledger，Verifier 判断蕴含与充分性，Writer 使用 Evidence ID 成文，独立 Critic 检查完整性、矛盾、外部推断和拒答
- Coverage-Aware Agentic RAG：lexical + 本地 BGE dense + RRF，并对多问句、枚举和分别题建立证据需求计划、分需求检索、邻接扩展与充分性门禁
- Qdrant 持久化向量索引，按视频和 ASR/OCR 来源过滤，支持快照复用、变化重建、媒体删除清理和故障降级
- 合成/真实检索评测集，输出 Recall@K、MRR、多证据完整命中、无答案误召回、延迟和逐题结果
- 模型工具调用与离线确定性工具流水线，统一执行元数据、时间轴检索、证据窗口、引用校验和报告生成
- 动态分析计划、逐工具 Trace、图执行元数据、证据引用评估、继续追问和任务状态查询
- 按用户/视频/目标隔离的 Agent 短期记忆，持久化追问会话；关闭侧栏或刷新页面后可恢复报告与对话，主页视频卡片展示会话摘要
- 带用户 Token 隔离的 SeeIt MCP Server，提供 14 个工具和 4 个资源模板
- 课程笔记、会议复盘、操作指南和证据审计 4 个可复用 Agent Skill
- Alembic 数据库迁移、失败重试、重复消息幂等和 Pytest 接口测试

## 架构

```text
Vue 3 前端
    |
FastAPI API 服务
    |-- MySQL：用户 / 视频 / 上传会话 / 导入任务 / 分析任务
    |-- Redis：活跃任务锁
    |-- RocketMQ：分析任务消息
    |-- Qdrant：视频证据向量 / Payload Filter / 持久化索引
    |-- 共享媒体存储
    |
RocketMQ Worker
    |-- yt-dlp 公开视频导入 / FFmpeg / faster-whisper base
    |-- PaddleOCR 隔离子进程 / ASR-OCR 时间轴融合
    |-- LangGraph Structured Planner / Evidence Ledger / Verifier / Writer / Critic
    |-- Coverage-Aware Retriever / Contextual Hybrid / Qdrant / 真实检索评测
    |-- AI Provider（模型工具调用或离线工具流水线）
    `-- 结构化证据报告

MCP Server
    |-- Bearer Token 转发与用户隔离
    |-- 14 个视频工具 / 4 个资源模板
    `-- Codex / Claude / 其他 MCP 客户端
```

## 快速启动

需要 Docker Desktop。

```bash
docker compose up --build
```

本地 Compose 默认由 Worker 启用 `faster-whisper base/int8`、PaddleOCR 和 Contextual Qdrant Hybrid Retrieval。首次使用会把约 141 MB 的 ASR 模型、约 21 MB 的 `PP-OCRv5_mobile_det/rec` 模型和约 23 MB 的 INT8 BGE ONNX 模型下载到独立 `seeit_models` volume，后续启动及重复检索复用缓存；Qdrant 索引保存在独立 `seeit_qdrant` volume。ASR 完成后会释放 Whisper 模型，再由 OCR 子进程执行推理，规避 Paddle oneDNN 的线程限制并控制内存峰值。

根目录 `.env.example` 已预置 DeepSeek OpenAI-compatible 配置，复制为 `.env` 后手动填写新生成的 `AI_API_KEY` 即可启用 `deepseek-v4-flash`；密钥为空时使用确定性 Mock Provider。

API 默认地址：`http://localhost:9090`

OpenAPI 文档：`http://localhost:9090/docs`

MCP Streamable HTTP 地址：`http://localhost:8001/mcp`

前端需要 Node.js 22+：

```bash
cd client
npm install
npm run dev
```

前端默认访问 `http://localhost:9090`，页面地址为 `http://localhost:5173`。

GitHub 提交、本地 Compose 构建、Docker Hub/GHCR 镜像发布和服务器更新的完整命令见 [GitHub 与 Docker 发布指南](./docs/GitHub与Docker发布指南.md)。

## 本地后端开发

推荐 Python 3.11+。

```bash
cd backend
python -m venv .venv
# Windows 激活虚拟环境
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python -m alembic upgrade head
uvicorn seeit.main:app --reload --port 9090
```

直接运行 Python 后端时，本地 ASR 默认关闭，可在 `.env` 中设置 `LOCAL_ASR_ENABLED=true`；也可以配置 OpenAI 兼容的远程 ASR。没有配置真实 LLM 时，报告生成使用确定性的抽取式 Mock Provider，但时间轴证据仍可来自真实本地 ASR。完整配置见 [backend/.env.example](./backend/.env.example)。

## MCP Server 与 Skills

MCP Server 支持远程 Streamable HTTP 和本地 stdio。HTTP 模式直接使用网站登录后获得的 JWT Bearer Token，因此 MCP 只能访问该用户自己的视频。Docker 启动后连接 `http://localhost:8001/mcp`；服务器部署地址为 `https://你的域名/mcp`。

本地 stdio 模式：

```bash
cd backend
# PowerShell：将网站登录接口返回的 token 仅放入当前终端环境变量
$env:SEEIT_MCP_TOKEN="你的用户令牌"
$env:SEEIT_API_URL="http://127.0.0.1:9090"
python -m seeit.mcp_server --transport stdio
```

仓库中的 Skills 位于 [`skills/`](./skills/)，分别处理课程笔记、会议复盘、操作指南和证据审计。每个 Skill 都声明 `seeit-ai` MCP 依赖，并规定证据不足、任务未完成和引用校验失败时的处理方式。

## 测试

```bash
cd backend
pytest -q
```

最近一次完整后端回归为 `103 passed, 2 skipped`；两个 skip 是需要外部 Qdrant 服务条件的集成项。覆盖上传与鉴权、任务重试、ASR/OCR、Retriever Profile、Qdrant 运行/降级、离线评测、结构化 Agent 槽位、Verifier 审计、Critic 有界收尾和 EvalOps 指标。

证据 RAG 基线命令：

```bash
cd backend
python scripts/evaluate_evidence_rag.py
```

合成基线使用 9 条 ASR/OCR 片段，Top-3 的 Recall@3、MRR 和 Hit Rate 均为 1.0。V1 真实集包含 2 条视频和 16 道人工标注问题，当前只作为开发集调试时间指代、邻接扩展和拒答阈值；此前的 2 视频 12 题已用于方案选择，也不再是严格未见留出集。评测统一输出 Recall@1/3/8、MRR、多证据完整命中、ASR/OCR 分项、无答案正检索率和延迟。

离线 A/B 与生产运行使用同一个固定版本的本地 BGE；模型文件不提交仓库、不打入镜像，由运行时下载到模型 volume：

```bash
cd backend
pip install -r requirements-eval.txt
python scripts/evaluate_retrieval_ab.py \
  --dataset evals/heldout_retrieval_eval.json \
  --snapshot /path/to/heldout-evidence-snapshots.json \
  --output /path/to/heldout-retrieval-ab.json \
  --model-dir /path/to/bge-small-zh-v1.5 \
  --allow-model-download
```

模型固定为 `Xenova/bge-small-zh-v1.5` 的 revision `75c43b069aac4d136ba6bc1122f995fedcfd2781` 和 INT8 ONNX 文件。V1 开发集加入 Contextual 策略后，Recall@8 从 `0.8667` 提升到 `1.0000`，多证据完整命中率从 `0.8000` 提升到 `1.0000`；这些数字只代表开发集调参结果。最终未见留出集（2 条新视频、14 题，其中 9 道可回答）上，lexical 的 MRR/Recall@8 为 `0.5972/0.7778`，Hybrid 与 Contextual/Qdrant 的 MRR/Recall@8 为 `0.7222/0.7778`，Hybrid 相对 lexical 的 MRR 绝对提升 `0.1250`；dense 的 Recall@8 最高为 `0.8333`，但早期排序较差。Qdrant 已作为可选持久化后端接入，最终集与内存 Contextual 的质量指标一致，查询均值约 `12.51 ms`，说明它解决的是持久化、Payload Filter 和跨进程访问，不是自动的准确率提升。

最终未见留出集还暴露出明确边界：7/9 道可回答题在 Qdrant Contextual Profile 下命中，2 道多证据题未完整召回；5 道不可回答题全部产生正检索，abstention precision/recall/F1 均为 `0`。11 段金标证据在 ASR/OCR 快照中全部存在，因此本轮失败属于检索覆盖/拒答泛化问题，不是证据抽取缺失。最终留出集只用于一次冻结验证，不能据此继续调参。

随后只使用 V1 16 题与此前 12 题作为开发数据实现 `coverage-aware-qdrant-hybrid-v2`。在旧 12 题开发集上，相对 legacy Contextual，MRR `0.8500 -> 0.8750`、Recall@8 `0.7750 -> 1.0000`、多证据完整命中@8 `0.7000 -> 1.0000`，2 道不可回答题均拒答且没有可回答题误拒；平均查询耗时约 `8.20 -> 14.96 ms`。V1 开发集质量指标保持 MRR `0.8633`、Recall@8/完整命中@8 `1.0000`，查询均值约 `7.37 -> 8.83 ms`。这些都是经过多轮规则调整的开发集结果，最终 14 题没有重跑，不能声称新策略已经泛化。

开发/生产示例现在默认 `coverage-aware-qdrant-hybrid-v2`；需要回滚时可将 `EVIDENCE_RETRIEVER_PROFILE` 改回 `contextual-qdrant-hybrid-v1`。新 Profile 对普通模型名的 ASR/OCR 变体保留语义候选，只在当前视频缺少领域锚点、比较对象或全大写关键缩写时直接拒答，并把 `coveragePlan/evidenceSufficiency` 交给 LangGraph 收尾门禁。

Coverage-Aware v2 随后在另一组冻结最终未见集（2 条新视频、16 题，其中 11 道可回答、5 道不可回答）上只运行一次。282 段 ASR/OCR 快照中的金标抽取覆盖率为 `1.0000`，但 lexical 的 MRR/Recall@8 为 `0.5682/0.6818`，Coverage-Aware 与 Qdrant Coverage 只有 `0.4651/0.5909`；11 道可回答题中 6 道完整命中，5 道不可回答题均未在检索层拒答。开发集上的提升没有泛化，`fullyCovered` 还错误地把每道可回答题都视为单一且已满足的 requirement。真实 DeepSeek 只做预算受控抽样：7/16 题完成、人工语义复核 3/7 通过，40 次请求累计 408,045 Token 后停止。该最终集已封存，不用于后续调参或重跑。

针对上述失败，默认 Agent 已升级为 `video-evidence-agent-v5.1-bounded-closeout`。v5.1 不再依赖“检索到候选即 fullyCovered”：DeepSeek 先输出带 `completionPolicy` 的结构化槽位计划，系统按槽位批量检索并压缩为最多 18 条 Evidence Ledger，Verifier 逐槽位返回 `supported/complete/evidenceIds`，必要时执行一次假阴性/悬空指代审计，Writer 只能引用已存在的 Evidence ID，Critic 同时执行模型审查和确定性 ID/槽位门禁。Critic 最多修订一次；修订后若确定性门禁通过则有界接受，否则输出零引用安全拒答，避免把确定性质量失败完整重跑三次。可通过 `AGENT_PIPELINE_VERSION=legacy-v4` 回滚。

在完全相同的两段合成证据和比较问题上，真实 DeepSeek v4/v5 均 4 次请求且回答正确；v5 Prompt Token `7802 -> 4018`、Total Token `8459 -> 4828`，分别下降约 `48.5%/42.9%`，Tool Call `5 -> 4`，总 Provider 延迟 `7127 -> 7422 ms`。这是单次开发冒烟，不是性能压测；v4 有 6528 Cache Hit Token 而 v5 首次运行无缓存，且费用单价未配置，不能据此宣称实际账单下降。另一个“标题相关但未给答案”的合成场景中，v5 以 4 次请求、4062 Token 完成零引用明确拒答。

在允许调参的 V1 两视频 16 题开发集上，v5.1 全量运行达到 `16/16` 任务完成、`9/16` 保守字符串规则命中、引用支持率 `1.0`，87 次 Provider 请求累计 158,894 Token；没有基础设施、限流或 Agent 未收尾失败。后续定向修复了技术名保留和单目标顺序题，但没有再次运行完整 16 题，因此不得把局部结果外推为新的全量准确率。残余失败主要来自 OCR 价格抽取错误、`token/Qwen/关羽` 被 ASR 识别为“偷懒/千万/关于”后的跨片段实体恢复，以及 Verifier 偶发结构化布尔值与说明文字冲突。V1 是开发集，不代表最终未见泛化；Coverage-Aware v2 最终集继续冻结且未重跑。

## 服务器部署

项目提供面向 `4 核 8 GB` 单机的本地 ASR/OCR 生产模板，完整运行 MySQL、Redis、RocketMQ、Qdrant、API、Worker、MCP、前端 Nginx 和 Caddy HTTPS。Worker 默认限制为 `2.5 GiB` 并串行处理任务，Qdrant 限制为 `768 MiB`；`2 核 8 GB` 也能运行单用户演示，但数分钟视频的 CPU 处理时间会明显增加。生产环境只对公网开放 `80/443`，数据库、缓存、消息队列、Qdrant 和 API 均位于 Docker 内部网络。

```bash
cp deploy/.env.production.example deploy/.env.production
# 修改域名、数据库密码、Redis 密码和 JWT_SECRET
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml config --quiet
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml up -d --build
```

详细的域名解析、安全组、首次启动、更新、备份和排错步骤见 [deploy/README.md](./deploy/README.md)。

生产模式会强制检查 MySQL、Alembic、CORS 和 JWT 密钥配置；上传或 BV 导入完成后使用 FFprobe 校验真实视频轨道与时长，并对登录、上传、导入和分析接口执行限流。BV 导入只接受固定格式的 BV 号并构造 Bilibili 官方视频地址，不接受任意 URL。

## 项目结构

```text
backend/                       Python/FastAPI 后端与 MCP Server
backend/alembic/               数据库迁移脚本
client/                        Vue 3 前端
skills/                        4 个 SeeIt Agent Skill
docker-compose.yml             MySQL、Redis、RocketMQ、Qdrant、API、Worker 与 MCP
docker-compose.prod.yml        4 核 8 GB 本地 ASR 服务器生产编排
deploy/                        HTTPS、生产环境模板、备份与部署文档
```

## 后续改进

1. 不再使用 Coverage-Aware v2 最终集调参；以 V1、旧 12 题、合成/变形测试和线上 shadow case 扩充 Planner/Verifier 回归。
2. 为 Evidence Ledger 增加跨片段关系类型与更严格的枚举完备性校验，但继续限制最多 6 个槽位和 18 条压缩证据。
3. 在真实任务 Trace 中长期观察 `byPhase` Token、上下文字符数、修订率和拒答率；费用门禁为可选评测参数，默认 Provider 调用不设总预算。
4. 暂不引入 Reranker；当前先验证槽位规划、充分性和 Critic 是否在 shadow case 中稳定。
5. 在 GitHub Actions 中增加真实 Qdrant/MySQL/Redis/RocketMQ 集成测试与 Agent 契约回归。
