# SeeIt AI

面向长视频内容理解的 Video Agent 平台。SeeIt AI 将视频转写、关键证据和用户目标组织成可追溯的结构化分析报告。

- 在线体验：[https://seeitai.online](https://seeitai.online)
- API 健康检查：[https://seeitai.online/api/health](https://seeitai.online/api/health)
- MCP Streamable HTTP：`https://seeitai.online/mcp`（需要网站用户 Bearer Token）

公网演示环境未配置真实模型和 ASR 密钥时使用 Mock Provider，用于验证完整业务、异步任务和工具调用链路，不代表真实模型准确率。

## 核心能力

- JWT 登录与用户资源隔离
- 大文件分片上传、断点查询与 MD5 内容指纹
- BV 号元数据预览、公开 B 站视频异步导入与来源追踪
- 分片原子写入、用户级内容去重和上传异常恢复
- MySQL 持久化与 Redis 重复任务锁
- RocketMQ 异步分析任务与独立 Worker
- FFmpeg 音频提取、ASR 时间戳片段和可选 OCR 关键帧证据
- 可替换的 AI Provider 与离线 Mock 演示
- 模型工具调用与离线确定性工具流水线，统一执行元数据、时间轴检索、证据窗口、引用校验和报告生成
- 动态分析计划、逐工具 Trace、证据引用评估、继续追问和任务状态查询
- 带用户 Token 隔离的 SeeIt MCP Server，提供 13 个工具和 4 个资源模板
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
    |-- 共享媒体存储
    |
RocketMQ Worker
    |-- yt-dlp 公开视频导入 / FFmpeg / ASR
    |-- Planner / Agent Tools / Critic
    |-- AI Provider（模型工具调用或离线工具流水线）
    `-- 结构化证据报告

MCP Server
    |-- Bearer Token 转发与用户隔离
    |-- 13 个视频工具 / 4 个资源模板
    `-- Codex / Claude / 其他 MCP 客户端
```

## 快速启动

需要 Docker Desktop。

```bash
docker compose up --build
```

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

没有配置真实模型和 ASR 密钥时，系统使用确定性的 Mock Provider，便于离线演示完整业务流程。真实调用配置见 [backend/.env.example](./backend/.env.example)。

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

当前 18 项测试覆盖：注册、鉴权隔离、浏览器 multipart 分片上传、断点查询、内容去重、分析幂等、失败重试、Agent 工具调用 Trace、模型 Function Calling、证据检索/窗口/引用校验、MCP 工具和资源注册、匿名请求拒绝、反馈持久化、JWT 撤销、生产配置校验、FFprobe 视频校验、RocketMQ 客户端契约，以及 BV 校验、预览时长限制、导入幂等和媒体来源入库。

## 服务器部署

项目提供面向 `2 核 8 GB` 单机的生产部署配置，完整运行 MySQL、Redis、RocketMQ、API、Worker、前端 Nginx 和 Caddy HTTPS。生产环境只对公网开放 `80/443`，数据库、缓存、消息队列和 API 均位于 Docker 内部网络。

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
docker-compose.yml             MySQL、Redis、RocketMQ、API、Worker 与 MCP
docker-compose.prod.yml        2 核 8 GB 服务器生产编排
deploy/                        HTTPS、生产环境模板、备份与部署文档
```

## 后续改进

1. 使用 MinIO 保存视频、音频和关键帧，替换共享 Docker volume。
2. 在 GitHub Actions 中启动 MySQL、Redis 和 RocketMQ，增加真实组件集成测试。
3. 建立离线评测集，量化证据覆盖率、时间戳命中率和结构化输出成功率。
4. 增加任务耗时、失败率、队列积压和 Provider 调用情况等可观测指标。
