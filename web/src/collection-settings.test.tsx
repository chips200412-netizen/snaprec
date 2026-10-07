import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CollectionDeepAnalysisSnapshot, CollectionItem, CollectionPreview } from "./collection-api";

vi.mock("./collection-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./collection-api")>();
  return {
    ...actual,
    collectionApi: {
      createPreview: vi.fn(),
      transcribeInspiration: vi.fn(),
      createItem: vi.fn(),
      getItem: vi.fn(),
      listItems: vi.fn(),
      getDeepAnalysis: vi.fn(),
      startDeepAnalysis: vi.fn(),
      retryDeepAnalysis: vi.fn(),
    },
  };
});

import { CollectionApp } from "./collection-app";
import { CollectionApiError, collectionApi } from "./collection-api";
import { ThemeProvider, type ThemeCommitScheduler } from "./theme";

const combinationKey = "collection-appearance-v1";
const originalInput = "保存这篇公开文章 https://example.com/settings-roundtrip";
const sourceCopy = "这是一段可独立展开、在设置往返后仍应保留展开状态的公开来源文案。".repeat(8);
const preview: CollectionPreview = {
  preview_id: "settings-preview",
  original_input: originalInput,
  source_url: "https://example.com/settings-roundtrip",
  canonical_url: "https://example.com/settings-roundtrip",
  identity_url: "https://example.com/settings-roundtrip",
  source_kind: "article",
  platform: "web",
  metadata_status: "generic",
  metadata: {
    title: { value: "设置往返素材", source: "open_graph", fetched_at: "2026-08-26T09:00:00Z" },
    author: { value: "公开作者", source: "page_metadata", fetched_at: "2026-08-26T09:00:00Z" },
    cover_url: { value: "", source: "none", fetched_at: "2026-08-26T09:00:00Z" },
    source_copy: { value: sourceCopy, source: "page_description", fetched_at: "2026-08-26T09:00:00Z" },
    platform_tags: [],
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
  created_at: "2026-08-26T09:00:00Z",
  expires_at: "2026-08-26T09:30:00Z",
};

const item: CollectionItem = {
  ...preview,
  id: "settings-material",
  user_title: null,
  display_title: "设置往返素材",
  organization_confirmation: {
    primary_category: "设计",
    secondary_category: "空间",
    organization_tags: ["自然光"],
  },
  personal_tags: [],
  inspiration: null,
  deep_analysis_resource_key: null,
  revision: 1,
  updated_at: "2026-08-26T09:10:00Z",
};

const deepSnapshot: CollectionDeepAnalysisSnapshot = {
  material_id: item.id,
  analysis_job_id: null,
  state: "unavailable",
  config_version: "collection-default-v1",
  job_revision: 0,
  failed_stage: "",
  can_start: false,
  can_retry: false,
  can_view_result: false,
  result_id: null,
  result_kind: "none",
  result_revision: "",
  updated_at: item.updated_at,
  limitation: "当前公开来源不提供深度解析能力。",
  error_code: null,
  error_message: "",
};

const page = {
  items: [{
    id: item.id,
    display_title: item.display_title,
    platform: item.platform,
    primary_category: "设计",
    cover_url: "",
    created_at: item.created_at,
    updated_at: item.updated_at,
  }],
  total: 1,
  limit: 24,
  next_cursor: null,
  facets: {
    platforms: [{ platform: "web" as const, count: 1 }],
    categories: [{ primary_category: "设计", count: 1, children: [{ secondary_category: "空间", count: 1 }] }],
    tags: [{ name: "自然光", source: "organization" as const, count: 1 }],
  },
};

type User = ReturnType<typeof userEvent.setup>;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function sourceCopyGeometry() {
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.id === "source-copy-content" && !this.closest("[hidden]") ? 300 : 0;
  });
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.id === "source-copy-content" && !this.closest("[hidden]") ? 60 : 0;
  });
}

