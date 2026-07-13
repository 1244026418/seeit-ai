# SeeIt AI 前端

本目录是 SeeIt AI 的 Vue 3 前端，提供本地视频分片上传、断点续传、任务列表、分析进度、证据报告、继续追问和用户认证界面。

## 本地启动

推荐使用 Node.js 22 或更高版本。

```bash
cd client
npm install
npm run dev
```

页面默认运行在 `http://localhost:5173`，后端 API 默认地址为 `http://localhost:9090`。当前页面只开放本地视频上传，链接导入等外部下载能力需要单独完成安全审计后再接入。需要修改 API 地址时，可设置环境变量：

```bash
VITE_API_BASE_URL=http://localhost:9090
```

## 演示模式

访问 `http://localhost:5173/?demo` 可直接查看内置的视频分析结果，不依赖后端、数据库或模型密钥。

## 构建检查

```bash
npm run build
```

前端基于 Vue 3、Vite 和 Marked 开发。界面渲染模型返回的 Markdown 前会过滤不受支持的标签和属性，避免直接执行模型输出中的危险 HTML。
 
