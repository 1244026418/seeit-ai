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
python -m alembic upgrade head
uvicorn seeit.main:app --reload --port 9090
```

服务启动后可访问：

- API 地址：`http://localhost:9090`
- OpenAPI 文档：`http://localhost:9090/docs`

默认开发数据库为 SQLite，默认 AI Provider 为结果稳定的本地 Mock。需要连接 MySQL 和 OpenAI 兼容模型时，在 `.env` 中配置 `DATABASE_URL`、`AI_BASE_URL` 和 `AI_API_KEY`。

RocketMQ Python 客户端依赖本地动态库，本项目建议通过 Docker/Linux 运行 RocketMQ 模式。需要单独运行 API 和 Worker 时，还需安装 `requirements-rocketmq.txt`。

开发演示不需要真实模型密钥：可以给已上传视频放置同名的 `.segments.json` 旁车文件，格式为 `{"segments": [{"start": 1.2, "end": 3.4, "source": "ASR", "text": "示例内容"}]}`，系统会按秒读取并生成时间轴证据。

## 已实现能力

- 使用 FastAPI 提供 REST API，通过 JWT 完成登录认证和用户资源归属校验。
- 支持视频分片上传、缺失分片查询、原子合并、MD5 内容指纹和用户级内容去重。
- 使用 SQLAlchemy + Alembic 管理 MySQL/SQLite 中的用户、视频、上传会话、证据片段和分析任务。
- 持久化分析任务状态，并通过 RocketMQ Producer/Consumer 解耦 API 与后台 Worker；重复消息通过原子抢占避免重复执行。
- 使用 Redis `SET NX EX` 和数据库唯一约束限制相同视频和分析目标重复提交。
- 使用 FFmpeg 提取音频，解析 ASR 时间戳片段，并在开启 `OCR_ENABLED` 时抽取关键帧执行 Tesseract OCR。
- 抽象 OpenAI 兼容 AI Provider，同时提供离线 Mock，方便无密钥演示。
- 输出带时间戳证据的 Markdown 报告，并持久化计划、阶段耗时、引用支持率、继续追问和用户反馈。
- 对模型异常提供最多 3 次有限重试；服务重启时会回收超时的 `PROCESSING` 任务。

## 当前验证边界

项目当前以完整业务流程和面试演示为目标，尚未提供生产吞吐量、消息零丢失或大规模并发数据。OCR 依赖容器中的 Tesseract，RocketMQ 需要 Linux/Docker 动态库；对外描述时应以代码和测试能够验证的能力为准。

## 后续改进顺序

1. 使用 MinIO 保存视频、音频和关键帧，替换共享 Docker volume。
2. 在 GitHub Actions 中启动 MySQL、Redis 和 RocketMQ，增加真实组件集成测试。
3. 建立离线评测集，量化证据覆盖率、时间戳命中率和结构化输出成功率。
4. 增加任务耗时、失败率、队列积压和 Provider 调用情况等可观测指标。
