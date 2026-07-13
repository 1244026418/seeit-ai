# SeeIt AI 后端

本目录是 SeeIt AI 的 Python/FastAPI 后端，负责用户认证、视频上传、异步分析任务和证据报告生成。核心流程如下：

`视频上传 -> MySQL/SQLite 持久化 -> 后台任务 -> FFmpeg/ASR -> 证据报告 -> 前端轮询`

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
uvicorn seeit.main:app --reload --port 9090
```

服务启动后可访问：

- API 地址：`http://localhost:9090`
- OpenAPI 文档：`http://localhost:9090/docs`

默认开发数据库为 SQLite，默认 AI Provider 为结果稳定的本地 Mock。需要连接 MySQL 和 OpenAI 兼容模型时，在 `.env` 中配置 `DATABASE_URL`、`AI_BASE_URL` 和 `AI_API_KEY`。

RocketMQ Python 客户端依赖本地动态库，本项目建议通过 Docker/Linux 运行 RocketMQ 模式。需要单独运行 API 和 Worker 时，还需安装 `requirements-rocketmq.txt`。

## 已实现能力

- 使用 FastAPI 提供 REST API，通过 JWT 完成登录认证和用户资源归属校验。
- 支持视频分片上传、缺失分片查询、顺序合并和 MD5 内容指纹计算。
- 使用 SQLAlchemy 统一管理 MySQL/SQLite 中的用户、视频、上传会话和分析任务。
- 持久化分析任务状态，并通过 RocketMQ Producer/Consumer 解耦 API 与后台 Worker。
- 使用 Redis `SET NX EX` 限制相同视频和分析目标被重复提交。
- 使用 FFmpeg 提取音频，并支持接入 ASR 服务。
- 抽象 OpenAI 兼容 AI Provider，同时提供离线 Mock，方便无密钥演示。
- 输出带时间戳证据的 Markdown 报告，并提供计划、执行轨迹、评估和继续追问接口。

## 当前验证边界

项目当前以完整业务流程和面试演示为目标，尚未提供生产吞吐量、消息零丢失或大规模并发数据。对外描述时应以代码和测试能够验证的能力为准。

## 后续改进顺序

1. 使用 Alembic 管理数据库迁移，并补充明确的 MySQL 索引。
2. 使用 MinIO 保存视频和关键帧，替换共享 Docker volume。
3. 按时间窗口执行 ASR，并持久化真实的时间戳片段。
4. 抽取关键帧、执行 OCR，并与 ASR 证据按时间轴融合。
5. 增加 RocketMQ 重试、死信队列、消费幂等和故障恢复测试。
6. 建立离线评测集，量化证据覆盖率和幻觉率。
7. 增加越权访问、重复分片、并发上传和任务恢复等集成测试。
