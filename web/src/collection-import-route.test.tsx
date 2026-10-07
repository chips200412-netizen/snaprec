import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CollectionImportBatchSnapshot, CollectionImportItemDetail } from "./collection-import-api";

vi.mock("./collection-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./collection-api")>();
  return {
    ...actual,
    collectionApi: {
      ...actual.collectionApi,
      listItems: vi.fn(),
      getItem: vi.fn(),
    },
  };
});

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

vi.mock("./legacy-app", () => ({ LegacyApp: () => <main>深度解析</main> }));

import { CollectionApp } from "./collection-app";
import { collectionApi } from "./collection-api";
import { collectionImportApi } from "./collection-import-api";
import { ThemeProvider } from "./theme";

const routeBatch: CollectionImportBatchSnapshot = {
  batch_id: "batch one",
  status: "awaiting_review",
  revision: 1,
  total: 2,
  queued: 1,
  previewing: 0,
  ready: 1,
  needs_review: 0,
  duplicates: 0,
  already_exists: 0,
  failed: 0,
  selected: 0,
  created_at: "2026-08-31T10:00:00Z",
  updated_at: "2026-08-31T10:00:00Z",
  terminal_at: null,
  items: [
    {
      batch_item_id: "item-1",
      client_item_id: "item-01",
      position: 0,
      display_label: "第一项",
      state: "ready",
      decision: "pending",
      item_revision: 1,
      preview_generation: 1,
      duplicate_of_batch_item_id: null,
      collection_item_id: null,
      error_code: null,
      terminal_reason: null,
    },
    {
      batch_item_id: "item-2",
      client_item_id: "item-02",
      position: 1,
      display_label: "第二项",
      state: "queued",
      decision: "pending",
      item_revision: 1,
      preview_generation: 0,
      duplicate_of_batch_item_id: null,
      collection_item_id: null,
      error_code: null,
      terminal_reason: null,
    },
  ],
};

const existingBatch: CollectionImportBatchSnapshot = {
  ...routeBatch,
  queued: 0,
  ready: 1,
  already_exists: 1,
  items: routeBatch.items.map((item) => item.batch_item_id === "item-1"
    ? { ...item, state: "already_exists" as const, collection_item_id: "material-existing" }
    : { ...item, state: "ready" as const }),
};

const existingDetail: CollectionImportItemDetail = {
  ...existingBatch.items[0],
  batch_id: existingBatch.batch_id,
  batch_revision: existingBatch.revision,
  input_available: false,
  input_text: null,
  preview: null,
  draft: {
    user_title: null,
    untitled_confirmed: false,
    organization_confirmation: { primary_category: "", secondary_category: "", organization_tags: [] },
    personal_tags: [],
    inspiration: null,
  },
  error_stage: null,
};

const confirmRouteBatch: CollectionImportBatchSnapshot = {
  ...routeBatch,
  status: "awaiting_review",
  revision: 8,
  queued: 0,
  ready: 2,
  selected: 1,
  items: routeBatch.items.map((item) => item.batch_item_id === "item-1"
    ? { ...item, state: "ready", decision: "save", item_revision: 4 }
    : { ...item, state: "ready", decision: "skip", item_revision: 3 }),
};

const confirmRouteDetail: CollectionImportItemDetail = {
  ...existingDetail,
  ...confirmRouteBatch.items[0],
  batch_id: confirmRouteBatch.batch_id,
  batch_revision: confirmRouteBatch.revision,
  input_available: true,
  input_text: "不应进入 history 的分享 https://example.com/private-confirm",
};

const savingRouteBatch: CollectionImportBatchSnapshot = {
  ...confirmRouteBatch,
  status: "saving",
  revision: 9,
  ready: 0,
  items: confirmRouteBatch.items.map((item) => item.batch_item_id === "item-1"
    ? { ...item, state: "save_queued", item_revision: 5 }
    : { ...item, state: "skipped", item_revision: 4 }),
};

function renderApp() {
  return render(<ThemeProvider><CollectionApp /></ThemeProvider>);
}

