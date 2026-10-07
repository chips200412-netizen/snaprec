/// <reference types="vite/client" />
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CollectionItem } from "./collection-api";
import editMergeCss from "./collection-edit-merge.css?raw";

vi.mock("./collection-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./collection-api")>();
  return {
    ...actual,
    collectionApi: {
      getItem: vi.fn(),
      updateItem: vi.fn(),
      uploadUserCover: vi.fn(),
      getUserCover: vi.fn(),
      deleteUserCover: vi.fn(),
    },
  };
});

import { CollectionApiError, collectionApi } from "./collection-api";
import { CollectionEditMergeSurface } from "./collection-edit-merge";
import {
  snapshotFromCreate,
  snapshotFromItem,
  readEditMergeSession,
  writeEditMergeSession,
  type EditMergeSession,
} from "./collection-edit-model";
import { ThemeProvider } from "./theme";

const item: CollectionItem = {
  id: "collection-1",
  original_input: "https://example.com/article",
  source_url: "https://example.com/article",
  canonical_url: "https://example.com/article",
  identity_url: "https://example.com/article",
  source_kind: "article",
  platform: "web",
  metadata_status: "recognized",
  metadata: {
    title: { value: "来源标题", source: "open_graph", fetched_at: "2026-08-29T09:00:00Z" },
    author: { value: "公开作者", source: "page_metadata", fetched_at: "2026-08-29T09:00:00Z" },
    cover_url: { value: "https://images.example/cover.jpg", source: "open_graph", fetched_at: "2026-08-29T09:00:00Z" },
    source_copy: { value: "公开摘要", source: "page_description", fetched_at: "2026-08-29T09:00:00Z" },
    platform_tags: [{ value: "公开标签", source: "page_metadata" }],
    warnings: [],
  },
  organization_suggestion: {
    primary_category: "设计",
    secondary_category: "空间",
    tags: ["自然光"],
    basis: "public_metadata",
    method: "deterministic",
    status: "generated",
  },
  created_at: "2026-08-29T09:00:00Z",
  user_title: "已有标题",
  display_title: "已有标题",
  organization_confirmation: {
    primary_category: "设计",
    secondary_category: "空间",
    organization_tags: ["自然光"],
  },
  personal_tags: ["参考"],
  inspiration: {
    id: "inspiration-1",
    collection_item_id: "collection-1",
    content: "已有灵感",
    input_mode: "voice",
    transcription_status: "completed",
    created_at: "2026-08-29T09:01:00Z",
    updated_at: "2026-08-29T09:01:00Z",
  },
  deep_analysis_resource_key: null,
  revision: 7,
  updated_at: "2026-08-29T09:01:00Z",
};

