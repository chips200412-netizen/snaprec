# SnapRec 统一后端

本目录包含 FastAPI 接口、公开来源适配、收藏与个人灵感服务，以及 SQLite 存储。Web App 调用统一后端，公开元信息、整理建议、收藏、封面缓存和搜索不在前端重复实现。

本版网站只服务收藏与用户自己的灵感。历史深度解析、字幕、问答、播放器与上传任务模块及兼容接口仍保留；它们不属于当前网站主线，收藏操作不会隐式触发这些处理。MCP / Skill 入口尚未实现。

## 安装与启动

完整安装、配置、语音边界与备份说明见 [从零开始](../docs/getting-started.md)。主要验证环境是 Windows、Python 3.12。复现本版优先使用根目录的 `requirements.lock`。

从项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -B scripts/check_runtime_config.py
.\.venv\Scripts\python.exe -m uvicorn backend.app.server:app --host 127.0.0.1 --port 8000 --no-access-log
```

配置来自后端进程环境，不自动读取 `.env` 或 `API.txt`。配置检查不打开文件数据库、不创建目录、不联网、不执行供应商；实际启动会初始化运行目录与数据库并执行需要的迁移，因此旧实例升级前必须做可恢复备份。

当前没有登录或账户隔离，服务仅适合本机单用户运行。旧兼容接口仍存在，不要把全部 API 直接开放到公网或不受保护的局域网。接口结构可在启动后通过 [本机 API 文档](http://127.0.0.1:8000/docs) 查看，不要使用真实个人数据做公开演示。

## 收藏接口概览

| 接口 | 作用 |
| --- | --- |
| `POST /api/v1/collection-previews` | 安全读取公开元信息或普通书签降级，生成服务端权威预览 |
| `POST /api/v1/collection-items` | 从有效预览原子保存收藏，使用 `Idempotency-Key` 防止重复提交 |
| `GET /api/v1/collection-items` | 本地搜索、筛选与分页 |
| `GET /api/v1/collection-items/{collection_item_id}` | 读取权威收藏详情 |
| `PATCH /api/v1/collection-items/{collection_item_id}` | 以 `expected_revision` 检查并发冲突，提交用户允许编辑的字段 |
| `GET /api/v1/collection-previews/{preview_id}/cover` | 读取与有效预览绑定的同源封面 |
| `GET /api/v1/collection-items/{collection_item_id}/cover` | 读取与收藏绑定的同源封面 |
| `POST /api/v1/user-cover-assets` | 接收用户主动选择的静态图片，生成受控资产 |
| `POST /api/v1/inspiration-transcriptions` | 接收主动录音的 multipart `file`，返回可编辑文字草稿，不自动保存 |
| `/api/v1/collection-import-batches` 及其子路由 | 2–10 项批量预览、逐项审核、确认、取消与恢复 |

来源信息由服务端预览确定，不能用客户端伪造的来源字段代替。重复来源先提示已有收藏，不按标题相似度自动合并；更新出现版本冲突或结果未知时先读取权威结果，不盲目覆盖或重发。

搜索、列表和详情 JSON 读取只使用本地数据；封面二进制请求可能按独立缓存策略访问已校验的来源图片。封面端点不接受任意 URL，抓取失败保留前端占位区域。公开包不带来源未记录的纸纹占位图，部分预览可能显示缺图，不影响普通书签保存；原开发网站的图保持不变，详见 [资产说明](../THIRD_PARTY_NOTICES.md)。公开作者、标题和封面尽力获取，不保证平台长期成功率。

## 服务与存储边界

- `services/capture.py` 与公开元信息相关模块负责输入识别、受控请求、缓存和预览；获取失败仍可保存普通书签。
- `services/collections.py` 与 SQLite repository 负责原子保存、版本冲突检查、编辑和搜索索引一致性。
- `services/collection_imports.py` 负责独立收藏批次，不复用历史深度解析批次。
- `services/cover_cache.py` 与 `services/user_cover_assets.py` 分别处理可重建的来源封面缓存和用户持久图片，两者不能混同清理。
- `services/inspiration.py` 与 `services/volcengine_inspiration.py` 只处理用户主动录音。语音供应商默认未配置；火山极速模式需自配凭据、ffprobe / ffmpeg，最多一次请求，不读来源音轨。

默认数据库是 `var/video_notes.sqlite3`，用户图片位于数据库所在目录的 `user-covers/`，其余默认受控目录见安装指南。历史数据暂保留，不能通过删库或删除整个运行目录来清理旧网站功能。

## 测试

从项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试以合成数据库、媒体夹具和模拟供应商验证行为；不能据此声称真机、真实平台、收费语音服务或公网部署通过。受支持的启动方式必须关闭访问日志，自定义日志不得记录查询字符串、请求正文、凭据或私人内容。

许可状态见 [README](../README.md#文档与许可)，安全边界见 [SECURITY.md](../SECURITY.md)。
