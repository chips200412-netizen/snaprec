import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  CollectionImportBatchSnapshot,
  CollectionImportItemDetail,
} from "./collection-import-api";

vi.mock("./collection-import-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./collection-import-api")>();
  return {
    ...actual,
    collectionImportApi: {
      createBatch: vi.fn(),
      getActiveBatch: vi.fn(),
      getBatch: vi.fn(),
      getItem: vi.fn(),
      updateItem: vi.fn(),
      repreviewItem: vi.fn(),
      confirmBatch: vi.fn(),
      resumeBatch: vi.fn(),
      cancelBatch: vi.fn(),
    },
  };
});

import { CollectionImportApiError, collectionImportApi } from "./collection-import-api";
import { CollectionImportPage, type CollectionImportRouteState } from "./collection-import-page";
import { CollectionImportReviewControls } from "./collection-import-review";

const firstInput = "分享 A https://example.com/a";
const secondInput = "分享 B https://example.com/b";

const batch: CollectionImportBatchSnapshot = {
  batch_id: "batch-1",
  status: "awaiting_review",
  revision: 3,
  total: 2,
  queued: 0,
  previewing: 1,
  ready: 1,
  needs_review: 0,
  duplicates: 0,
  already_exists: 0,
  failed: 0,
  selected: 0,
  created_at: "2026-08-31T10:00:00Z",
  updated_at: "2026-08-31T10:01:00Z",
  terminal_at: null,
  items: [
    {
      batch_item_id: "item-2",
      client_item_id: "item-02",
      position: 1,
      display_label: "第二项",
      state: "previewing",
      decision: "pending",
      item_revision: 1,
      preview_generation: 1,
      duplicate_of_batch_item_id: null,
      collection_item_id: null,
      error_code: null,
      terminal_reason: null,
    },
    {
      batch_item_id: "item-1",
      client_item_id: "item-01",
      position: 0,
      display_label: "第一项",
      state: "ready",
      decision: "pending",
      item_revision: 2,
      preview_generation: 1,
      duplicate_of_batch_item_id: null,
      collection_item_id: null,
      error_code: null,
      terminal_reason: null,
    },
  ],
};

const itemDetail: CollectionImportItemDetail = {
  ...batch.items[1],
  batch_id: batch.batch_id,
  batch_revision: batch.revision,
  input_available: true,
  input_text: firstInput,
  preview: {
    preview_id: "preview-1",
    original_input: firstInput,
    source_url: "https://example.com/a",
    canonical_url: "https://example.com/a",
    identity_url: "https://example.com/a",
    source_kind: "article",
    platform: "web",
    metadata_status: "recognized",
    metadata: {
      title: { value: "公开标题", source: "open_graph", fetched_at: "2026-08-31T10:00:00Z" },
      author: { value: "公开作者", source: "page_metadata", fetched_at: "2026-08-31T10:00:00Z" },
      cover_url: { value: "", source: "none", fetched_at: "2026-08-31T10:00:00Z" },
      source_copy: { value: "公开摘要", source: "page_description", fetched_at: "2026-08-31T10:00:00Z" },
      platform_tags: [],
      warnings: [],
    },
    organization_suggestion: {
      primary_category: "文章",
      secondary_category: "待整理",
      tags: [],
      basis: "public_metadata",
      method: "deterministic",
      status: "generated",
    },
    created_at: "2026-08-31T10:00:00Z",
    expires_at: "2026-08-31T10:30:00Z",
  },
  draft: {
    user_title: null,
    untitled_confirmed: false,
    organization_confirmation: {
      primary_category: "",
      secondary_category: "",
      organization_tags: [],
    },
    personal_tags: [],
    inspiration: null,
  },
  error_stage: null,
};

const reviewBatch: CollectionImportBatchSnapshot = {
  ...batch,
  revision: 4,
  queued: 0,
  previewing: 0,
  ready: 2,
  items: batch.items.map((item) => ({ ...item, state: "ready" as const })),
};

const reviewDetail: CollectionImportItemDetail = {
  ...itemDetail,
  batch_revision: reviewBatch.revision,
  preview: itemDetail.preview && {
    ...itemDetail.preview,
    organization_suggestion: {
      ...itemDetail.preview.organization_suggestion,
      primary_category: "文章",
      secondary_category: "产品研究",
      tags: ["公开信息", "待读"],
    },
  },
};

function snapshotAfterDecision(decision: "save" | "skip"): CollectionImportBatchSnapshot {
  return {
    ...reviewBatch,
    revision: 5,
    selected: decision === "save" ? 1 : 0,
    items: reviewBatch.items.map((item) => item.batch_item_id === "item-1"
      ? { ...item, decision, item_revision: 3 }
      : item),
  };
}

const confirmReadyBatch: CollectionImportBatchSnapshot = {
  ...reviewBatch,
  revision: 8,
  selected: 1,
  items: reviewBatch.items.map((item) => item.batch_item_id === "item-1"
    ? { ...item, decision: "save", item_revision: 4 }
    : { ...item, decision: "skip", item_revision: 3 }),
};

const confirmReadyDetail: CollectionImportItemDetail = {
  ...reviewDetail,
  batch_revision: confirmReadyBatch.revision,
  decision: "save",
  item_revision: 4,
};

const savingBatch: CollectionImportBatchSnapshot = {
  ...confirmReadyBatch,
  status: "saving",
  revision: 9,
  ready: 0,
  items: confirmReadyBatch.items.map((item) => item.batch_item_id === "item-1"
    ? { ...item, state: "save_queued", item_revision: 5 }
    : { ...item, state: "skipped", item_revision: 4 }),
};

const interruptedFrozenBatch: CollectionImportBatchSnapshot = {
  ...savingBatch,
  status: "interrupted",
  items: savingBatch.items.map((item) => item.batch_item_id === "item-1"
    ? { ...item, state: "interrupted", decision: "save" }
    : item),
};

const interruptedReviewBatch: CollectionImportBatchSnapshot = {
  ...reviewBatch,
  status: "interrupted",
  revision: 9,
  ready: 1,
  items: reviewBatch.items.map((item) => item.batch_item_id === "item-1"
    ? { ...item, state: "ready", item_revision: 4 }
    : { ...item, state: "interrupted", item_revision: 3 }),
};

const outcomeUnknownBatch: CollectionImportBatchSnapshot = {
  ...interruptedReviewBatch,
  items: interruptedReviewBatch.items.map((item) => item.batch_item_id === "item-2"
    ? { ...item, state: "outcome_unknown", decision: "save" }
    : item),
};

const refreshedAwaitingReviewBatch: CollectionImportBatchSnapshot = {
  ...outcomeUnknownBatch,
  status: "awaiting_review",
  revision: 10,
  ready: 2,
  updated_at: "2026-08-31T10:02:00Z",
  items: outcomeUnknownBatch.items.map((item) => item.batch_item_id === "item-2"
    ? { ...item, state: "ready", decision: "pending", item_revision: item.item_revision + 1 }
    : item),
};

const resumedPreviewBatch: CollectionImportBatchSnapshot = {
  ...interruptedReviewBatch,
  status: "previewing",
  revision: 10,
  queued: 1,
  ready: 1,
  items: interruptedReviewBatch.items.map((item) => item.batch_item_id === "item-2"
    ? { ...item, state: "queued", item_revision: 4 }
    : item),
};

const cancelledBatch: CollectionImportBatchSnapshot = {
  ...reviewBatch,
  status: "cancelled",
  revision: 5,
  ready: 0,
  terminal_at: "2026-08-31T10:05:00Z",
  items: reviewBatch.items.map((item) => ({ ...item, state: "cancelled", item_revision: item.item_revision + 1 })),
};

const cancellingBatch: CollectionImportBatchSnapshot = {
  ...savingBatch,
  status: "cancelling",
  revision: 10,
};

const failedAfterConfirmBatch: CollectionImportBatchSnapshot = {
  ...confirmReadyBatch,
  status: "awaiting_review",
  revision: 10,
  selected: 0,
  failed: 1,
  ready: 0,
  items: confirmReadyBatch.items.map((item) => item.batch_item_id === "item-1"
    ? { ...item, state: "failed", decision: "pending", item_revision: 6, error_code: "SAVE_FAILED" }
    : { ...item, state: "skipped", item_revision: 4 }),
};

const partialRetryBatch: CollectionImportBatchSnapshot = {
  ...confirmReadyBatch,
  revision: 12,
  total: 5,
  ready: 1,
  failed: 1,
  already_exists: 1,
  selected: 3,
  items: [
    { ...confirmReadyBatch.items[0], batch_item_id: "retry-failed", client_item_id: "item-01", position: 0, state: "failed", decision: "save", item_revision: 7 },
    { ...confirmReadyBatch.items[0], batch_item_id: "saved-before", client_item_id: "item-02", position: 1, state: "saved", decision: "save", item_revision: 6 },
    { ...confirmReadyBatch.items[0], batch_item_id: "existing-before", client_item_id: "item-03", position: 2, state: "already_exists", decision: "save", item_revision: 5, collection_item_id: "material-1" },
    { ...confirmReadyBatch.items[1], batch_item_id: "skipped-before", client_item_id: "item-04", position: 3, state: "skipped", decision: "skip", item_revision: 5 },
    { ...confirmReadyBatch.items[1], batch_item_id: "skip-now", client_item_id: "item-05", position: 4, state: "ready", decision: "skip", item_revision: 5 },
  ],
};

const advancedReviewBatch: CollectionImportBatchSnapshot = {
  ...confirmReadyBatch,
  revision: 9,
  items: confirmReadyBatch.items.map((item) => item.batch_item_id === "item-1"
    ? { ...item, item_revision: 5 }
    : item),
};

function viewport(mobile = false) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn((query: string) => ({
      matches: mobile ? query.includes("max-width") : query.includes("min-width"),
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

function viewportAt(width: number) {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn((query: string) => {
      const max = query.match(/max-width:\s*(\d+)px/);
      const min = query.match(/min-width:\s*(\d+)px/);
      return {
        matches: max ? width <= Number(max[1]) : min ? width >= Number(min[1]) : false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      };
    }),
  });
}

function resizableViewport(initialWidth: number) {
  let width = initialWidth;
  Object.defineProperty(window, "innerWidth", { configurable: true, get: () => width });
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn((query: string) => {
      const max = query.match(/max-width:\s*(\d+)px/);
      const min = query.match(/min-width:\s*(\d+)px/);
      return {
        get matches() { return max ? width <= Number(max[1]) : min ? width >= Number(min[1]) : false; },
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      };
    }),
  });
  return (nextWidth: number) => {
    width = nextWidth;
    window.dispatchEvent(new Event("resize"));
  };
}

function responsiveViewport(initialWidth: number) {
  let width = initialWidth;
  const entries: Array<{
    query: string;
    matches: boolean;
    listeners: Set<(event: MediaQueryListEvent) => void>;
  }> = [];
  Object.defineProperty(window, "innerWidth", { configurable: true, get: () => width });
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn((query: string) => {
      const evaluate = () => {
        const max = query.match(/max-width:\s*(\d+)px/);
        const min = query.match(/min-width:\s*(\d+)px/);
        return max ? width <= Number(max[1]) : min ? width >= Number(min[1]) : false;
      };
      const entry = { query, matches: evaluate(), listeners: new Set<(event: MediaQueryListEvent) => void>() };
      entries.push(entry);
      return {
        get matches() { return entry.matches; },
        media: query,
        onchange: null,
        addListener: vi.fn((listener: (event: MediaQueryListEvent) => void) => entry.listeners.add(listener)),
        removeListener: vi.fn((listener: (event: MediaQueryListEvent) => void) => entry.listeners.delete(listener)),
        addEventListener: vi.fn((_type: string, listener: (event: MediaQueryListEvent) => void) => entry.listeners.add(listener)),
        removeEventListener: vi.fn((_type: string, listener: (event: MediaQueryListEvent) => void) => entry.listeners.delete(listener)),
        dispatchEvent: vi.fn(),
      };
    }),
  });
  return (nextWidth: number) => {
    width = nextWidth;
    for (const entry of entries) {
      const max = entry.query.match(/max-width:\s*(\d+)px/);
      const min = entry.query.match(/min-width:\s*(\d+)px/);
      const matches = max ? width <= Number(max[1]) : min ? width >= Number(min[1]) : false;
      if (matches === entry.matches) continue;
      entry.matches = matches;
      const event = { matches, media: entry.query } as MediaQueryListEvent;
      for (const listener of entry.listeners) listener(event);
    }
    window.dispatchEvent(new Event("resize"));
  };
}

function renderPage(route: CollectionImportRouteState = { view: "list" }) {
  const callbacks = {
    onRouteChange: vi.fn(),
    onBackToLibrary: vi.fn(),
    onBackToList: vi.fn(),
    onBackFromConfirm: vi.fn(),
    onOpenExisting: vi.fn(),
    registerLeaveGuard: vi.fn(),
  };
  const rendered = render(<CollectionImportPage route={route} {...callbacks} />);
  return { ...rendered, ...callbacks };
}

