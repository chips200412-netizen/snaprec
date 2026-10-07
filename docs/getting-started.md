# 从零开始

本指南针对 `v0.1.0-alpha` 公开源码快照，在 Windows 本机运行收藏网站。它不包含账户系统、公网部署或一键手机局域网配置。

## 1. 准备环境

| 工具 | 要求 |
| --- | --- |
| Python | 3.12.x；项目约束为 `>=3.12,<3.13` |
| SQLite | Python 自带的 SQLite 必须支持 FTS5 与 trigram；由配置检查验证 |
| Node.js | 24.15.0 或更高的 24.x；锁文件中的测试依赖要求这一最低版本 |
| pnpm | 11.19.0，与 `web/package.json` 中的 `packageManager` 一致 |

主要验证环境为 Windows、Python 3.12、Node.js 24.19.0 和 pnpm 11.19.0。其他操作系统及其他 Node.js 主版本未作本版运行保证。

若尚未安装 pnpm，可在已经安装所需 Node.js 的环境中手动运行：

```powershell
npm install --global pnpm@11.19.0
```

安装依赖需要访问包分发服务。应用不会自动安装 Python、Node.js、浏览器或语音工具。先确认本版的 [许可状态](../README.md#文档与许可)，再在获得所需授权的前提下运行项目。

将公开源码放入一个新的目录，在该目录打开 PowerShell。初次安装不要复制其他实例的数据库、配置或录音。

## 2. 安装后端

在项目根目录执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -B scripts/check_runtime_config.py
```

`requirements.lock` 冻结本版 Windows / Python 3.12 的运行和测试依赖；`requirements.txt` 保留声明范围，复现本版请优先使用锁定文件。

配置检查不会创建运行目录、打开文件数据库、联网或调用语音供应商。它检查配置结构、路径边界、Python 版本和内存 SQLite 的 FTS5 trigram 能力，报告不输出配置值。

- `ready` 或 `ready_with_warnings` 且退出码 0：检查通过。未配置语音、缺少可选 ffprobe、目录尚不存在等可能产生告警，不影响普通文字收藏。
- `invalid` 且非零退出码：按稳定错误码处理环境或配置问题，再启动服务。

检查通过不代表路径可写、外部平台连通、供应商鉴权或真实数据升级已经验证。

## 3. 安装前端

```powershell
cd web
pnpm install --frozen-lockfile
cd ..
```

不要用 `npm ci` 替代：本版提供的是 pnpm 锁文件，没有 npm 的 `package-lock.json`。若冻结安装提示锁文件与声明不一致，先核对源码是否来自同一版本，不要通过忽略锁文件继续安装。

## 4. 启动网站

在项目根目录的第一个终端执行：

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.server:app --host 127.0.0.1 --port 8000 --no-access-log
```

启动会创建默认运行目录与数据库，并执行当前版本所需的数据库初始化或迁移。已有数据的升级应先备份；下文说明备份原则。

在第二个终端执行：

```powershell
cd web
pnpm dev --host 127.0.0.1 --port 5173
```

打开 [http://127.0.0.1:5173/](http://127.0.0.1:5173/)。前端 `/api` 代理目标是 `http://127.0.0.1:8000`，因此后端必须同时运行。关闭服务可在各终端按 `Ctrl+C`。

关闭访问日志是受支持启动方式的一部分，避免搜索词等个人内容进入请求日志。自行增加反向代理或日志时，也要去除查询字符串和请求正文。

不要直接打开 `web/index.html`。`pnpm build` 生成静态前端，`pnpm preview` 仅用于静态预览，不提供上述 `/api` 开发代理。本版没有完整的生产托管方案。

## 5. 配置与可选能力

普通收藏无需供应商凭据。应用从**后端进程的环境变量**读取配置，不会自动加载 `.env` 或 `API.txt`。[.env.example](../.env.example) 只是可选字段示例，复制文件本身不会启用配置。不要将真实值写入前端、源码、版本库或公开问题报告。

默认数据位置如下，均相对于项目根目录：

| 配置或资产 | 默认位置 |
| --- | --- |
| `VIDEO_DB_PATH` | `var/video_notes.sqlite3`，保存收藏与个人灵感 |
| 用户封面 | 数据库所在目录下的 `user-covers/`，默认 `var/user-covers/` |
| `COVER_CACHE_ROOT` | `var/cover-cache`，可重建的来源封面缓存 |
| `INSPIRATION_TEMP_ROOT` | `var/inspiration-recordings`，临时主动录音 |
| `VIDEO_TEMP_ROOT` / `VIDEO_UPLOAD_ROOT` / `VIDEO_MEDIA_ROOT` | `var/tmp` / `var/uploads` / `var/media`，含历史兼容模块的受控目录 |

例如，可在启动后端的同一终端为一个新实例指定独立数据库：

```powershell
$env:VIDEO_DB_PATH = 'var/new-library.sqlite3'
.\.venv\Scripts\python.exe -B scripts/check_runtime_config.py
```

随后仍用第 4 节命令启动。不要把路径指向系统根、用户目录根、符号链接、junction 或其他实例的运行目录；配置校验会拒绝危险或重叠的受控根。

### 公开元信息与浏览器补取

粘贴公开链接后，后端按来源尝试读取标题、作者、封面和来源文案。页面需要登录、被限制、内容不兼容或获取失败时，可补充标题并继续普通书签收藏。来源文案不代表完整视频内容。

部分平台的可选补取使用 Playwright 在临时、无登录上下文中渲染公开页面。Python 依赖已包含 Playwright，但它不等于浏览器程序；当前默认使用 `msedge` 通道，需要本机可用的 Microsoft Edge。仅安装 Chromium 不会自动切换此通道。浏览器缺失、超时或失败会降级，应用不自动下载或安装浏览器，不读取个人浏览器资料或 Cookie，不下载来源音视频。

### 主动录音与火山极速版

默认 `INSPIRATION_ASR_PROVIDER=http` 且未配置 `INSPIRATION_ASR_ENDPOINT`，语音转写不可用。文字输入或空灵感收藏不受影响。网站录音入口限定为识别到的手机环境；电脑使用文字输入，无法可靠识别的设备也降级文字。

若自行启用火山录音文件极速版，需要准备：

- 可执行的 ffprobe 与 ffmpeg，通过 PATH 或 `INSPIRATION_FFPROBE_PATH` / `INSPIRATION_FFMPEG_PATH` 指定。
- 在供应商控制台开通对应极速资源，确认费用、数据处理与保留政策。
- 在后端环境中设置 `INSPIRATION_ASR_PROVIDER=volcengine_flash`、自己的 `INSPIRATION_VOLC_APP_ID` 和 `INSPIRATION_VOLC_ACCESS_TOKEN`；此模式下 `INSPIRATION_ASR_ENDPOINT` / `INSPIRATION_ASR_TOKEN` / `INSPIRATION_ASR_MODEL` 必须为空或未设置。

实现使用固定极速端点和 `volc.bigasr.auc_turbo` 资源，采用 APP ID 与 Access Token，不使用 Secret Key，也不会自动读取凭据文件。供应商配置与服务可用性需要由使用者另行核实。

只处理用户在当前交互中主动录制的音频，不读取素材音轨。录音上限为 120 秒、10 MiB，支持的声明类型为 WebM、Ogg、MP4 音频和 WAV；服务端验证真实容器、纯音频和时长，转换为内存 WAV 后最多请求一次，不自动重试。转写先成为可编辑草稿，明确采用并保存后才保留文字。成功、失败和取消会清理临时录音；已经发出的第三方请求不能保证撤回处理或费用。

手机麦克风需要兼容浏览器与安全上下文。电脑上的 `127.0.0.1` 地址不能直接用于手机访问；本版没有附带一键手机 HTTPS / 局域网部署脚本。不要为了录音把无登录后端直接开放给其他设备。手机真机与真实语音端到端仍需单独验证，历史真实录音失败不能视为已解决。

## 6. 测试与构建

在项目根目录执行后端测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

在 `web` 目录执行：

```powershell
pnpm test
pnpm build
```

`build` 包含 TypeScript 检查并生成 `web/dist`；成功构建会替换旧 `dist`，请不要在仍需保留旧部署产物的目录中直接执行。测试使用合成数据与模拟供应商，不能代替真机、真实平台获取率、收费调用或生产部署验收。发布工具专项测试可在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m pytest scripts/tests/test_export_release.py -q
```

## 7. 数据与备份

数据库不是缓存，包含收藏、个人灵感与历史兼容数据。用户补充的封面也是持久资产；`cover-cache` 属于可重建缓存。不要通过删除整个 `var` 来排查问题。

升级已有实例前，停止后端并确认没有写入，再备份数据库及持久资产目录；默认实例可备份完整 `var` 到一个独立、受保护的位置，同时记录应用版本和非敏感配置。不要在后端仍运行时只复制 `.sqlite3` 文件，因为 SQLite 可能有尚未合并的 WAL 数据。需要在线备份时，应使用 SQLite 的一致性备份机制并验证恢复副本。

恢复先在独立目录与同版本程序中验证，保留原数据，不直接覆盖唯一副本。备份中含私人内容，应限制访问；发布源码或反馈问题时不要附上数据库、图片、录音、日志、凭据、Cookie 或完整私密路径。

## 8. 常见情况

| 情况 | 处理 |
| --- | --- |
| 页面空白 | 用 Vite 启动并访问 HTTP 地址，检查 Node.js 版本；不要双击 HTML |
| 前端能开但 API 失败 | 确认后端运行在本机 8000 端口，并从 5173 开发入口访问 |
| 某平台缺作者或封面 | 按提示补充信息或普通书签保存，不能据此推断视频内容 |
| 语音未配置或失败 | 继续文字输入；核对供应商模式、工具和脱敏错误码，不自动重发录音 |
| FTS5 trigram 检查失败 | 使用受支持的 Python 3.12 / SQLite 环境，保留原库，不强行跳过迁移 |

更多限制见 [版本说明](releases/v0.1.0-alpha.md)，隐私与报告方式见 [SECURITY.md](../SECURITY.md)。
