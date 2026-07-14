# SeeIt AI

面向长视频内容理解的 Video Agent 平台。SeeIt AI 将视频转写、关键证据和用户目标组织成可追溯的结构化分析报告。

## 核心能力

- JWT 登录与用户资源隔离
- 大文件分片上传、断点查询与 MD5 内容指纹
- BV 号元数据预览、公开 B 站视频异步导入与来源追踪
- 分片原子写入、用户级内容去重和上传异常恢复
- MySQL 持久化与 Redis 重复任务锁
- RocketMQ 异步分析任务与独立 Worker
- FFmpeg 音频提取、ASR 时间戳片段和可选 OCR 关键帧证据
- 可替换的 AI Provider 与离线 Mock 演示
- 分析计划、执行轨迹、证据引用评估、继续追问和任务状态查询
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
    |-- AI Provider
    `-- 结构化证据报告
```

## 快速启动

需要 Docker Desktop。

```bash
docker compose up --build
```

API 默认地址：`http://localhost:9090`

OpenAPI 文档：`http://localhost:9090/docs`

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

## 测试

```bash
cd backend
pytest -q
```

当前 12 项测试覆盖：注册、鉴权隔离、浏览器 multipart 分片上传、断点查询、内容去重、分析幂等、失败重试、证据评估、反馈持久化、JWT 撤销、生产配置校验、FFprobe 视频校验、RocketMQ 客户端契约，以及 BV 校验、预览时长限制、导入幂等和媒体来源入库。

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
backend/                       Python/FastAPI 后端
backend/alembic/               数据库迁移脚本
client/                        Vue 3 前端
docker-compose.yml             MySQL、Redis、RocketMQ、API 与 Worker
docker-compose.prod.yml        2 核 8 GB 服务器生产编排
deploy/                        HTTPS、生产环境模板、备份与部署文档
```

## 后续改进

1. 使用 MinIO 保存视频、音频和关键帧，替换共享 Docker volume。
2. 在 GitHub Actions 中启动 MySQL、Redis 和 RocketMQ，增加真实组件集成测试。
3. 建立离线评测集，量化证据覆盖率、时间戳命中率和结构化输出成功率。
4. 增加任务耗时、失败率、队列积压和 Provider 调用情况等可观测指标。
