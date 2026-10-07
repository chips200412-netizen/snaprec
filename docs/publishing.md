# 发布初版

本次准备的是 **v0.1.0-alpha 源码快照**，不是部署包。版本限制见[版本说明](releases/v0.1.0-alpha.md)，安装见[入门文档](getting-started.md)。项目作者选择公开展示，但本版暂不添加许可证。第三方依赖的许可证仍独立适用。

## 生成公开副本

在项目根目录运行：

```powershell
python scripts/export_release.py --check
python scripts/export_release.py
```

导出工具只复制 `release-manifest.json` 明确列出的源码、测试、公开文档和应用静态资产。结果打印项目相对目录、压缩包和 SHA-256 校验值；每次创建全新 `var/releases/snaprec-v0.1.0-alpha-*`，不覆盖已有文件。不需要 Git 或任何平台密钥。

发布副本不包含原 `.git` 历史、用户数据库、录音、缓存、证书、密钥、日志、内部交接、内部规格和个人设备启动配置。**不要直接把原开发目录或原 Git 历史推到公开仓库。** `.gitignore` 不能清除已经提交过的历史内容。扫描仅是辅助检查，发布前仍需人工检查文件清单；不要把真实分享文本、收藏、录音或截图当作演示数据提交。

`RELEASE_MANIFEST.json` 记录每个已导出源文件的大小和 SHA-256；不包含绝对本机路径。修改公开副本后应重新生成清单，原清单不再代表修改后的内容。

## 上传到新的 GitHub 仓库

作者已明确选择从公开包排除来源未记录的纸纹占位图，原开发网站保留。清单与导出测试检查该图不被分发，详见 [第三方与资产说明](../THIRD_PARTY_NOTICES.md)。不要从原目录手动补回该图，也不要用真实收藏截图作为演示资产。

这一步由作者明确指定目标账号/仓库后执行。先解压干净副本，在副本内部操作；不要在原开发仓库执行以下命令。不要上传安装后生成的 `node_modules`、`.venv`、`var` 和 `.env`。

```powershell
git init -b main
git add .
git status --short
git diff --cached --stat
git commit -m "chore: initial SnapRec alpha source snapshot"
```

审核暂存内容后，再为副本配置自己创建的远程仓库并首次推送。仓库地址必须由作者指定，本文不预填账号、URL 或访问令牌。不要把访问令牌嵌入远程地址，也不要强制推送原仓库历史。

`.github/workflows/ci.yml` 配置 Windows 上的依赖安装、快照校验、后端测试、无外发冒烟、前端测试与构建，不包含部署、真实供应商调用或数据库迁移。只有实际上传后通过的 Actions 运行才能称为 GitHub 验证通过；本地准备不等于远程运行成功。

工作流使用官方 [checkout](https://github.com/actions/checkout)、[setup-python](https://github.com/actions/setup-python)、[setup-node](https://github.com/actions/setup-node) 文档中的接口。后续更新动作或依赖时，先核实官方说明并重跑本地与远程检查。

## 发布前检查

- 发布说明不承诺全部平台/手机的百分之百成功率。
- `.env.example` 中所有凭据字段为空，真实密钥未打包。
- 不含作者真实收藏库、原音频、登录状态、证书或手机网段设置。
- 文档中不链接内部交接、设计证据或个人路径。
- 发布副本通过测试和构建，安装版本与锁文件一致。
- 原项目数据与历史保留，本次没有部署或重启真实服务。
- 作者明确选择本版暂不添加许可证，不能把公开展示说成已授予开源使用权限。