describe("R2.6-B1 批量导入创建、恢复与 preview 壳", () => {
  beforeEach(() => {
    viewport();
    Object.defineProperty(window, "requestAnimationFrame", {
      configurable: true,
      value: (callback: FrameRequestCallback) => { callback(0); return 1; },
    });
    vi.mocked(collectionImportApi.createBatch).mockReset();
    vi.mocked(collectionImportApi.getActiveBatch).mockReset();
    vi.mocked(collectionImportApi.getBatch).mockReset();
    vi.mocked(collectionImportApi.getItem).mockReset();
    vi.mocked(collectionImportApi.updateItem).mockReset();
    vi.mocked(collectionImportApi.repreviewItem).mockReset();
    vi.mocked(collectionImportApi.confirmBatch).mockReset();
    vi.mocked(collectionImportApi.resumeBatch).mockReset();
    vi.mocked(collectionImportApi.cancelBatch).mockReset();
    vi.mocked(collectionImportApi.getActiveBatch).mockResolvedValue(null);
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(batch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(itemDetail);
    vi.mocked(collectionImportApi.createBatch).mockResolvedValue(batch);
    vi.mocked(collectionImportApi.updateItem).mockResolvedValue(snapshotAfterDecision("save"));
    vi.mocked(collectionImportApi.repreviewItem).mockResolvedValue({
      ...reviewBatch,
      status: "previewing",
      revision: 5,
      queued: 1,
      ready: 1,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, state: "queued", decision: "pending", item_revision: 3, preview_generation: 1 }
        : item),
    });
    vi.mocked(collectionImportApi.confirmBatch).mockResolvedValue(savingBatch);
    vi.mocked(collectionImportApi.resumeBatch).mockResolvedValue(interruptedFrozenBatch);
    vi.mocked(collectionImportApi.cancelBatch).mockResolvedValue({
      snapshot: { ...interruptedFrozenBatch, status: "cancelled", revision: interruptedFrozenBatch.revision + 1 },
      status: 200,
    });
  });

  it("active 204 后展示两个独立输入，并在提交前说明字符与 UTF-8 边界", async () => {
    renderPage();

    const inputs = await screen.findAllByRole("textbox", { name: /完整分享文本/ });
    expect(inputs).toHaveLength(2);
    expect(screen.getByText(/2 至 10 条完整分享文本/)).toBeInTheDocument();
    expect(screen.getByText(/65,536 UTF-8 bytes/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "创建批次并开始检查" })).toBeDisabled();

    fireEvent.change(inputs[0], { target: { value: "😀" } });
    expect(screen.getByText("1/10,000 字符")).toBeInTheDocument();
  });

  it("完整保留原序文本；未知结果先 GET active，再以同一个幂等键对账", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.createBatch)
      .mockRejectedValueOnce(new CollectionImportApiError("连接中断，结果未知。", "NETWORK_ERROR", 0))
      .mockResolvedValueOnce(batch);
    const callbacks = renderPage();
    const inputs = await screen.findAllByRole("textbox", { name: /完整分享文本/ });
    fireEvent.change(inputs[0], { target: { value: firstInput } });
    fireEvent.change(inputs[1], { target: { value: secondInput } });

    await user.click(screen.getByRole("button", { name: "创建批次并开始检查" }));

    await waitFor(() => expect(collectionImportApi.createBatch).toHaveBeenCalledTimes(2));
    expect(collectionImportApi.getActiveBatch).toHaveBeenCalledTimes(2);
    const first = vi.mocked(collectionImportApi.createBatch).mock.calls[0];
    const second = vi.mocked(collectionImportApi.createBatch).mock.calls[1];
    expect(first[0]).toEqual({
      items: [
        { client_item_id: "item-01", input_text: firstInput },
        { client_item_id: "item-02", input_text: secondInput },
      ],
    });
    expect(second[0]).toEqual(first[0]);
    expect(second[1]).toBe(first[1]);
    expect(callbacks.onRouteChange).toHaveBeenCalledWith(
      { batchId: "batch-1", view: "list", focus: "summary" },
      true,
    );
  });

  it("发现 active 批次时只替换到批次列表，不创建、不选择第一项", async () => {
    vi.mocked(collectionImportApi.getActiveBatch).mockResolvedValue(batch);
    const callbacks = renderPage();

    await waitFor(() => expect(callbacks.onRouteChange).toHaveBeenCalledWith(
      { batchId: "batch-1", view: "list", focus: "summary" },
      true,
    ));
    expect(collectionImportApi.createBatch).not.toHaveBeenCalled();
    expect(collectionImportApi.getItem).not.toHaveBeenCalled();
  });

  it("GET batch 恢复原序索引但保持无 current item，点击后才请求安全路由", async () => {
    const user = userEvent.setup();
    const callbacks = renderPage({ batchId: "batch-1", view: "list" });

    await screen.findByRole("heading", { name: "选择一项开始审核" });
    expect(collectionImportApi.getItem).not.toHaveBeenCalled();
    const list = screen.getByRole("heading", { name: "原序列表" }).closest("section");
    expect(list).not.toBeNull();
    const buttons = within(list as HTMLElement).getAllByRole("button");
    expect(buttons[0]).toHaveTextContent("第一项");
    expect(buttons[0]).toHaveTextContent(/^1/);
    expect(buttons[1]).toHaveTextContent("第二项");
    expect(buttons[1]).toHaveTextContent(/^2/);

    await user.click(buttons[0]);
    expect(callbacks.onRouteChange).toHaveBeenCalledWith({
      batchId: "batch-1",
      view: "item",
      itemId: "item-1",
      focus: "item:item-1",
      anchorItemId: "item-1",
      anchorOffset: 0,
      trigger: "pointer",
    });
    expect(JSON.stringify(callbacks.onRouteChange.mock.calls)).not.toContain(firstInput);
    expect(collectionImportApi.getItem).not.toHaveBeenCalled();
  });

  it("GET item 才展示审核详情；原始输入默认折叠且 Escape 关闭并归还焦点", async () => {
    const user = userEvent.setup();
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    expect(await screen.findByRole("heading", { name: "公开标题" })).toBeInTheDocument();
    expect(screen.getByText("第 1/2 项")).toBeInTheDocument();
    expect(collectionImportApi.getItem).toHaveBeenCalledWith("batch-1", "item-1", expect.any(AbortSignal));
    expect(screen.queryByText(firstInput)).not.toBeInTheDocument();
    const disclosure = screen.getByRole("button", { name: "查看原始输入" });
    await user.click(disclosure);
    expect(screen.getByText(firstInput)).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByText(firstInput)).not.toBeInTheDocument();
    expect(disclosure).toHaveFocus();
  });

  it("桌面 item 缩到 900px 以下时把焦点从隐藏索引迁到当前项标题", async () => {
    let mobile = false;
    const listeners = new Set<(event: MediaQueryListEvent) => void>();
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn((query: string) => ({
        matches: mobile ? query.includes("max-width") : query.includes("min-width"),
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn((type: string, listener: (event: MediaQueryListEvent) => void) => {
          if (type === "change") listeners.add(listener);
        }),
        removeEventListener: vi.fn((type: string, listener: (event: MediaQueryListEvent) => void) => {
          if (type === "change") listeners.delete(listener);
        }),
        dispatchEvent: vi.fn(),
      })),
    });
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    const heading = await screen.findByRole("heading", { name: "公开标题" });
    const list = screen.getByRole("heading", { name: "原序列表" }).closest("section");
    const trigger = within(list as HTMLElement).getAllByRole("button")[0];
    trigger.focus();
    expect(trigger).toHaveFocus();

    mobile = true;
    listeners.forEach((listener) => listener({ matches: true } as MediaQueryListEvent));
    await waitFor(() => expect(heading).toHaveFocus());
  });

  it("用两级 CAS 记录保存决定，payload 无 preview_id，成功仍停在当前项并保持按钮焦点", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(reviewBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(reviewDetail);
    const callbacks = renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.type(title, "我的标题");
    const save = screen.getByRole("button", { name: "标记保存" });
    await user.click(save);

    await waitFor(() => expect(collectionImportApi.updateItem).toHaveBeenCalledWith(
      "batch-1",
      "item-1",
      {
        expected_batch_revision: 4,
        expected_item_revision: 2,
        decision: "save",
        user_title: "我的标题",
        untitled_confirmed: false,
        organization_confirmation: { primary_category: "", secondary_category: "", organization_tags: [] },
        personal_tags: [],
        inspiration: null,
      },
    ));
    expect(JSON.stringify(vi.mocked(collectionImportApi.updateItem).mock.calls[0][2])).not.toContain("preview_id");
    expect(screen.getByText("已记录保存决定")).toBeInTheDocument();
    expect(callbacks.onRouteChange).not.toHaveBeenCalled();
    expect(save).toHaveFocus();
  });

  it("审核普通状态、dirty 与成功只经唯一 polite live region 公告且不抢焦点", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(reviewBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(reviewDetail);
    vi.mocked(collectionImportApi.updateItem).mockResolvedValue(reviewBatch);
    const rendered = renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    const liveRegions = () => rendered.container.querySelectorAll(".batch-import-page [aria-live='polite'], .batch-import-page [role='status']");
    expect(liveRegions()).toHaveLength(1);

    await user.type(title, "只经聚合区公告");
    await waitFor(() => expect(liveRegions()[0]).toHaveTextContent("有未提交修改"));
    expect(title).toHaveFocus();

    const save = screen.getByRole("button", { name: "标记保存" });
    await user.click(save);
    await waitFor(() => expect(liveRegions()[0]).toHaveTextContent("已记录保存决定"));
    expect(save).toHaveFocus();
    expect(liveRegions()).toHaveLength(1);
  });

  it("整理建议逐字段主动采用，只改本地 draft，不自动 PATCH 或批量采用", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(reviewBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(reviewDetail);
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    const primary = await screen.findByRole("textbox", { name: "一级分类" });
    const secondary = screen.getByRole("textbox", { name: "二级分类" });
    await user.click(screen.getByRole("button", { name: "采用一级分类建议：文章" }));

    expect(primary).toHaveValue("文章");
    expect(secondary).toHaveValue("");
    expect(screen.getAllByText("有未提交修改")).not.toHaveLength(0);
    expect(collectionImportApi.updateItem).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /全部采用/ })).not.toBeInTheDocument();
  });

  it("TagEditor 尚未点添加的显式输入会随 PATCH 持久化，不会成功后静默丢失", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(reviewBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(reviewDetail);
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.type(await screen.findByRole("textbox", { name: "自定义标题" }), "带标签标题");
    await user.type(screen.getByRole("textbox", { name: "个人标签" }), "尚未点添加");
    await user.click(screen.getByRole("button", { name: "标记保存" }));

    await waitFor(() => expect(collectionImportApi.updateItem).toHaveBeenCalledWith(
      "batch-1",
      "item-1",
      expect.objectContaining({ personal_tags: ["尚未点添加"] }),
    ));
    expect(screen.getByText("已记录保存决定")).toBeInTheDocument();
  });

  it("dirty 时 batch summary revision 前进也只能使用同一 authority detail 的旧 CAS", async () => {
    const user = userEvent.setup();
    const callbacks = {
      onBatchSnapshot: vi.fn(),
      onAuthoritativeDetail: vi.fn(),
      onOpenExisting: vi.fn(),
      registerGuard: vi.fn(),
    };
    const advancedBatch: CollectionImportBatchSnapshot = {
      ...reviewBatch,
      revision: 8,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, item_revision: 9 }
        : item),
    };
    const rendered = render(<CollectionImportReviewControls batch={reviewBatch} detail={reviewDetail} {...callbacks} />);

    await user.type(screen.getByRole("textbox", { name: "自定义标题" }), "仍绑定旧 authority");
    rendered.rerender(<CollectionImportReviewControls batch={advancedBatch} detail={reviewDetail} {...callbacks} />);
    await user.click(screen.getByRole("button", { name: "标记保存" }));

    await waitFor(() => expect(collectionImportApi.updateItem).toHaveBeenCalledWith(
      "batch-1",
      "item-1",
      expect.objectContaining({
        expected_batch_revision: 4,
        expected_item_revision: 2,
        user_title: "仍绑定旧 authority",
      }),
    ));
  });

  it("dirty 离开先原位门禁，放弃后只执行一次原动作", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(reviewBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(reviewDetail);
    const callbacks = renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.type(await screen.findByRole("textbox", { name: "自定义标题" }), "未提交");
    const back = screen.getByRole("button", { name: "返回素材库" });
    await user.click(back);
    expect(callbacks.onBackToLibrary).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "先处理未提交修改" })).toHaveFocus();
    expect(screen.getByRole("button", { name: "更新后继续原动作" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(back).toHaveFocus();
    await user.click(back);

    await user.click(screen.getByRole("button", { name: "放弃未提交修改并继续原动作" }));
    expect(callbacks.onBackToLibrary).toHaveBeenCalledTimes(1);
    await user.keyboard("{Enter}");
    expect(callbacks.onBackToLibrary).toHaveBeenCalledTimes(1);
  });

  it("移动返回与浏览器 Back 的 dirty 门禁恢复到原触发器或原字段焦点", async () => {
    viewport(true);
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(reviewBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(reviewDetail);
    const callbacks = renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.type(await screen.findByRole("textbox", { name: "自定义标题" }), "未提交");
    const mobileBack = document.querySelector<HTMLButtonElement>(".batch-import-mobile-back")!;
    mobileBack.style.setProperty("display", "block", "important");
    await user.click(mobileBack);
    expect(screen.getByRole("heading", { name: "先处理未提交修改" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(mobileBack).toHaveFocus();

    const editedTitle = screen.getByRole("textbox", { name: "自定义标题" });
    editedTitle.focus();
    const registered = [...callbacks.registerLeaveGuard.mock.calls]
      .reverse()
      .map(([guard]) => guard)
      .find((guard): guard is () => boolean => typeof guard === "function");
    expect(registered).toBeTypeOf("function");
    act(() => { expect(registered?.()).toBe(true); });
    expect(screen.getByRole("heading", { name: "先处理未提交修改" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(editedTitle).toHaveFocus();
  });

  it("移动 item 的一次性 route focus 不会在权威详情刷新后抢走当前动作焦点", async () => {
    viewport(true);
    const user = userEvent.setup();
    const savedBatch = snapshotAfterDecision("save");
    const savedDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      decision: "save",
      batch_revision: savedBatch.revision,
      item_revision: savedBatch.items[0].item_revision,
    };
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(reviewBatch);
    vi.mocked(collectionImportApi.getItem)
      .mockResolvedValueOnce(reviewDetail)
      .mockResolvedValue(savedDetail);
    vi.mocked(collectionImportApi.updateItem).mockResolvedValue(savedBatch);
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1", focus: "item-heading" });

    expect(await screen.findByRole("heading", { name: "公开标题" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "标记保存" }));
    await waitFor(() => expect(vi.mocked(collectionImportApi.getItem).mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(screen.getByRole("button", { name: "更新本项审核" })).toHaveFocus();
  });

  it("更新后继续遇 CAS 时保留首次 continuation，人工 merge 成功后只执行一次", async () => {
    const user = userEvent.setup();
    const decidedBatch = {
      ...reviewBatch,
      selected: 1,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1" ? { ...item, decision: "save" as const } : item),
    };
    const decidedDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      decision: "save",
      draft: { ...reviewDetail.draft, user_title: "已记录标题" },
    };
    const latestBatch = { ...decidedBatch, revision: 8 };
    const latestDetail = { ...decidedDetail, batch_revision: 8, item_revision: 9 };
    vi.mocked(collectionImportApi.getBatch).mockResolvedValueOnce(decidedBatch).mockResolvedValueOnce(latestBatch);
    vi.mocked(collectionImportApi.getItem)
      .mockResolvedValueOnce(decidedDetail)
      .mockResolvedValueOnce(decidedDetail)
      .mockResolvedValueOnce(latestDetail);
    vi.mocked(collectionImportApi.updateItem)
      .mockRejectedValueOnce(new CollectionImportApiError("审核已变化", "BATCH_REVISION_CONFLICT", 409, null, null, null, 8))
      .mockResolvedValueOnce({ ...snapshotAfterDecision("save"), revision: 9 });
    const callbacks = renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.type(await screen.findByRole("textbox", { name: "自定义标题" }), "更新");
    await user.click(screen.getByRole("button", { name: "返回素材库" }));
    await user.click(screen.getByRole("button", { name: "更新后继续原动作" }));
    expect(await screen.findByRole("heading", { name: "审核已在别处更新" })).toBeInTheDocument();
    expect(callbacks.onBackToLibrary).not.toHaveBeenCalled();

    for (const label of ["审核决定", "自定义标题", "无标题确认", "一级分类", "二级分类", "整理标签", "个人标签", "我的灵感"]) {
      await user.click(screen.getByRole("radio", { name: `${label}：保留我的值` }));
    }
    await user.click(screen.getByRole("button", { name: "用最新 revision 重新记录决定" }));
    await waitFor(() => expect(callbacks.onBackToLibrary).toHaveBeenCalledTimes(1));
    expect(collectionImportApi.updateItem).toHaveBeenCalledTimes(2);
  });

  it("batch/item CAS 冲突 GET 权威详情并要求每个字段人工取舍，不自动重试", async () => {
    const user = userEvent.setup();
    const latestBatch = { ...reviewBatch, revision: 8 };
    const latestDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      batch_revision: 8,
      item_revision: 9,
      decision: "skip",
      draft: { ...reviewDetail.draft, user_title: "最新标题" },
    };
    vi.mocked(collectionImportApi.getBatch).mockResolvedValueOnce(reviewBatch).mockResolvedValueOnce(latestBatch);
    vi.mocked(collectionImportApi.getItem)
      .mockResolvedValueOnce(reviewDetail)
      .mockResolvedValueOnce(reviewDetail)
      .mockResolvedValueOnce(latestDetail);
    vi.mocked(collectionImportApi.updateItem)
      .mockRejectedValueOnce(new CollectionImportApiError("项目审核已更新", "BATCH_ITEM_REVISION_CONFLICT", 409, null, null, null, 9))
      .mockResolvedValueOnce({ ...snapshotAfterDecision("save"), revision: 9 });
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.type(await screen.findByRole("textbox", { name: "自定义标题" }), "我的标题");
    await user.click(screen.getByRole("button", { name: "标记保存" }));
    expect(await screen.findByRole("heading", { name: "审核已在别处更新" })).toHaveFocus();
    expect(collectionImportApi.updateItem).toHaveBeenCalledTimes(1);

    const firstChoice = screen.getByRole("radio", { name: "审核决定：保留我的值" });
    await user.click(firstChoice);
    expect(firstChoice).toHaveFocus();
    for (const label of ["自定义标题", "无标题确认", "一级分类", "二级分类", "整理标签", "个人标签", "我的灵感"]) {
      await user.click(screen.getByRole("radio", { name: `${label}：保留我的值` }));
    }
    await user.click(screen.getByRole("button", { name: "用最新 revision 重新记录决定" }));

    expect(collectionImportApi.updateItem).toHaveBeenCalledTimes(2);
    expect(vi.mocked(collectionImportApi.updateItem).mock.calls[1][2]).toMatchObject({
      expected_batch_revision: 8,
      expected_item_revision: 9,
      decision: "save",
      user_title: "我的标题",
    });
  });

  it("PATCH BATCH_STATE_CONFLICT 对账到可写 advanced authority 时也要求逐字段取舍", async () => {
    const user = userEvent.setup();
    const latestBatch = { ...reviewBatch, revision: 8 };
    const latestDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      batch_revision: 8,
      item_revision: 9,
      decision: "skip",
      draft: { ...reviewDetail.draft, user_title: "竞态后的权威标题" },
    };
    vi.mocked(collectionImportApi.getBatch).mockResolvedValueOnce(reviewBatch).mockResolvedValueOnce(latestBatch);
    vi.mocked(collectionImportApi.getItem)
      .mockResolvedValueOnce(reviewDetail)
      .mockResolvedValueOnce(reviewDetail)
      .mockResolvedValueOnce(latestDetail);
    vi.mocked(collectionImportApi.updateItem)
      .mockRejectedValue(new CollectionImportApiError("state changed", "BATCH_STATE_CONFLICT", 409));
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.type(await screen.findByRole("textbox", { name: "自定义标题" }), "我的标题");
    await user.click(screen.getByRole("button", { name: "标记保存" }));

    expect(await screen.findByRole("heading", { name: "审核已在别处更新" })).toBeInTheDocument();
    expect(screen.getByText("竞态后的权威标题")).toBeInTheDocument();
    expect(collectionImportApi.updateItem).toHaveBeenCalledTimes(1);
  });

  it("PATCH INVALID_RESPONSE 按传输未知先 GET；revision 精确未变才重新提供显式动作", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(reviewBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(reviewDetail);
    vi.mocked(collectionImportApi.updateItem)
      .mockRejectedValueOnce(new CollectionImportApiError("响应不可解析", "INVALID_RESPONSE", 200))
      .mockResolvedValueOnce(snapshotAfterDecision("save"));
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.type(title, "保留的本地标题");
    await user.click(screen.getByRole("button", { name: "标记保存" }));

    expect(await screen.findByText(/权威状态证明上次请求未生效/)).toBeInTheDocument();
    expect(collectionImportApi.updateItem).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("heading", { name: "审核已在别处更新" })).not.toBeInTheDocument();
    expect(title).toHaveValue("保留的本地标题");

    await user.click(screen.getByRole("button", { name: "标记保存" }));
    expect(collectionImportApi.updateItem).toHaveBeenCalledTimes(2);
  });

  it("PATCH 传输未知且权威已前进为不可写状态时不暴露逐字段重提入口", async () => {
    const user = userEvent.setup();
    const unknownBatch: CollectionImportBatchSnapshot = {
      ...reviewBatch,
      status: "interrupted",
      revision: 8,
      ready: 1,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, state: "outcome_unknown" as const, item_revision: 9 }
        : item),
    };
    const unknownDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      state: "outcome_unknown",
      batch_revision: 8,
      item_revision: 9,
    };
    vi.mocked(collectionImportApi.updateItem)
      .mockRejectedValueOnce(new CollectionImportApiError("connection lost", "NETWORK_ERROR", 0));
    vi.mocked(collectionImportApi.getBatch).mockImplementation(async () => (
      vi.mocked(collectionImportApi.updateItem).mock.calls.length ? unknownBatch : reviewBatch
    ));
    vi.mocked(collectionImportApi.getItem).mockImplementation(async () => (
      vi.mocked(collectionImportApi.updateItem).mock.calls.length ? unknownDetail : reviewDetail
    ));
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.type(title, "未知结果保留的标题");
    await user.click(screen.getByRole("button", { name: "标记保存" }));

    expect(await screen.findByText(/当前状态不允许记录这项决定/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "审核已在别处更新" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "用最新 revision 重新记录决定" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新权威状态" })).toBeEnabled();
    expect(title).toHaveValue("未知结果保留的标题");
    expect(screen.getByRole("button", { name: "标记保存" })).toBeDisabled();
    expect(collectionImportApi.updateItem).toHaveBeenCalledTimes(1);
  });

  it("权威 batch/item revision 连续两次不一致时阻断危险动作且不丢本地 draft", async () => {
    const user = userEvent.setup();
    const mismatchBatch = { ...reviewBatch, revision: 7 };
    const mismatchDetail = { ...reviewDetail, batch_revision: 6, item_revision: 5 };
    vi.mocked(collectionImportApi.updateItem).mockRejectedValue(new CollectionImportApiError("连接中断", "NETWORK_ERROR", 0));
    vi.mocked(collectionImportApi.getBatch).mockImplementation(async () => (
      vi.mocked(collectionImportApi.updateItem).mock.calls.length ? mismatchBatch : reviewBatch
    ));
    vi.mocked(collectionImportApi.getItem).mockImplementation(async () => (
      vi.mocked(collectionImportApi.updateItem).mock.calls.length ? mismatchDetail : reviewDetail
    ));
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.type(title, "不能丢的标题");
    await user.click(screen.getByRole("button", { name: "标记保存" }));

    expect(await screen.findByText(/无法确认上次审核请求是否生效/)).toBeInTheDocument();
    expect(title).toHaveValue("不能丢的标题");
    expect(screen.getByRole("button", { name: "标记保存" })).toBeDisabled();
    expect(collectionImportApi.updateItem).toHaveBeenCalledTimes(1);
  });

  it("首次权威 GET 失败后刷新到 advanced revision，必须先逐字段取舍且不能直接 PATCH", async () => {
    const user = userEvent.setup();
    const latestBatch = { ...reviewBatch, revision: 8 };
    const latestDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      batch_revision: 8,
      item_revision: 9,
      decision: "skip",
      draft: { ...reviewDetail.draft, user_title: "别处最新标题" },
    };
    vi.mocked(collectionImportApi.updateItem).mockRejectedValue(new CollectionImportApiError("连接中断", "NETWORK_ERROR", 0));
    vi.mocked(collectionImportApi.getBatch).mockImplementation(async () => {
      if (vi.mocked(collectionImportApi.updateItem).mock.calls.length) throw new Error("authority offline");
      return reviewBatch;
    });
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(reviewDetail);
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.type(title, "不能覆盖的本地标题");
    await user.click(screen.getByRole("button", { name: "标记保存" }));
    expect(await screen.findByText(/无法确认上次审核请求是否生效/)).toBeInTheDocument();

    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(latestBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(latestDetail);
    await user.click(screen.getByRole("button", { name: "刷新权威状态" }));

    expect(await screen.findByRole("heading", { name: "审核已在别处更新" })).toBeInTheDocument();
    expect(title).toHaveValue("不能覆盖的本地标题");
    expect(screen.getByRole("button", { name: "标记保存" })).toBeDisabled();
    expect(collectionImportApi.updateItem).toHaveBeenCalledTimes(1);
  });

  it("刷新到 advanced 权威状态时把 TagEditor 未添加文本纳入本地冲突草稿", async () => {
    const user = userEvent.setup();
    const latestBatch = { ...reviewBatch, revision: 8 };
    const latestDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      batch_revision: 8,
      item_revision: 9,
      decision: "skip",
      draft: { ...reviewDetail.draft, personal_tags: ["远端标签"] },
    };
    vi.mocked(collectionImportApi.updateItem).mockRejectedValue(new CollectionImportApiError("连接中断", "NETWORK_ERROR", 0));
    vi.mocked(collectionImportApi.getBatch).mockImplementation(async () => {
      if (vi.mocked(collectionImportApi.updateItem).mock.calls.length) throw new Error("authority offline");
      return reviewBatch;
    });
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(reviewDetail);
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    const pendingPersonalTag = await screen.findByRole("textbox", { name: "个人标签" });
    await user.type(pendingPersonalTag, "尚未点添加");
    await user.click(screen.getByRole("button", { name: "标记保存" }));
    expect(await screen.findByText(/无法确认上次审核请求是否生效/)).toBeInTheDocument();

    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(latestBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(latestDetail);
    await user.click(screen.getByRole("button", { name: "刷新权威状态" }));
    await screen.findByRole("heading", { name: "审核已在别处更新" });
    for (const label of ["审核决定", "自定义标题", "无标题确认", "一级分类", "二级分类", "整理标签", "个人标签", "我的灵感"]) {
      await user.click(screen.getByRole("radio", { name: `${label}：保留我的值` }));
    }
    await user.click(screen.getByRole("button", { name: "应用逐字段取舍" }));

    expect(screen.queryByRole("heading", { name: "审核已在别处更新" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "删除个人标签：尚未点添加" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "个人标签" })).toHaveValue("");
    expect(collectionImportApi.updateItem).toHaveBeenCalledTimes(1);
  });

  it("逐项 outcome_unknown 刷新后按钮仍存在时把焦点还给原刷新按钮", async () => {
    const user = userEvent.setup();
    const unknownBatch: CollectionImportBatchSnapshot = {
      ...reviewBatch,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, item_revision: 3, state: "outcome_unknown" as const }
        : item),
    };
    const unknownDetail: CollectionImportItemDetail = { ...reviewDetail, state: "outcome_unknown" };
    const latestBatch: CollectionImportBatchSnapshot = { ...unknownBatch, revision: 5 };
    const latestDetail: CollectionImportItemDetail = {
      ...unknownDetail,
      batch_revision: 5,
      item_revision: 3,
    };
    vi.mocked(collectionImportApi.getBatch).mockImplementation(async () => (
      vi.mocked(collectionImportApi.getBatch).mock.calls.length > 1 ? latestBatch : unknownBatch
    ));
    vi.mocked(collectionImportApi.getItem).mockImplementation(async () => (
      vi.mocked(collectionImportApi.getBatch).mock.calls.length > 1 ? latestDetail : unknownDetail
    ));
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    const trigger = await screen.findByRole("button", { name: "刷新权威状态" });
    await user.click(trigger);

    expect(await screen.findByText("已刷新权威状态")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新权威状态" })).toHaveFocus();
  });

  it("clean outcome_unknown 刷新到一致的 advanced 权威状态时直接收敛并恢复可审核", async () => {
    const user = userEvent.setup();
    const unknownBatch: CollectionImportBatchSnapshot = {
      ...reviewBatch,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, state: "outcome_unknown" as const }
        : item),
    };
    const unknownDetail: CollectionImportItemDetail = { ...reviewDetail, state: "outcome_unknown" };
    const latestBatch = { ...reviewBatch, revision: 5 };
    const latestDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      batch_revision: 5,
      item_revision: 3,
      state: "ready",
    };
    vi.mocked(collectionImportApi.getBatch).mockImplementation(async () => (
      vi.mocked(collectionImportApi.getBatch).mock.calls.length > 1 ? latestBatch : unknownBatch
    ));
    vi.mocked(collectionImportApi.getItem).mockImplementation(async () => (
      vi.mocked(collectionImportApi.getBatch).mock.calls.length > 1 ? latestDetail : unknownDetail
    ));
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.click(await screen.findByRole("button", { name: "刷新权威状态" }));
    expect(await screen.findByText("已刷新权威状态")).toBeInTheDocument();
    const status = screen.getByRole("heading", { name: "待审核" });
    expect(status).toBeInTheDocument();
    expect(status).toHaveAttribute("id", "batch-item-status-title");
    expect(status).toHaveFocus();
    expect(screen.queryByRole("button", { name: "刷新权威状态" })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "自定义标题" })).toBeEnabled();
    expect(screen.queryByRole("heading", { name: "审核已在别处更新" })).not.toBeInTheDocument();
  });

  it("preview generation 达上限时诚实说明稳定恢复路径且不自动发送危险请求", async () => {
    const attemptLimitBatch: CollectionImportBatchSnapshot = {
      ...reviewBatch,
      ready: 1,
      failed: 1,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1"
        ? {
            ...item,
            state: "failed" as const,
            preview_generation: 5,
            error_code: "BATCH_ATTEMPT_LIMIT",
          }
        : item),
    };
    const attemptLimitDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      state: "failed",
      preview_generation: 5,
      error_code: "BATCH_ATTEMPT_LIMIT",
      error_stage: "preview",
    };
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(attemptLimitBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(attemptLimitDetail);
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    expect(await screen.findByText("来源检查次数已达上限")).toBeInTheDocument();
    expect(screen.getByText("不能再次检查；可跳过本项、取消批次，或在新批次重新导入。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新检查来源" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "跳过本项" })).toBeEnabled();
    expect(within(screen.getByRole("region", { name: "批次恢复与取消" })).getByRole("button", { name: "取消批次" })).toBeInTheDocument();
    expect(collectionImportApi.updateItem).not.toHaveBeenCalled();
    expect(collectionImportApi.repreviewItem).not.toHaveBeenCalled();
    expect(collectionImportApi.confirmBatch).not.toHaveBeenCalled();
    expect(collectionImportApi.resumeBatch).not.toHaveBeenCalled();
    expect(collectionImportApi.cancelBatch).not.toHaveBeenCalled();
  });

  it("普通 failed preview 未达 generation 上限时仍显示显式重试与跳过", async () => {
    const retryableBatch: CollectionImportBatchSnapshot = {
      ...reviewBatch,
      ready: 1,
      failed: 1,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, state: "failed" as const, preview_generation: 4, error_code: "PREVIEW_FAILED" }
        : item),
    };
    const retryableDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      state: "failed",
      preview_generation: 4,
      error_code: "PREVIEW_FAILED",
      error_stage: "preview",
    };
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(retryableBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(retryableDetail);
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    expect(await screen.findByText("来源检查失败")).toBeInTheDocument();
    expect(screen.getByText("可以显式重新检查，或明确跳过本项。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新检查来源" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "跳过本项" })).toBeEnabled();
  });

  it("repreview 使用原位二次确认；202 后冻结审核、停留当前项并聚焦权威状态", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(reviewBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(reviewDetail);
    const callbacks = renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    const trigger = await screen.findByRole("button", { name: "重新检查来源" });
    await user.click(trigger);
    const replacement = screen.getByRole("textbox", { name: "可选替换输入" });
    await user.type(replacement, "替换分享 https://example.com/new");
    await user.click(screen.getByRole("button", { name: "确认重新检查来源" }));

    await waitFor(() => expect(collectionImportApi.repreviewItem).toHaveBeenCalledWith(
      "batch-1",
      "item-1",
      {
        expected_batch_revision: 4,
        expected_item_revision: 2,
        input_text: "替换分享 https://example.com/new",
      },
    ));
    const status = screen.getByRole("heading", { name: "等待检查" });
    expect(status).toHaveFocus();
    expect(screen.getByRole("textbox", { name: "自定义标题" })).toBeDisabled();
    expect(callbacks.onRouteChange).not.toHaveBeenCalled();
  });

  it("repreview 确认区展开时冻结背景审核字段与决定动作，撤回后恢复", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(reviewBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(reviewDetail);
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.click(await screen.findByRole("button", { name: "重新检查来源" }));
    expect(screen.getByRole("textbox", { name: "自定义标题" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "个人标签" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "标记保存" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "跳过本项" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "撤回重预览" }));
    expect(screen.getByRole("textbox", { name: "自定义标题" })).toBeEnabled();
    expect(screen.getByRole("textbox", { name: "个人标签" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "标记保存" })).toBeEnabled();
  });

  it("repreview 替换输入计入 dirty；门禁固定首次原动作且不让后续动作覆盖", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(reviewBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(reviewDetail);
    const callbacks = renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.click(await screen.findByRole("button", { name: "重新检查来源" }));
    await user.type(screen.getByRole("textbox", { name: "可选替换输入" }), "尚未提交的替换输入");
    await user.click(screen.getByRole("button", { name: "返回素材库" }));
    await user.click(screen.getByRole("button", { name: /2 第二项/ }));

    expect(callbacks.onBackToLibrary).not.toHaveBeenCalled();
    expect(callbacks.onRouteChange).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "放弃未提交修改并继续原动作" }));
    expect(callbacks.onBackToLibrary).toHaveBeenCalledTimes(1);
    expect(callbacks.onRouteChange).not.toHaveBeenCalled();
  });

  it("repreview 已知 CAS 拒绝不会把他处 queued 误认成本次成功，也不清空替换输入", async () => {
    const user = userEvent.setup();
    const queuedBatch: CollectionImportBatchSnapshot = {
      ...reviewBatch,
      revision: 8,
      status: "previewing",
      queued: 1,
      ready: 1,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, state: "queued" as const, item_revision: 9 }
        : item),
    };
    const queuedDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      batch_revision: 8,
      item_revision: 9,
      state: "queued",
    };
    vi.mocked(collectionImportApi.repreviewItem)
      .mockRejectedValue(new CollectionImportApiError("revision changed", "BATCH_ITEM_REVISION_CONFLICT", 409, null, null, null, 9));
    vi.mocked(collectionImportApi.getBatch).mockImplementation(async () => (
      vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? queuedBatch : reviewBatch
    ));
    vi.mocked(collectionImportApi.getItem).mockImplementation(async () => (
      vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? queuedDetail : reviewDetail
    ));
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.click(await screen.findByRole("button", { name: "重新检查来源" }));
    await user.type(screen.getByRole("textbox", { name: "可选替换输入" }), "必须保留");
    await user.click(screen.getByRole("button", { name: "确认重新检查来源" }));

    expect(await screen.findByText(/已被 revision 冲突拒绝/)).toBeInTheDocument();
    expect(screen.queryByText(/已排队重新检查来源/)).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "可选替换输入" })).toHaveValue("必须保留");
    expect(screen.getByRole("button", { name: "确认重新检查来源" })).toBeDisabled();
    expect(collectionImportApi.repreviewItem).toHaveBeenCalledTimes(1);
  });

  it("repreview 传输未知看到他处 queued 时不归因为本次成功且保留替换输入", async () => {
    const user = userEvent.setup();
    const queuedBatch: CollectionImportBatchSnapshot = {
      ...reviewBatch,
      revision: 8,
      status: "previewing",
      queued: 1,
      ready: 1,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, state: "queued" as const, item_revision: 9 }
        : item),
    };
    const queuedDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      batch_revision: 8,
      item_revision: 9,
      state: "queued",
      input_text: "另一标签页提交的替换输入",
    };
    vi.mocked(collectionImportApi.repreviewItem)
      .mockRejectedValueOnce(new CollectionImportApiError("connection lost", "NETWORK_ERROR", 0));
    vi.mocked(collectionImportApi.getBatch).mockImplementation(async () => (
      vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? queuedBatch : reviewBatch
    ));
    vi.mocked(collectionImportApi.getItem).mockImplementation(async () => (
      vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? queuedDetail : reviewDetail
    ));
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.click(await screen.findByRole("button", { name: "重新检查来源" }));
    await user.type(screen.getByRole("textbox", { name: "可选替换输入" }), "我的替换输入");
    await user.click(screen.getByRole("button", { name: "确认重新检查来源" }));

    expect(await screen.findByText(/当前状态不允许重新检查来源/)).toBeInTheDocument();
    expect(screen.queryByText(/已排队重新检查来源/)).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "可选替换输入" })).toHaveValue("我的替换输入");
    expect(screen.getByRole("button", { name: "确认重新检查来源" })).toBeDisabled();
    expect(collectionImportApi.repreviewItem).toHaveBeenCalledTimes(1);
  });

  it("repreview INVALID_RESPONSE 只有在 GET 证明 revision 未变后才保留输入并允许再次明确确认", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(reviewBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(reviewDetail);
    vi.mocked(collectionImportApi.repreviewItem)
      .mockRejectedValueOnce(new CollectionImportApiError("响应不可解析", "INVALID_RESPONSE", 200))
      .mockResolvedValueOnce({
        ...reviewBatch,
        status: "previewing",
        revision: 5,
        queued: 1,
        ready: 1,
        items: reviewBatch.items.map((item) => item.batch_item_id === "item-1"
          ? { ...item, state: "queued", item_revision: 3 }
          : item),
      });
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.click(await screen.findByRole("button", { name: "重新检查来源" }));
    const replacement = screen.getByRole("textbox", { name: "可选替换输入" });
    await user.type(replacement, "保留替换输入");
    await user.click(screen.getByRole("button", { name: "确认重新检查来源" }));

    expect(await screen.findByText(/已证明上次重预览未生效/)).toBeInTheDocument();
    expect(collectionImportApi.repreviewItem).toHaveBeenCalledTimes(1);
    expect(replacement).toHaveValue("保留替换输入");
    await user.click(screen.getByRole("button", { name: "确认重新检查来源" }));
    expect(collectionImportApi.repreviewItem).toHaveBeenCalledTimes(2);
  });

  it("repreview 传输未知且权威已前进为不可写状态时不暴露冲突重提入口", async () => {
    const user = userEvent.setup();
    const unknownBatch: CollectionImportBatchSnapshot = {
      ...reviewBatch,
      status: "interrupted",
      revision: 8,
      ready: 1,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, state: "outcome_unknown" as const, item_revision: 9 }
        : item),
    };
    const unknownDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      state: "outcome_unknown",
      batch_revision: 8,
      item_revision: 9,
    };
    vi.mocked(collectionImportApi.repreviewItem)
      .mockRejectedValueOnce(new CollectionImportApiError("connection lost", "NETWORK_ERROR", 0));
    vi.mocked(collectionImportApi.getBatch).mockImplementation(async () => (
      vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? unknownBatch : reviewBatch
    ));
    vi.mocked(collectionImportApi.getItem).mockImplementation(async () => (
      vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? unknownDetail : reviewDetail
    ));
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.click(await screen.findByRole("button", { name: "重新检查来源" }));
    await user.type(screen.getByRole("textbox", { name: "可选替换输入" }), "不确定时保留");
    await user.click(screen.getByRole("button", { name: "确认重新检查来源" }));

    expect(await screen.findByText(/当前状态不允许重新检查来源/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "审核已在别处更新" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "记录取舍并重新检查来源" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新权威状态" })).toBeEnabled();
    expect(screen.getByRole("textbox", { name: "可选替换输入" })).toHaveValue("不确定时保留");
    expect(screen.getByRole("button", { name: "确认重新检查来源" })).toBeDisabled();
    expect(collectionImportApi.repreviewItem).toHaveBeenCalledTimes(1);
  });

  it("repreview CAS 选择我的字段时先 PATCH 持久取舍，再用新 revision 恰好一次重预览", async () => {
    const user = userEvent.setup();
    const mineBatch = {
      ...reviewBatch,
      selected: 1,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1" ? { ...item, decision: "save" as const } : item),
    };
    const mineDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      decision: "save",
      draft: { ...reviewDetail.draft, user_title: "我的旧标题" },
    };
    const latestBatch = { ...mineBatch, revision: 8 };
    const latestDetail: CollectionImportItemDetail = {
      ...mineDetail,
      batch_revision: 8,
      item_revision: 9,
      draft: { ...mineDetail.draft, user_title: "最新标题" },
    };
    const patchedBatch = {
      ...mineBatch,
      revision: 9,
      items: mineBatch.items.map((item) => item.batch_item_id === "item-1" ? { ...item, item_revision: 10 } : item),
    };
    const queuedBatch = {
      ...patchedBatch,
      revision: 10,
      status: "previewing" as const,
      queued: 1,
      ready: 1,
      selected: 0,
      items: patchedBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, state: "queued" as const, decision: "pending" as const, item_revision: 11 }
        : item),
    };
    vi.mocked(collectionImportApi.repreviewItem)
      .mockRejectedValueOnce(new CollectionImportApiError("revision changed", "BATCH_ITEM_REVISION_CONFLICT", 409, null, null, null, 9))
      .mockResolvedValueOnce(queuedBatch);
    vi.mocked(collectionImportApi.getBatch).mockImplementation(async () => (
      vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? latestBatch : mineBatch
    ));
    vi.mocked(collectionImportApi.getItem).mockImplementation(async () => (
      vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? latestDetail : mineDetail
    ));
    vi.mocked(collectionImportApi.updateItem).mockResolvedValue(patchedBatch);
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.click(await screen.findByRole("button", { name: "重新检查来源" }));
    await user.type(screen.getByRole("textbox", { name: "可选替换输入" }), "替换输入");
    await user.click(screen.getByRole("button", { name: "确认重新检查来源" }));
    expect(await screen.findByRole("heading", { name: "审核已在别处更新" })).toBeInTheDocument();

    for (const label of ["审核决定", "自定义标题", "无标题确认", "一级分类", "二级分类", "整理标签", "个人标签", "我的灵感"]) {
      await user.click(screen.getByRole("radio", { name: `${label}：保留我的值` }));
    }
    await user.click(screen.getByRole("button", { name: "记录取舍并重新检查来源" }));

    await waitFor(() => expect(collectionImportApi.updateItem).toHaveBeenCalledWith(
      "batch-1",
      "item-1",
      expect.objectContaining({
        expected_batch_revision: 8,
        expected_item_revision: 9,
        decision: "save",
        user_title: "我的旧标题",
      }),
    ));
    expect(collectionImportApi.repreviewItem).toHaveBeenLastCalledWith(
      "batch-1",
      "item-1",
      {
        expected_batch_revision: 9,
        expected_item_revision: 10,
        input_text: "替换输入",
      },
    );
    expect(vi.mocked(collectionImportApi.updateItem).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(collectionImportApi.repreviewItem).mock.invocationCallOrder[1],
    );
    expect(collectionImportApi.repreviewItem).toHaveBeenCalledTimes(2);
  });

  it("repreview 的 mine-PATCH 再遇 CAS 时继续保留 continuation，第二次取舍后只重预览一次", async () => {
    const user = userEvent.setup();
    const mineBatch = {
      ...reviewBatch,
      selected: 1,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1" ? { ...item, decision: "save" as const } : item),
    };
    const mineDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      decision: "save",
      draft: { ...reviewDetail.draft, user_title: "我的标题" },
    };
    const latestBatch1 = { ...mineBatch, revision: 8 };
    const latestDetail1: CollectionImportItemDetail = {
      ...mineDetail,
      batch_revision: 8,
      item_revision: 9,
      draft: { ...mineDetail.draft, user_title: "第一版最新" },
    };
    const latestBatch2 = { ...mineBatch, revision: 12 };
    const latestDetail2: CollectionImportItemDetail = {
      ...mineDetail,
      batch_revision: 12,
      item_revision: 13,
      draft: { ...mineDetail.draft, user_title: "第二版最新" },
    };
    const patchedBatch = {
      ...mineBatch,
      revision: 13,
      items: mineBatch.items.map((item) => item.batch_item_id === "item-1" ? { ...item, item_revision: 14 } : item),
    };
    const queuedBatch = {
      ...patchedBatch,
      revision: 14,
      status: "previewing" as const,
      queued: 1,
      ready: 1,
      items: patchedBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, state: "queued" as const, decision: "pending" as const, item_revision: 15 }
        : item),
    };
    vi.mocked(collectionImportApi.repreviewItem)
      .mockRejectedValueOnce(new CollectionImportApiError("first conflict", "BATCH_ITEM_REVISION_CONFLICT", 409, null, null, null, 9))
      .mockResolvedValueOnce(queuedBatch);
    vi.mocked(collectionImportApi.updateItem)
      .mockRejectedValueOnce(new CollectionImportApiError("second conflict", "BATCH_REVISION_CONFLICT", 409, null, null, null, 12))
      .mockResolvedValueOnce(patchedBatch);
    vi.mocked(collectionImportApi.getBatch).mockImplementation(async () => (
      vi.mocked(collectionImportApi.updateItem).mock.calls.length ? latestBatch2
        : vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? latestBatch1 : mineBatch
    ));
    vi.mocked(collectionImportApi.getItem).mockImplementation(async () => (
      vi.mocked(collectionImportApi.updateItem).mock.calls.length ? latestDetail2
        : vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? latestDetail1 : mineDetail
    ));
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.click(await screen.findByRole("button", { name: "重新检查来源" }));
    await user.type(screen.getByRole("textbox", { name: "可选替换输入" }), "两轮冲突后仍保留");
    await user.click(screen.getByRole("button", { name: "确认重新检查来源" }));
    const chooseMine = async () => {
      for (const label of ["审核决定", "自定义标题", "无标题确认", "一级分类", "二级分类", "整理标签", "个人标签", "我的灵感"]) {
        await user.click(screen.getByRole("radio", { name: `${label}：保留我的值` }));
      }
    };
    await screen.findByRole("heading", { name: "审核已在别处更新" });
    await chooseMine();
    await user.click(screen.getByRole("button", { name: "记录取舍并重新检查来源" }));
    await screen.findByText("第二版最新");
    await chooseMine();
    await user.click(screen.getByRole("button", { name: "记录取舍并重新检查来源" }));

    await waitFor(() => expect(collectionImportApi.repreviewItem).toHaveBeenCalledTimes(2));
    expect(collectionImportApi.updateItem).toHaveBeenCalledTimes(2);
    expect(collectionImportApi.repreviewItem).toHaveBeenLastCalledWith("batch-1", "item-1", {
      expected_batch_revision: 13,
      expected_item_revision: 14,
      input_text: "两轮冲突后仍保留",
    });
  });

  it("repreview 冲突内二次 PATCH 遇 state conflict 且 latest 不可写时清除旧冲突、保留草稿与替换输入", async () => {
    const user = userEvent.setup();
    const mineBatch = {
      ...reviewBatch,
      selected: 1,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, decision: "save" as const }
        : item),
    };
    const mineDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      decision: "save",
      draft: { ...reviewDetail.draft, user_title: "我的标题" },
    };
    const latestBatch = { ...mineBatch, revision: 8 };
    const latestDetail: CollectionImportItemDetail = {
      ...mineDetail,
      batch_revision: 8,
      item_revision: 9,
      draft: { ...mineDetail.draft, user_title: "最新标题" },
    };
    const savingBatch: CollectionImportBatchSnapshot = {
      ...mineBatch,
      status: "saving",
      revision: 12,
      ready: 1,
      items: mineBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, state: "saving" as const, item_revision: 13 }
        : item),
    };
    const savingDetail: CollectionImportItemDetail = {
      ...mineDetail,
      state: "saving",
      batch_revision: 12,
      item_revision: 13,
      draft: { ...mineDetail.draft, user_title: "保存中的权威标题" },
    };
    vi.mocked(collectionImportApi.repreviewItem)
      .mockRejectedValueOnce(new CollectionImportApiError("first conflict", "BATCH_ITEM_REVISION_CONFLICT", 409, null, null, null, 9));
    vi.mocked(collectionImportApi.updateItem)
      .mockRejectedValueOnce(new CollectionImportApiError("state changed", "BATCH_STATE_CONFLICT", 409));
    vi.mocked(collectionImportApi.getBatch).mockImplementation(async () => (
      vi.mocked(collectionImportApi.updateItem).mock.calls.length ? savingBatch
        : vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? latestBatch : mineBatch
    ));
    vi.mocked(collectionImportApi.getItem).mockImplementation(async () => (
      vi.mocked(collectionImportApi.updateItem).mock.calls.length ? savingDetail
        : vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? latestDetail : mineDetail
    ));
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.click(await screen.findByRole("button", { name: "重新检查来源" }));
    await user.type(screen.getByRole("textbox", { name: "可选替换输入" }), "状态变化后仍保留");
    await user.click(screen.getByRole("button", { name: "确认重新检查来源" }));
    await screen.findByRole("heading", { name: "审核已在别处更新" });
    for (const label of ["审核决定", "自定义标题", "无标题确认", "一级分类", "二级分类", "整理标签", "个人标签", "我的灵感"]) {
      await user.click(screen.getByRole("radio", { name: `${label}：保留我的值` }));
    }
    await user.click(screen.getByRole("button", { name: "记录取舍并重新检查来源" }));

    expect(await screen.findByText(/权威状态已改变，本次决定没有记录/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "审核已在别处更新" })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "自定义标题" })).toHaveValue("我的标题");
    expect(screen.getByRole("textbox", { name: "可选替换输入" })).toHaveValue("状态变化后仍保留");
    expect(screen.getByRole("button", { name: "确认重新检查来源" })).toBeDisabled();
    expect(collectionImportApi.updateItem).toHaveBeenCalledTimes(1);
    expect(collectionImportApi.repreviewItem).toHaveBeenCalledTimes(1);
  });

  it("repreview 冲突内二次 PATCH 无法读取 authority 时冻结旧冲突，不允许再次提交", async () => {
    const user = userEvent.setup();
    const mineBatch = {
      ...reviewBatch,
      selected: 1,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, decision: "save" as const }
        : item),
    };
    const mineDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      decision: "save",
      draft: { ...reviewDetail.draft, user_title: "我的标题" },
    };
    const latestBatch = { ...mineBatch, revision: 8 };
    const latestDetail: CollectionImportItemDetail = {
      ...mineDetail,
      batch_revision: 8,
      item_revision: 9,
      draft: { ...mineDetail.draft, user_title: "最新标题" },
    };
    vi.mocked(collectionImportApi.repreviewItem)
      .mockRejectedValueOnce(new CollectionImportApiError("first conflict", "BATCH_ITEM_REVISION_CONFLICT", 409, null, null, null, 9));
    vi.mocked(collectionImportApi.updateItem)
      .mockRejectedValueOnce(new CollectionImportApiError("connection lost", "NETWORK_ERROR", 0));
    vi.mocked(collectionImportApi.getBatch).mockImplementation(async () => {
      if (vi.mocked(collectionImportApi.updateItem).mock.calls.length) throw new Error("authority offline");
      return vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? latestBatch : mineBatch;
    });
    vi.mocked(collectionImportApi.getItem).mockImplementation(async () => (
      vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? latestDetail : mineDetail
    ));
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.click(await screen.findByRole("button", { name: "重新检查来源" }));
    await user.type(screen.getByRole("textbox", { name: "可选替换输入" }), "权威失败后仍保留");
    await user.click(screen.getByRole("button", { name: "确认重新检查来源" }));
    await screen.findByRole("heading", { name: "审核已在别处更新" });
    for (const label of ["审核决定", "自定义标题", "无标题确认", "一级分类", "二级分类", "整理标签", "个人标签", "我的灵感"]) {
      await user.click(screen.getByRole("radio", { name: `${label}：保留我的值` }));
    }
    await user.click(screen.getByRole("button", { name: "记录取舍并重新检查来源" }));

    expect(await screen.findByText(/无法确认上次审核请求是否生效/)).toBeInTheDocument();
    const staleConflictAction = screen.getByRole("button", { name: "记录取舍并重新检查来源" });
    expect(staleConflictAction).toBeDisabled();
    expect(screen.getByRole("button", { name: "刷新权威状态" })).toBeEnabled();
    expect(screen.getByRole("textbox", { name: "自定义标题" })).toHaveValue("我的标题");
    await user.click(staleConflictAction);
    expect(collectionImportApi.updateItem).toHaveBeenCalledTimes(1);
    expect(collectionImportApi.repreviewItem).toHaveBeenCalledTimes(1);
  });

  it("repreview CAS 的取舍 PATCH 传输未知但被观察为成功时，恢复确认区且不自动发送重预览", async () => {
    const user = userEvent.setup();
    const mineBatch = {
      ...reviewBatch,
      selected: 1,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1" ? { ...item, decision: "save" as const } : item),
    };
    const mineDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      decision: "save",
      draft: { ...reviewDetail.draft, user_title: "我的旧标题" },
    };
    const latestBatch = { ...mineBatch, revision: 8 };
    const latestDetail: CollectionImportItemDetail = {
      ...mineDetail,
      batch_revision: 8,
      item_revision: 9,
      draft: { ...mineDetail.draft, user_title: "最新标题" },
    };
    const observedBatch = {
      ...mineBatch,
      revision: 9,
      items: mineBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, decision: "save" as const, item_revision: 10 }
        : item),
    };
    const observedDetail: CollectionImportItemDetail = {
      ...mineDetail,
      batch_revision: 9,
      item_revision: 10,
      decision: "save",
    };
    vi.mocked(collectionImportApi.repreviewItem)
      .mockRejectedValue(new CollectionImportApiError("revision changed", "BATCH_ITEM_REVISION_CONFLICT", 409, null, null, null, 9));
    vi.mocked(collectionImportApi.updateItem)
      .mockRejectedValue(new CollectionImportApiError("连接中断", "NETWORK_ERROR", 0));
    vi.mocked(collectionImportApi.getBatch).mockImplementation(async () => (
      vi.mocked(collectionImportApi.updateItem).mock.calls.length
        ? observedBatch
        : vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? latestBatch : mineBatch
    ));
    vi.mocked(collectionImportApi.getItem).mockImplementation(async () => (
      vi.mocked(collectionImportApi.updateItem).mock.calls.length
        ? observedDetail
        : vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? latestDetail : mineDetail
    ));
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.click(await screen.findByRole("button", { name: "重新检查来源" }));
    await user.type(screen.getByRole("textbox", { name: "可选替换输入" }), "保留替换输入");
    await user.click(screen.getByRole("button", { name: "确认重新检查来源" }));
    await screen.findByRole("heading", { name: "审核已在别处更新" });
    for (const label of ["审核决定", "自定义标题", "无标题确认", "一级分类", "二级分类", "整理标签", "个人标签", "我的灵感"]) {
      await user.click(screen.getByRole("radio", { name: `${label}：保留我的值` }));
    }
    await user.click(screen.getByRole("button", { name: "记录取舍并重新检查来源" }));

    expect(await screen.findByText(/重预览尚未发送/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "可选替换输入" })).toHaveValue("保留替换输入");
    expect(screen.getByRole("button", { name: "确认重新检查来源" })).toBeEnabled();
    expect(collectionImportApi.updateItem).toHaveBeenCalledTimes(1);
    expect(collectionImportApi.repreviewItem).toHaveBeenCalledTimes(1);
  });

  it("取舍 PATCH 成功后重预览被证明未生效时，恢复确认区、保留输入且不自动重发", async () => {
    const user = userEvent.setup();
    const mineBatch = {
      ...reviewBatch,
      selected: 1,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1" ? { ...item, decision: "save" as const } : item),
    };
    const mineDetail: CollectionImportItemDetail = {
      ...reviewDetail,
      decision: "save",
      draft: { ...reviewDetail.draft, user_title: "我的旧标题" },
    };
    const latestBatch = { ...mineBatch, revision: 8 };
    const latestDetail: CollectionImportItemDetail = {
      ...mineDetail,
      batch_revision: 8,
      item_revision: 9,
      draft: { ...mineDetail.draft, user_title: "最新标题" },
    };
    const patchedBatch = {
      ...mineBatch,
      revision: 9,
      items: mineBatch.items.map((item) => item.batch_item_id === "item-1" ? { ...item, item_revision: 10 } : item),
    };
    const patchedDetail: CollectionImportItemDetail = {
      ...mineDetail,
      batch_revision: 9,
      item_revision: 10,
    };
    vi.mocked(collectionImportApi.repreviewItem)
      .mockRejectedValueOnce(new CollectionImportApiError("revision changed", "BATCH_ITEM_REVISION_CONFLICT", 409, null, null, null, 9))
      .mockRejectedValueOnce(new CollectionImportApiError("连接中断", "NETWORK_ERROR", 0));
    vi.mocked(collectionImportApi.updateItem).mockResolvedValue(patchedBatch);
    vi.mocked(collectionImportApi.getBatch).mockImplementation(async () => (
      vi.mocked(collectionImportApi.updateItem).mock.calls.length
        ? patchedBatch
        : vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? latestBatch : mineBatch
    ));
    vi.mocked(collectionImportApi.getItem).mockImplementation(async () => (
      vi.mocked(collectionImportApi.updateItem).mock.calls.length
        ? patchedDetail
        : vi.mocked(collectionImportApi.repreviewItem).mock.calls.length ? latestDetail : mineDetail
    ));
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.click(await screen.findByRole("button", { name: "重新检查来源" }));
    await user.type(screen.getByRole("textbox", { name: "可选替换输入" }), "保留替换输入");
    await user.click(screen.getByRole("button", { name: "确认重新检查来源" }));
    await screen.findByRole("heading", { name: "审核已在别处更新" });
    for (const label of ["审核决定", "自定义标题", "无标题确认", "一级分类", "二级分类", "整理标签", "个人标签", "我的灵感"]) {
      await user.click(screen.getByRole("radio", { name: `${label}：保留我的值` }));
    }
    await user.click(screen.getByRole("button", { name: "记录取舍并重新检查来源" }));

    expect(await screen.findByText(/已证明上次重预览未生效/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "可选替换输入" })).toHaveValue("保留替换输入");
    expect(collectionImportApi.updateItem).toHaveBeenCalledTimes(1);
    expect(collectionImportApi.repreviewItem).toHaveBeenCalledTimes(2);
  });

  it("visualViewport 只设置一个键盘 offset 并清理监听，IME Enter 不触发危险请求", async () => {
    const originalViewport = Object.getOwnPropertyDescriptor(window, "visualViewport");
    const originalInnerHeight = Object.getOwnPropertyDescriptor(window, "innerHeight");
    const visual = new EventTarget() as VisualViewport;
    Object.defineProperties(visual, {
      height: { configurable: true, value: 500 },
      offsetTop: { configurable: true, value: 20 },
    });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 800 });
    Object.defineProperty(window, "visualViewport", { configurable: true, value: visual });
    const remove = vi.spyOn(visual, "removeEventListener");
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(reviewBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(reviewDetail);
    const rendered = renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await screen.findByRole("textbox", { name: "自定义标题" });
    const page = rendered.container.querySelector<HTMLElement>(".batch-import-page");
    expect(page?.style.getPropertyValue("--batch-import-keyboard-offset")).toBe("280px");
    Object.defineProperty(visual, "height", { configurable: true, value: 650 });
    act(() => visual.dispatchEvent(new Event("resize")));
    expect(page?.style.getPropertyValue("--batch-import-keyboard-offset")).toBe("130px");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "重新检查来源" }));
    fireEvent.keyDown(screen.getByRole("textbox", { name: "可选替换输入" }), { key: "Enter", isComposing: true });
    expect(collectionImportApi.updateItem).not.toHaveBeenCalled();
    expect(collectionImportApi.repreviewItem).not.toHaveBeenCalled();

    rendered.unmount();
    expect(remove).toHaveBeenCalledWith("resize", expect.any(Function));
    expect(remove).toHaveBeenCalledWith("scroll", expect.any(Function));
    if (originalViewport) Object.defineProperty(window, "visualViewport", originalViewport);
    else Reflect.deleteProperty(window, "visualViewport");
    if (originalInnerHeight) Object.defineProperty(window, "innerHeight", originalInnerHeight);
  });

  it("首次动作只打开同批次 confirm 路由，不发送确认请求", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(confirmReadyBatch);
    const callbacks = renderPage({ batchId: "batch-1", view: "list" });

    const trigger = await screen.findByRole("button", { name: "核对并保存已选 1 项" });
    expect(trigger).toBeEnabled();
    await user.click(trigger);

    expect(callbacks.onRouteChange).toHaveBeenCalledWith(expect.objectContaining({
      batchId: "batch-1",
      view: "confirm",
      focus: "confirm-heading",
    }));
    expect(collectionImportApi.confirmBatch).not.toHaveBeenCalled();
  });

  it("partial retry 的入口与摘要只计算本轮决定，不把历史 saved/already_exists/skipped 重复计入", async () => {
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(partialRetryBatch);
    const list = renderPage({ batchId: "batch-1", view: "list" });
    expect(await screen.findByRole("button", { name: "核对并保存已选 1 项" })).toBeEnabled();
    expect(screen.getByText("已完成审核，可核对 1 项保存决定")).toBeInTheDocument();
    list.unmount();

    renderPage({ batchId: "batch-1", view: "confirm" });
    const final = await screen.findByRole("button", { name: "确认保存 1 项" });
    const surface = final.closest<HTMLElement>(".batch-import-confirm-surface");
    expect(surface).not.toBeNull();
    const terms = within(surface!).getAllByRole("term");
    const values = within(surface!).getAllByRole("definition");
    expect(terms.map((node) => node.textContent)).toEqual(["保存", "跳过", "批内重复", "已存在", "失败"]);
    expect(values.map((node) => node.textContent)).toEqual(["1", "1", "0", "1", "1"]);
  });

  it("真实 dirty 包含字段、TagEditor pending 与替换输入，并直接禁用确认入口", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(confirmReadyBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(confirmReadyDetail);
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    const trigger = await screen.findByRole("button", { name: "核对并保存已选 1 项" });
    expect(trigger).toBeEnabled();
    await user.type(screen.getByRole("textbox", { name: "整理标签" }), "待加入");
    expect(trigger).toBeDisabled();
    expect(screen.queryByRole("heading", { name: "先处理未提交修改" })).not.toBeInTheDocument();
    expect(collectionImportApi.confirmBatch).not.toHaveBeenCalled();
  });

  it("confirm 直达先 GET 复核且绝不自动 POST；桌面保留当前项但摘要不复制输入或草稿", async () => {
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(confirmReadyBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(confirmReadyDetail);
    const rendered = renderPage({ batchId: "batch-1", view: "confirm", itemId: "item-1" });

    const heading = await screen.findByRole("heading", { name: "核对本次批次决定" });
    expect(collectionImportApi.getBatch).toHaveBeenCalledWith("batch-1", expect.any(AbortSignal));
    expect(collectionImportApi.getItem).toHaveBeenCalledWith("batch-1", "item-1", expect.any(AbortSignal));
    expect(collectionImportApi.confirmBatch).not.toHaveBeenCalled();
    await waitFor(() => expect(heading).toHaveFocus());
    expect(screen.getByRole("textbox", { name: "自定义标题" })).toBeDisabled();

    const surface = rendered.container.querySelector<HTMLElement>(".batch-import-confirm-surface");
    expect(surface).not.toBeNull();
    expect(within(surface!).getByText("保存")).toBeInTheDocument();
    expect(within(surface!).getByText("跳过")).toBeInTheDocument();
    expect(within(surface!).queryByText(firstInput)).not.toBeInTheDocument();
    expect(within(surface!).queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("第二次动作只 POST 精确 revision 一次，202 后 replace 到 list 并冻结当前编辑", async () => {
    let resolveConfirm: ((snapshot: CollectionImportBatchSnapshot) => void) | null = null;
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(confirmReadyBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(confirmReadyDetail);
    vi.mocked(collectionImportApi.confirmBatch).mockImplementation(() => new Promise((resolve) => { resolveConfirm = resolve; }));
    const callbacks = renderPage({ batchId: "batch-1", view: "confirm", itemId: "item-1" });
    const button = await screen.findByRole("button", { name: "确认保存 1 项" });

    fireEvent.click(button);
    fireEvent.click(button);
    expect(collectionImportApi.confirmBatch).toHaveBeenCalledTimes(1);
    expect(collectionImportApi.confirmBatch).toHaveBeenCalledWith("batch-1", { expected_batch_revision: 8 });
    expect(screen.getByRole("textbox", { name: "自定义标题" })).toBeDisabled();
    await act(async () => { resolveConfirm?.(savingBatch); });

    expect(callbacks.onRouteChange).toHaveBeenCalledWith({
      batchId: "batch-1",
      view: "list",
      focus: "batch-status",
    }, true);
  });

  it("CAS 冲突只 GET 最新并停止，要求返回审核后显式重新激活", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(confirmReadyBatch)
      .mockResolvedValueOnce(confirmReadyBatch);
    vi.mocked(collectionImportApi.confirmBatch).mockRejectedValue(new CollectionImportApiError(
      "批次 revision 已更新",
      "BATCH_REVISION_CONFLICT",
      409,
      null,
      null,
      null,
      8,
    ));
    const callbacks = renderPage({ batchId: "batch-1", view: "confirm" });

    await user.click(await screen.findByRole("button", { name: "确认保存 1 项" }));
    expect(await screen.findByRole("heading", { name: "本次确认已停止" })).toBeInTheDocument();
    expect(screen.getByText(/返回审核并按最新权威状态重新打开核对/)).toBeInTheDocument();
    expect(collectionImportApi.getBatch).toHaveBeenCalledTimes(2);
    expect(collectionImportApi.confirmBatch).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "返回审核" }));
    expect(callbacks.onBackFromConfirm).toHaveBeenCalledTimes(1);
  });

  it("transport unknown 坚持 GET-first：未观察到冻结时不重放，revision 前进也不误归因", async () => {
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(confirmReadyBatch)
      .mockResolvedValueOnce(advancedReviewBatch);
    vi.mocked(collectionImportApi.confirmBatch).mockRejectedValue(new CollectionImportApiError(
      "无法连接批量导入服务",
      "NETWORK_ERROR",
      0,
    ));
    renderPage({ batchId: "batch-1", view: "confirm" });

    fireEvent.click(await screen.findByRole("button", { name: "确认保存 1 项" }));
    expect(await screen.findByRole("heading", { name: "本次确认已停止" })).toBeInTheDocument();
    expect(screen.getByText(/没有证明上次确认已生效/)).toBeInTheDocument();
    expect(collectionImportApi.confirmBatch).toHaveBeenCalledTimes(1);
    expect(collectionImportApi.getBatch).toHaveBeenCalledTimes(2);
  });

  it.each([
    ["NETWORK_ERROR", new CollectionImportApiError("网络未知", "NETWORK_ERROR", 0)],
    ["INVALID_RESPONSE", null],
  ])("%s 且 GET exact-unchanged 时只阻断，不自动重放", async (_code, rejection) => {
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(confirmReadyBatch)
      .mockResolvedValueOnce(confirmReadyBatch);
    if (rejection) vi.mocked(collectionImportApi.confirmBatch).mockRejectedValue(rejection);
    else vi.mocked(collectionImportApi.confirmBatch).mockResolvedValue(confirmReadyBatch);
    renderPage({ batchId: "batch-1", view: "confirm" });

    fireEvent.click(await screen.findByRole("button", { name: "确认保存 1 项" }));
    expect(await screen.findByRole("heading", { name: "本次确认已停止" })).toBeInTheDocument();
    expect(screen.getByText(/没有证明上次确认已生效/)).toBeInTheDocument();
    expect(collectionImportApi.confirmBatch).toHaveBeenCalledTimes(1);
    expect(collectionImportApi.getBatch).toHaveBeenCalledTimes(2);
  });

  it("authority GET 首次失败后显式刷新可继续对账，但绝不自动发第二次 confirm", async () => {
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(confirmReadyBatch)
      .mockRejectedValueOnce(new CollectionImportApiError("read failed", "NETWORK_ERROR", 0))
      .mockResolvedValueOnce(failedAfterConfirmBatch);
    vi.mocked(collectionImportApi.confirmBatch).mockRejectedValue(new CollectionImportApiError(
      "network unknown",
      "NETWORK_ERROR",
      0,
    ));
    const callbacks = renderPage({ batchId: "batch-1", view: "confirm" });

    fireEvent.click(await screen.findByRole("button", { name: "确认保存 1 项" }));
    await screen.findByText(/无法确认上次请求是否生效/);
    fireEvent.click(screen.getByRole("button", { name: "刷新权威状态" }));

    await waitFor(() => expect(callbacks.onRouteChange).toHaveBeenCalledWith({
      batchId: "batch-1",
      view: "list",
      focus: "batch-status",
    }, true));
    expect(collectionImportApi.confirmBatch).toHaveBeenCalledTimes(1);
    expect(collectionImportApi.getBatch).toHaveBeenCalledTimes(3);
  });

  it("INVALID_RESPONSE 后 GET 仅观察到无关 revision 前进时仍不误判确认成功", async () => {
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(confirmReadyBatch)
      .mockResolvedValueOnce(advancedReviewBatch);
    vi.mocked(collectionImportApi.confirmBatch).mockResolvedValue(confirmReadyBatch);
    const callbacks = renderPage({ batchId: "batch-1", view: "confirm" });

    fireEvent.click(await screen.findByRole("button", { name: "确认保存 1 项" }));
    expect(await screen.findByRole("heading", { name: "本次确认已停止" })).toBeInTheDocument();
    expect(callbacks.onRouteChange).not.toHaveBeenCalledWith(expect.objectContaining({ focus: "batch-status" }), true);
    expect(collectionImportApi.confirmBatch).toHaveBeenCalledTimes(1);
  });

  it.each(["save", "skip"] as const)("failed/save 并发 PATCH 改为 %s 且未冻结时不得误判确认成功", async (afterDecision) => {
    const retryReady: CollectionImportBatchSnapshot = {
      ...confirmReadyBatch,
      revision: 14,
      failed: 1,
      ready: 0,
      items: confirmReadyBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, state: "failed", decision: "save", item_revision: 7, error_code: "SAVE_FAILED" }
        : { ...item, state: "skipped", item_revision: 4 }),
    };
    const concurrentPatch = {
      ...retryReady,
      revision: 15,
      items: retryReady.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, decision: afterDecision, item_revision: 8 }
        : item),
    };
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(retryReady)
      .mockResolvedValueOnce(concurrentPatch);
    vi.mocked(collectionImportApi.confirmBatch).mockRejectedValue(new CollectionImportApiError(
      "revision conflict",
      "BATCH_REVISION_CONFLICT",
      409,
    ));
    const callbacks = renderPage({ batchId: "batch-1", view: "confirm" });

    fireEvent.click(await screen.findByRole("button", { name: "确认保存 1 项" }));
    expect(await screen.findByRole("heading", { name: "本次确认已停止" })).toBeInTheDocument();
    expect(callbacks.onRouteChange).not.toHaveBeenCalledWith(expect.objectContaining({ focus: "batch-status" }), true);
    expect(collectionImportApi.confirmBatch).toHaveBeenCalledTimes(1);
    expect(collectionImportApi.getBatch).toHaveBeenCalledTimes(2);
  });

  it.each([
    ["CAS", new CollectionImportApiError("revision conflict", "BATCH_REVISION_CONFLICT", 409)],
    ["NETWORK", new CollectionImportApiError("network unknown", "NETWORK_ERROR", 0)],
    ["INVALID_RESPONSE", null],
  ])("%s 后 authority GET 失败时进入 unknown 错误面，仍只有一次 POST", async (_label, rejection) => {
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(confirmReadyBatch)
      .mockRejectedValueOnce(new CollectionImportApiError("read failed", "NETWORK_ERROR", 0));
    if (rejection) vi.mocked(collectionImportApi.confirmBatch).mockRejectedValue(rejection);
    else vi.mocked(collectionImportApi.confirmBatch).mockResolvedValue(confirmReadyBatch);
    renderPage({ batchId: "batch-1", view: "confirm" });

    fireEvent.click(await screen.findByRole("button", { name: "确认保存 1 项" }));
    expect(await screen.findByText(/无法确认上次请求是否生效/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新权威状态" })).toBeInTheDocument();
    expect(collectionImportApi.confirmBatch).toHaveBeenCalledTimes(1);
    expect(collectionImportApi.getBatch).toHaveBeenCalledTimes(2);
  });

  it.each([
    ["GET 观察到 saving", savingBatch],
    ["协调器唤醒失败后观察到 interrupted 冻结", interruptedFrozenBatch],
    ["worker 已快速收敛为 known-not-written failed", failedAfterConfirmBatch],
  ])("transport unknown 在%s时认定已冻结且不盲重放", async (_label, authority) => {
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(confirmReadyBatch)
      .mockResolvedValueOnce(authority);
    vi.mocked(collectionImportApi.confirmBatch).mockRejectedValue(new CollectionImportApiError(
      "无法连接批量导入服务",
      "NETWORK_ERROR",
      0,
    ));
    const callbacks = renderPage({ batchId: "batch-1", view: "confirm" });

    fireEvent.click(await screen.findByRole("button", { name: "确认保存 1 项" }));
    await waitFor(() => expect(callbacks.onRouteChange).toHaveBeenCalledWith({
      batchId: "batch-1",
      view: "list",
      focus: "batch-status",
    }, true));
    expect(collectionImportApi.confirmBatch).toHaveBeenCalledTimes(1);
  });

  it("202 返回 interrupted 冻结快照时仍完成确认，绝不重新开放核对", async () => {
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(confirmReadyBatch);
    vi.mocked(collectionImportApi.confirmBatch).mockResolvedValue(interruptedFrozenBatch);
    const callbacks = renderPage({ batchId: "batch-1", view: "confirm" });

    fireEvent.click(await screen.findByRole("button", { name: "确认保存 1 项" }));
    await waitFor(() => expect(callbacks.onRouteChange).toHaveBeenCalledWith({
      batchId: "batch-1",
      view: "list",
      focus: "batch-status",
    }, true));
    expect(collectionImportApi.confirmBatch).toHaveBeenCalledTimes(1);
  });

  it("进入串行保存后继续轮询权威 GET，并用 polite live region 公告收敛状态", async () => {
    const completed: CollectionImportBatchSnapshot = {
      ...savingBatch,
      status: "completed",
      revision: 10,
      terminal_at: "2026-08-31T10:04:00Z",
      items: savingBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, state: "saved", item_revision: 6, collection_item_id: "material-new" }
        : item),
    };
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(savingBatch)
      .mockResolvedValueOnce(completed);
    const rendered = renderPage({ batchId: "batch-1", view: "list" });

    const status = await screen.findByRole("heading", { name: "正在按原序保存" });
    status.focus();
    expect(await screen.findByText("批次状态已更新：批次已完成。", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(rendered.container.querySelectorAll(".batch-import-page [aria-live='polite'], .batch-import-page [role='status']")).toHaveLength(1);
    expect(status).toHaveFocus();
    expect(collectionImportApi.getBatch).toHaveBeenCalledTimes(2);
  });

  it("saving 轮询的一次 GET 暂时失败后仍继续读取并收敛", async () => {
    const completed: CollectionImportBatchSnapshot = {
      ...savingBatch,
      status: "completed",
      revision: 10,
      terminal_at: "2026-08-31T10:04:00Z",
      items: savingBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, state: "saved", item_revision: 6, collection_item_id: "material-new" }
        : item),
    };
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(savingBatch)
      .mockRejectedValueOnce(new CollectionImportApiError("temporary read failure", "NETWORK_ERROR", 0))
      .mockResolvedValueOnce(completed);
    renderPage({ batchId: "batch-1", view: "list" });

    await screen.findByRole("heading", { name: "正在按原序保存" });
    expect(await screen.findByText("批次状态已更新：批次已完成。", {}, { timeout: 6000 })).toBeInTheDocument();
    expect(collectionImportApi.getBatch).toHaveBeenCalledTimes(3);
  }, 7000);

  it("刷新或 Forward 进入已失去资格的 confirm 时只 GET 并 replace 回 list", async () => {
    const outcomeUnknown: CollectionImportBatchSnapshot = {
      ...confirmReadyBatch,
      status: "interrupted",
      revision: 9,
      items: confirmReadyBatch.items.map((item, index) => index === 0
        ? { ...item, state: "outcome_unknown", item_revision: 5 }
        : item),
    };
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(outcomeUnknown);
    const callbacks = renderPage({ batchId: "batch-1", view: "confirm", itemId: "item-1" });

    await waitFor(() => expect(callbacks.onRouteChange).toHaveBeenCalledWith({
      batchId: "batch-1",
      view: "list",
      focus: "batch-status",
    }, true));
    expect(collectionImportApi.confirmBatch).not.toHaveBeenCalled();
  });

  it.each([360, 390, 900, 901])("%ipx 下 confirm 保留同一 itemId 与安全摘要，跨 900/901 不丢审核上下文", async (width) => {
    viewportAt(width);
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(confirmReadyBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(confirmReadyDetail);
    const rendered = renderPage({ batchId: "batch-1", view: "confirm", itemId: "item-1" });

    await screen.findByRole("heading", { name: "核对本次批次决定" });
    expect(rendered.container.querySelector(".batch-import-page")?.getAttribute("data-view")).toBe("confirm");
    expect(collectionImportApi.getItem).toHaveBeenCalledWith("batch-1", "item-1", expect.any(AbortSignal));
    expect(screen.getByRole("button", { name: "确认保存 1 项" })).toBeEnabled();
  });

  it("confirm 打开时真实 901→900→901 resize 只切呈现，不复制 DOM、丢 itemId/焦点或发请求", async () => {
    const resizeTo = resizableViewport(901);
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(confirmReadyBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(confirmReadyDetail);
    const callbacks = renderPage({ batchId: "batch-1", view: "confirm", itemId: "item-1" });

    const heading = await screen.findByRole("heading", { name: "核对本次批次决定" });
    const surface = heading.closest<HTMLElement>(".batch-import-confirm-surface");
    const itemHeading = document.querySelector<HTMLElement>("#batch-item-title");
    const final = screen.getByRole("button", { name: "确认保存 1 项" });
    await waitFor(() => expect(heading).toHaveFocus());

    act(() => resizeTo(900));
    expect(document.querySelectorAll(".batch-import-confirm-surface")).toHaveLength(1);
    expect(document.querySelector(".batch-import-confirm-surface")).toBe(surface);
    expect(document.querySelector("#batch-item-title")).toBe(itemHeading);
    expect(screen.getByRole("button", { name: "确认保存 1 项" })).toBe(final);
    expect(heading).toHaveFocus();

    act(() => resizeTo(901));
    expect(document.querySelectorAll(".batch-import-confirm-surface")).toHaveLength(1);
    expect(document.querySelector("#batch-item-title")).toBe(itemHeading);
    expect(heading).toHaveFocus();
    expect(collectionImportApi.getItem).toHaveBeenCalledWith("batch-1", "item-1", expect.any(AbortSignal));
    expect(collectionImportApi.getBatch).toHaveBeenCalledTimes(1);
    expect(collectionImportApi.confirmBatch).not.toHaveBeenCalled();
    expect(callbacks.onRouteChange).not.toHaveBeenCalled();
  });

  it.each([
    [900, "item-heading"],
    [901, "confirm-trigger"],
  ] as const)("跨断点返回到 %ipx 时把焦点落到可见的 %s", async (width, target) => {
    viewportAt(width);
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(confirmReadyBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(confirmReadyDetail);
    renderPage({
      batchId: "batch-1",
      view: "item",
      itemId: "item-1",
      focus: "confirm-trigger",
      restoreToken: 3,
    });

    await screen.findByRole("heading", { name: "公开标题" });
    const expected = target === "item-heading"
      ? document.querySelector<HTMLElement>("#batch-item-title")
      : screen.getByRole("button", { name: "核对并保存已选 1 项" });
    await waitFor(() => expect(expected).toHaveFocus());
  });

  it("interrupted 才开放恢复；同 revision 的 202 可如实保持待核对且不伪造持久变化", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(outcomeUnknownBatch);
    vi.mocked(collectionImportApi.resumeBatch).mockResolvedValue(outcomeUnknownBatch);
    const rendered = renderPage({ batchId: "batch-1", view: "list" });

    const resume = await screen.findByRole("button", { name: "恢复并对账" });
    const liveRegion = rendered.container.querySelector<HTMLElement>(".batch-import-page [aria-live='polite']");
    const announcements: string[] = [];
    let previousAnnouncement = liveRegion?.textContent ?? "";
    const observer = new MutationObserver(() => {
      const nextAnnouncement = liveRegion?.textContent ?? "";
      if (nextAnnouncement && nextAnnouncement !== previousAnnouncement) announcements.push(nextAnnouncement);
      previousAnnouncement = nextAnnouncement;
    });
    expect(liveRegion).not.toBeNull();
    observer.observe(liveRegion!, { childList: true, characterData: true, subtree: true });
    expect(rendered.container.querySelector(".batch-import-lifecycle-actions-desktop")).toContainElement(resume);
    await user.click(resume);

    expect(collectionImportApi.resumeBatch).toHaveBeenCalledWith("batch-1", { expected_batch_revision: 9 });
    expect(rendered.container.querySelector(".batch-import-command-feedback")?.textContent).toContain("仍有结果待核对");
    expect(screen.getByRole("button", { name: "恢复并对账" })).toBeInTheDocument();
    expect(rendered.container.querySelectorAll(".batch-import-page [aria-live='polite'], .batch-import-page [role='status']")).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "恢复并对账" }));
    await waitFor(() => expect(announcements.filter((text) => text.includes("仍有结果待核对"))).toHaveLength(2));
    expect(collectionImportApi.resumeBatch).toHaveBeenCalledTimes(2);
    observer.disconnect();
  });

  it("恢复进入 preview 时只发一次 POST，阶段文案真实且消失按钮把焦点移到批次状态", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(interruptedReviewBatch);
    vi.mocked(collectionImportApi.resumeBatch).mockResolvedValue(resumedPreviewBatch);
    renderPage({ batchId: "batch-1", view: "list" });

    await user.click(await screen.findByRole("button", { name: "恢复并对账" }));

    expect(collectionImportApi.resumeBatch).toHaveBeenCalledTimes(1);
    expect(document.querySelector(".batch-import-command-feedback")).toHaveTextContent("本地对账已完成，正在继续检查公开来源。");
    expect(screen.queryByRole("button", { name: "恢复并对账" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "正在逐项检查" })).toHaveFocus();
  });

  it("resume CAS 只 GET 后重激活动作，必须再次点击才使用最新 revision", async () => {
    const user = userEvent.setup();
    const latest = { ...outcomeUnknownBatch, revision: 10 };
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(outcomeUnknownBatch)
      .mockResolvedValueOnce(latest);
    vi.mocked(collectionImportApi.resumeBatch)
      .mockRejectedValueOnce(new CollectionImportApiError("revision conflict", "BATCH_REVISION_CONFLICT", 409))
      .mockResolvedValueOnce({ ...latest, revision: 11 });
    const rendered = renderPage({ batchId: "batch-1", view: "list" });

    await user.click(await screen.findByRole("button", { name: "恢复并对账" }));
    expect(collectionImportApi.getBatch).toHaveBeenCalledTimes(2);
    expect(collectionImportApi.resumeBatch).toHaveBeenCalledTimes(1);
    expect(rendered.container.querySelector(".batch-import-command-feedback")).toHaveTextContent("系统没有自动重放恢复");
    await waitFor(() => expect(rendered.container.querySelector(".batch-import-page [aria-live='polite']")).toHaveTextContent("系统没有自动重放恢复"));
    expect(rendered.container.querySelector(".batch-import-page [aria-live='polite']")).not.toHaveTextContent("批次状态已更新");

    await user.click(screen.getByRole("button", { name: "恢复并对账" }));
    expect(collectionImportApi.resumeBatch).toHaveBeenCalledTimes(2);
    expect(collectionImportApi.resumeBatch).toHaveBeenLastCalledWith("batch-1", { expected_batch_revision: 10 });
  });

  it("resume 同 revision 但权威内容漂移时视为无效响应，GET-first 后仍不重放", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(outcomeUnknownBatch)
      .mockResolvedValueOnce(outcomeUnknownBatch);
    vi.mocked(collectionImportApi.resumeBatch).mockResolvedValue({ ...outcomeUnknownBatch, status: "previewing" });
    renderPage({ batchId: "batch-1", view: "list" });

    await user.click(await screen.findByRole("button", { name: "恢复并对账" }));

    await waitFor(() => expect(document.querySelector(".batch-import-command-feedback")?.textContent).toMatch(/系统没有自动重放恢复/));
    expect(collectionImportApi.resumeBatch).toHaveBeenCalledTimes(1);
    expect(collectionImportApi.getBatch).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("heading", { name: "需要恢复与对账" })).toBeInTheDocument();
  });

  it("resume 传输未知且 GET 失败时只留显式刷新，错误 assertive 一次并聚焦邻近摘要", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(outcomeUnknownBatch)
      .mockRejectedValueOnce(new CollectionImportApiError("read failed", "NETWORK_ERROR", 0))
      .mockResolvedValueOnce(outcomeUnknownBatch);
    vi.mocked(collectionImportApi.resumeBatch).mockRejectedValue(new CollectionImportApiError("unknown", "NETWORK_ERROR", 0));
    const rendered = renderPage({ batchId: "batch-1", view: "list" });

    await user.click(await screen.findByRole("button", { name: "恢复并对账" }));
    const error = await screen.findByRole("alert");
    expect(error).toHaveFocus();
    expect(rendered.container.querySelectorAll(".batch-import-lifecycle-actions [role='alert']")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "恢复并对账" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "取消批次" })).not.toBeInTheDocument();
    expect(collectionImportApi.resumeBatch).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "刷新权威状态" }));
    expect(collectionImportApi.resumeBatch).toHaveBeenCalledTimes(1);
    const refresh = await screen.findByRole("button", { name: "刷新权威状态" });
    expect(screen.getByRole("button", { name: "恢复并对账" })).toBeInTheDocument();
    expect(refresh).toHaveFocus();
  });

  it("resume 未结构化 HTTP_ERROR 5xx 先 GET 对账；GET 失败后只保留刷新且不重放 POST", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(outcomeUnknownBatch)
      .mockRejectedValueOnce(new CollectionImportApiError("读取失败", "HTTP_ERROR", 503));
    vi.mocked(collectionImportApi.resumeBatch).mockRejectedValue(
      new CollectionImportApiError("非 JSON 5xx", "HTTP_ERROR", 502),
    );
    renderPage({ batchId: "batch-1", view: "list" });

    await user.click(await screen.findByRole("button", { name: "恢复并对账" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("系统没有重放请求；请先刷新权威状态");
    expect(collectionImportApi.resumeBatch).toHaveBeenCalledTimes(1);
    expect(collectionImportApi.getBatch).toHaveBeenCalledTimes(2);
    expect(vi.mocked(collectionImportApi.resumeBatch).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(collectionImportApi.getBatch).mock.invocationCallOrder[1],
    );
    expect(screen.getByRole("button", { name: "刷新权威状态" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "恢复并对账" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "取消批次" })).not.toBeInTheDocument();
  });

  it("resume 未知结果刷新到 awaiting_review 时消失的刷新按钮把焦点交给批次状态", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(outcomeUnknownBatch)
      .mockRejectedValueOnce(new CollectionImportApiError("read failed", "NETWORK_ERROR", 0))
      .mockResolvedValueOnce(refreshedAwaitingReviewBatch);
    vi.mocked(collectionImportApi.resumeBatch).mockRejectedValue(new CollectionImportApiError("unknown", "NETWORK_ERROR", 0));
    renderPage({ batchId: "batch-1", view: "list" });

    await user.click(await screen.findByRole("button", { name: "恢复并对账" }));
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "刷新权威状态" }));

    expect(await screen.findByRole("heading", { name: "等待逐项审核" })).toHaveFocus();
    expect(screen.queryByRole("button", { name: "刷新权威状态" })).not.toBeInTheDocument();
    expect(collectionImportApi.resumeBatch).toHaveBeenCalledTimes(1);
  });

  it("resume 显式刷新拒绝同 revision 的 status 漂移并保持仅刷新且不 replay", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(outcomeUnknownBatch)
      .mockRejectedValueOnce(new CollectionImportApiError("read failed", "NETWORK_ERROR", 0))
      .mockResolvedValueOnce({ ...outcomeUnknownBatch, status: "previewing" });
    vi.mocked(collectionImportApi.resumeBatch).mockRejectedValue(new CollectionImportApiError("unknown", "NETWORK_ERROR", 0));
    renderPage({ batchId: "batch-1", view: "list" });

    await user.click(await screen.findByRole("button", { name: "恢复并对账" }));
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "刷新权威状态" }));

    expect(await screen.findByText("权威状态仍无法读取。恢复、取消和再次确认继续阻断。")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "需要恢复与对账" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新权威状态" })).toBeInTheDocument();
    expect(collectionImportApi.resumeBatch).toHaveBeenCalledTimes(1);
  });

  it("dirty 恢复先走同一门禁，放弃后保存的原动作只执行一次", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(interruptedReviewBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue({ ...reviewDetail, batch_revision: 9 });
    vi.mocked(collectionImportApi.resumeBatch).mockResolvedValue(interruptedReviewBatch);
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.type(await screen.findByRole("textbox", { name: "自定义标题" }), "恢复前草稿");
    await user.click(screen.getByRole("button", { name: "恢复并对账" }));
    expect(screen.getByRole("heading", { name: "先处理未提交修改" })).toHaveFocus();
    expect(collectionImportApi.resumeBatch).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "放弃未提交修改并继续原动作" }));
    expect(collectionImportApi.resumeBatch).toHaveBeenCalledTimes(1);
    expect(collectionImportApi.resumeBatch).toHaveBeenCalledWith("batch-1", { expected_batch_revision: 9 });
    await user.keyboard("{Enter}");
    expect(collectionImportApi.resumeBatch).toHaveBeenCalledTimes(1);
  });

  it("dirty 更新后继续恢复使用 PATCH 返回的最新 batch revision", async () => {
    const user = userEvent.setup();
    const decidedInterrupted: CollectionImportBatchSnapshot = {
      ...interruptedReviewBatch,
      selected: 1,
      items: interruptedReviewBatch.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, decision: "save", item_revision: 4 }
        : item),
    };
    const updatedInterrupted: CollectionImportBatchSnapshot = {
      ...decidedInterrupted,
      revision: 10,
      items: decidedInterrupted.items.map((item) => item.batch_item_id === "item-1"
        ? { ...item, item_revision: 5 }
        : item),
    };
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(decidedInterrupted);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue({
      ...confirmReadyDetail,
      batch_revision: decidedInterrupted.revision,
      item_revision: 4,
    });
    vi.mocked(collectionImportApi.updateItem).mockResolvedValue(updatedInterrupted);
    vi.mocked(collectionImportApi.resumeBatch).mockResolvedValue(updatedInterrupted);
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.type(await screen.findByRole("textbox", { name: "自定义标题" }), "更新后恢复");
    await user.click(screen.getByRole("button", { name: "恢复并对账" }));
    const updateThenContinue = screen.getByRole("button", { name: "更新后继续原动作" });
    expect(updateThenContinue).toBeEnabled();
    await user.click(updateThenContinue);

    expect(collectionImportApi.updateItem).toHaveBeenCalledWith("batch-1", "item-1", expect.objectContaining({
      expected_batch_revision: 9,
      expected_item_revision: 4,
      decision: "save",
    }));
    expect(collectionImportApi.resumeBatch).toHaveBeenCalledTimes(1);
    expect(collectionImportApi.resumeBatch).toHaveBeenCalledWith("batch-1", { expected_batch_revision: 10 });
  });

  it("取消是两步原位确认：Escape/撤回归还焦点，IME Enter 不提交，200 才写已取消", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(reviewBatch);
    vi.mocked(collectionImportApi.cancelBatch).mockResolvedValue({ snapshot: cancelledBatch, status: 200 });
    renderPage({ batchId: "batch-1", view: "list" });

    const trigger = await screen.findByRole("button", { name: "取消批次" });
    await user.click(trigger);
    const heading = screen.getByRole("heading", { name: "确认取消当前批次" });
    expect(heading).toHaveFocus();
    expect(screen.getByText(/已保存的项会保留/)).toBeInTheDocument();
    expect(screen.getByText(/不会强制中止正在写入的保存/)).toBeInTheDocument();
    expect(collectionImportApi.cancelBatch).not.toHaveBeenCalled();

    fireEvent.keyDown(heading, { key: "Escape" });
    const restoredTrigger = screen.getByRole("button", { name: "取消批次" });
    await waitFor(() => expect(restoredTrigger).toHaveFocus());
    await user.click(restoredTrigger);
    const confirm = screen.getByRole("button", { name: "确认取消批次" });
    fireEvent.keyDown(confirm, { key: "Enter", isComposing: true });
    expect(collectionImportApi.cancelBatch).not.toHaveBeenCalled();
    await user.click(confirm);

    expect(collectionImportApi.cancelBatch).toHaveBeenCalledWith("batch-1", { expected_batch_revision: 4 });
    expect(document.querySelector(".batch-import-command-feedback")).toHaveTextContent("已取消。已保存项仍会保留。");
    expect(screen.getByRole("heading", { name: "批次已取消" })).toHaveFocus();
  });

  it("dirty 取消先保存唯一原意图；放弃后才打开二次确认并再次提示草稿不可恢复", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(reviewBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(reviewDetail);
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    await user.type(await screen.findByRole("textbox", { name: "自定义标题" }), "未提交");
    await user.click(screen.getByRole("button", { name: "取消批次" }));
    expect(screen.getByRole("heading", { name: "先处理未提交修改" })).toHaveFocus();
    expect(screen.queryByRole("heading", { name: "确认取消当前批次" })).not.toBeInTheDocument();
    expect(collectionImportApi.cancelBatch).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "放弃未提交修改并继续原动作" }));
    expect(screen.getByRole("heading", { name: "确认取消当前批次" })).toHaveFocus();
    expect(screen.getByText("若你刚才放弃了未提交修改，这些修改无法恢复。")).toBeInTheDocument();
    expect(collectionImportApi.cancelBatch).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "确认取消批次" }));
    expect(collectionImportApi.cancelBatch).toHaveBeenCalledTimes(1);
  });

  it("取消确认展开或命令在途时冻结审核与核对入口，撤回后原 draft 仍可编辑", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(confirmReadyBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(confirmReadyDetail);
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    const confirmEntry = screen.getByRole("button", { name: "核对并保存已选 1 项" });
    expect(title).toBeEnabled();
    expect(confirmEntry).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "取消批次" }));

    expect(title).toBeDisabled();
    expect(confirmEntry).toBeDisabled();
    expect(screen.getByText("当前审核暂时冻结。系统不会在批次动作期间提交或丢弃本项修改。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "撤回取消" }));
    expect(title).toBeEnabled();
    expect(confirmEntry).toBeEnabled();
    expect(collectionImportApi.cancelBatch).not.toHaveBeenCalled();
  });

  it("202 只写正在取消，并在 cancelling GET 轮询后收敛为权威终态", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(savingBatch)
      .mockResolvedValueOnce(cancelledBatch);
    vi.mocked(collectionImportApi.cancelBatch).mockResolvedValue({ snapshot: cancellingBatch, status: 202 });
    const rendered = renderPage({ batchId: "batch-1", view: "list" });

    await user.click(await screen.findByRole("button", { name: "取消批次" }));
    await user.click(screen.getByRole("button", { name: "确认取消批次" }));
    await waitFor(() => expect(rendered.container.querySelector(".batch-import-command-feedback")).toHaveTextContent("正在取消。已保存及正在提交的项可能保留"));
    await waitFor(() => expect(rendered.container.querySelector(".batch-import-page [aria-live='polite']")).toHaveTextContent("正在取消。已保存及正在提交的项可能保留"));
    expect(rendered.container.querySelector(".batch-import-page [aria-live='polite']")).not.toHaveTextContent("批次状态已更新：正在取消");
    expect(screen.queryByText(/^已取消。/)).not.toBeInTheDocument();
    expect(await screen.findByText("批次状态已更新：批次已取消。", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(collectionImportApi.getBatch).toHaveBeenCalledTimes(2);
  });

  it("cancelling 轮询一次 GET 失败后仍继续读取，不把临时失败包装成已取消", async () => {
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(cancellingBatch)
      .mockRejectedValueOnce(new CollectionImportApiError("temporary read failure", "NETWORK_ERROR", 0))
      .mockResolvedValueOnce(cancelledBatch);
    renderPage({ batchId: "batch-1", view: "list" });

    await screen.findByRole("heading", { name: "正在取消" });
    expect(screen.queryByText(/^已取消。/)).not.toBeInTheDocument();
    expect(await screen.findByText("批次状态已更新：批次已取消。", {}, { timeout: 6000 })).toBeInTheDocument();
    expect(collectionImportApi.getBatch).toHaveBeenCalledTimes(3);
  }, 7000);

  it("cancel CAS 只 GET 最新状态并关闭确认；重新打开且再次确认才用新 revision POST", async () => {
    const user = userEvent.setup();
    const latest = { ...reviewBatch, revision: 5 };
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(reviewBatch)
      .mockResolvedValueOnce(latest);
    vi.mocked(collectionImportApi.cancelBatch)
      .mockRejectedValueOnce(new CollectionImportApiError("revision conflict", "BATCH_REVISION_CONFLICT", 409))
      .mockResolvedValueOnce({ snapshot: { ...cancelledBatch, revision: 6 }, status: 200 });
    renderPage({ batchId: "batch-1", view: "list" });

    await user.click(await screen.findByRole("button", { name: "取消批次" }));
    await user.click(screen.getByRole("button", { name: "确认取消批次" }));

    await waitFor(() => expect(document.querySelector(".batch-import-command-feedback")).toHaveTextContent(/系统没有自动重放取消/));
    expect(collectionImportApi.getBatch).toHaveBeenCalledTimes(2);
    expect(collectionImportApi.cancelBatch).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("heading", { name: "确认取消当前批次" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "取消批次" }));
    await user.click(screen.getByRole("button", { name: "确认取消批次" }));
    expect(collectionImportApi.cancelBatch).toHaveBeenCalledTimes(2);
    expect(collectionImportApi.cancelBatch).toHaveBeenLastCalledWith("batch-1", { expected_batch_revision: 5 });
  });

  it.each([
    ["cancelling", cancellingBatch, /权威状态显示正在取消/],
    ["cancelled", cancelledBatch, /权威状态显示批次已取消/],
  ] as const)("cancel 传输未知 GET 到 %s 时只归因权威状态，不重放 POST", async (_status, authority, message) => {
    const user = userEvent.setup();
    const initial = authority.status === "cancelling" ? savingBatch : reviewBatch;
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(authority);
    vi.mocked(collectionImportApi.cancelBatch).mockRejectedValue(new CollectionImportApiError("unknown", "NETWORK_ERROR", 0));
    renderPage({ batchId: "batch-1", view: "list" });

    await user.click(await screen.findByRole("button", { name: "取消批次" }));
    await user.click(screen.getByRole("button", { name: "确认取消批次" }));

    await waitFor(() => expect(document.querySelector(".batch-import-command-feedback")).toHaveTextContent(message));
    expect(collectionImportApi.getBatch).toHaveBeenCalledTimes(2);
    expect(collectionImportApi.cancelBatch).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("heading", { name: "确认取消当前批次" })).not.toBeInTheDocument();
  });

  it("cancel 传输未知且 GET 失败时阻断为仅刷新；刷新成功也不自动重发", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(reviewBatch)
      .mockRejectedValueOnce(new CollectionImportApiError("read failed", "NETWORK_ERROR", 0))
      .mockResolvedValueOnce(reviewBatch);
    vi.mocked(collectionImportApi.cancelBatch).mockRejectedValue(new CollectionImportApiError("unknown", "NETWORK_ERROR", 0));
    renderPage({ batchId: "batch-1", view: "list" });

    await user.click(await screen.findByRole("button", { name: "取消批次" }));
    await user.click(screen.getByRole("button", { name: "确认取消批次" }));
    const error = await screen.findByRole("alert");

    expect(error).toHaveFocus();
    expect(screen.queryByRole("button", { name: "取消批次" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新权威状态" })).toBeInTheDocument();
    expect(collectionImportApi.cancelBatch).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "刷新权威状态" }));
    expect(await screen.findByRole("button", { name: "取消批次" })).toBeInTheDocument();
    expect(collectionImportApi.cancelBatch).toHaveBeenCalledTimes(1);
  });

  it("cancel 未结构化 HTTP_ERROR 4xx 也先 GET 对账；GET 失败后只保留刷新且不重放 POST", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(reviewBatch)
      .mockRejectedValueOnce(new CollectionImportApiError("读取失败", "NETWORK_ERROR", 0));
    vi.mocked(collectionImportApi.cancelBatch).mockRejectedValue(
      new CollectionImportApiError("非结构化 4xx", "HTTP_ERROR", 400),
    );
    renderPage({ batchId: "batch-1", view: "list" });

    await user.click(await screen.findByRole("button", { name: "取消批次" }));
    await user.click(screen.getByRole("button", { name: "确认取消批次" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("系统没有重放请求；请先刷新权威状态");
    expect(collectionImportApi.cancelBatch).toHaveBeenCalledTimes(1);
    expect(collectionImportApi.getBatch).toHaveBeenCalledTimes(2);
    expect(vi.mocked(collectionImportApi.cancelBatch).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(collectionImportApi.getBatch).mock.invocationCallOrder[1],
    );
    expect(screen.getByRole("button", { name: "刷新权威状态" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "取消批次" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "确认取消当前批次" })).not.toBeInTheDocument();
  });

  it("cancel 未知结果刷新到 awaiting_review 时消失的刷新按钮把焦点交给批次状态", async () => {
    const user = userEvent.setup();
    const advanced = { ...reviewBatch, revision: 5, updated_at: "2026-08-31T10:02:00Z" };
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(reviewBatch)
      .mockRejectedValueOnce(new CollectionImportApiError("read failed", "NETWORK_ERROR", 0))
      .mockResolvedValueOnce(advanced);
    vi.mocked(collectionImportApi.cancelBatch).mockRejectedValue(new CollectionImportApiError("unknown", "NETWORK_ERROR", 0));
    renderPage({ batchId: "batch-1", view: "list" });

    await user.click(await screen.findByRole("button", { name: "取消批次" }));
    await user.click(screen.getByRole("button", { name: "确认取消批次" }));
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "刷新权威状态" }));

    expect(await screen.findByRole("heading", { name: "等待逐项审核" })).toHaveFocus();
    expect(screen.queryByRole("button", { name: "刷新权威状态" })).not.toBeInTheDocument();
    expect(collectionImportApi.cancelBatch).toHaveBeenCalledTimes(1);
  });

  it("cancel GET 对账拒绝同 revision 的 item 漂移并保持仅刷新且不 replay", async () => {
    const user = userEvent.setup();
    const changedItem = {
      ...reviewBatch,
      items: reviewBatch.items.map((item) => item.batch_item_id === "item-1" ? { ...item, state: "cancelled" as const } : item),
    };
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(reviewBatch)
      .mockResolvedValueOnce(changedItem);
    vi.mocked(collectionImportApi.cancelBatch).mockRejectedValue(new CollectionImportApiError("unknown", "NETWORK_ERROR", 0));
    renderPage({ batchId: "batch-1", view: "list" });

    await user.click(await screen.findByRole("button", { name: "取消批次" }));
    await user.click(screen.getByRole("button", { name: "确认取消批次" }));

    expect(await screen.findByRole("alert")).toHaveFocus();
    expect(screen.getByRole("heading", { name: "等待逐项审核" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新权威状态" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "取消批次" })).not.toBeInTheDocument();
    expect(collectionImportApi.cancelBatch).toHaveBeenCalledTimes(1);
  });

  it.each([360, 390, 1440].flatMap((width) => [
    [width, "completed", "批次已完成", "本批次已结束，无需再次确认。可在列表中查看各项结果。"],
    [width, "completed_with_issues", "批次已完成，部分项目未导入", "未成功的项目已跳过；已保存的素材会保留。"],
    [width, "cancelled", "批次已取消", "已保存的素材会保留；其余项目已停止处理。"],
  ] as const))("SIM-FIX-001 %ipx %s 终态提示真实且不重新确认", async (width, status, heading, message) => {
    viewportAt(width);
    const terminal: CollectionImportBatchSnapshot = {
      ...reviewBatch, status, ready: 0, selected: 0,
      terminal_at: "2026-08-31T10:05:00Z",
      items: reviewBatch.items.map((item, index) => ({
        ...item, state: index === 0 ? "saved" : status === "cancelled" ? "cancelled" : "skipped",
        decision: index === 0 ? "save" : "skip",
        terminal_reason: index === 1 && status === "completed_with_issues" ? "preview_failed" : null,
      })),
    };
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(terminal);
    renderPage({ batchId: "batch-1", view: "list" });
    const dock = await screen.findByRole("region", { name: "批次确认动作" });
    expect(within(dock).getByText(heading, { exact: true })).toBeInTheDocument();
    expect(within(dock).getByText(message, { exact: true })).toBeInTheDocument();
    expect(within(dock).queryByText("批次审核尚未满足确认条件")).not.toBeInTheDocument();
    expect(within(dock).getByRole("button")).toBeDisabled();
    fireEvent.click(within(dock).getByRole("button"));
    expect(collectionImportApi.confirmBatch).not.toHaveBeenCalled();
    expect(collectionImportApi.resumeBatch).not.toHaveBeenCalled();
    expect(collectionImportApi.cancelBatch).not.toHaveBeenCalled();
  });

  it.each([360, 390, 900])("%ipx 的恢复/取消只在列表动作 Dock，item 表面不复制", async (width) => {
    viewportAt(width);
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(interruptedReviewBatch);
    const list = renderPage({ batchId: "batch-1", view: "list" });
    const resume = await screen.findByRole("button", { name: "恢复并对账" });
    const dock = list.container.querySelector<HTMLElement>(".batch-import-mobile-batch-dock");
    const index = list.container.querySelector<HTMLElement>(".batch-import-index");
    expect(dock).toContainElement(resume);
    expect(dock?.querySelectorAll("[aria-label='批次确认动作']")).toHaveLength(1);
    expect(dock?.querySelector(".batch-import-action-dock")).toBeNull();
    expect(index && dock && Boolean(index.compareDocumentPosition(dock) & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true);
    expect(list.container.querySelector(".batch-import-lifecycle-actions-desktop")).toBeNull();
    list.unmount();

    vi.mocked(collectionImportApi.getItem).mockResolvedValue({ ...reviewDetail, batch_revision: 9 });
    renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });
    await screen.findByRole("heading", { name: "公开标题" });
    expect(screen.queryByRole("button", { name: "恢复并对账" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "取消批次" })).not.toBeInTheDocument();
  });

  it("901↔900 保留 list 动作焦点、item dirty 与 route，不新增 history 或命令", async () => {
    const user = userEvent.setup();
    const resizeTo = responsiveViewport(901);
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(interruptedReviewBatch);
    const list = renderPage({ batchId: "batch-1", view: "list" });
    const desktopResume = await screen.findByRole("button", { name: "恢复并对账" });
    desktopResume.focus();

    act(() => resizeTo(900));
    const mobileResume = screen.getByRole("button", { name: "恢复并对账" });
    expect(mobileResume).not.toBe(desktopResume);
    expect(mobileResume).toHaveFocus();
    act(() => resizeTo(901));
    expect(screen.getByRole("button", { name: "恢复并对账" })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "取消批次" }));
    expect(screen.getByRole("heading", { name: "确认取消当前批次" })).toHaveFocus();
    act(() => resizeTo(900));
    expect(screen.getAllByRole("heading", { name: "确认取消当前批次" })).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "确认取消当前批次" })).toHaveFocus();
    act(() => resizeTo(901));
    expect(screen.getByRole("heading", { name: "确认取消当前批次" })).toHaveFocus();
    expect(collectionImportApi.resumeBatch).not.toHaveBeenCalled();
    expect(collectionImportApi.cancelBatch).not.toHaveBeenCalled();
    expect(list.onRouteChange).not.toHaveBeenCalled();
    list.unmount();

    const resizeItem = responsiveViewport(901);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue({ ...reviewDetail, batch_revision: 9 });
    const item = renderPage({ batchId: "batch-1", view: "item", itemId: "item-1" });
    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await userEvent.setup().type(title, "跨断点草稿");
    act(() => resizeItem(900));
    expect(screen.getByRole("textbox", { name: "自定义标题" })).toHaveValue("跨断点草稿");
    act(() => resizeItem(901));
    expect(screen.getByRole("textbox", { name: "自定义标题" })).toHaveValue("跨断点草稿");
    expect(item.onRouteChange).not.toHaveBeenCalled();
    expect(collectionImportApi.updateItem).not.toHaveBeenCalled();
  });

  it("Slice 4 仍不暴露 retention、GC 或 R2.7 文案", async () => {
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(confirmReadyBatch);
    renderPage({ batchId: "batch-1", view: "list" });
    await screen.findByRole("button", { name: "核对并保存已选 1 项" });

    expect(screen.queryByText(/retention|垃圾回收|GC/)).not.toBeInTheDocument();
    expect(screen.queryByText(/系统分享|快捷捕获/)).not.toBeInTheDocument();
  });
});
