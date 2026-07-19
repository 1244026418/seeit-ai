# GitHub 与 Docker 发布指南

本文以 SeeIt AI 当前目录和 Windows PowerShell 为例。命令中的用户名、版本号需要按实际情况替换，任何 `.env`、API Key、服务器密码和访问令牌都不能提交到仓库。

## 1. 先理解三种“上传”

| 操作 | 保存的内容 | 主要用途 |
| --- | --- | --- |
| `git push` | 源代码和 Git 历史 | GitHub 协作、展示和服务器拉取代码 |
| `docker compose up -d --build` | 在本机根据源码构建镜像并启动容器 | 本地开发、联调和验收 |
| `docker push` | 构建完成的 Docker 镜像 | 发布到 Docker Hub 或 GHCR，供其他机器直接拉取 |

SeeIt AI 当前的服务器部署方案是“从 GitHub 拉取源码，再由 Compose 构建镜像”，因此正常更新项目只需要 `git push`，服务器端执行 `git pull` 和 `docker compose ... up -d --build`。只有想让服务器跳过构建、直接拉镜像时，才需要 Docker Hub 或 GHCR。

## 2. Git 首次配置

安装 Git 后设置提交者信息，这些信息会显示在提交历史中：

```powershell
git config --global user.name "你的 GitHub 用户名"
git config --global user.email "你的 GitHub 邮箱"
git config --global init.defaultBranch main
```

检查配置：

```powershell
git config --global --list
```

已有本项目时直接进入目录：

```powershell
Set-Location "C:\Users\12440\Desktop\code\new\SeeIt AI"
```

在一台新电脑上首次取得项目：

```powershell
git clone https://github.com/1244026418/seeit-ai.git
Set-Location .\seeit-ai
```

`git clone` 会下载代码、提交历史并自动添加名为 `origin` 的远端。

## 3. 每次向 GitHub 发布代码

### 3.1 查看改动和分支

```powershell
git status --short
git branch --show-current
git remote -v
```

- `git status --short`：查看修改、删除和新增文件。
- `git branch --show-current`：确认当前位于 `main`。
- `git remote -v`：确认代码会推送到正确仓库。

### 3.2 运行检查

后端测试：

```powershell
Set-Location .\backend
pytest -q
Set-Location ..
```

前端构建检查：

```powershell
Set-Location .\client
npm install
npm run build
Set-Location ..
```

检查 Git 补丁中的空白错误：

```powershell
git diff --check
```

### 3.3 暂存并复核

```powershell
git add README.md backend client deploy docs docker-compose.yml docker-compose.prod.yml
git status --short
git diff --cached --stat
git diff --cached
```

`git add` 只是把文件加入“本次准备提交的快照”，不会上传。这里显式列目录比无条件执行 `git add .` 更容易发现误放的文件。`git diff --cached` 用于提交前最后复核。

不要暂存以下内容：

- `.env`、`backend/.env`、`deploy/.env.production`
- API Key、GitHub Token、数据库密码、JWT Secret
- 上传的视频、数据库文件、模型缓存和备份

如果误暂存但还没提交，可仅取消暂存，不删除本地文件：

```powershell
git restore --staged 路径
```

### 3.4 提交、同步和推送

```powershell
git commit -m "功能：简要描述本次改动"
git pull --rebase origin main
git push origin main
```

- `git commit`：在本地创建一个带说明的版本。
- `git pull --rebase`：先取得远端新提交，再把自己的提交接到其后，减少无意义的合并提交。
- `git push`：把本地提交上传到 GitHub。

若 `pull --rebase` 发生冲突，不要强行覆盖远端。解决冲突后执行：

```powershell
git add 冲突文件
git rebase --continue
git push origin main
```

发布后核对本地和远端提交：

```powershell
git status --short
git log -1 --oneline
git ls-remote origin refs/heads/main
```

## 4. 本地 Docker 构建和启动

首次启动或源码、依赖、Dockerfile 有变化时：

```powershell
Set-Location "C:\Users\12440\Desktop\code\new\SeeIt AI"
docker compose config --quiet
docker compose up -d --build
```

