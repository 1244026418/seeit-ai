# SeeIt AI 服务器部署

这套配置面向一台 `2 核 8 GB` 的 Ubuntu 云服务器，使用 Docker Compose 运行 MySQL、Redis、RocketMQ、FastAPI、Worker、Vue/Nginx 和 Caddy。

## 1. 服务器准备

- 安装 Ubuntu 22.04/24.04、Docker Engine 和 Docker Compose Plugin。
- 在 DNS 服务商添加域名 A 记录，指向服务器公网 IPv4。
- 安全组只放行 `22`、`80`、`443`；MySQL、Redis、RocketMQ 和 API 端口不对公网开放。
- 使用普通 SSH 用户部署，只有安装 Docker 或更新系统时使用 `sudo`。

如果服务器位于中国大陆，域名通常需要完成 ICP 备案后才能正常提供网站服务；需要立即演示时可以先选择中国香港地域，或确认云厂商当前的备案要求。

如果 Docker Hub 拉取超时，先在腾讯云、阿里云或华为云控制台获取对应区域的 Docker 镜像加速地址并配置到 `/etc/docker/daemon.json`，不要随意使用来源不明的公共镜像站。

Caddy 会自动申请 Let's Encrypt 证书，因此域名必须已经解析到服务器，且 `80/443` 可以从公网访问。

## 2. 配置生产环境

在项目根目录执行：

```bash
cp deploy/.env.production.example deploy/.env.production
openssl rand -hex 32
```

编辑 `deploy/.env.production`：

- 将 `DOMAIN` 改成你的真实域名。
- 域名未备案或尚未解析时，可临时使用 `DOMAIN=http://服务器公网IP`，同时将 `CORS_ALLOWED_ORIGINS` 改成相同地址。
- 为 `MYSQL_PASSWORD`、`MYSQL_ROOT_PASSWORD`、`REDIS_PASSWORD` 和 `JWT_SECRET` 生成不同的随机值。
- `DATABASE_URL` 中的 MySQL 用户和密码必须与上面的变量一致。
- `CORS_ALLOWED_ORIGINS` 改成 `https://你的域名`。
- 填写 `AI_BASE_URL`、`AI_API_KEY`、`AI_MODEL`；没有模型密钥时可以留空，使用 Mock Provider。
- `OCR_ENABLED` 默认关闭。2 核服务器建议先关闭，避免 OCR 和视频分析同时占满 CPU。

大陆服务器访问官方依赖源不稳定时，可以只在生产环境文件中覆盖构建镜像源：

```dotenv
DEBIAN_MIRROR=mirrors.cloud.tencent.com
PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
NPM_REGISTRY=https://registry.npmmirror.com
```

Dockerfile 的默认值仍是 Debian、PyPI 和 npm 官方源，因此这组配置不会影响其他地区的构建。

生产环境文件包含密码，不能提交 GitHub：

```bash
chmod 600 deploy/.env.production
```

## 3. 首次启动

```bash
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml config --quiet
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml up -d --build
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml ps
```

API 容器启动时会自动执行 `alembic upgrade head`；数据库刚启动时，入口脚本会以 2 秒间隔进行最多 30 次有限重试。查看迁移和 Worker 日志：

```bash
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml logs -f api worker
```

检查服务：

```bash
curl https://你的域名/api/health
```

浏览器访问 `https://你的域名`。OpenAPI 文档地址为 `https://你的域名/api/docs`。

## 4. 更新版本

```bash
git pull --ff-only
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml up -d --build
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml ps
```

不要使用 `docker compose down -v`，这个命令会删除 MySQL、Redis、RocketMQ 和上传文件的持久化卷。

## 5. 数据库备份

先执行：

```bash
chmod +x deploy/backup-mysql.sh
./deploy/backup-mysql.sh
```

脚本会把 MySQL 备份写入项目的 `backups/` 目录，并自动保留最近 7 天。上传视频位于 Docker volume，正式环境还应定期创建云盘快照；演示项目可以设置较短的文件保留周期。

## 6. 2 核 8 GB 资源建议

- API 只运行 1 个 Uvicorn 进程。
- Worker 消费线程数设置为 1，任务串行处理。
- MySQL buffer pool 为 256 MB，Redis 上限为 192 MB。
- RocketMQ NameServer/Broker 已限制 JVM 堆内存。
- 单个视频默认不超过 512 MB、时长不超过 10 分钟。
- 如果服务器内存持续超过 80%，先关闭 OCR、减少日志和清理历史上传，再考虑升级机器。

## 7. 常用排错

```bash
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml logs --tail=100 api
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml logs --tail=100 worker
docker stats
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml restart worker
```

如果服务器可以拉取 GitHub 页面但无法稳定下载 RocketMQ 客户端二进制，可在可信网络中从官方 Release 下载 `rocketmq-client-cpp-2.0.0.amd64.deb`，校验 SHA-256 为：

```text
d8a97b5aed30559a6bffe846835f0de39c6cb3f051b9ef665e461e1111ddd785
```

将校验通过的文件改名为 `rocketmq-client.deb` 并放入 `backend/vendor/` 后重新构建。该文件已被 `.gitignore` 排除；Dockerfile 构建时还会再次校验哈希，目录中没有缓存时则自动从官方 Release 下载。

生产演示只使用你自己的测试视频和模型密钥，不要在公开环境中保存隐私视频或长期保留第三方内容。