function viewport(mobile: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn((query: string) => ({
      matches: mobile && query.includes("max-width"),
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

function renderApp(scheduleCommit?: ThemeCommitScheduler) {
  return render(<ThemeProvider scheduleCommit={scheduleCommit}><CollectionApp /></ThemeProvider>);
}

async function openPreferences(user: User) {
  await user.click(await screen.findByRole("button", { name: "个人偏好" }));
}

async function requestSettings(user: User) {
  await openPreferences(user);
  const settings = screen.queryByRole("menuitem", { name: "隐私与存储设置" })
    ?? screen.getByRole("button", { name: "隐私与存储设置" });
  await user.click(settings);
}

async function openSettings(user: User) {
  await requestSettings(user);
  return screen.findByRole("heading", { name: "隐私与存储设置" });
}

async function openReview(user: User) {
  await user.type(screen.getByLabelText("粘贴链接或分享文本"), originalInput);
  await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
  await screen.findByRole("heading", { name: item.display_title });
  await waitFor(() => expect(screen.getByRole("textbox", { name: "一级分类" })).toHaveValue("设计"));
}

async function browserBack() {
  await act(async () => {
    await new Promise<void>((resolve) => {
      window.addEventListener("popstate", () => resolve(), { once: true });
      window.history.back();
    });
  });
}

async function browserForward() {
  await act(async () => {
    await new Promise<void>((resolve) => {
      window.addEventListener("popstate", () => resolve(), { once: true });
      window.history.forward();
    });
  });
}

function expectNoBusinessWrites() {
  expect(collectionApi.createPreview).not.toHaveBeenCalled();
  expect(collectionApi.createItem).not.toHaveBeenCalled();
  expect(collectionApi.transcribeInspiration).not.toHaveBeenCalled();
  expect(collectionApi.startDeepAnalysis).not.toHaveBeenCalled();
  expect(collectionApi.retryDeepAnalysis).not.toHaveBeenCalled();
}

describe("D04/M04 锁定设置合同", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    window.history.replaceState({}, "", "/");
    viewport(false);
    Object.defineProperty(window, "scrollY", { configurable: true, writable: true, value: 0 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 844 });
    Object.defineProperty(document.documentElement, "scrollHeight", { configurable: true, value: 3000 });
    Object.defineProperty(window, "scrollTo", {
      configurable: true,
      value: vi.fn((options: ScrollToOptions | number, top?: number) => {
        window.scrollY = typeof options === "number" ? top ?? 0 : options.top ?? 0;
      }),
    });
    Object.defineProperty(globalThis.CSS, "escape", { configurable: true, value: (value: string) => value });
    vi.mocked(collectionApi.createPreview).mockReset().mockResolvedValue(preview);
    vi.mocked(collectionApi.createItem).mockReset().mockResolvedValue(item);
    vi.mocked(collectionApi.listItems).mockReset().mockResolvedValue(page);
    vi.mocked(collectionApi.getItem).mockReset().mockResolvedValue(item);
    vi.mocked(collectionApi.getDeepAnalysis).mockReset().mockResolvedValue(deepSnapshot);
    vi.mocked(collectionApi.startDeepAnalysis).mockReset().mockResolvedValue(deepSnapshot);
    vi.mocked(collectionApi.retryDeepAnalysis).mockReset().mockResolvedValue(deepSnapshot);
    vi.mocked(collectionApi.transcribeInspiration).mockReset();
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: undefined });
    Object.defineProperty(globalThis, "MediaRecorder", { configurable: true, value: undefined });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("仅提供两主题、三强调色与返回，四项隐私和三项存储说明保持非交互", async () => {
    const user = userEvent.setup();
    renderApp();
    const heading = await openSettings(user);

    expect(heading).toHaveFocus();
    expect(within(screen.getByRole("radiogroup", { name: "主题" })).getAllByRole("radio")).toHaveLength(2);
    expect(within(screen.getByRole("radiogroup", { name: "强调色" })).getAllByRole("radio")).toHaveLength(3);
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "返回素材库" })).toBeInTheDocument();
    expect(screen.getByText("瞬时录").closest("a, button")).toBeNull();
    for (const [title, count] of [["隐私保护", 4], ["存储边界", 3]] as const) {
      const section = screen.getByRole("heading", { name: title }).closest("section");
      expect(section).not.toBeNull();
      expect(within(section!).getAllByRole("listitem")).toHaveLength(count);
      expect(within(section!).queryByRole("button")).not.toBeInTheDocument();
      expect(within(section!).queryByRole("link")).not.toBeInTheDocument();
      expect(within(section!).queryByRole("switch")).not.toBeInTheDocument();
    }
    expect(screen.queryByRole("button", { name: /保存设置|导出|删除|清缓存|账户|同步/ })).not.toBeInTheDocument();
    expect(screen.getByText("用户补充封面")).toBeInTheDocument();
    expect(screen.getByText("你主动上传并保存到收藏的封面由后端保留；临时录音处理后清理，不随收藏保留。")).toBeInTheDocument();
    expect(screen.queryByText(/可选保留媒体|单条本地上传|深度解析/)).not.toBeInTheDocument();
    expectNoBusinessWrites();
  });

  it("手机从 clean 整理进入设置显示短返回文案，返回不重新请求元信息", async () => {
    const user = userEvent.setup();
    viewport(true);
    renderApp();
    await openReview(user);
    await openSettings(user);

    await user.click(screen.getByRole("button", { name: "返回整理" }));

    await screen.findByRole("heading", { name: "保存前整理" });
    expect(collectionApi.createPreview).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByRole("button", { name: "个人偏好" })).toHaveFocus());
  });

  it("手机从详情进入设置显示返回详情，偏好不会产生素材或任务写入", async () => {
    const user = userEvent.setup();
    viewport(true);
    renderApp();
    await user.click(await screen.findByRole("button", { name: `打开素材：${item.display_title}` }));
    await screen.findByRole("heading", { name: item.display_title });
    await openSettings(user);

    expect(screen.getByRole("button", { name: "返回详情" })).toBeInTheDocument();
    expectNoBusinessWrites();
  });

  it("同值选择不写入、不重复播报，也不伪装修复安全默认", async () => {
    const user = userEvent.setup();
    renderApp();
    await openSettings(user);
    const before = screen.getAllByRole("status").map((node) => node.textContent);
    const write = vi.spyOn(Storage.prototype, "setItem");

    await user.click(screen.getByRole("radio", { name: "浅色" }));
    await user.click(screen.getByRole("radio", { name: "靛蓝" }));

    expect(write).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(combinationKey)).toBeNull();
    expect(screen.getAllByRole("status").map((node) => node.textContent)).toEqual(before);
  });

  it("不同组合即时作用全应用，刷新设置仍恢复同一完整组合", async () => {
    const user = userEvent.setup();
    const view = renderApp();
    await openSettings(user);
    await user.click(screen.getByRole("radio", { name: "深色" }));
    await user.click(screen.getByRole("radio", { name: "青绿" }));

    await waitFor(() => {
      expect(document.documentElement.dataset.theme).toBe("dark");
      expect(document.documentElement.dataset.accent).toBe("teal");
      expect(JSON.parse(window.localStorage.getItem(combinationKey) ?? "null")).toMatchObject({ appearance: "dark", accent: "teal" });
    });
    view.unmount();
    renderApp();

    expect(await screen.findByRole("radio", { name: "深色" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "青绿" })).toHaveAttribute("aria-checked", "true");
    expectNoBusinessWrites();
  });

  it("最新存储失败整套回滚、就近给出可感知失败并保留触发选项焦点", async () => {
    const user = userEvent.setup();
    window.localStorage.setItem(combinationKey, JSON.stringify({ appearance: "light", accent: "indigo" }));
    renderApp();
    await openSettings(user);
    const write = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("Storage unavailable", "QuotaExceededError"); });
    const dark = screen.getByRole("radio", { name: "深色" });

    await user.click(dark);

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/(未能|无法|失败).*(保存|恢复|回滚)|(保存|持久化).*(失败|未能)/));
    expect(dark).toHaveFocus();
    expect(dark).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("radio", { name: "浅色" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "靛蓝" })).toHaveAttribute("aria-checked", "true");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(document.documentElement.dataset.accent).toBe("indigo");
    const writes = write.mock.calls.length;
    const failure = screen.getByRole("alert").textContent;
    await user.click(screen.getByRole("radio", { name: "浅色" }));
    expect(write).toHaveBeenCalledTimes(writes);
    expect(screen.getByRole("alert")).toHaveTextContent(failure ?? "");
    expectNoBusinessWrites();
  });

  it("pending 同值 no-op，跨轴最新可见意图在乱序完成后只收敛到最后完整组合", async () => {
    const user = userEvent.setup();
    const queue: Array<() => void> = [];
    renderApp((commit) => { queue.push(commit); });
    await openSettings(user);
    const write = vi.spyOn(Storage.prototype, "setItem");
    await user.click(screen.getByRole("radio", { name: "深色" }));
    const applying = screen.getAllByRole("status").map((node) => node.textContent);
    await user.click(screen.getByRole("radio", { name: "深色" }));
    expect(queue).toHaveLength(1);
    expect(screen.getAllByRole("status").map((node) => node.textContent)).toEqual(applying);
    expect(write).not.toHaveBeenCalled();
    await user.click(screen.getByRole("radio", { name: "青绿" }));
    expect(queue).toHaveLength(2);
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(document.documentElement.dataset.accent).toBe("teal");
    expect(screen.getByRole("radio", { name: "青绿" })).toHaveFocus();

    await act(async () => { queue[1](); });
    const completed = screen.getAllByRole("status").map((node) => node.textContent);
    const writes = write.mock.calls.length;
    await act(async () => { queue[0](); });

    expect(write).toHaveBeenCalledTimes(writes);
    expect(JSON.parse(window.localStorage.getItem(combinationKey) ?? "null")).toMatchObject({ appearance: "dark", accent: "teal" });
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(document.documentElement.dataset.accent).toBe("teal");
    expect(screen.getAllByRole("status").map((node) => node.textContent)).toEqual(completed);
    expect(screen.queryByRole("alert")?.textContent ?? "").toBe("");
    expectNoBusinessWrites();
  });

  it("最新 pending 失败回滚完整确认组合，旧 attempt 不写入或覆盖失败反馈", async () => {
    const user = userEvent.setup();
    const queue: Array<() => void> = [];
    window.localStorage.setItem(combinationKey, JSON.stringify({ appearance: "light", accent: "indigo" }));
    renderApp((commit) => { queue.push(commit); });
    await openSettings(user);
    await user.click(screen.getByRole("radio", { name: "深色" }));
    const teal = screen.getByRole("radio", { name: "青绿" });
    await user.click(teal);
    const write = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("unavailable"); });

    await act(async () => { queue[1](); });
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/未能保存|保存失败/));
    const failure = screen.getByRole("alert");
    const failureMessage = failure.textContent;
    const writes = write.mock.calls.length;
    await act(async () => { queue[0](); });

    expect(write).toHaveBeenCalledTimes(writes);
    expect(screen.getByRole("alert")).toHaveTextContent(failureMessage ?? "");
    expect(teal).toHaveFocus();
    expect(screen.getByRole("radio", { name: "浅色" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "靛蓝" })).toHaveAttribute("aria-checked", "true");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(document.documentElement.dataset.accent).toBe("indigo");
  });

  it("离开设置不阻塞 pending 收敛，旧设置成功反馈不跨路由播报或抢焦点", async () => {
    const user = userEvent.setup();
    const queue: Array<() => void> = [];
    renderApp((commit) => { queue.push(commit); });
    await openSettings(user);
    await user.click(screen.getByRole("radio", { name: "暖橙" }));
    await user.click(screen.getByRole("button", { name: "返回素材库" }));
    await screen.findByRole("heading", { name: /把看到的/ });
    await waitFor(() => expect(screen.getByRole("button", { name: "个人偏好" })).toHaveFocus());
    const focused = document.activeElement;
    const libraryStatus = screen.getAllByRole("status").map((node) => node.textContent);

    await act(async () => { queue[0](); });

    expect(document.documentElement.dataset.accent).toBe("orange");
    expect(JSON.parse(window.localStorage.getItem(combinationKey) ?? "null")).toMatchObject({ appearance: "light", accent: "orange" });
    expect(document.activeElement).toBe(focused);
    expect(screen.getAllByRole("status").map((node) => node.textContent)).toEqual(libraryStatus);
    expect(screen.queryByRole("alert")?.textContent ?? "").toBe("");
  });

  it.each([
    ["损坏 JSON", "{broken"],
    ["不完整组合", JSON.stringify({ appearance: "dark" })],
    ["无效枚举", JSON.stringify({ appearance: "night", accent: "orange" })],
    ["不兼容版本", JSON.stringify({ version: 999, appearance: "dark", accent: "orange" })],
  ])("%s 初始化完整安全默认，不改异常原值且说明后端数据未受影响", async (_name, raw) => {
    window.localStorage.setItem(combinationKey, raw);
    window.localStorage.setItem("video-knowledge-theme", "dark");
    window.localStorage.setItem("video-knowledge-accent", "orange");
    const user = userEvent.setup();
    renderApp();
    const write = vi.spyOn(Storage.prototype, "setItem");
    await openSettings(user);

    expect(screen.getByRole("radio", { name: "浅色" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "靛蓝" })).toHaveAttribute("aria-checked", "true");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(document.documentElement.dataset.accent).toBe("indigo");
    expect(screen.getByRole("status")).toHaveTextContent(/收藏.*(灵感|后端).*(未受影响|没有丢失|不受影响)/);
    expect(screen.queryByRole("alert")?.textContent ?? "").toBe("");
    await user.click(screen.getByRole("radio", { name: "浅色" }));
    expect(write).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(combinationKey)).toBe(raw);
  });

  it("设置单选组按 DOM 循环，失败不抢焦点且再次 Tab 入组停靠安全选中项", async () => {
    const user = userEvent.setup();
    viewport(true);
    renderApp();
    await openSettings(user);
    const light = screen.getByRole("radio", { name: "浅色" });
    const dark = screen.getByRole("radio", { name: "深色" });
    light.focus();
    await user.keyboard("{ArrowLeft}");
    expect(dark).toHaveFocus();
    expect(dark).toHaveAttribute("aria-checked", "true");
    await user.keyboard("{ArrowRight}");
    expect(light).toHaveFocus();
    expect(light).toHaveAttribute("aria-checked", "true");
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("unavailable"); });
    await user.keyboard("{ArrowDown}");
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/未能保存|保存失败/));
    expect(dark).toHaveFocus();
    expect(light).toHaveAttribute("tabindex", "0");
    expect(dark).toHaveAttribute("tabindex", "-1");
    await user.tab();
    expect(screen.getByRole("radio", { name: "靛蓝" })).toHaveFocus();
    await user.tab({ shift: true });
    expect(light).toHaveFocus();
  });

  it.each(["page", "browser"] as const)("素材库通过 %s 返回保留查询、已应用筛选、捕获草稿、滚动与偏好入口，不重复读取列表", async (method) => {
    const user = userEvent.setup();
    renderApp();
    await screen.findByRole("button", { name: `打开素材：${item.display_title}` });
    await user.type(screen.getByRole("textbox", { name: "搜索素材和我的灵感" }), "设置");
    await waitFor(() => expect(collectionApi.listItems).toHaveBeenLastCalledWith(expect.objectContaining({ query: "设置" }), expect.any(AbortSignal)));
    await user.click(screen.getByRole("button", { name: "设计" }));
    await user.click(screen.getByRole("button", { name: /筛选/ }));
    await user.click(screen.getByRole("radio", { name: "网页" }));
    await user.click(screen.getByRole("radio", { name: "空间" }));
    await user.click(screen.getByRole("button", { name: "应用筛选" }));
    await waitFor(() => expect(collectionApi.listItems).toHaveBeenLastCalledWith(expect.objectContaining({ query: "设置", primaryCategory: "设计", platform: "web", secondaryCategory: "空间" }), expect.any(AbortSignal)));
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), originalInput);
    window.scrollY = 540;
    const count = vi.mocked(collectionApi.listItems).mock.calls.length;
    await openSettings(user);
    await user.click(screen.getByRole("radio", { name: "暖橙" }));

    if (method === "page") await user.click(screen.getByRole("button", { name: "返回素材库" }));
    else await browserBack();

    await screen.findByRole("heading", { name: /把看到的/ });
    await waitFor(() => expect(screen.getByRole("button", { name: "个人偏好" })).toHaveFocus());
    expect(screen.getByRole("textbox", { name: "搜索素材和我的灵感" })).toHaveValue("设置");
    expect(screen.getByRole("button", { name: "设计" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("粘贴链接或分享文本")).toHaveValue(originalInput);
    expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 540, behavior: "auto" });
    expect(collectionApi.listItems).toHaveBeenCalledTimes(count);
    expect(document.documentElement.dataset.accent).toBe("orange");
    expectNoBusinessWrites();
  });

  it("手机设置冻结打开偏好抽屉前的位置，不采纳弹层锁滚造成的临时坐标", async () => {
    const user = userEvent.setup();
    viewport(true);
    renderApp();
    await screen.findByRole("button", { name: `打开素材：${item.display_title}` });
    window.scrollY = 510;
    await user.click(screen.getByRole("button", { name: "个人偏好" }));
    await screen.findByRole("dialog", { name: "个人偏好" });
    window.scrollY = 127;
    await user.click(screen.getByRole("button", { name: "隐私与存储设置" }));
    await screen.findByRole("heading", { name: "隐私与存储设置" });
    await browserBack();
    await screen.findByRole("heading", { name: /把看到的/ });
    await waitFor(() => expect(screen.getByRole("button", { name: "个人偏好" })).toHaveFocus());
    expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 510, behavior: "auto" });
  });

  it.each(["page", "browser"] as const)("clean 整理通过 %s 返回精确快照、来源 disclosure 与偏好入口，不重建 preview", async (method) => {
    const user = userEvent.setup();
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) { return this.id === "source-copy-content" ? 300 : 0; });
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(function (this: HTMLElement) { return this.id === "source-copy-content" ? 60 : 0; });
    renderApp();
    await openReview(user);
    await user.click(await screen.findByRole("button", { name: "展开全文" }));
    window.scrollY = 600;
    await openSettings(user);
    await user.click(screen.getByRole("radio", { name: "深色" }));

    if (method === "page") await user.click(screen.getByRole("button", { name: "返回保存整理" }));
    else await browserBack();

    await screen.findByRole("heading", { name: "保存前整理" });
    expect(screen.getByRole("button", { name: "收起" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("textbox", { name: "一级分类" })).toHaveValue("设计");
    expect(screen.getByRole("textbox", { name: "二级分类" })).toHaveValue("空间");
    expect(screen.getByText(sourceCopy)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "个人偏好" })).toHaveFocus());
    expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 600, behavior: "auto" });
    expect(collectionApi.createPreview).toHaveBeenCalledTimes(1);
    expect(collectionApi.createItem).not.toHaveBeenCalled();
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it.each(["page", "browser"] as const)("clean 整理在途元信息离页即中止，%s 返回只恢复已接受的来源、建议和 disclosure", async (method) => {
    const user = userEvent.setup();
    const refreshing = deferred<CollectionPreview>();
    sourceCopyGeometry();
    vi.mocked(collectionApi.createPreview).mockResolvedValueOnce(preview).mockImplementationOnce(() => refreshing.promise);
    renderApp();
    await openReview(user);
    await user.click(await screen.findByRole("button", { name: "展开全文" }));
    await user.click(screen.getByRole("button", { name: "重新获取公开信息" }));
    await waitFor(() => expect(collectionApi.createPreview).toHaveBeenCalledTimes(2));
    const pendingSignal = vi.mocked(collectionApi.createPreview).mock.calls[1][2];
    expect(pendingSignal?.aborted).toBe(false);
    await openSettings(user);
    expect(pendingSignal?.aborted).toBe(true);

    await act(async () => {
      refreshing.resolve({
        ...preview,
        preview_id: "stale-settings-preview",
        metadata: {
          ...preview.metadata,
          title: { ...preview.metadata.title, value: "设置期间迟到的新标题" },
          source_copy: { ...preview.metadata.source_copy, value: "不得覆盖原快照的迟到来源文案" },
        },
        organization_suggestion: { ...preview.organization_suggestion, primary_category: "旅行", secondary_category: "山野", tags: ["徒步"] },
      });
    });
    expect(screen.getByRole("heading", { name: "隐私与存储设置" })).toBeInTheDocument();
    if (method === "page") await user.click(screen.getByRole("button", { name: "返回保存整理" }));
    else await browserBack();

    expect(await screen.findByRole("heading", { name: item.display_title })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "设置期间迟到的新标题" })).not.toBeInTheDocument();
    expect(screen.getByText(sourceCopy)).toBeVisible();
    expect(screen.queryByText("不得覆盖原快照的迟到来源文案")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "收起" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("textbox", { name: "一级分类" })).toHaveValue("设计");
    expect(screen.getByRole("textbox", { name: "二级分类" })).toHaveValue("空间");
    expect(screen.getByRole("button", { name: "删除整理标签：自然光" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /采用.*建议/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新获取公开信息" })).toBeEnabled();
    expect(collectionApi.createPreview).toHaveBeenCalledTimes(2);
    expect(collectionApi.createItem).not.toHaveBeenCalled();
  });

  it.each(["page", "browser"] as const)("首次来源请求未完成时 clean 进入设置，%s 返回丢弃旧响应且仅显式重试可重新获取", async (method) => {
    const user = userEvent.setup();
    const initial = deferred<CollectionPreview>();
    vi.mocked(collectionApi.createPreview).mockImplementationOnce(() => initial.promise);
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), originalInput);
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "保存前整理" });
    await waitFor(() => expect(collectionApi.createPreview).toHaveBeenCalledTimes(1));
    const pendingSignal = vi.mocked(collectionApi.createPreview).mock.calls[0][2];
    await openSettings(user);
    expect(pendingSignal?.aborted).toBe(true);
    await act(async () => {
      initial.resolve({ ...preview, metadata: { ...preview.metadata, title: { ...preview.metadata.title, value: "首次请求迟到标题" } } });
    });

    if (method === "page") await user.click(screen.getByRole("button", { name: "返回保存整理" }));
    else await browserBack();

    await screen.findByRole("heading", { name: "保存前整理" });
    expect(screen.queryByRole("heading", { name: "首次请求迟到标题" })).not.toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "重试元信息" });
    expect(retry.closest("[role=alert]")).toBeVisible();
    expect(retry.closest("[role=alert]")).toHaveTextContent(/来源|元信息/);
    expect(screen.getByRole("button", { name: "保存到素材库" })).toBeDisabled();
    expect(collectionApi.createPreview).toHaveBeenCalledTimes(1);
    await user.click(retry);

    await screen.findByRole("heading", { name: item.display_title });
    expect(collectionApi.createPreview).toHaveBeenCalledTimes(2);
    expect(collectionApi.createPreview).toHaveBeenLastCalledWith(originalInput, true, expect.any(AbortSignal));
    expect(screen.queryByRole("button", { name: "重试元信息" })).not.toBeInTheDocument();
    expect(collectionApi.createItem).not.toHaveBeenCalled();
  });

  it("长来源文案在设置期间隐藏并 resize，返回后仍有可用的展开入口", async () => {
    const user = userEvent.setup();
    sourceCopyGeometry();
    renderApp();
    await openReview(user);
    expect(await screen.findByRole("button", { name: "展开全文" })).toHaveAttribute("aria-expanded", "false");
    await openSettings(user);
    const hiddenCopy = document.getElementById("source-copy-content");
    expect(hiddenCopy).not.toBeVisible();
    expect(hiddenCopy?.scrollHeight).toBe(0);
    expect(hiddenCopy?.clientHeight).toBe(0);
    await act(async () => { window.dispatchEvent(new Event("resize")); });
    await user.click(screen.getByRole("button", { name: "返回保存整理" }));

    const expand = await screen.findByRole("button", { name: "展开全文" });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    await user.click(expand);
    expect(screen.getByRole("button", { name: "收起" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(sourceCopy)).toBeVisible();
    expect(collectionApi.createPreview).toHaveBeenCalledTimes(1);
    expect(collectionApi.createItem).not.toHaveBeenCalled();
  });

  it("手机素材库设置往返保留已加载第二页及下一游标，不因返回重复加载", async () => {
    const user = userEvent.setup();
    viewport(true);
    let intersect: IntersectionObserverCallback | undefined;
    vi.stubGlobal("IntersectionObserver", class {
      constructor(callback: IntersectionObserverCallback) { intersect = callback; }
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    vi.mocked(collectionApi.listItems)
      .mockResolvedValueOnce({ ...page, total: 3, next_cursor: "cursor-page2" })
      .mockResolvedValueOnce({ ...page, items: [{ ...page.items[0], id: "settings-second", display_title: "第二页已加载素材" }], total: 3, next_cursor: "cursor-page3" })
      .mockResolvedValueOnce({ ...page, items: [{ ...page.items[0], id: "settings-third", display_title: "第三页新素材" }], total: 3, next_cursor: null });
    renderApp();
    await screen.findByRole("button", { name: `打开素材：${item.display_title}` });
    await act(async () => { intersect?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver); });
    await screen.findByRole("button", { name: "打开素材：第二页已加载素材" });
    await user.type(screen.getByLabelText("粘贴链接或分享文本（手机）"), originalInput);
    await openSettings(user);
    await browserBack();

    await screen.findByRole("button", { name: "打开素材：第二页已加载素材" });
    await waitFor(() => expect(screen.getByRole("button", { name: "个人偏好" })).toHaveFocus());
    expect(screen.getByLabelText("粘贴链接或分享文本（手机）")).toHaveValue(originalInput);
    expect(collectionApi.listItems).toHaveBeenCalledTimes(2);
    await act(async () => { intersect?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver); });
    await screen.findByRole("button", { name: "打开素材：第三页新素材" });
    expect(collectionApi.listItems).toHaveBeenLastCalledWith(expect.objectContaining({ cursor: "cursor-page3" }));
  });

  it("脏整理进入设置先确认，继续整理保留原文并回到设置触发入口", async () => {
    const user = userEvent.setup();
    viewport(true);
    renderApp();
    await openReview(user);
    await user.type(screen.getByPlaceholderText(/我喜欢这种自然光/), "不能被设置导航丢弃的草稿");
    await requestSettings(user);
    const confirm = await screen.findByRole("dialog", { name: "放弃当前整理吗？" });

    await user.click(within(confirm).getByRole("button", { name: "继续整理" }));

    expect(screen.queryByRole("heading", { name: "隐私与存储设置" })).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText(/我喜欢这种自然光/)).toHaveValue("不能被设置导航丢弃的草稿");
    expect(collectionApi.createPreview).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByRole("button", { name: "隐私与存储设置" })).toHaveFocus());
  });

  it("脏整理放弃后设置只持有素材库返回，back/forward 不复活整理草稿", async () => {
    const user = userEvent.setup();
    renderApp();
    await openReview(user);
    await user.type(screen.getByPlaceholderText(/我喜欢这种自然光/), "已经明确放弃的灵感");
    await requestSettings(user);
    await user.click(await screen.findByRole("button", { name: "放弃并前往设置" }));
    await screen.findByRole("heading", { name: "隐私与存储设置" });
    expect(screen.getByRole("button", { name: "返回素材库" })).toBeInTheDocument();

    await browserBack();
    await screen.findByRole("heading", { name: /把看到的/ });
    await browserForward();

    expect(await screen.findByRole("heading", { name: /把看到的/ })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "保存前整理" })).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("已经明确放弃的灵感")).not.toBeInTheDocument();
    expect(collectionApi.createPreview).toHaveBeenCalledTimes(1);
    expect(collectionApi.createItem).not.toHaveBeenCalled();
  });

  it("干净草稿保存结果未知仍要求确认，放弃后不重放 attempt 或创建替代 key", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionApi.createItem).mockRejectedValue(new CollectionApiError("网络暂时中断，结果未知", "NETWORK_ERROR", 0));
    renderApp();
    await openReview(user);
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));
    await screen.findByRole("button", { name: "重试保存" });
    await requestSettings(user);
    const confirm = await screen.findByRole("dialog", { name: "放弃当前整理吗？" });
    expect(confirm).toHaveTextContent(/可能.*(已经成功|保存成功|已成功)/);

    await user.click(within(confirm).getByRole("button", { name: "放弃并前往设置" }));
    await screen.findByRole("heading", { name: "隐私与存储设置" });
    await browserBack();
    await screen.findByRole("heading", { name: /把看到的/ });
    await browserForward();

    await screen.findByRole("heading", { name: /把看到的/ });
    expect(screen.queryByRole("button", { name: "重试保存" })).not.toBeInTheDocument();
    expect(collectionApi.createItem).toHaveBeenCalledTimes(1);
    expect(collectionApi.createPreview).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["page", false],
    ["browser", false],
    ["page", true],
    ["browser", true],
  ] as const)("空库中服务端已保存但结果未知，设置 %s 返回重新读取权威列表（首次刷新失败：%s）", async (method, refreshFails) => {
    const user = userEvent.setup();
    let persisted = false;
    const refresh = deferred<typeof page>();
    const emptyPage = { ...page, items: [], total: 0, facets: { platforms: [], categories: [], tags: [] } };
    vi.mocked(collectionApi.listItems).mockImplementation(() => persisted ? refresh.promise : Promise.resolve(emptyPage));
    vi.mocked(collectionApi.createItem).mockImplementation(async () => {
      persisted = true;
      throw new CollectionApiError("保存响应丢失，结果未知", "NETWORK_ERROR", 0);
    });
    renderApp();
    await screen.findByRole("heading", { name: "从第一条素材开始" });
    expect(collectionApi.listItems).toHaveBeenCalledTimes(1);
    const initialQuery = vi.mocked(collectionApi.listItems).mock.calls[0][0];
    await openReview(user);
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));
    await screen.findByRole("button", { name: "重试保存" });
    expect(persisted).toBe(true);
    await requestSettings(user);
    const confirm = await screen.findByRole("dialog", { name: "放弃当前整理吗？" });
    expect(confirm).toHaveTextContent(/可能.*(已经成功|保存成功|已成功)/);
    await user.click(within(confirm).getByRole("button", { name: "放弃并前往设置" }));
    await screen.findByRole("heading", { name: "隐私与存储设置" });
    expect(screen.getByRole("button", { name: "返回素材库" })).toBeInTheDocument();
    expect(collectionApi.listItems).toHaveBeenCalledTimes(1);
    expect(collectionApi.createItem).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/已保存到素材库|已保存，但/)).not.toBeInTheDocument();

    if (method === "page") await user.click(screen.getByRole("button", { name: "返回素材库" }));
    else await browserBack();

    await screen.findByRole("heading", { name: /把看到的/ });
    await waitFor(() => expect(collectionApi.listItems).toHaveBeenCalledTimes(2));
    expect(collectionApi.listItems).toHaveBeenLastCalledWith(initialQuery, expect.any(AbortSignal));
    expect(vi.mocked(collectionApi.listItems).mock.calls[1][1]).not.toBe(vi.mocked(collectionApi.listItems).mock.calls[0][1]);
    expect(screen.queryByRole("button", { name: `打开素材：${item.display_title}` })).not.toBeInTheDocument();
    expect(screen.queryByText(/已保存到素材库|已保存，但/)).not.toBeInTheDocument();
    if (refreshFails) {
      await act(async () => { refresh.reject(new CollectionApiError("权威列表暂时不可达", "NETWORK_ERROR", 0)); });
      expect(await screen.findByRole("heading", { name: "素材库暂时没有读出来" })).toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent("权威列表暂时不可达");
      expect(screen.queryByRole("heading", { name: "从第一条素材开始" })).not.toBeInTheDocument();
      expect(collectionApi.listItems).toHaveBeenCalledTimes(2);
      vi.mocked(collectionApi.listItems).mockResolvedValue(page);
      await user.click(screen.getByRole("button", { name: "按原条件重试" }));
    } else {
      await act(async () => { refresh.resolve(page); });
    }

    expect(await screen.findByRole("button", { name: `打开素材：${item.display_title}` })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "保存前整理" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试保存" })).not.toBeInTheDocument();
    expect(collectionApi.listItems).toHaveBeenCalledTimes(refreshFails ? 3 : 2);
    expect(collectionApi.listItems).toHaveBeenLastCalledWith(initialQuery, expect.any(AbortSignal));
    expect(collectionApi.createItem).toHaveBeenCalledTimes(1);
    expect(collectionApi.createPreview).toHaveBeenCalledTimes(1);
    expect(collectionApi.transcribeInspiration).not.toHaveBeenCalled();
    expect(collectionApi.startDeepAnalysis).not.toHaveBeenCalled();
  });

  it("详情合法设置 token 返回只重读权威核心，焦点恢复不依赖深度快照", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(await screen.findByRole("button", { name: `打开素材：${item.display_title}` }));
    await screen.findByRole("heading", { name: item.display_title });
    vi.mocked(collectionApi.getDeepAnalysis).mockImplementation(() => new Promise(() => undefined));
    window.scrollY = 420;
    await openSettings(user);
    vi.mocked(collectionApi.getItem).mockResolvedValue({ ...item, display_title: "设置期间更新后的权威标题", updated_at: "2026-08-26T10:00:00Z" });

    await user.click(screen.getByRole("button", { name: "返回素材详情" }));

    expect(await screen.findByRole("heading", { name: "设置期间更新后的权威标题" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "个人偏好" })).toHaveFocus());
    expect(collectionApi.getDeepAnalysis).not.toHaveBeenCalled();
    expect(collectionApi.getItem).toHaveBeenCalledTimes(2);
    expect(collectionApi.startDeepAnalysis).not.toHaveBeenCalled();
    expect(collectionApi.retryDeepAnalysis).not.toHaveBeenCalled();
    expect(collectionApi.createPreview).not.toHaveBeenCalled();
  });

  it("设置 token 消费后再 forward 不复用来源滚动或偏好焦点", async () => {
    const user = userEvent.setup();
    renderApp();
    await screen.findByRole("button", { name: `打开素材：${item.display_title}` });
    window.scrollY = 650;
    await openSettings(user);
    await browserBack();
    await screen.findByRole("heading", { name: /把看到的/ });
    await waitFor(() => expect(screen.getByRole("button", { name: "个人偏好" })).toHaveFocus());
    await browserForward();

    await waitFor(() => expect(screen.getByRole("heading", { name: /把看到的/ })).toHaveFocus());
    expect(window.scrollY).toBe(0);
    expectNoBusinessWrites();
  });

  it("合法设置返回双击最多执行一次历史导航", async () => {
    const user = userEvent.setup();
    renderApp();
    await openSettings(user);
    const back = vi.spyOn(window.history, "back");

    await user.dblClick(screen.getByRole("button", { name: "返回素材库" }));

    await screen.findByRole("heading", { name: /把看到的/ });
    expect(back).toHaveBeenCalledTimes(1);
    expectNoBusinessWrites();
  });

  it("直接进入无 token 设置仍有安全返回，且不猜测任何详情或草稿", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/settings/privacy-storage");
    renderApp();
    await screen.findByRole("heading", { name: "隐私与存储设置" });

    await user.click(screen.getByRole("button", { name: "返回素材库" }));

    await waitFor(() => expect(screen.getByRole("heading", { name: /把看到的/ })).toHaveFocus());
    expect(window.scrollY).toBe(0);
    expect(collectionApi.getItem).not.toHaveBeenCalled();
    expectNoBusinessWrites();
  });

  it.each(["library", "review", "detail"] as const)("设置刷新使 %s 来源 token 失效，返回只进入可用素材库并聚焦标题", async (from) => {
    const user = userEvent.setup();
    const view = renderApp();
    await screen.findByRole("button", { name: `打开素材：${item.display_title}` });
    if (from === "review") await openReview(user);
    if (from === "detail") {
      await user.click(screen.getByRole("button", { name: `打开素材：${item.display_title}` }));
      await screen.findByRole("heading", { name: item.display_title });
    }
    await openSettings(user);
    view.unmount();
    const previewCalls = vi.mocked(collectionApi.createPreview).mock.calls.length;
    const itemCalls = vi.mocked(collectionApi.getItem).mock.calls.length;
    renderApp();
    await screen.findByRole("heading", { name: "隐私与存储设置" });

    await user.click(screen.getByRole("button", { name: "返回素材库" }));

    await waitFor(() => expect(screen.getByRole("heading", { name: /把看到的/ })).toHaveFocus());
    expect(collectionApi.createPreview).toHaveBeenCalledTimes(previewCalls);
    expect(collectionApi.getItem).toHaveBeenCalledTimes(itemCalls);
    expect(screen.queryByRole("heading", { name: "保存前整理" })).not.toBeInTheDocument();
    expect(window.scrollY).toBe(0);
  });
});