function viewport(desktop = true) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn((query: string) => ({
      matches: desktop ? query.includes("min-width") : query.includes("max-width"),
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
}

function renderSurface(mode: "edit" | "merge", routeEntryId = "route-1", strict = false) {
  const callbacks = {
    onAuthoritativeItem: vi.fn(),
    onSuccessCommitted: vi.fn(),
    onCompleted: vi.fn(),
    onExit: vi.fn(),
    onItemMissingExit: vi.fn(),
    onUnknownDiscard: vi.fn(),
    registerLeaveGuard: vi.fn(),
  };
  const surface = (
    <ThemeProvider>
      <CollectionEditMergeSurface
        itemId={item.id}
        mode={mode}
        routeEntryId={routeEntryId}
        {...callbacks}
      />
    </ThemeProvider>
  );
  const rendered = render(strict ? <StrictMode>{surface}</StrictMode> : surface);
  return { ...callbacks, unmount: rendered.unmount };
}

function mergeSession(routeEntryId = "route-merge"): EditMergeSession {
  return {
    version: 1,
    mode: "merge",
    itemId: item.id,
    routeEntryId,
    returnKind: "review",
    reviewEntryId: "review-1",
    base: null,
    desired: null,
    incoming: snapshotFromCreate({
      preview_id: "preview-new",
      user_title: "此版本标题",
      untitled_confirmed: false,
      organization_confirmation: {
        primary_category: "灵感",
        secondary_category: "案例",
        organization_tags: ["自然光", "Agent"],
      },
      personal_tags: ["参考", "效率"],
      inspiration: {
        content: "此版本灵感",
        input_mode: "text",
        transcription_status: "not_applicable",
      },
    }),
    expectedRevision: null,
    requestState: "draft",
    conflictRecovery: null,
    updatedAt: Date.now(),
  };
}

describe("R2.5 编辑与显式合并表面", () => {
  beforeEach(() => {
    viewport();
    window.sessionStorage.clear();
    vi.mocked(collectionApi.getItem).mockReset();
    vi.mocked(collectionApi.getItem).mockResolvedValue(item);
    vi.mocked(collectionApi.updateItem).mockReset();
    vi.mocked(collectionApi.updateItem).mockResolvedValue({ ...item, revision: 8 });
    vi.mocked(collectionApi.uploadUserCover).mockReset();
    vi.mocked(collectionApi.getUserCover).mockReset();
    vi.mocked(collectionApi.getUserCover).mockResolvedValue(new Blob(["cover"], { type: "image/webp" }));
    vi.mocked(collectionApi.deleteUserCover).mockReset();
  });

  it("编辑先 GET 最新版本，再用 expected_revision 提交完整用户快照", async () => {
    const user = userEvent.setup();
    const updated = { ...item, user_title: "逐字保留的-ＩＡ-标题", display_title: "逐字保留的-ＩＡ-标题", revision: 8 };
    vi.mocked(collectionApi.updateItem).mockResolvedValue(updated);
    const callbacks = renderSurface("edit");

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.clear(title);
    await user.type(title, "逐字保留的-ＩＡ-标题");
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => expect(collectionApi.updateItem).toHaveBeenCalledOnce());
    expect(collectionApi.updateItem).toHaveBeenCalledWith(item.id, {
      expected_revision: 7,
      user_title: "逐字保留的-ＩＡ-标题",
      organization_confirmation: item.organization_confirmation,
      personal_tags: item.personal_tags,
      inspiration: {
        content: "已有灵感",
        input_mode: "voice",
        transcription_status: "completed",
      },
      user_author: null,
      user_cover_asset_id: null,
    }, undefined, undefined);
    await waitFor(() => expect(callbacks.onCompleted).toHaveBeenCalledWith(updated, "detail"));
  });

  it("慢 GET 期间立即把 modal 初始焦点放到任务标题，完成后不重抢用户焦点", async () => {
    let resolveItem!: (value: CollectionItem) => void;
    vi.mocked(collectionApi.getItem).mockReturnValueOnce(new Promise((resolve) => { resolveItem = resolve; }));
    renderSurface("edit", "pending-focus");

    const heading = screen.getByRole("heading", { name: "编辑收藏" });
    await waitFor(() => expect(heading).toHaveFocus());
    const close = screen.getByRole("button", { name: "关闭编辑" });
    close.focus();
    expect(close).toHaveFocus();
    await act(async () => { resolveItem(item); });
    expect(await screen.findByRole("textbox", { name: "自定义标题" })).toBeInTheDocument();
    expect(close).toHaveFocus();
  });

  it("桌面 focus trap 会把标题或 surface 外的 Tab/Shift+Tab 收束到首尾控件", async () => {
    const user = userEvent.setup();
    renderSurface("edit", "focus-trap-boundary");
    const heading = screen.getByRole("heading", { name: "编辑收藏" });
    await screen.findByRole("textbox", { name: "自定义标题" });
    const first = screen.getByRole("button", { name: "关闭编辑" });
    const last = screen.getByRole("button", { name: "取消" });

    heading.focus();
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(last).toHaveFocus();
    heading.focus();
    await user.keyboard("{Tab}");
    expect(first).toHaveFocus();

    document.body.tabIndex = -1;
    document.body.focus();
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(last).toHaveFocus();
    document.body.removeAttribute("tabindex");
  });

  it.each([
    ["一级分类", "primary_category", "edit-primary-category-error"],
    ["二级分类", "secondary_category", "edit-secondary-category-error"],
  ] as const)("%s 以 Unicode code point 执行 64/65 边界并就地关联错误", async (label, field, errorId) => {
    const user = userEvent.setup();
    const astral = "😀";
    renderSurface("edit", `category-${field}`);

    const input = await screen.findByRole("textbox", { name: label });
    await user.clear(input);
    await user.type(input, astral.repeat(64));
    expect(input).toHaveAttribute("aria-invalid", "false");
    expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled();

    await user.type(input, astral);
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAttribute("aria-describedby", errorId);
    expect(document.getElementById(errorId)).toHaveTextContent("最多 64 个字符");
    input.focus();
    expect(input).toHaveFocus();
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
  });

  it("标题与灵感超限字段暴露 aria-invalid，灵感同时关联计数和唯一 live error", async () => {
    renderSurface("edit", "field-a11y-errors");
    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    fireEvent.change(title, { target: { value: "题".repeat(501) } });
    expect(title).toHaveAttribute("aria-invalid", "true");
    expect(title).toHaveAttribute("aria-describedby", "edit-user-title-help edit-user-title-error");
    expect(screen.getAllByRole("alert").filter((node) => node.textContent?.includes("自定义标题最多 500 个字符"))).toHaveLength(1);

    fireEvent.change(title, { target: { value: "合法标题" } });
    const inspiration = screen.getByRole("textbox", { name: "我的灵感" });
    fireEvent.change(inspiration, { target: { value: "灵".repeat(4001) } });
    expect(inspiration).toHaveAttribute("aria-invalid", "true");
    expect(inspiration).toHaveAttribute("aria-describedby", "edit-inspiration-count edit-inspiration-error");
    expect(document.getElementById("edit-inspiration-count")).toHaveTextContent("4001 / 4000");
    expect(screen.getAllByRole("alert").filter((node) => node.textContent?.includes("我的灵感最多 4000 个字符"))).toHaveLength(1);
    expect(document.querySelector(".edit-merge-error-summary")).not.toHaveAttribute("role");
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
  });

  it("PATCH 200 的 revision 未推进时不接受响应，先 GET 对账且零二次 PATCH", async () => {
    const user = userEvent.setup();
    const staleResponse = { ...item, user_title: "提交标题", display_title: "提交标题" };
    vi.mocked(collectionApi.getItem)
      .mockResolvedValueOnce(item)
      .mockRejectedValueOnce(new CollectionApiError("暂时无法对账", "NETWORK_ERROR", 0));
    vi.mocked(collectionApi.updateItem).mockResolvedValueOnce(staleResponse);
    const callbacks = renderSurface("edit", "stale-patch-response");

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.clear(title);
    await user.type(title, "提交标题");
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    expect(await screen.findByRole("button", { name: "再次核验" })).toBeInTheDocument();
    expect(callbacks.onSuccessCommitted).not.toHaveBeenCalled();
    expect(callbacks.onCompleted).not.toHaveBeenCalled();
    expect(collectionApi.getItem).toHaveBeenCalledTimes(2);
    expect(collectionApi.updateItem).toHaveBeenCalledOnce();
  });

  it("PATCH 200 快照不匹配时忽略该响应，GET 到冻结提交快照后才权威成功", async () => {
    const user = userEvent.setup();
    const mismatched = { ...item, user_title: "错误响应 C", display_title: "错误响应 C", revision: 8 };
    const reached = { ...item, user_title: "冻结提交 B", display_title: "冻结提交 B", revision: 8 };
    vi.mocked(collectionApi.getItem)
      .mockResolvedValueOnce(item)
      .mockResolvedValueOnce(reached);
    vi.mocked(collectionApi.updateItem).mockResolvedValueOnce(mismatched);
    const callbacks = renderSurface("edit", "mismatched-patch-response");

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.clear(title);
    await user.type(title, "冻结提交 B");
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => expect(callbacks.onSuccessCommitted).toHaveBeenCalledWith(reached, "detail"));
    await waitFor(() => expect(callbacks.onCompleted).toHaveBeenCalledWith(reached, "detail"));
    expect(callbacks.onSuccessCommitted).not.toHaveBeenCalledWith(mismatched, "detail");
    expect(collectionApi.getItem).toHaveBeenCalledTimes(2);
    expect(collectionApi.updateItem).toHaveBeenCalledOnce();
  });

  it("成功响应同步收束一次，160ms 只延迟视觉退出且 unmount 不撤销已完成副作用", async () => {
    const updated = { ...item, user_title: "同步收束", display_title: "同步收束", revision: 8 };
    let resolveUpdate!: (value: CollectionItem) => void;
    vi.mocked(collectionApi.updateItem).mockReturnValueOnce(new Promise((resolve) => { resolveUpdate = resolve; }));
    const callbacks = renderSurface("edit", "synchronous-commit");
    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    fireEvent.change(title, { target: { value: "同步收束" } });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(collectionApi.updateItem).toHaveBeenCalledOnce());

    vi.useFakeTimers();
    try {
      await act(async () => { resolveUpdate(updated); });
      expect(callbacks.onSuccessCommitted).toHaveBeenCalledOnce();
      expect(callbacks.onSuccessCommitted).toHaveBeenCalledWith(updated, "detail");
      expect(callbacks.onCompleted).not.toHaveBeenCalled();
      const successStatus = screen.getByText("收藏内容已更新。").closest<HTMLElement>(".edit-merge-status");
      expect(successStatus).toHaveAttribute("tabindex", "-1");
      expect(successStatus).toHaveFocus();
      expect(screen.getByRole("dialog", { name: "编辑收藏" })).toContainElement(document.activeElement as HTMLElement);
      fireEvent.keyDown(document, { key: "Tab" });
      expect(successStatus).toHaveFocus();
      fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
      expect(successStatus).toHaveFocus();
      callbacks.unmount();
      act(() => vi.advanceTimersByTime(200));
      expect(callbacks.onSuccessCommitted).toHaveBeenCalledOnce();
      expect(callbacks.onCompleted).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("受控编辑保留多词空格与灵感前导换行，预览和 PATCH 使用同一最终规范快照", async () => {
    const user = userEvent.setup();
    const expectedInspiration = "\n前导灵感\n";
    vi.mocked(collectionApi.updateItem).mockResolvedValue({
      ...item,
      user_title: "多 词 标题",
      display_title: "多 词 标题",
      organization_confirmation: {
        ...item.organization_confirmation,
        primary_category: "AI 创 意",
      },
      inspiration: {
        ...item.inspiration!,
        content: expectedInspiration,
        input_mode: "text",
        transcription_status: "not_applicable",
      },
      revision: 8,
    });
    renderSurface("edit", "raw-editor");

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    const primary = screen.getByRole("textbox", { name: "一级分类" });
    const inspiration = screen.getByRole("textbox", { name: "我的灵感" });
    expect(title).not.toHaveAttribute("maxlength");
    expect(primary).not.toHaveAttribute("maxlength");
    expect(screen.getByRole("textbox", { name: "二级分类" })).not.toHaveAttribute("maxlength");
    await user.clear(title);
    await user.type(title, "  多 词 标题  ");
    await user.clear(primary);
    await user.type(primary, "  ＡＩ 创 意  ");
    await user.clear(inspiration);
    await user.type(inspiration, "{enter}前导灵感{enter}");

    expect(title).toHaveValue("  多 词 标题  ");
    expect(primary).toHaveValue("  ＡＩ 创 意  ");
    expect(inspiration).toHaveValue(expectedInspiration);
    expect(screen.getByText("多 词 标题", { selector: ".final-snapshot-preview dd" })).toBeInTheDocument();
    expect(screen.getByText("AI 创 意 / 空间", { selector: ".final-snapshot-preview dd" })).toBeInTheDocument();
    expect(screen.getByText((_, element) => element?.tagName === "DD" && element.textContent === expectedInspiration)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(collectionApi.updateItem).toHaveBeenCalledOnce());
    expect(vi.mocked(collectionApi.updateItem).mock.calls[0][1]).toMatchObject({
      user_title: "多 词 标题",
      organization_confirmation: {
        ...item.organization_confirmation,
        primary_category: "AI 创 意",
      },
      inspiration: {
        content: expectedInspiration,
        input_mode: "text",
        transcription_status: "not_applicable",
      },
    });
  });

  it("StrictMode 重放 effect 后仍处理 PATCH 成功响应并完成编辑", async () => {
    const user = userEvent.setup();
    const updated = { ...item, user_title: "严格模式标题", display_title: "严格模式标题", revision: 8 };
    let resolveUpdate!: (value: CollectionItem) => void;
    vi.mocked(collectionApi.updateItem).mockReturnValue(new Promise((resolve) => {
      resolveUpdate = resolve;
    }));
    const callbacks = renderSurface("edit", "route-strict", true);

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.clear(title);
    await user.type(title, "严格模式标题");
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    expect(screen.getByRole("button", { name: "正在保存" })).toBeDisabled();

    await act(async () => resolveUpdate(updated));

    await waitFor(() => expect(callbacks.onCompleted).toHaveBeenCalledWith(updated, "detail"));
    expect(screen.queryByRole("button", { name: "正在保存" })).not.toBeInTheDocument();
  });

  it("合并默认保留已有标量并合并标签，显式选新标题与追加灵感后原子保存", async () => {
    const user = userEvent.setup();
    const routeEntryId = "route-merge";
    expect(writeEditMergeSession(mergeSession(routeEntryId))).toBe(true);
    renderSurface("merge", routeEntryId);

    expect(await screen.findByText("默认保留已有标量和灵感，标签已按规范身份去重合并。")).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: /使用此版本此版本标题/ }));
    await user.click(screen.getByRole("radio", { name: "追加到现有内容" }));
    await user.click(screen.getByRole("button", { name: "确认合并" }));

    await waitFor(() => expect(collectionApi.updateItem).toHaveBeenCalledOnce());
    expect(collectionApi.updateItem).toHaveBeenCalledWith(item.id, {
      expected_revision: 7,
      user_title: "此版本标题",
      organization_confirmation: {
        primary_category: "设计",
        secondary_category: "空间",
        organization_tags: ["自然光", "Agent"],
      },
      personal_tags: ["参考", "效率"],
      inspiration: {
        content: "已有灵感\n\n此版本灵感",
        input_mode: "text",
        transcription_status: "not_applicable",
      },
      user_author: null,
      user_cover_asset_id: null,
    }, undefined, undefined);
  });

  it("CQ3-S1 合并默认分别保留旧作者与旧封面，显式选择此版本后携 claim 原子保存", async () => {
    const user = userEvent.setup();
    const routeEntryId = "merge-user-supplements";
    const existing = { ...item, user_author: "现有作者", user_cover_asset_id: "a".repeat(32) };
    const stored = mergeSession(routeEntryId);
    stored.incoming = {
      ...stored.incoming!,
      user_author: "此版本作者",
      user_cover_asset_id: "b".repeat(32),
    };
    stored.userCoverClaimToken = "claim-token-long-enough";
    vi.mocked(collectionApi.getItem).mockResolvedValue(existing);
    expect(writeEditMergeSession(stored)).toBe(true);
    renderSurface("merge", routeEntryId);

    const existingAuthor = await screen.findByRole("radio", { name: /现有内容现有作者/ });
    const existingCover = screen.getByRole("radio", { name: /现有内容.*用户补充封面/ });
    expect(existingAuthor).toBeChecked();
    expect(existingCover).toBeChecked();
    await user.click(screen.getByRole("radio", { name: /使用此版本此版本作者/ }));
    await user.click(screen.getByRole("radio", { name: /使用此版本.*用户补充封面/ }));
    await user.click(screen.getByRole("button", { name: "确认合并" }));

    await waitFor(() => expect(collectionApi.updateItem).toHaveBeenCalledOnce());
    expect(collectionApi.updateItem).toHaveBeenCalledWith(item.id, expect.objectContaining({
      user_author: "此版本作者",
      user_cover_asset_id: "b".repeat(32),
    }), undefined, "claim-token-long-enough");
  });

  it("长灵感比较完整呈现原文，由外层 sidecar 正文承担滚动", async () => {
    const routeEntryId = "long-inspiration";
    const existingContent = `现有段落起点\n${"现有长灵感".repeat(180)}\n现有段落终点`;
    const incomingContent = `此版本起点\n${"此版本长灵感".repeat(180)}\n此版本终点`;
    const existing = {
      ...item,
      inspiration: { ...item.inspiration!, content: existingContent },
    };
    const stored = mergeSession(routeEntryId);
    stored.incoming = {
      ...stored.incoming!,
      inspiration: { content: incomingContent, input_mode: "text", transcription_status: "not_applicable" },
    };
    vi.mocked(collectionApi.getItem).mockResolvedValue(existing);
    expect(writeEditMergeSession(stored)).toBe(true);
    renderSurface("merge", routeEntryId);

    const existingParagraph = await screen.findByText((_, element) => (
      element?.tagName === "P" && element.textContent === existingContent
    ));
    const incomingParagraph = screen.getByText((_, element) => (
      element?.tagName === "P" && element.textContent === incomingContent
    ));
    expect(existingParagraph.tagName).toBe("P");
    expect(incomingParagraph.tagName).toBe("P");
    expect(existingParagraph.closest(".edit-merge-scroll")).toBeInTheDocument();
    expect(incomingParagraph.closest(".edit-merge-scroll")).toBeInTheDocument();
  });

  it("身份摘要封面只从同源代理回退一次，fallback 失败后停止循环", async () => {
    renderSurface("edit", "cover-fallback");

    const identityTitle = await screen.findByRole("heading", { name: "正在修改这条收藏" });
    const cover = identityTitle.closest(".immutable-identity")?.querySelector("img");
    expect(cover).toBeInstanceOf(HTMLImageElement);
    expect(cover).toHaveAttribute("src", `/api/v1/collection-items/${item.id}/cover?cache=only`);

    fireEvent.error(cover!);
    expect(cover).toHaveAttribute("src", "/assets/material-cover-fallback.webp");
    fireEvent.error(cover!);
    expect(cover).toHaveAttribute("src", "/assets/material-cover-fallback.webp");
    expect(cover).not.toHaveAttribute("src", item.metadata.cover_url.value);
  });

  it("未添加的标签草稿阻断保存和离页，继续编辑保留输入，添加后提交真实快照", async () => {
    const user = userEvent.setup();
    const updated = {
      ...item,
      organization_confirmation: {
        ...item.organization_confirmation,
        organization_tags: ["自然光", "待添加"],
      },
      revision: 8,
    };
    vi.mocked(collectionApi.updateItem).mockResolvedValue(updated);
    renderSurface("edit", "tag-draft");

    const tagInput = await screen.findByRole("textbox", { name: "整理标签" });
    await user.type(tagInput, "  待添加  ");
    expect(screen.getByText("先添加或清空尚未提交标签。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "关闭编辑" }));
    expect(await screen.findByRole("dialog", { name: "放弃未保存的内容吗？" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(tagInput).toHaveValue("  待添加  ");

    await user.click(screen.getByRole("button", { name: "添加" }));
    expect(tagInput).toHaveValue("");
    const save = screen.getByRole("button", { name: "保存修改" });
    expect(save).toBeEnabled();
    await user.click(save);

    await waitFor(() => expect(collectionApi.updateItem).toHaveBeenCalledOnce());
    expect(collectionApi.updateItem).toHaveBeenCalledWith(item.id, expect.objectContaining({
      expected_revision: 7,
      organization_confirmation: {
        ...item.organization_confirmation,
        organization_tags: ["自然光", "待添加"],
      },
    }), undefined, undefined);
  });

  it("409 后只要求选择真正冲突字段，并以最新 revision 再保存", async () => {
    const user = userEvent.setup();
    const latest = { ...item, user_title: "其他位置标题", display_title: "其他位置标题", revision: 8 };
    const resolved = { ...latest, user_title: "我的标题", display_title: "我的标题", revision: 9 };
    vi.mocked(collectionApi.getItem)
      .mockResolvedValueOnce(item)
      .mockResolvedValueOnce(latest);
    vi.mocked(collectionApi.updateItem)
      .mockRejectedValueOnce(new CollectionApiError(
        "收藏已在其他位置更新",
        "COLLECTION_REVISION_CONFLICT",
        409,
        null,
        undefined,
        7,
        8,
      ))
      .mockResolvedValueOnce(resolved);
    renderSurface("edit");

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.clear(title);
    await user.type(title, "我的标题");
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    expect(await screen.findByRole("heading", { name: "重新选择冲突字段" })).toBeInTheDocument();
    expect(screen.getAllByRole("group")).toHaveLength(1);
    await user.click(screen.getByRole("radio", { name: /保留我的内容我的标题/ }));
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => expect(collectionApi.updateItem).toHaveBeenCalledTimes(2));
    expect(vi.mocked(collectionApi.updateItem).mock.calls[1][1]).toMatchObject({
      expected_revision: 8,
      user_title: "我的标题",
    });
  });

  it("409 current_revision 建立恢复下界，低 revision GET 不降级草稿或产生二次 PATCH", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionApi.getItem)
      .mockResolvedValueOnce(item)
      .mockResolvedValueOnce(item);
    vi.mocked(collectionApi.updateItem).mockRejectedValueOnce(new CollectionApiError(
      "收藏已在其他位置更新",
      "COLLECTION_REVISION_CONFLICT",
      409,
      item.id,
      undefined,
      7,
      8,
    ));
    renderSurface("edit", "stale-after-conflict");

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.clear(title);
    await user.type(title, "我的标题");
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    expect(await screen.findByRole("button", { name: "重新比较" })).toBeInTheDocument();
    expect(readEditMergeSession("edit", item.id, "stale-after-conflict")?.expectedRevision).toBe(8);
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(collectionApi.updateItem).toHaveBeenCalledOnce();
  });

  it("普通恢复与 outcome 对账都拒绝低于 expectedRevision 的权威响应", async () => {
    const base = snapshotFromItem(item);
    const desired = { ...base, user_title: "待恢复标题" };
    const ordinaryEntry = "ordinary-stale-authority";
    expect(writeEditMergeSession({
      version: 1,
      mode: "edit",
      itemId: item.id,
      routeEntryId: ordinaryEntry,
      returnKind: "detail",
      reviewEntryId: null,
      base,
      desired,
      incoming: null,
      expectedRevision: 8,
      requestState: "draft",
      conflictRecovery: null,
      updatedAt: Date.now(),
    })).toBe(true);
    const ordinary = renderSurface("edit", ordinaryEntry);
    expect(await screen.findByRole("button", { name: "重新比较" })).toBeInTheDocument();
    expect(readEditMergeSession("edit", item.id, ordinaryEntry)?.expectedRevision).toBe(8);
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
    ordinary.unmount();

    window.sessionStorage.clear();
    vi.mocked(collectionApi.getItem).mockClear();
    vi.mocked(collectionApi.getItem).mockResolvedValueOnce(item);
    const outcomeEntry = "outcome-stale-authority";
    expect(writeEditMergeSession({
      version: 1,
      mode: "edit",
      itemId: item.id,
      routeEntryId: outcomeEntry,
      returnKind: "detail",
      reviewEntryId: null,
      base,
      desired,
      incoming: null,
      expectedRevision: 8,
      requestState: "outcome_unknown",
      conflictRecovery: null,
      updatedAt: Date.now(),
    })).toBe(true);
    renderSurface("edit", outcomeEntry);
    expect(await screen.findByRole("button", { name: "再次核验" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "自定义标题" })).toBeDisabled();
    expect(readEditMergeSession("edit", item.id, outcomeEntry)?.expectedRevision).toBe(8);
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
  });

  it("409 后 GET 已达到提交快照会直接权威成功且零二次 PATCH", async () => {
    const user = userEvent.setup();
    const reached = { ...item, user_title: "已达成标题", display_title: "已达成标题", revision: 8 };
    vi.mocked(collectionApi.getItem)
      .mockResolvedValueOnce(item)
      .mockResolvedValueOnce(reached);
    vi.mocked(collectionApi.updateItem).mockRejectedValueOnce(new CollectionApiError(
      "收藏已在其他位置更新",
      "COLLECTION_REVISION_CONFLICT",
      409,
      item.id,
      undefined,
      7,
      8,
    ));
    const callbacks = renderSurface("edit", "conflict-intent-reached");

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.clear(title);
    await user.type(title, "已达成标题");
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => expect(callbacks.onCompleted).toHaveBeenCalledWith(reached, "detail"));
    expect(collectionApi.updateItem).toHaveBeenCalledOnce();
    expect(readEditMergeSession("edit", item.id, "conflict-intent-reached")).toBeNull();
  });

  it("dirty 草稿刷新时权威等于 desired 会直接完成，pristine 才保留普通打开", async () => {
    const base = snapshotFromItem(item);
    const desired = { ...base, user_title: "已由别处写入" };
    const reached = { ...item, user_title: "已由别处写入", display_title: "已由别处写入", revision: 8 };
    const routeEntryId = "draft-intent-reached";
    expect(writeEditMergeSession({
      version: 1,
      mode: "edit",
      itemId: item.id,
      routeEntryId,
      returnKind: "detail",
      reviewEntryId: null,
      base,
      desired,
      incoming: null,
      expectedRevision: 7,
      requestState: "draft",
      conflictRecovery: null,
      updatedAt: Date.now(),
    })).toBe(true);
    vi.mocked(collectionApi.getItem).mockResolvedValueOnce(reached);
    const callbacks = renderSurface("edit", routeEntryId);

    await waitFor(() => expect(callbacks.onCompleted).toHaveBeenCalledWith(reached, "detail"));
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
    expect(readEditMergeSession("edit", item.id, routeEntryId)).toBeNull();
  });

  it("dirty 草稿的字段意图已达成且只多出非冲突远端更新时也直接权威成功", async () => {
    const base = snapshotFromItem(item);
    const desired = { ...base, user_title: "意图标题" };
    const reachedWithRemote = {
      ...item,
      user_title: "意图标题",
      display_title: "意图标题",
      organization_confirmation: { ...item.organization_confirmation, primary_category: "远端新分类" },
      revision: 8,
    };
    const routeEntryId = "rebased-intent-reached";
    expect(writeEditMergeSession({
      version: 1,
      mode: "edit",
      itemId: item.id,
      routeEntryId,
      returnKind: "detail",
      reviewEntryId: null,
      base,
      desired,
      incoming: null,
      expectedRevision: 7,
      requestState: "draft",
      conflictRecovery: null,
      updatedAt: Date.now(),
    })).toBe(true);
    vi.mocked(collectionApi.getItem).mockResolvedValueOnce(reachedWithRemote);
    const callbacks = renderSurface("edit", routeEntryId);

    await waitFor(() => expect(callbacks.onCompleted).toHaveBeenCalledWith(reachedWithRemote, "detail"));
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
  });

  it("未决冲突刷新后仍恢复 recovery gate；权威再换代时重新三方比较且零二次 PATCH", async () => {
    const user = userEvent.setup();
    const routeEntryId = "conflict-refresh-unresolved";
    const latest = { ...item, user_title: "其他位置标题", display_title: "其他位置标题", revision: 8 };
    const newer = {
      ...latest,
      organization_confirmation: { ...latest.organization_confirmation, primary_category: "新远端分类" },
      revision: 9,
    };
    vi.mocked(collectionApi.getItem)
      .mockResolvedValueOnce(item)
      .mockResolvedValueOnce(latest)
      .mockResolvedValueOnce(latest)
      .mockResolvedValueOnce(newer);
    vi.mocked(collectionApi.updateItem).mockRejectedValueOnce(new CollectionApiError(
      "收藏已在其他位置更新",
      "COLLECTION_REVISION_CONFLICT",
      409,
      null,
      undefined,
      7,
      8,
    ));
    const first = renderSurface("edit", routeEntryId);

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.clear(title);
    await user.type(title, "我的标题");
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    expect(await screen.findByRole("heading", { name: "重新选择冲突字段" })).toBeInTheDocument();
    first.unmount();

    const second = renderSurface("edit", routeEntryId);
    expect(await screen.findByRole("heading", { name: "重新选择冲突字段" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /使用最新内容其他位置标题/ })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(collectionApi.updateItem).toHaveBeenCalledOnce();
    second.unmount();

    renderSurface("edit", routeEntryId);
    expect(await screen.findByRole("heading", { name: "重新选择冲突字段" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /使用最新内容其他位置标题/ })).not.toBeChecked();
    expect(screen.getByText("新远端分类 / 空间", { selector: ".final-snapshot-preview dd" })).toBeInTheDocument();
    expect(readEditMergeSession("edit", item.id, routeEntryId)?.expectedRevision).toBe(9);
    expect(collectionApi.updateItem).toHaveBeenCalledOnce();
  });

  it("部分冲突选择刷新后只恢复已选项，剩余字段继续阻断二次 PATCH", async () => {
    const user = userEvent.setup();
    const routeEntryId = "conflict-refresh-partial";
    const latest = {
      ...item,
      user_title: "其他位置标题",
      display_title: "其他位置标题",
      organization_confirmation: { ...item.organization_confirmation, primary_category: "远端分类" },
      revision: 8,
    };
    vi.mocked(collectionApi.getItem)
      .mockResolvedValueOnce(item)
      .mockResolvedValueOnce(latest)
      .mockResolvedValueOnce(latest);
    vi.mocked(collectionApi.updateItem).mockRejectedValueOnce(new CollectionApiError(
      "收藏已在其他位置更新",
      "COLLECTION_REVISION_CONFLICT",
      409,
      null,
      undefined,
      7,
      8,
    ));
    const first = renderSurface("edit", routeEntryId);

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    const primary = screen.getByRole("textbox", { name: "一级分类" });
    await user.clear(title);
    await user.type(title, "我的标题");
    await user.clear(primary);
    await user.type(primary, "我的分类");
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    expect(await screen.findByRole("heading", { name: "重新选择冲突字段" })).toBeInTheDocument();
    const acceptedLatestTitle = screen.getByRole("radio", { name: /使用最新内容其他位置标题/ });
    await user.click(acceptedLatestTitle);
    expect(screen.getByText("还有 1 个冲突字段待选择。")).toBeInTheDocument();
    first.unmount();

    renderSurface("edit", routeEntryId);
    expect(await screen.findByRole("heading", { name: "重新选择冲突字段" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /使用最新内容其他位置标题/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /使用最新内容远端分类/ })).not.toBeChecked();
    expect(screen.getByText("还有 1 个冲突字段待选择。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(collectionApi.updateItem).toHaveBeenCalledOnce();
  });

  it("authority 换代时已选 latest 跟随新值，未决且值未变的字段仍保持 gate", async () => {
    const routeEntryId = "conflict-selected-latest-advance";
    const latest = {
      ...item,
      user_title: "远端标题 v8",
      display_title: "远端标题 v8",
      organization_confirmation: { ...item.organization_confirmation, primary_category: "远端分类" },
      revision: 8,
    };
    const newer = { ...latest, user_title: "远端标题 v9", display_title: "远端标题 v9", revision: 9 };
    const base = snapshotFromItem(latest);
    const mine = {
      ...base,
      user_title: "我的标题",
      organization_confirmation: { ...base.organization_confirmation, primary_category: "我的分类" },
    };
    const desired = { ...mine, user_title: base.user_title };
    expect(writeEditMergeSession({
      version: 1,
      mode: "edit",
      itemId: item.id,
      routeEntryId,
      returnKind: "detail",
      reviewEntryId: null,
      base,
      desired,
      incoming: null,
      expectedRevision: 8,
      requestState: "draft",
      conflictRecovery: {
        latest: base,
        mine,
        conflictFields: ["user_title", "primary_category"],
        choices: { user_title: "latest" },
        latestRevision: 8,
      },
      updatedAt: Date.now(),
    })).toBe(true);
    vi.mocked(collectionApi.getItem).mockResolvedValueOnce(newer);
    renderSurface("edit", routeEntryId);

    expect(await screen.findByRole("heading", { name: "重新选择冲突字段" })).toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: /远端标题 v9/ })).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /使用最新内容远端分类/ })).not.toBeChecked();
    expect(screen.getByText("远端标题 v9", { selector: ".final-snapshot-preview dd" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(readEditMergeSession("edit", item.id, routeEntryId)?.conflictRecovery?.conflictFields)
      .toEqual(["primary_category"]);
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
  });

  it("authority 换代时已选 mine 按新 base 重做三方并重新要求选择", async () => {
    const routeEntryId = "conflict-selected-mine-advance";
    const latest = { ...item, user_title: "远端标题 v8", display_title: "远端标题 v8", revision: 8 };
    const newer = { ...latest, user_title: "远端标题 v9", display_title: "远端标题 v9", revision: 9 };
    const base = snapshotFromItem(latest);
    const mine = { ...base, user_title: "我的标题" };
    expect(writeEditMergeSession({
      version: 1,
      mode: "edit",
      itemId: item.id,
      routeEntryId,
      returnKind: "detail",
      reviewEntryId: null,
      base,
      desired: mine,
      incoming: null,
      expectedRevision: 8,
      requestState: "draft",
      conflictRecovery: {
        latest: base,
        mine,
        conflictFields: ["user_title"],
        choices: { user_title: "mine" },
        latestRevision: 8,
      },
      updatedAt: Date.now(),
    })).toBe(true);
    vi.mocked(collectionApi.getItem).mockResolvedValueOnce(newer);
    renderSurface("edit", routeEntryId);

    expect(await screen.findByRole("heading", { name: "重新选择冲突字段" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /保留我的内容我的标题/ })).not.toBeChecked();
    expect(screen.getByRole("radio", { name: /使用最新内容远端标题 v9/ })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(readEditMergeSession("edit", item.id, routeEntryId)?.conflictRecovery?.choices).toEqual({});
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
  });

  it("冲突选择后普通合并控件改同字段会清除旧选择，再确认后按新 UI 快照提交", async () => {
    const user = userEvent.setup();
    const routeEntryId = "merge-conflict-choice";
    const latest = { ...item, user_title: "其他位置标题", display_title: "其他位置标题", revision: 8 };
    const resolved = { ...latest, user_title: "此版本标题", display_title: "此版本标题", revision: 9 };
    expect(writeEditMergeSession(mergeSession(routeEntryId))).toBe(true);
    vi.mocked(collectionApi.getItem)
      .mockResolvedValueOnce(item)
      .mockResolvedValueOnce(latest);
    vi.mocked(collectionApi.updateItem)
      .mockRejectedValueOnce(new CollectionApiError(
        "收藏已在其他位置更新",
        "COLLECTION_REVISION_CONFLICT",
        409,
        null,
        undefined,
        7,
        8,
      ))
      .mockResolvedValueOnce(resolved);
    renderSurface("merge", routeEntryId);

    await user.click(await screen.findByRole("radio", { name: /使用此版本此版本标题/ }));
    await user.click(screen.getByRole("button", { name: "确认合并" }));
    const latestChoice = await screen.findByRole("radio", { name: /使用最新内容其他位置标题/ });
    await user.click(latestChoice);
    expect(latestChoice).toBeChecked();

    await user.click(screen.getByRole("radio", { name: /使用此版本此版本标题/ }));
    expect(latestChoice).not.toBeChecked();
    expect(screen.getByText("还有 1 个冲突字段待选择。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认合并" })).toBeDisabled();

    await user.click(screen.getByRole("radio", { name: /保留我的内容此版本标题/ }));
    await user.click(screen.getByRole("button", { name: "确认合并" }));
    await waitFor(() => expect(collectionApi.updateItem).toHaveBeenCalledTimes(2));
    expect(vi.mocked(collectionApi.updateItem).mock.calls[1][1]).toMatchObject({
      expected_revision: 8,
      user_title: "此版本标题",
    });
  });

  it("灵感冲突选择 latest/mine 会同步下方合并方式，并按所见最终文本提交", async () => {
    const user = userEvent.setup();
    const routeEntryId = "merge-inspiration-conflict";
    const latest = {
      ...item,
      inspiration: { ...item.inspiration!, content: "远端最新灵感" },
      revision: 8,
    };
    const resolved = {
      ...latest,
      inspiration: { ...item.inspiration!, content: "此版本灵感", input_mode: "text" as const, transcription_status: "not_applicable" as const },
      revision: 9,
    };
    expect(writeEditMergeSession(mergeSession(routeEntryId))).toBe(true);
    vi.mocked(collectionApi.getItem)
      .mockResolvedValueOnce(item)
      .mockResolvedValueOnce(latest);
    vi.mocked(collectionApi.updateItem)
      .mockRejectedValueOnce(new CollectionApiError(
        "收藏已在其他位置更新",
        "COLLECTION_REVISION_CONFLICT",
        409,
        null,
        undefined,
        7,
        8,
      ))
      .mockResolvedValueOnce(resolved);
    renderSurface("merge", routeEntryId);

    await user.click(await screen.findByRole("radio", { name: "替换为此版本内容" }));
    await user.click(screen.getByRole("button", { name: "确认合并" }));
    const latestChoice = await screen.findByRole("radio", { name: /使用最新内容远端最新灵感/ });
    const mineChoice = screen.getByRole("radio", { name: /保留我的内容此版本灵感/ });

    await user.click(latestChoice);
    expect(screen.getByRole("radio", { name: "保留现有内容" })).toBeChecked();
    expect(screen.getByText("远端最新灵感", { selector: ".final-snapshot-preview dd" })).toBeInTheDocument();

    await user.click(mineChoice);
    expect(screen.getByRole("radio", { name: "替换为此版本内容" })).toBeChecked();
    expect(screen.getByText("此版本灵感", { selector: ".final-snapshot-preview dd" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "确认合并" }));

    await waitFor(() => expect(collectionApi.updateItem).toHaveBeenCalledTimes(2));
    expect(vi.mocked(collectionApi.updateItem).mock.calls[1][1]).toMatchObject({
      expected_revision: 8,
      inspiration: {
        content: "此版本灵感",
        input_mode: "text",
        transcription_status: "not_applicable",
      },
    });
  });

  it("500 也视为结果未知，先 GET 对账；权威内容已到达时不重复 PATCH", async () => {
    const user = userEvent.setup();
    const reached = { ...item, user_title: "已到达标题", display_title: "已到达标题", revision: 8 };
    vi.mocked(collectionApi.getItem)
      .mockResolvedValueOnce(item)
      .mockResolvedValueOnce(reached);
    vi.mocked(collectionApi.updateItem).mockRejectedValueOnce(
      new CollectionApiError("代理没有返回可判断结果", "HTTP_ERROR", 500),
    );
    const callbacks = renderSurface("edit");

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.clear(title);
    await user.type(title, "已到达标题");
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => expect(callbacks.onCompleted).toHaveBeenCalledWith(reached, "detail"));
    expect(collectionApi.getItem).toHaveBeenCalledTimes(2);
    expect(collectionApi.updateItem).toHaveBeenCalledOnce();
  });

  it("408 不视为确定未写入，必须 GET-first 对账且零盲目二次 PATCH", async () => {
    const user = userEvent.setup();
    const reached = { ...item, user_title: "408 已到达", display_title: "408 已到达", revision: 8 };
    vi.mocked(collectionApi.getItem)
      .mockResolvedValueOnce(item)
      .mockResolvedValueOnce(reached);
    vi.mocked(collectionApi.updateItem).mockRejectedValueOnce(
      new CollectionApiError("请求超时", "HTTP_ERROR", 408),
    );
    const callbacks = renderSurface("edit", "http-408-reconcile");

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.clear(title);
    await user.type(title, "408 已到达");
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => expect(callbacks.onSuccessCommitted).toHaveBeenCalledWith(reached, "detail"));
    expect(collectionApi.getItem).toHaveBeenCalledTimes(2);
    expect(collectionApi.updateItem).toHaveBeenCalledOnce();
  });

  it("结果未知且权威仍为 base 时只开放同一快照的安全重试", async () => {
    const user = userEvent.setup();
    const updated = { ...item, user_title: "安全重试标题", display_title: "安全重试标题", revision: 8 };
    vi.mocked(collectionApi.getItem)
      .mockResolvedValueOnce(item)
      .mockResolvedValueOnce(item);
    vi.mocked(collectionApi.updateItem)
      .mockRejectedValueOnce(new CollectionApiError("网络中断", "NETWORK_ERROR", 0))
      .mockResolvedValueOnce(updated);
    renderSurface("edit");

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.clear(title);
    await user.type(title, "安全重试标题");
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    const retry = await screen.findByRole("button", { name: "安全重试" });
    const tagDraft = screen.getByRole("textbox", { name: "整理标签" });
    await user.type(tagDraft, "尚未添加");
    expect(retry).toBeDisabled();
    expect(screen.getByText("先添加或清空尚未提交标签。")).toBeInTheDocument();
    await user.clear(tagDraft);
    expect(retry).toBeEnabled();
    await user.click(retry);

    await waitFor(() => expect(collectionApi.updateItem).toHaveBeenCalledTimes(2));
    expect(vi.mocked(collectionApi.updateItem).mock.calls[1]).toEqual(
      vi.mocked(collectionApi.updateItem).mock.calls[0],
    );
  });

  it("结果未知后 GET 仍失败会保持 unknown，反复核验也不产生第二次 PATCH", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionApi.getItem)
      .mockResolvedValueOnce(item)
      .mockRejectedValue(new CollectionApiError("暂时读不到权威收藏", "NETWORK_ERROR", 0));
    vi.mocked(collectionApi.updateItem).mockRejectedValueOnce(
      new CollectionApiError("没有收到可判断结果", "NETWORK_ERROR", 0),
    );
    const callbacks = renderSurface("edit");

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.clear(title);
    await user.type(title, "待对账标题");
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    const reconcileAgain = await screen.findByRole("button", { name: "再次核验" });
    expect(collectionApi.updateItem).toHaveBeenCalledOnce();
    await user.click(reconcileAgain);
    await waitFor(() => expect(collectionApi.getItem).toHaveBeenCalledTimes(3));
    expect(await screen.findByRole("button", { name: "再次核验" })).toBeInTheDocument();
    expect(title).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "整理标签" })).toBeDisabled();
    const frozenTitle = title.getAttribute("value");
    await user.type(title, "（本地继续编辑）");
    expect(title).toHaveValue(frozenTitle);
    expect(screen.getByRole("button", { name: "再次核验" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(collectionApi.updateItem).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "取消" }));
    await user.click(await screen.findByRole("button", { name: "放弃并退出" }));
    expect(callbacks.onUnknownDiscard).toHaveBeenCalledOnce();
  });

  it("unknown 首次 GET 失败后冻结实际 PATCH 快照，再核验读到该快照会成功且零二次 PATCH", async () => {
    const user = userEvent.setup();
    const reached = { ...item, user_title: "实际提交 B", display_title: "实际提交 B", revision: 8 };
    vi.mocked(collectionApi.getItem)
      .mockResolvedValueOnce(item)
      .mockRejectedValueOnce(new CollectionApiError("暂时读不到权威收藏", "NETWORK_ERROR", 0))
      .mockResolvedValueOnce(reached);
    vi.mocked(collectionApi.updateItem).mockRejectedValueOnce(
      new CollectionApiError("没有收到可判断结果", "NETWORK_ERROR", 0),
    );
    const callbacks = renderSurface("edit", "unknown-frozen-payload");

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.clear(title);
    await user.type(title, "实际提交 B");
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    const reconcileAgain = await screen.findByRole("button", { name: "再次核验" });
    expect(title).toBeDisabled();
    await user.type(title, "试图改成 C");
    expect(title).toHaveValue("实际提交 B");
    expect(readEditMergeSession("edit", item.id, "unknown-frozen-payload")?.desired?.user_title).toBe("实际提交 B");
    await user.click(reconcileAgain);

    await waitFor(() => expect(callbacks.onCompleted).toHaveBeenCalledWith(reached, "detail"));
    expect(collectionApi.updateItem).toHaveBeenCalledOnce();
  });

  it.each([
    ["saving", "reached"],
    ["saving", "base"],
    ["outcome_unknown", "reached"],
    ["outcome_unknown", "base"],
  ] as const)("首次恢复 %s 遇到 GET 失败保持 unknown；再次核验 %s 时只收敛或开放同快照重试", async (requestState, authority) => {
    const user = userEvent.setup();
    const routeEntryId = `initial-${requestState}-${authority}`;
    const base = snapshotFromItem(item);
    const desired = { ...base, user_title: "冻结尝试标题" };
    expect(writeEditMergeSession({
      version: 1,
      mode: "edit",
      itemId: item.id,
      routeEntryId,
      returnKind: "detail",
      reviewEntryId: null,
      base,
      desired,
      incoming: null,
      expectedRevision: item.revision,
      requestState,
      conflictRecovery: null,
      updatedAt: Date.now(),
    })).toBe(true);
    const reached = { ...item, user_title: "冻结尝试标题", display_title: "冻结尝试标题", revision: 8 };
    vi.mocked(collectionApi.getItem)
      .mockRejectedValueOnce(new CollectionApiError("首次核验失败", "NETWORK_ERROR", 0))
      .mockResolvedValueOnce(authority === "reached" ? reached : item);
    const callbacks = renderSurface("edit", routeEntryId);

    const reconcileAgain = await screen.findByRole("button", { name: "再次核验" });
    const title = screen.getByRole("textbox", { name: "自定义标题" });
    expect(title).toHaveValue("冻结尝试标题");
    expect(title).toBeDisabled();
    expect(readEditMergeSession("edit", item.id, routeEntryId)?.requestState).toBe("outcome_unknown");
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
    await user.click(reconcileAgain);

    if (authority === "reached") {
      await waitFor(() => expect(callbacks.onSuccessCommitted).toHaveBeenCalledWith(reached, "detail"));
      expect(readEditMergeSession("edit", item.id, routeEntryId)).toBeNull();
    } else {
      expect(await screen.findByRole("button", { name: "安全重试" })).toBeEnabled();
      expect(title).toBeEnabled();
      expect(readEditMergeSession("edit", item.id, routeEntryId)?.requestState).toBe("draft");
    }
    expect(collectionApi.getItem).toHaveBeenCalledTimes(2);
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
  });

  it("恢复中的首次 GET 身份错配也保持 attempted desired 冻结并只允许再次核验", async () => {
    const routeEntryId = "initial-unknown-identity-mismatch";
    const base = snapshotFromItem(item);
    const desired = { ...base, user_title: "身份错配时冻结" };
    expect(writeEditMergeSession({
      version: 1,
      mode: "edit",
      itemId: item.id,
      routeEntryId,
      returnKind: "detail",
      reviewEntryId: null,
      base,
      desired,
      incoming: null,
      expectedRevision: item.revision,
      requestState: "outcome_unknown",
      conflictRecovery: null,
      updatedAt: Date.now(),
    })).toBe(true);
    vi.mocked(collectionApi.getItem).mockResolvedValueOnce({ ...item, id: "other-item" });
    renderSurface("edit", routeEntryId);

    expect(await screen.findByRole("button", { name: "再次核验" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "自定义标题" })).toBeDisabled();
    expect(readEditMergeSession("edit", item.id, routeEntryId)?.desired?.user_title).toBe("身份错配时冻结");
    expect(readEditMergeSession("edit", item.id, routeEntryId)?.requestState).toBe("outcome_unknown");
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
  });

  it("409 后 latest GET 失败保持冲突恢复门禁，本地编辑也不会产生第二次 PATCH", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionApi.getItem)
      .mockResolvedValueOnce(item)
      .mockRejectedValue(new CollectionApiError("暂时读不到最新收藏", "NETWORK_ERROR", 0));
    vi.mocked(collectionApi.updateItem).mockRejectedValueOnce(new CollectionApiError(
      "收藏已在其他位置更新",
      "COLLECTION_REVISION_CONFLICT",
      409,
      null,
      undefined,
      7,
      8,
    ));
    renderSurface("edit", "conflict-recovery-gate");

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.clear(title);
    await user.type(title, "待重新比较标题");
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    expect(await screen.findByRole("button", { name: "重新比较" })).toBeInTheDocument();
    await user.type(title, "（继续编辑）");
    expect(screen.getByRole("button", { name: "重新比较" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(collectionApi.updateItem).toHaveBeenCalledOnce();
  });

  it.each(["resolve", "reject"] as const)("saving 放弃后迟到 PATCH %s 不重建 session、不 GET、不完成第二次导航", async (outcome) => {
    const user = userEvent.setup();
    let resolveUpdate!: (value: CollectionItem) => void;
    let rejectUpdate!: (reason: unknown) => void;
    vi.mocked(collectionApi.updateItem).mockReturnValue(new Promise((resolve, reject) => {
      resolveUpdate = resolve;
      rejectUpdate = reject;
    }));
    const callbacks = renderSurface("edit", `late-${outcome}`);

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.clear(title);
    await user.type(title, "请求在途时离开");
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    expect(screen.getByRole("button", { name: "正在保存" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "关闭编辑" }));
    expect(await screen.findByRole("dialog", { name: "放弃未保存的内容吗？" })).toHaveTextContent("服务端可能已经收到提交");
    await user.click(screen.getByRole("button", { name: "放弃并退出" }));

    expect(callbacks.onUnknownDiscard).toHaveBeenCalledOnce();
    expect(callbacks.onExit).toHaveBeenCalledOnce();
    expect(window.sessionStorage.length).toBe(0);
    await act(async () => {
      if (outcome === "resolve") resolveUpdate({ ...item, user_title: "请求在途时离开", revision: 8 });
      else rejectUpdate(new CollectionApiError("迟到失败", "HTTP_ERROR", 500));
      await Promise.resolve();
    });

    expect(window.sessionStorage.length).toBe(0);
    expect(collectionApi.getItem).toHaveBeenCalledOnce();
    expect(collectionApi.updateItem).toHaveBeenCalledOnce();
    expect(callbacks.onCompleted).not.toHaveBeenCalled();
    expect(callbacks.onExit).toHaveBeenCalledOnce();
  });

  it("dirty 关闭与浏览器返回共享继续/放弃确认；放弃清 session 且零 PATCH", async () => {
    const user = userEvent.setup();
    const callbacks = renderSurface("edit", "dirty-entry");
    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.clear(title);
    await user.type(title, "尚未保存");

    const beforeUnload = new Event("beforeunload", { cancelable: true });
    expect(window.dispatchEvent(beforeUnload)).toBe(false);
    expect(beforeUnload.defaultPrevented).toBe(true);

    const guard = vi.mocked(callbacks.registerLeaveGuard).mock.calls.at(-1)?.[0];
    expect(guard).toEqual(expect.any(Function));
    let browserBackBlocked: boolean | undefined;
    const browserBack = document.createElement("button");
    browserBack.type = "button";
    browserBack.addEventListener("click", () => { browserBackBlocked = guard?.(); });
    document.body.append(browserBack);
    try {
      await user.click(browserBack);
      expect(browserBackBlocked).toBe(true);
      expect(await screen.findByRole("dialog", { name: "放弃未保存的内容吗？" })).toHaveClass("edit-merge-leave-dialog");
      await user.click(screen.getByRole("button", { name: "继续编辑" }));
      expect(callbacks.onExit).not.toHaveBeenCalled();

      await user.click(screen.getByRole("button", { name: "关闭编辑" }));
      expect(await screen.findByRole("dialog", { name: "放弃未保存的内容吗？" })).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "放弃并退出" }));

      expect(callbacks.onExit).toHaveBeenCalledWith("detail");
      expect(window.sessionStorage.length).toBe(0);
      expect(collectionApi.updateItem).not.toHaveBeenCalled();
    } finally {
      browserBack.remove();
    }
  });

  it("当前标签页存储不可用时展示完整表单但暂停 PATCH", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    try {
      renderSurface("edit", "storage-failure");

      expect(await screen.findByText("恢复草稿未能写入当前标签页")).toBeInTheDocument();
      expect(screen.getByRole("textbox", { name: "自定义标题" })).toHaveValue("已有标题");
      expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
      expect(collectionApi.updateItem).not.toHaveBeenCalled();
    } finally {
      setItem.mockRestore();
    }
  });

  it("item 初读失败后只重做 GET 恢复，不把未提交草稿误判为 unknown", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionApi.getItem)
      .mockRejectedValueOnce(new CollectionApiError("暂时读取失败", "NETWORK_ERROR", 0))
      .mockResolvedValueOnce(item);
    renderSurface("edit", "read-retry");

    await user.click(await screen.findByRole("button", { name: "重试读取" }));
    expect(await screen.findByRole("textbox", { name: "自定义标题" })).toHaveValue("已有标题");
    expect(screen.queryByRole("button", { name: "安全重试" })).not.toBeInTheDocument();
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
  });

  it("初始恢复 GET 404 时 dirty 草稿必须确认；继续保留，放弃后才回素材库", async () => {
    const user = userEvent.setup();
    const routeEntryId = "missing-on-read";
    const base = snapshotFromItem(item);
    expect(writeEditMergeSession({
      version: 1,
      mode: "edit",
      itemId: item.id,
      routeEntryId,
      returnKind: "detail",
      reviewEntryId: null,
      base,
      desired: { ...base, user_title: "仍需保留的草稿" },
      incoming: null,
      expectedRevision: item.revision,
      requestState: "draft",
      conflictRecovery: null,
      updatedAt: Date.now(),
    })).toBe(true);
    vi.mocked(collectionApi.getItem).mockRejectedValueOnce(new CollectionApiError("不存在", "NOT_FOUND", 404));
    const callbacks = renderSurface("edit", routeEntryId);

    const back = await screen.findByRole("button", { name: "返回素材库" });
    await user.click(back);
    expect(await screen.findByRole("dialog", { name: "放弃未保存的内容吗？" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(readEditMergeSession("edit", item.id, routeEntryId)?.desired?.user_title).toBe("仍需保留的草稿");
    expect(callbacks.onItemMissingExit).not.toHaveBeenCalled();

    await user.click(back);
    await user.click(await screen.findByRole("button", { name: "放弃并退出" }));
    expect(callbacks.onItemMissingExit).toHaveBeenCalledOnce();
    expect(window.sessionStorage.length).toBe(0);
    expect(collectionApi.getItem).toHaveBeenCalledOnce();
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
  });

  it("PATCH 404 后 dirty 草稿也必须确认，放弃仅回素材库且不追加请求", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionApi.updateItem).mockRejectedValueOnce(new CollectionApiError("不存在", "NOT_FOUND", 404));
    const callbacks = renderSurface("edit", "missing-on-patch");

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.clear(title);
    await user.type(title, "保存前草稿");
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    const back = await screen.findByRole("button", { name: "返回素材库" });
    await user.click(back);
    expect(await screen.findByRole("dialog", { name: "放弃未保存的内容吗？" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "放弃并退出" }));

    expect(callbacks.onItemMissingExit).toHaveBeenCalledOnce();
    expect(window.sessionStorage.length).toBe(0);
    expect(collectionApi.getItem).toHaveBeenCalledOnce();
    expect(collectionApi.updateItem).toHaveBeenCalledOnce();
    expect(callbacks.onCompleted).not.toHaveBeenCalled();
  });

  it("merge 路由缺少当前标签页会话时不 GET、不 PATCH，也不从 URL 推测候选", async () => {
    renderSurface("merge", "missing-entry");
    expect(await screen.findByRole("heading", { name: "合并会话已失效" })).toBeInTheDocument();
    expect(collectionApi.getItem).not.toHaveBeenCalled();
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
  });

  it("超过 4000 字的可解析 session 草稿会恢复为业务校验错误并保持零 PATCH", async () => {
    const routeEntryId = "overlong-session";
    const base = snapshotFromItem(item);
    expect(writeEditMergeSession({
      version: 1,
      mode: "edit",
      itemId: item.id,
      routeEntryId,
      returnKind: "detail",
      reviewEntryId: null,
      base,
      desired: {
        ...base,
        inspiration: { content: "x".repeat(4001), input_mode: "text", transcription_status: "not_applicable" },
      },
      incoming: null,
      expectedRevision: item.revision,
      requestState: "draft",
      conflictRecovery: null,
      updatedAt: Date.now(),
    })).toBe(true);
    renderSurface("edit", routeEntryId);

    expect(await screen.findAllByText("我的灵感最多 4000 个字符。")).not.toHaveLength(0);
    expect(screen.getByRole("textbox", { name: "我的灵感" })).toHaveValue("x".repeat(4001));
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
  });

  it("类型正确但关系损坏的 merge session 会清除并保持零 GET/零 PATCH", async () => {
    const routeEntryId = "malformed-merge";
    expect(writeEditMergeSession({
      ...mergeSession(routeEntryId),
      returnKind: "detail",
      reviewEntryId: null,
      incoming: {
        ...mergeSession(routeEntryId).incoming!,
        inspiration: { content: "非法组合", input_mode: "voice", transcription_status: "not_applicable" },
      },
    })).toBe(true);
    renderSurface("merge", routeEntryId);

    expect(await screen.findByRole("heading", { name: "合并会话已失效" })).toBeInTheDocument();
    expect(window.sessionStorage.length).toBe(0);
    expect(collectionApi.getItem).not.toHaveBeenCalled();
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
  });

  it("类型正确但字段不完整的 edit 重建权威草稿；merge 直接 hard boundary", async () => {
    const editEntryId = "incomplete-edit";
    const base = snapshotFromItem(item);
    expect(writeEditMergeSession({
      version: 1,
      mode: "edit",
      itemId: item.id,
      routeEntryId: editEntryId,
      returnKind: "detail",
      reviewEntryId: null,
      base: null,
      desired: base,
      incoming: null,
      expectedRevision: null,
      requestState: "draft",
      conflictRecovery: null,
      updatedAt: Date.now(),
    })).toBe(true);
    const edit = renderSurface("edit", editEntryId);
    expect(await screen.findByRole("textbox", { name: "自定义标题" })).toHaveValue("已有标题");
    expect(readEditMergeSession("edit", item.id, editEntryId)).toEqual(expect.objectContaining({
      base: expect.any(Object),
      desired: expect.any(Object),
      expectedRevision: item.revision,
    }));
    expect(collectionApi.getItem).toHaveBeenCalledOnce();
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
    edit.unmount();
    window.sessionStorage.clear();

    vi.mocked(collectionApi.getItem).mockClear();
    const mergeEntryId = "incomplete-merge";
    expect(writeEditMergeSession({
      ...mergeSession(mergeEntryId),
      base,
      desired: null,
      expectedRevision: item.revision,
    })).toBe(true);
    renderSurface("merge", mergeEntryId);
    expect(await screen.findByRole("heading", { name: "合并会话已失效" })).toBeInTheDocument();
    expect(window.sessionStorage.length).toBe(0);
    expect(collectionApi.getItem).not.toHaveBeenCalled();
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
  });

  it("360px 手机按动态三按钮 footer 实际 border-box 高度只预留一次正文空间", async () => {
    const user = userEvent.setup();
    const originalResizeObserver = globalThis.ResizeObserver;
    const originalInnerWidth = window.innerWidth;
    let footerHeight = 92;
    let resizeCallback: ResizeObserverCallback | null = null;
    class TestResizeObserver {
      constructor(callback: ResizeObserverCallback) { resizeCallback = callback; }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    Object.defineProperty(globalThis, "ResizeObserver", { configurable: true, value: TestResizeObserver });
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 360 });
    viewport(false);
    const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const height = this.classList.contains("edit-merge-actions") ? footerHeight : 0;
      return { x: 0, y: 0, width: 360, height, top: 0, right: 360, bottom: height, left: 0, toJSON: () => ({}) } as DOMRect;
    });
    vi.mocked(collectionApi.updateItem).mockRejectedValueOnce(
      new CollectionApiError("内容未通过校验", "VALIDATION_ERROR", 400),
    );
    try {
      renderSurface("edit", "mobile-footer");
      const title = await screen.findByRole("textbox", { name: "自定义标题" });
      const surface = title.closest<HTMLElement>(".edit-merge-surface")!;
      expect(surface.style.getPropertyValue("--edit-merge-footer-height")).toBe("92px");
      await user.clear(title);
      await user.type(title, "触发三按钮状态");
      footerHeight = 148;
      await user.click(screen.getByRole("button", { name: "保存修改" }));
      expect(await screen.findByRole("button", { name: "重试保存" })).toBeInTheDocument();
      expect(document.querySelectorAll(".edit-merge-actions button")).toHaveLength(3);
      expect(surface.style.getPropertyValue("--edit-merge-footer-height")).toBe("148px");

      footerHeight = 164;
      act(() => resizeCallback?.([] as ResizeObserverEntry[], {} as ResizeObserver));
      expect(surface.style.getPropertyValue("--edit-merge-footer-height")).toBe("164px");
    } finally {
      rect.mockRestore();
      Object.defineProperty(window, "innerWidth", { configurable: true, value: originalInnerWidth });
      Object.defineProperty(globalThis, "ResizeObserver", { configurable: true, value: originalResizeObserver });
    }
  });

  it("M03/M04 使用 route-level 单屏表面，不伪装成桌面 modal", async () => {
    viewport(false);
    renderSurface("edit", "mobile-entry");

    const heading = await screen.findByRole("heading", { name: "编辑收藏" });
    const surface = heading.closest(".edit-merge-surface");
    expect(surface).not.toHaveAttribute("role");
    expect(surface?.parentElement).toHaveClass("is-mobile");
    expect(document.body.style.overflow).toBe("");
  });

  it("M03/M04 mobile CSS 清除 surface 动画与 transform，footer 不落入文档 containing block", () => {
    const css = editMergeCss;
    const mobileStart = css.indexOf("@media (max-width: 900px)");
    const mobileEnd = css.indexOf("@media (max-width: 380px)");
    expect(mobileStart).toBeGreaterThanOrEqual(0);
    expect(mobileEnd).toBeGreaterThan(mobileStart);
    const mobileCss = css.slice(mobileStart, mobileEnd);
    expect(mobileCss).toMatch(/\.edit-merge-surface\s*\{[^}]*animation:\s*none;[^}]*transform:\s*none;/u);
    expect(mobileCss).not.toContain("animation-name:");
  });

  it("overlay 与 portal Dialog 持有完整 dark/teal/orange 主题 token", async () => {
    const user = userEvent.setup();
    const style = document.createElement("style");
    style.textContent = editMergeCss;
    document.head.append(style);
    try {
      renderSurface("edit", "theme-overlay");
      const title = await screen.findByRole("textbox", { name: "自定义标题" });
      document.documentElement.dataset.theme = "dark";
      document.documentElement.dataset.accent = "orange";
      const overlay = document.querySelector<HTMLElement>(".edit-merge-overlay")!;
      expect(getComputedStyle(overlay).getPropertyValue("--collection-surface").trim()).toBe("#171a1f");
      expect(getComputedStyle(overlay).getPropertyValue("--collection-accent").trim()).toBe("#a74325");

      await user.clear(title);
      await user.type(title, "打开离页确认");
      await user.click(screen.getByRole("button", { name: "关闭编辑" }));
      const dialog = await screen.findByRole("dialog", { name: "放弃未保存的内容吗？" });
      expect(dialog).toHaveClass("edit-merge-leave-dialog");
      expect(getComputedStyle(dialog).getPropertyValue("--collection-surface").trim()).toBe("#171a1f");
      expect(getComputedStyle(dialog).getPropertyValue("--collection-accent").trim()).toBe("#a74325");

      document.documentElement.dataset.accent = "teal";
      expect(getComputedStyle(overlay).getPropertyValue("--collection-accent").trim()).toBe("#0b7168");
      expect(getComputedStyle(dialog).getPropertyValue("--collection-accent").trim()).toBe("#0b7168");
    } finally {
      style.remove();
      delete document.documentElement.dataset.theme;
      delete document.documentElement.dataset.accent;
    }
  });

  it("reduced-motion 同时覆盖 overlay、portal descendants 与无限 spin", () => {
    const reducedStart = editMergeCss.indexOf("@media (prefers-reduced-motion: reduce)");
    expect(reducedStart).toBeGreaterThanOrEqual(0);
    const reducedCss = editMergeCss.slice(reducedStart);
    expect(reducedCss).toContain(".edit-merge-overlay *");
    expect(reducedCss).toContain(".edit-merge-leave-dialog *");
    expect(reducedCss).toMatch(/animation-iteration-count:\s*1\s*!important/u);
    expect(reducedCss).toMatch(/\.edit-merge-overlay \.spin[\s\S]*animation:\s*none\s*!important/u);
  });

  it("恢复、终态与离页 Dialog 按钮均由 R2.5 表面显式保证 44px 命中区", () => {
    const css = editMergeCss;
    expect(css).toMatch(/\.edit-merge-recovery \.rt-Button,\s*\.edit-merge-terminal \.rt-Button,\s*\.edit-merge-leave-dialog \.dialog-actions \.rt-Button\s*\{[^}]*min-width:\s*44px;[^}]*min-height:\s*44px;/u);
  });

  it("M03 merge 顶部返回语义指向保存前整理，加载完成后焦点落在任务标题", async () => {
    viewport(false);
    const routeEntryId = "mobile-merge-return";
    expect(writeEditMergeSession(mergeSession(routeEntryId))).toBe(true);
    renderSurface("merge", routeEntryId);

    const heading = await screen.findByRole("heading", { name: "合并重复收藏" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(screen.getByRole("button", { name: "返回保存前整理" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "返回收藏详情" })).not.toBeInTheDocument();
  });
});