describe("R2.6-B1 自管路由与素材库次入口", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
    Object.defineProperty(window, "scrollTo", { configurable: true, value: vi.fn() });
    Object.defineProperty(window, "requestAnimationFrame", {
      configurable: true,
      value: (callback: FrameRequestCallback) => { callback(0); return 1; },
    });
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn((query: string) => ({
        matches: query.includes("min-width"),
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });
    vi.mocked(collectionApi.listItems).mockReset();
    vi.mocked(collectionApi.getItem).mockReset();
    vi.mocked(collectionApi.listItems).mockResolvedValue({
      items: [],
      total: 0,
      limit: 24,
      next_cursor: null,
      facets: { platforms: [], categories: [], tags: [] },
    });
    vi.mocked(collectionApi.getItem).mockImplementation(() => new Promise(() => undefined));
    vi.mocked(collectionImportApi.createBatch).mockReset();
    vi.mocked(collectionImportApi.getActiveBatch).mockReset();
    vi.mocked(collectionImportApi.getBatch).mockReset();
    vi.mocked(collectionImportApi.getItem).mockReset();
    vi.mocked(collectionImportApi.updateItem).mockReset();
    vi.mocked(collectionImportApi.repreviewItem).mockReset();
    vi.mocked(collectionImportApi.confirmBatch).mockReset();
    vi.mocked(collectionImportApi.getActiveBatch).mockResolvedValue(null);
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(routeBatch);
    vi.mocked(collectionImportApi.confirmBatch).mockResolvedValue(savingRouteBatch);
  });

  it("D01/M01 只增加克制次入口；进入创建页后 history.state 不保存输入内容", async () => {
    const user = userEvent.setup();
    renderApp();

    const primary = await screen.findAllByRole("button", { name: "收进来" });
    const secondary = screen.getByRole("button", { name: "需要一次整理多条？进入批量导入" });
    expect(primary).toHaveLength(2);
    await user.click(secondary);

    const inputs = await screen.findAllByRole("textbox", { name: /完整分享文本/ });
    expect(window.location.pathname).toBe("/capture/batch");
    expect(window.history.state.collectionRoute).toMatchObject({
      name: "batch",
      view: "list",
      batchDepth: 0,
    });
    fireEvent.change(inputs[0], { target: { value: "不应进入 history 的分享 https://example.com/private" } });
    const serialized = JSON.stringify(window.history.state);
    expect(serialized).not.toContain("example.com/private");
    expect(serialized).not.toContain("draft");
    expect(serialized).not.toContain("inputText");
  });

  it("直达 /capture/batch/{batch_id} 只 GET 权威批次、不自动选第一项，并清洗旧 history 内容", async () => {
    window.history.replaceState({
      collectionRoute: {
        name: "batch",
        batchId: "batch one",
        view: "list",
        batchDepth: 0,
        draft: "旧草稿不得恢复",
        inputText: "旧分享文本不得恢复",
      },
    }, "", "/capture/batch/batch%20one");
    renderApp();

    await screen.findByRole("heading", { name: "选择一项开始审核" });
    expect(collectionImportApi.getBatch).toHaveBeenCalledWith("batch one", expect.any(AbortSignal));
    expect(collectionImportApi.getItem).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe("/capture/batch/batch%20one");
    await waitFor(() => {
      const serialized = JSON.stringify(window.history.state);
      expect(serialized).not.toContain("旧草稿");
      expect(serialized).not.toContain("旧分享文本");
      expect(serialized).not.toContain("draft");
      expect(serialized).not.toContain("inputText");
    });
  });

  it("already_exists 正常 push 到既有详情，浏览器 Back 恢复同一批次项、滚动和原入口焦点", async () => {
    const user = userEvent.setup();
    Object.defineProperty(document.documentElement, "scrollHeight", { configurable: true, value: 1800 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 800 });
    window.history.replaceState({
      collectionRoute: {
        name: "batch",
        batchId: "batch one",
        view: "item",
        itemId: "item-1",
        batchDepth: 1,
      },
    }, "", "/capture/batch/batch%20one");
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(existingBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(existingDetail);
    const replaceState = vi.spyOn(window.history, "replaceState");
    renderApp();

    const trigger = await screen.findByRole("button", { name: "打开既有收藏" });
    Object.defineProperty(window, "scrollY", { configurable: true, value: 345 });
    await user.click(trigger);
    await waitFor(() => expect(window.location.pathname).toBe("/materials/material-existing"));
    expect(screen.getByRole("button", { name: "返回素材库" })).toBeInTheDocument();
    const sourceCall = [...replaceState.mock.calls].reverse().find(([state]) => (
      state?.collectionRoute?.name === "batch"
      && state.collectionRoute.restoreScrollY === 345
      && typeof state.collectionRoute.restoreToken === "number"
    ));
    expect(sourceCall).toBeDefined();
    const sourceRoute = structuredClone(sourceCall![0].collectionRoute);
    let resolveReturnedItem: ((detail: CollectionImportItemDetail) => void) | null = null;
    vi.mocked(collectionImportApi.getItem)
      .mockReset()
      .mockImplementationOnce(() => new Promise((resolve) => { resolveReturnedItem = resolve; }))
      .mockResolvedValue(existingDetail);

    window.history.replaceState({ collectionRoute: sourceRoute }, "", "/capture/batch/batch%20one");
    act(() => window.dispatchEvent(new PopStateEvent("popstate", { state: { collectionRoute: sourceRoute } })));
    expect(window.scrollTo).not.toHaveBeenCalledWith({ top: 345, behavior: "auto" });
    await act(async () => { resolveReturnedItem?.(existingDetail); });

    const restored = await screen.findByRole("button", { name: "打开既有收藏" });
    await waitFor(() => expect(restored).toHaveFocus());
    expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 345, behavior: "auto" });
    expect(window.location.pathname).toBe("/capture/batch/batch%20one");
  });

  it("confirm 首击同 URL push 并保留 itemId；Back 恢复审核滚动与原入口焦点且 history 无用户内容", async () => {
    const user = userEvent.setup();
    Object.defineProperty(document.documentElement, "scrollHeight", { configurable: true, value: 1800 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 800 });
    Object.defineProperty(window, "scrollY", { configurable: true, value: 321 });
    window.history.replaceState({
      collectionRoute: {
        name: "batch",
        batchId: "batch one",
        view: "item",
        itemId: "item-1",
        batchDepth: 1,
      },
    }, "", "/capture/batch/batch%20one");
    vi.mocked(collectionImportApi.getBatch).mockResolvedValue(confirmRouteBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(confirmRouteDetail);
    const replaceState = vi.spyOn(window.history, "replaceState");
    renderApp();

    const trigger = await screen.findByRole("button", { name: "核对并保存已选 1 项" });
    await user.click(trigger);
    await screen.findByRole("heading", { name: "核对本次批次决定" });
    expect(window.location.pathname).toBe("/capture/batch/batch%20one");
    expect(window.history.state.collectionRoute).toMatchObject({
      name: "batch",
      view: "confirm",
      itemId: "item-1",
      batchDepth: 2,
    });
    expect(collectionImportApi.confirmBatch).not.toHaveBeenCalled();
    const sourceCall = [...replaceState.mock.calls].find(([state]) => (
      state?.collectionRoute?.name === "batch"
      && state.collectionRoute.view === "item"
      && state.collectionRoute.focus === "confirm-trigger"
      && state.collectionRoute.restoreScrollY === 321
    ));
    expect(sourceCall).toBeDefined();
    expect(JSON.stringify(window.history.state)).not.toContain("private-confirm");
    expect(JSON.stringify(window.history.state)).not.toContain("input_text");
    const sourceRoute = structuredClone(sourceCall![0].collectionRoute);

    window.history.replaceState({ collectionRoute: sourceRoute }, "", "/capture/batch/batch%20one");
    act(() => window.dispatchEvent(new PopStateEvent("popstate", { state: { collectionRoute: sourceRoute } })));
    const restored = await screen.findByRole("button", { name: "核对并保存已选 1 项" });
    await waitFor(() => expect(restored).toHaveFocus());
    expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 321, behavior: "auto" });
  });

  it("来源 item push confirm 后，202 replace 回 list；真实 Back 只回冻结 item 而不复活提交摘要", async () => {
    const user = userEvent.setup();
    window.history.replaceState({
      collectionRoute: {
        name: "batch",
        batchId: "batch one",
        view: "item",
        itemId: "item-1",
        batchDepth: 1,
      },
    }, "", "/capture/batch/batch%20one");
    vi.mocked(collectionImportApi.getBatch)
      .mockResolvedValueOnce(confirmRouteBatch)
      .mockResolvedValueOnce(confirmRouteBatch)
      .mockResolvedValue(savingRouteBatch);
    vi.mocked(collectionImportApi.getItem).mockResolvedValue(confirmRouteDetail);
    const pushState = vi.spyOn(window.history, "pushState");
    const replaceState = vi.spyOn(window.history, "replaceState");
    renderApp();

    await user.click(await screen.findByRole("button", { name: "核对并保存已选 1 项" }));
    const confirm = await screen.findByRole("button", { name: "确认保存 1 项" });
    expect(window.history.state.collectionRoute).toMatchObject({ view: "confirm", itemId: "item-1", batchDepth: 2 });
    pushState.mockClear();
    replaceState.mockClear();
    await user.click(confirm);

    await waitFor(() => expect(window.history.state.collectionRoute).toMatchObject({
      name: "batch",
      view: "list",
      focus: "batch-status",
      batchDepth: 2,
    }));
    expect(pushState).not.toHaveBeenCalled();
    expect(replaceState).toHaveBeenCalled();
    expect(screen.queryByRole("heading", { name: "核对本次批次决定" })).not.toBeInTheDocument();
    const savingTitle = document.querySelector<HTMLElement>("#batch-import-status-title");
    await waitFor(() => expect(savingTitle).toHaveFocus());

    window.history.back();
    await waitFor(() => expect(window.history.state.collectionRoute).toMatchObject({
      name: "batch",
      view: "item",
      itemId: "item-1",
      batchDepth: 1,
    }));
    await waitFor(() => expect(document.querySelector(".batch-import-page")?.getAttribute("data-view")).toBe("item"));
    expect(screen.queryByRole("button", { name: "确认保存 1 项" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "核对本次批次决定" })).not.toBeInTheDocument();
    expect(collectionImportApi.confirmBatch).toHaveBeenCalledTimes(1);
  });
});
