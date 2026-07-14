# 本地构建依赖缓存

服务器无法稳定访问 GitHub Release 时，可将已经校验过 SHA-256 的
`rocketmq-client.deb` 放在此目录后再构建镜像。该二进制文件已被
`.gitignore` 排除，不会提交到仓库；文件缺失时 Dockerfile 会从官方地址下载。

当前版本要求的 SHA-256 记录在 `backend/Dockerfile` 的
`ROCKETMQ_CLIENT_SHA256` 参数中，镜像构建时还会再次强制校验。
