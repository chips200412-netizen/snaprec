/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import css from "./collection-import.css?raw";

describe("R2.6-B1 双栏暂存台响应式与可访问性 CSS 合同", () => {
  it("桌面保持 3:7 单文档双栏，索引可 sticky 但没有独立纵向滚动", () => {
    expect(css).toContain("grid-template-columns: minmax(260px, 3fr) minmax(0, 7fr)");
    expect(css).toMatch(/\.batch-import-index\s*\{[^}]*position:\s*sticky/s);
    expect(css).not.toMatch(/\.batch-import-index[^{}]*\{[^}]*overflow-y:\s*(auto|scroll)/s);
    expect(css).not.toMatch(/\.batch-import-review[^{}]*\{[^}]*overflow-y:\s*(auto|scroll)/s);
  });

  it("900px 与 380px 断点使用同一 DOM 互斥列表/详情且约束横向溢出", () => {
    expect(css).toContain("@media (max-width: 900px)");
    expect(css).toContain("@media (max-width: 380px)");
    expect(css).toMatch(/\.batch-import-page\s*\{[^}]*overflow-x:\s*clip/s);
    expect(css).toMatch(/data-view="list"[^}]*\.batch-import-review[\s\S]*data-view="item"[^}]*\.batch-import-index[\s\S]*display:\s*none/);
  });

  it("交互目标、聚焦、状态动效与 reduced-motion 门禁从首切片成立", () => {
    expect(css).toMatch(/\.batch-import-page button\s*\{[^}]*min-width:\s*44px[^}]*min-height:\s*44px/s);
    expect(css).toContain(":focus-visible");
    expect(css).toMatch(/transition:[^;]*(140|150|160|170|180)ms/);
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
    expect(css).toContain("animation-iteration-count: 1 !important");
  });

  it("审核字段与唯一动作 Dock 保持 44px、单文档滚动和移动 safe-area", () => {
    expect(css).toMatch(/\.batch-import-field > input,[\s\S]*min-height:\s*44px/);
    expect(css).toMatch(/\.batch-import-action-dock\s*\{[^}]*position:\s*sticky[^}]*bottom:\s*var\(--batch-import-keyboard-offset,\s*0px\)/s);
    expect(css).not.toMatch(/\.batch-import-action-dock\s*\{[^}]*position:\s*fixed/s);
    expect(css).not.toMatch(/\.batch-import-action-dock\s*\{[^}]*overflow-y:\s*(auto|scroll)/s);
    expect(css).toMatch(/\.batch-import-action-dock\s*\{[^}]*env\(safe-area-inset-bottom\)/s);
    expect(css).not.toMatch(/padding[^;]*--batch-import-keyboard-offset/);
  });

  it("dirty、CAS 与重预览都使用原位非 modal 表面，360px 字段允许收缩", () => {
    expect(css).toContain(".batch-import-inline-gate");
    expect(css).toContain(".batch-import-inline-repreview");
    expect(css).toContain(".batch-import-conflict");
    expect(css).not.toMatch(/\.batch-import-(inline-gate|inline-repreview|conflict)\s*\{[^}]*position:\s*fixed/s);
    expect(css).toMatch(/\.batch-import-field > input,[\s\S]*width:\s*100%[\s\S]*min-width:\s*0/);
    expect(css).toContain("@media (max-width: 380px)");
  });

  it("成功状态在深色主题使用独立高对比色", () => {
    expect(css).toMatch(/:root\[data-theme="dark"\]\s+\.batch-import-review-success\s*\{[^}]*color:\s*#7bd9ad/s);
  });

  it("确认面在桌面只原位替换 Dock，900px 及以下才切成单列表全屏摘要", () => {
    expect(css).toMatch(/\.batch-import-confirm-surface\s*\{[^}]*position:\s*sticky[^}]*bottom:\s*var\(--batch-import-keyboard-offset,\s*0px\)/s);
    expect(css).toMatch(/\.batch-import-page\[data-view="confirm"\]\s+\.batch-import-review\s+\.batch-import-action-dock\s*\{[^}]*display:\s*none/s);
    expect(css).toMatch(/@media \(max-width:\s*900px\)[\s\S]*data-view="confirm"[^}]*\.batch-import-review[^}]*\{[^}]*display:\s*none/s);
    expect(css).toMatch(/@media \(max-width:\s*900px\)[\s\S]*\.batch-import-confirm-surface\s*\{[^}]*position:\s*static[^}]*100dvh[^}]*env\(safe-area-inset-bottom\)/s);
    expect(css).toMatch(/@media \(max-width:\s*900px\)[\s\S]*\.batch-import-confirm-counts\s*\{[^}]*repeat\(2,\s*minmax\(0,\s*1fr\)\)/s);
  });

  it("恢复、取消与核对在移动列表复用唯一 sticky Dock，并跟随键盘 offset 与 safe-area", () => {
    expect(css).toMatch(/@media \(max-width:\s*900px\)[\s\S]*\.batch-import-mobile-batch-dock\s*\{[^}]*position:\s*sticky[^}]*bottom:\s*var\(--batch-import-keyboard-offset,\s*0px\)[^}]*env\(safe-area-inset-bottom\)/s);
    expect(css).toMatch(/\.batch-import-lifecycle-actions-mobile\s*\{[^}]*position:\s*static/s);
    expect(css).not.toMatch(/\.batch-import-mobile-batch-dock\s*\{[^}]*position:\s*fixed/s);
    expect(css).not.toMatch(/\.batch-import-mobile-batch-dock\s*\{[^}]*overflow-y:\s*(auto|scroll)/s);
    expect(css).toMatch(/\.batch-import-lifecycle-actions\s+\.rt-Button\s*\{[^}]*min-height:\s*44px/s);
  });
});
