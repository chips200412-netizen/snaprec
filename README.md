# 瞬时录 SnapRec

把公开链接收藏下来，留下自己的灵感。

SnapRec 是一个本机单用户的收藏工具：粘贴链接或平台分享文本，确认来源信息、分类和标签，再写下个人想法并保存到可搜索的素材库。当前发布候选为 `v0.1.0-alpha`，主要验证环境是 Windows 与 Python 3.12。

## 当前能做什么

- 收藏单个公开 HTTP/HTTPS 链接或含单个链接的分享文本；支持抖音、哔哩哔哩、小红书、YouTube 和通用网页的公开元信息尽力获取。
- 元信息缺失、页面访问受限或平台不兼容时，保留原链接，允许补充标题并保存为普通书签。
- 确认或修改分类、标签；记录可留空的文字灵感；补充作者或自己的封面图片。
- 在本地素材库搜索、筛选、查看和编辑收藏；遇到相同来源时，显式比较并合并，避免自动覆盖。
- 一次导入 2–10 个链接，逐项审核后保存，支持部分成功、取消和恢复。
- 手机录音组件支持主动录音和转写草稿；语音供应商默认未配置，文字收藏始终可用。

网站只服务收藏与个人灵感，不提供深度解析、字幕问答或来源音轨转写。旧后端解析模块和兼容接口仍保留在源码中，不属于本版网站主线。MCP 与 Skill 入口尚未实现。

## 本地运行

准备 Python **3.12.x**、Node.js **24.15.0 或更高的 24.x**、pnpm **11.19.0**。本版采用 `requirements.lock` 和 `web/pnpm-lock.yaml` 固定依赖；安装步骤及配置说明见 [从零开始](docs/getting-started.md)。

在项目根目录创建环境并安装后端依赖：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -B scripts/check_runtime_config.py
```

第一个终端启动后端：

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.server:app --host 127.0.0.1 --port 8000 --no-access-log
```

第二个终端安装并启动前端：

```powershell
cd web
pnpm install --frozen-lockfile
pnpm dev --host 127.0.0.1 --port 5173
```

浏览器打开 [本地应用](http://127.0.0.1:5173/)。Vite 将 `/api` 请求转发到本机后端。不要直接双击 `web/index.html`；`pnpm preview` 也不提供这里的开发代理，上传 `dist` 不能代替完整部署。

## 使用边界

平台作者、封面和标题的获取结果会随公开页面、网络和访问策略变化，本版不保证获取率。可选浏览器补取需要 Playwright 和可用的 Microsoft Edge；缺失或失败时安全降级，不自动下载安装浏览器、不登录、不使用个人 Cookie、不下载来源媒体。

语音只处理用户主动录制的个人灵感，默认清理临时录音，仅在用户采用并保存后保留文字。启用火山极速版需要自行配置凭据、ffprobe 和 ffmpeg，并了解供应商费用与数据政策。手机真机、完整浏览器状态矩阵和真实语音故障的解决尚未全面验证；系统分享入口也受浏览器、安装状态和 HTTPS 条件限制，手动粘贴是默认可用入口。本版不提供一键手机局域网部署。

当前没有登录与用户隔离，请仅绑定本机回环地址。**不要直接暴露到公网或不受保护的局域网。** 本地数据、配置和备份也需要由使用者保护。详见 [安全说明](SECURITY.md)。

## 文档与许可

- [安装、配置、测试与备份](docs/getting-started.md)
- [v0.1.0-alpha 版本说明](docs/releases/v0.1.0-alpha.md)
- [贡献说明](CONTRIBUTING.md)
- [如何生成干净发布副本](docs/publishing.md)
- [第三方与资产说明](THIRD_PARTY_NOTICES.md)

本版公开源码用于展示与评审，**暂未添加许可证，也未授予开源许可**。使用、修改或再分发项目代码需取得权利方另行授权；第三方依赖适用各自的许可证。