- `docker compose config --quiet`：先验证 Compose 配置能否解析。
- `up`：创建并启动服务。
- `-d`：在后台运行，终端可以继续使用。
- `--build`：启动前根据最新源码重新构建镜像。

只改了 `.env` 或只需要重启现有镜像时：

```powershell
docker compose up -d
```

只重建 API、Worker 和 MCP：

```powershell
docker compose up -d --build api worker mcp
```

查看状态和日志：

```powershell
docker compose ps
docker compose logs --tail 100 api
docker compose logs --tail 100 worker
docker compose logs --tail 100 mcp
docker compose logs -f worker
```

`logs -f` 会持续跟踪日志，按 `Ctrl+C` 只会退出日志查看，不会关闭容器。

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:9090/health
```

停止与恢复：

```powershell
docker compose stop
docker compose start
```

删除容器和网络但保留命名卷中的数据：

```powershell
docker compose down
```

不要随手执行 `docker compose down -v`。其中 `-v` 会删除 Compose 命名卷，可能同时清除 MySQL 数据、上传文件和已下载的 ASR/OCR 模型。

## 5. 发布到 Docker Hub

这一步是可选的。先在 Docker Hub 创建仓库，例如 `你的用户名/seeit-ai-backend`，再登录。密码位置应使用 Docker Hub Access Token，并在交互式提示中输入：

```powershell
docker login --username 你的DockerHub用户名
```

SeeIt AI 的 API、Worker 和 MCP 使用同一个后端镜像，通过不同启动命令承担不同角色。先构建一次并打版本标签：

```powershell
docker build -t 你的DockerHub用户名/seeit-ai-backend:1.0.0 .\backend
docker tag 你的DockerHub用户名/seeit-ai-backend:1.0.0 你的DockerHub用户名/seeit-ai-backend:latest
```

推送两个标签：

```powershell
docker push 你的DockerHub用户名/seeit-ai-backend:1.0.0
docker push 你的DockerHub用户名/seeit-ai-backend:latest
```

查看本地镜像：

```powershell
docker image ls
```

版本标签用于准确回滚，`latest` 只是一个方便的移动标签，不能代替明确版本号。生产环境应固定使用类似 `1.0.0` 或 Git 提交号的标签。

## 6. 发布到 GitHub Container Registry（GHCR）

在 GitHub 创建只具有 `write:packages` 权限的 Personal Access Token。登录命令不要直接携带 Token，执行后在提示中粘贴：

```powershell
docker login ghcr.io --username 1244026418
```

构建和推送：

```powershell
docker build -t ghcr.io/1244026418/seeit-ai-backend:1.0.0 .\backend
docker tag ghcr.io/1244026418/seeit-ai-backend:1.0.0 ghcr.io/1244026418/seeit-ai-backend:latest
docker push ghcr.io/1244026418/seeit-ai-backend:1.0.0
docker push ghcr.io/1244026418/seeit-ai-backend:latest
```

首次发布后，在 GitHub Packages 页面确认包的可见性。若服务器拉取私有包，服务器也需要具有 `read:packages` 权限的登录凭据；公开包则不需要保存拉取密钥。

退出 Registry 登录：

```powershell
docker logout
docker logout ghcr.io
```

## 7. 服务器更新源码构建版

在服务器的项目目录中执行：

```bash
git status --short
git pull --rebase origin main
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml config --quiet
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml up -d --build
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml ps
python3 deploy/smoke-test.py --base-url https://seeitai.online
```

先运行 `git status` 是为了避免服务器上未提交的手工改动被覆盖。生产环境的 `deploy/.env.production` 只保存在服务器，不上传 GitHub。

更新失败时优先查看：

```bash
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml logs --tail 200 api
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml logs --tail 200 worker
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml logs --tail 200 caddy
```

## 8. 推荐发布顺序

1. 本地运行后端测试和前端构建。
2. 本地执行 `docker compose up -d --build` 并完成一次核心流程验收。
3. 复核暂存文件，提交并推送 GitHub。
4. 在服务器拉取源码并由生产 Compose 重建。
5. 执行 smoke test，检查 API、Worker、MCP 和网站。
6. 只有确实需要镜像分发时，再额外推送 Docker Hub 或 GHCR。
