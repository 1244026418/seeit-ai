# SeeIt AI

面向长视频内容理解的 Video Agent 平台。SeeIt AI 将视频转写、关键证据和用户目标组织成可追溯的结构化分析报告。

## 核心能力

- JWT 登录与用户资源隔离
- 大文件分片上传、断点查询与 MD5 内容指纹
- MySQL 持久化与 Redis 重复任务锁
- RocketMQ 异步分析任务与独立 Worker
- FFmpeg 音频提取及 OpenAI 兼容 ASR 接口
- 可替换的 AI Provider 与离线 Mock 演示
- 分析计划、执行轨迹、证据报告、继续追问和任务状态查询

## 架构

```text
Vue 3 Client
    |
FastAPI API
    |-- MySQL: users / media / upload sessions / analysis tasks
    |-- Redis: active-task lock
    |-- RocketMQ: analysis messages
    |-- Shared media storage
    |
RocketMQ Worker
    |-- FFmpeg / ASR
    |-- AI Provider
    `-- structured evidence report
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
# Windows
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn seeit.main:app --reload --port 9090
```

没有配置真实模型和 ASR 密钥时，系统使用确定性的 Mock Provider，便于离线演示完整业务流程。真实调用配置见 [backend/.env.example](./backend/.env.example)。

## 测试

```bash
cd backend
pytest -q
```

当前接口测试覆盖：注册、JWT、分片上传、文件合并、媒体列表、分析提交、后台执行与结果轮询。

## 项目结构

```text
backend/                       Python/FastAPI 后端
client/                        Vue 3 前端
docker-compose.yml             MySQL、Redis、RocketMQ、API 与 Worker
```

## 后续改进

1. 使用 Alembic 管理数据库迁移与索引。
2. 使用 MinIO 保存视频和关键帧，替换共享 Docker volume。
3. 按时间窗口执行 ASR，并持久化真实时间戳片段。
4. 抽取关键帧、执行 OCR，并与 ASR 按时间轴融合。
5. 增加 RocketMQ 重试、死信队列、消费幂等和故障恢复测试。
6. 建立离线评测集，量化证据覆盖率和幻觉率。
7. 补充并发上传、越权访问、重复分片和异常恢复测试。

## 许可

SeeIt AI 的 Python 后端、任务模型和视觉系统经过独立重写。项目保留适用于衍生部分的 MIT 许可声明。
