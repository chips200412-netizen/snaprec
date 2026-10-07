# 第三方依赖与资产说明

本文不是项目许可证，不改变本版暂未授予开源许可的状态。

## 安装依赖

Python 依赖列于 `pyproject.toml`、`requirements.txt` 和冻结的 `requirements.lock`；前端依赖列于 `web/package.json` 与 `web/pnpm-lock.yaml`。本源码包不分发安装后的第三方依赖目录；安装与再分发依赖或构建产物时，须保留并遵守每个依赖自身的版权声明与许可证。依赖自身的许可不代表 SnapRec 项目已授予许可。

## Unicode 数值表

`web/src/unicode-casefold.ts` 与 `unicode-normalize.ts` 的文件头记录：数值映射和范围由 Python 3.12 的算法行为生成，对齐 Unicode 15.0.0。它们不是从某份未记录来源的网页直接复制的代码。可用 `scripts/verify_unicode15_frontend.py` 只读复核。

Python 由 Python Software Foundation 及其贡献者维护；条款见 [Python 3.12 许可说明](https://docs.python.org/3.12/license.html)。Unicode 数据与标准由 Unicode, Inc. 维护；相关版权与许可见 [Unicode 官方声明](https://www.unicode.org/license.txt)。本文不宣称两者为项目背书，也不对上游数据另外授予许可。

## 静态资产

- `web/public/icons/` 三个 PNG 是“瞬时录”字标的栅格图标，历史生成使用本机 KaiTi 字体，不包含或分发字体文件。字体条款参考 [Microsoft 字体 FAQ](https://learn.microsoft.com/en-us/typography/fonts/font-faq)。
- 既有纸纹占位图 `web/public/assets/material-cover-fallback.webp` 没有来源记录。作者已选择不在公开源码快照中分发；原开发网站的文件保持不变，本包未另造替代图。公开副本无封面时仍保留既有占位区域，但纸纹不会显示，部分预览可能显示缺图；不影响普通书签保存。

真实收藏封面、用户主动上传图片、录音和测试过程中缓存的第三方作品不属于公开源码包。
