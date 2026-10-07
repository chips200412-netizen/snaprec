import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CollectionDeepAnalysisSnapshot, CollectionItem, CollectionPreview } from "./collection-api";

vi.mock("./collection-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./collection-api")>();
  return {
    ...actual,
    collectionApi: {
      createPreview: vi.fn(),
      transcribeInspiration: vi.fn(),
      createItem: vi.fn(),
      updateItem: vi.fn(),
      getItem: vi.fn(),
      listItems: vi.fn(),
      getDeepAnalysis: vi.fn(),
      startDeepAnalysis: vi.fn(),
      retryDeepAnalysis: vi.fn(),
      uploadUserCover: vi.fn(),
      getUserCover: vi.fn(),
      deleteUserCover: vi.fn(),
    },
  };
});

vi.mock("./legacy-app", () => ({
  LegacyApp: ({ initialResourceKey, onCollectionReturn }: { initialResourceKey?: string; onCollectionReturn?: () => void }) => (
    <main>
      <h1>已打开深度结果 {initialResourceKey}</h1>
      <button type="button" onClick={onCollectionReturn}>返回素材详情</button>
    </main>
  ),
}));

import { CollectionApp } from "./collection-app";
import { App as PublicApp } from "./App";
import { CollectionApiError, collectionApi } from "./collection-api";
import { readEditMergeSession } from "./collection-edit-model";
import { ThemeProvider } from "./theme";

const preview: CollectionPreview = {
  preview_id: "preview-1",
  original_input: "分享 https://example.com/cafe",
  source_url: "https://example.com/cafe",
  canonical_url: "https://example.com/cafe",
  identity_url: "https://example.com/cafe",
  source_kind: "article",
  platform: "web",
  metadata_status: "generic",
  metadata: {
    title: { value: "东京咖啡馆空间与光线", source: "open_graph", fetched_at: "2026-08-23T09:00:00Z" },
    author: { value: "Mori Studio", source: "page_metadata", fetched_at: "2026-08-23T09:00:00Z" },
    cover_url: { value: "https://images.example/cafe.jpg", source: "open_graph", fetched_at: "2026-08-23T09:00:00Z" },
    source_copy: { value: "阳光透过百叶窗洒进来，空间安静而温柔。", source: "page_description", fetched_at: "2026-08-23T09:00:00Z" },
    platform_tags: [{ value: "咖啡馆", source: "page_metadata" }],
    warnings: [],
  },
  organization_suggestion: {
    primary_category: "设计",
    secondary_category: "空间",
    tags: ["自然光", "日式风格"],
    basis: "public_metadata",
    method: "deterministic",
    status: "generated",
  },
  created_at: "2026-08-23T09:00:00Z",
  expires_at: "2026-08-23T09:30:00Z",
};

const savedItem: CollectionItem = {
  id: "collection-1",
  ...preview,
  user_title: null,
  display_title: preview.metadata.title.value,
  organization_confirmation: {
    primary_category: "设计",
    secondary_category: "空间",
    organization_tags: ["自然光", "日式风格"],
  },
  personal_tags: [],
  inspiration: {
    id: "inspiration-1",
    collection_item_id: "collection-1",
    content: "想在书房试试这种光线。",
    input_mode: "text",
    transcription_status: "not_applicable",
    created_at: "2026-08-23T09:10:00Z",
    updated_at: "2026-08-23T09:10:00Z",
  },
  deep_analysis_resource_key: null,
  revision: 1,
  updated_at: "2026-08-23T09:10:00Z",
};

const unavailableDeepAnalysis: CollectionDeepAnalysisSnapshot = {
  material_id: savedItem.id,
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
  updated_at: savedItem.updated_at,
  limitation: "当前来源尚不支持深度解析。",
  error_code: null,
  error_message: "",
};

const desktopUserAgent = navigator.userAgent;
const originalViewportWidth = window.innerWidth;
const originalTouchPoints = navigator.maxTouchPoints;
function usePhoneDevice() {
  Object.defineProperty(navigator, "userAgent", { configurable: true, value: "Mozilla/5.0 (Linux; Android 14; Pixel 8) Mobile Safari/537.36" });
}
async function prepareTapVoice(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "点按录音" }));
  await user.click(screen.getByRole("button", { name: "开始录音" }));
  await screen.findByText("已准备好，请点击开始录音");
}
function renderApp() {
  return render(<ThemeProvider><CollectionApp /></ThemeProvider>);
}

function mockPhoneRecorder() {
  usePhoneDevice();
  const stopTrack = vi.fn();
  const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [{ stop: stopTrack }] });
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia } });
  class Recorder {
    state: RecordingState = "inactive";
    mimeType = "audio/webm";
    ondataavailable: ((event: BlobEvent) => void) | null = null;
    onstop: (() => void) | null = null;
    start() { this.state = "recording"; }
    stop() { this.state = "inactive"; this.ondataavailable?.({ data: new Blob(["synthetic user recording"]) } as BlobEvent); this.onstop?.(); }
  }
  Object.defineProperty(globalThis, "MediaRecorder", { configurable: true, value: Recorder });
  return { getUserMedia, stopTrack };
}

describe("瞬时录收藏主线", () => {
  beforeEach(() => {
    Object.defineProperty(navigator, "userAgent", { configurable: true, value: desktopUserAgent });
    Object.defineProperty(navigator, "maxTouchPoints", { configurable: true, value: originalTouchPoints });
    Object.defineProperty(window, "innerWidth", { configurable: true, value: originalViewportWidth });
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
    Object.defineProperty(window, "scrollTo", { configurable: true, value: vi.fn() });
    Object.defineProperty(window, "scrollBy", { configurable: true, value: vi.fn() });
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
    Object.defineProperty(globalThis.CSS, "escape", { configurable: true, value: (value: string) => value });
    vi.mocked(collectionApi.createPreview).mockReset();
    vi.mocked(collectionApi.createPreview).mockResolvedValue(preview);
    vi.mocked(collectionApi.createItem).mockReset();
    vi.mocked(collectionApi.createItem).mockResolvedValue(savedItem);
    vi.mocked(collectionApi.updateItem).mockReset();
    vi.mocked(collectionApi.updateItem).mockResolvedValue(savedItem);
    vi.mocked(collectionApi.listItems).mockReset();
    vi.mocked(collectionApi.listItems).mockResolvedValue({
      items: [],
      total: 0,
      limit: 24,
      next_cursor: null,
      facets: { platforms: [], categories: [], tags: [] },
    });
    vi.mocked(collectionApi.transcribeInspiration).mockReset();
    vi.mocked(collectionApi.getItem).mockReset();
    vi.mocked(collectionApi.getItem).mockResolvedValue(savedItem);
    vi.mocked(collectionApi.getDeepAnalysis).mockReset();
    vi.mocked(collectionApi.getDeepAnalysis).mockResolvedValue(unavailableDeepAnalysis);
    vi.mocked(collectionApi.startDeepAnalysis).mockReset();
    vi.mocked(collectionApi.startDeepAnalysis).mockResolvedValue(unavailableDeepAnalysis);
    vi.mocked(collectionApi.retryDeepAnalysis).mockReset();
    vi.mocked(collectionApi.retryDeepAnalysis).mockResolvedValue(unavailableDeepAnalysis);
    vi.mocked(collectionApi.uploadUserCover).mockReset();
    vi.mocked(collectionApi.getUserCover).mockReset();
    vi.mocked(collectionApi.getUserCover).mockResolvedValue(new Blob(["cover"], { type: "image/webp" }));
    vi.mocked(collectionApi.deleteUserCover).mockReset();
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: undefined });
    Object.defineProperty(globalThis, "MediaRecorder", { configurable: true, value: undefined });
  });

  it("INT-ACC-CQ2-001 默认不选，原生键盘切换、清空并显式保存空集", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    const topic = await screen.findByRole("checkbox", { name: "咖啡馆" });
    expect(topic).not.toBeChecked();
    expect(screen.getByRole("button", { name: "清空选择" })).toBeDisabled();
    topic.focus();
    await user.keyboard(" ");
    expect(topic).toBeChecked();
    expect(screen.getByText(/已选 1 项/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "清空选择" }));
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));
    expect(collectionApi.createItem).toHaveBeenCalledWith(expect.objectContaining({ selected_source_topic_indices: [] }), expect.any(String), undefined, undefined);
  });

  it("CQ3-S1 审核页原子保存个人作者和已消毒封面 claim", async () => {
    const user = userEvent.setup();
    const draft = {
      asset_id: "b".repeat(32),
      claim_token: "claim-token-long-enough",
      media_type: "image/webp" as const,
      width: 640,
      height: 480,
      size_bytes: 1024,
      expires_at: "2099-09-11T00:00:00Z",
    };
    vi.mocked(collectionApi.uploadUserCover).mockResolvedValue(draft);
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:review-cover") });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    const rendered = renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });

    await user.type(screen.getByLabelText(/我补充的作者/), "  Mori  本人  ");
    const fileInput = rendered.container.querySelector<HTMLInputElement>('#review-user-cover-file')!;
    await user.upload(fileInput, new File(["png"], "cover.png", { type: "image/png" }));
    await screen.findByText("图片已就绪；保存收藏后才会正式绑定。");
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));

    await waitFor(() => expect(collectionApi.createItem).toHaveBeenCalledOnce());
    expect(collectionApi.createItem).toHaveBeenCalledWith(expect.objectContaining({
      user_author: "Mori 本人",
      user_cover_asset_id: draft.asset_id,
    }), expect.any(String), undefined, draft.claim_token);
  });

  it("INT-ACC-CQ2-002 刷新按身份映射索引，失败保留绑定快照，成功空结果才取消", async () => {
    const user = userEvent.setup();
    const next: CollectionPreview = { ...preview, preview_id: "preview-2", metadata: { ...preview.metadata, platform_tags: [{ value: "新增", source: "page_metadata" }, ...preview.metadata.platform_tags] } };
    vi.mocked(collectionApi.createPreview).mockResolvedValueOnce(preview).mockResolvedValueOnce(next)
      .mockResolvedValueOnce({ ...next, preview_id: "failed-preview", metadata_status: "metadata_unavailable", metadata: { ...next.metadata, platform_tags: [], warnings: ["获取失败"] } })
      .mockResolvedValueOnce({ ...next, preview_id: "preview-empty", metadata: { ...next.metadata, platform_tags: [] } });
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await user.click(await screen.findByRole("checkbox", { name: "咖啡馆" }));
    await user.click(screen.getByRole("button", { name: "重新获取公开信息" }));
    expect(await screen.findByRole("checkbox", { name: "新增" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "咖啡馆" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "重新获取公开信息" }));
    expect(await screen.findByText(/已保留上次来源信息和话题选择/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "咖啡馆" })).toBeChecked();
    vi.mocked(collectionApi.createItem).mockRejectedValueOnce(new CollectionApiError("测试拒绝", "COLLECTION_VALIDATION_FAILED", 400));
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));
    await screen.findByText("测试拒绝");
    expect(collectionApi.createItem).toHaveBeenLastCalledWith(expect.objectContaining({ preview_id: "preview-2", selected_source_topic_indices: [1] }), expect.any(String), undefined, undefined);
    await user.click(screen.getByRole("button", { name: "重新获取公开信息" }));
    expect(await screen.findByText(/暂无可选来源话题/)).toBeInTheDocument();
    expect(screen.getByText(/取消了 1 项/)).toBeInTheDocument();
  });

  it("INT-ACC-CQ2-004B 自动建议不回灌来源话题，手动同名标签在刷新后保留", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionApi.createPreview).mockResolvedValue({ ...preview, organization_suggestion: { ...preview.organization_suggestion, tags: ["咖啡馆", "自然光"] } });
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("checkbox", { name: "咖啡馆" });
    expect(screen.queryByRole("button", { name: "删除整理标签：咖啡馆" })).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("整理标签"), "咖啡馆{Enter}");
    await user.click(screen.getByRole("button", { name: "重新获取公开信息" }));
    await waitFor(() => expect(collectionApi.createPreview).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("button", { name: "删除整理标签：咖啡馆" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));
    expect(collectionApi.createItem).toHaveBeenCalledWith(expect.objectContaining({ selected_source_topic_indices: [], organization_confirmation: expect.objectContaining({ organization_tags: ["自然光", "咖啡馆"] }) }), expect.any(String), undefined, undefined);
  });

  it("INT-ACC-CQ2-005 选择触发离页保护且失败重试冻结同一索引集合", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionApi.createItem).mockRejectedValueOnce(new Error("网络结果未知"));
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await user.click(await screen.findByRole("checkbox", { name: "咖啡馆" }));
    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(await screen.findByRole("dialog", { name: "放弃当前整理吗？" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "继续整理" }));
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));
    expect(await screen.findByRole("checkbox", { name: "咖啡馆" })).toBeChecked();
    const retry = await screen.findByRole("button", { name: "重试保存" });
    await user.click(retry);
    expect(vi.mocked(collectionApi.createItem).mock.calls[0]).toEqual(vi.mocked(collectionApi.createItem).mock.calls[1]);
    expect(vi.mocked(collectionApi.createItem).mock.calls[0][0]).toMatchObject({ selected_source_topic_indices: [0] });
  });

  it.each([null, undefined, [], [1]])("INT-ACC-CQ2-004B 详情遵循独立子集和旧响应兼容 %j", async (selection) => {
    window.history.replaceState({}, "", "/materials/collection-1");
    vi.mocked(collectionApi.getItem).mockResolvedValue({
      ...savedItem, selected_source_topic_indices: selection,
      metadata: { ...savedItem.metadata, platform_tags: [{ value: "未选话题", source: "share_text" }, { value: "已选话题", source: "share_text" }] },
    });
    renderApp();
    await screen.findByRole("heading", { name: "整理结果" });
    const result = document.querySelector(".detail-organization")!;
    if (selection == null) {
      expect(within(result as HTMLElement).getByText("未选话题")).toBeInTheDocument();
      expect(within(result as HTMLElement).getByText("已选话题")).toBeInTheDocument();
      expect(within(result as HTMLElement).getAllByText("分享话题")).toHaveLength(2);
    } else {
      expect(within(result as HTMLElement).queryByText("未选话题")).not.toBeInTheDocument();
      expect(within(result as HTMLElement).queryByText("已选话题") !== null).toBe(selection.length > 0);
      expect(within(result as HTMLElement).queryByText("来源话题") !== null).toBe(selection.length > 0);
    }
  });

  it("INT-ACC-CQ2-002 新候选命中早期自动建议时移除自动项并冻结刷新操作", async () => {
    const user = userEvent.setup();
    let resolveRefresh!: (value: CollectionPreview) => void;
    vi.mocked(collectionApi.createPreview).mockResolvedValueOnce(preview).mockReturnValueOnce(new Promise((resolve) => { resolveRefresh = resolve; }));
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await user.click(await screen.findByRole("checkbox", { name: "咖啡馆" }));
    await user.click(screen.getByRole("button", { name: "重新获取公开信息" }));
    expect(screen.getByRole("checkbox", { name: "咖啡馆" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "清空选择" })).toBeDisabled();
    await act(async () => resolveRefresh({ ...preview, preview_id: "new", metadata: { ...preview.metadata, platform_tags: [...preview.metadata.platform_tags, { value: "自然光", source: "page_metadata" }] } }));
    expect(await screen.findByRole("checkbox", { name: "自然光" })).not.toBeChecked();
    expect(screen.queryByRole("button", { name: "删除整理标签：自然光" })).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "咖啡馆" })).toBeChecked();
  });

  it("INT-ACC-CQ2-003 文案展开不改选择，刷新正文重置折叠且消失焦点回到刷新", async () => {
    const user = userEvent.setup();
    let resolveRefresh!: (value: CollectionPreview) => void;
    vi.mocked(collectionApi.createPreview).mockResolvedValueOnce(preview).mockReturnValueOnce(new Promise((resolve) => { resolveRefresh = resolve; }));
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    const topic = await screen.findByRole("checkbox", { name: "咖啡馆" });
    await user.click(topic);
    const copy = document.getElementById("source-copy-content")!;
    Object.defineProperty(copy, "scrollHeight", { configurable: true, value: 500 });
    Object.defineProperty(copy, "clientHeight", { configurable: true, value: 100 });
    fireEvent(window, new Event("resize"));
    await user.click(await screen.findByRole("button", { name: "展开全文" }));
    expect(copy).toHaveClass("is-expanded");
    expect(topic).toBeChecked();
    topic.focus();
    // Programmatic activation keeps focus on the candidate until it disappears.
    fireEvent.click(screen.getByRole("button", { name: "重新获取公开信息" }));
    await waitFor(() => expect(collectionApi.createPreview).toHaveBeenCalledTimes(2));
    await act(async () => resolveRefresh({
      ...preview, preview_id: "changed-copy",
      metadata: { ...preview.metadata, source_copy: { ...preview.metadata.source_copy, value: "刷新后的来源文案" }, platform_tags: [] },
    }));
    expect(await screen.findByText("刷新后的来源文案")).not.toHaveClass("is-expanded");
    expect(screen.getByRole("button", { name: "重新获取公开信息" })).toHaveFocus();
    expect(screen.getByText(/已选 0 项/)).toBeInTheDocument();
  });

  it("INT-ACC-CQ2-001/005 保存期间冻结选择，失败后变更选择使用新的安全提交", async () => {
    const user = userEvent.setup();
    let rejectSave!: (error: Error) => void;
    vi.mocked(collectionApi.createItem).mockReturnValueOnce(new Promise((_resolve, reject) => { rejectSave = reject; }));
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    const topic = await screen.findByRole("checkbox", { name: "咖啡馆" });
    await user.click(topic);
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));
    expect(topic).toBeDisabled();
    expect(screen.getByRole("button", { name: "清空选择" })).toBeDisabled();
    await act(async () => rejectSave(new Error("未知结果")));
    await screen.findByRole("button", { name: "重试保存" });
    await user.click(topic);
    expect(screen.queryByRole("button", { name: "重试保存" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));
    const calls = vi.mocked(collectionApi.createItem).mock.calls;
    expect(calls[0][0].selected_source_topic_indices).toEqual([0]);
    expect(calls[1][0].selected_source_topic_indices).toEqual([]);
    expect(calls[1][1]).not.toBe(calls[0][1]);
  });

  it("INT-ACC-CQ2-002 拒绝刷新不丢选择，退出后迟到响应不污染新的整理", async () => {
    const user = userEvent.setup();
    let resolveLate!: (value: CollectionPreview) => void;
    vi.mocked(collectionApi.createPreview).mockResolvedValueOnce(preview)
      .mockRejectedValueOnce(new Error("刷新网络失败"))
      .mockReturnValueOnce(new Promise((resolve) => { resolveLate = resolve; }))
      .mockResolvedValueOnce({ ...preview, preview_id: "fresh-page" });
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await user.click(await screen.findByRole("checkbox", { name: "咖啡馆" }));
    await user.click(screen.getByRole("button", { name: "重新获取公开信息" }));
    await screen.findByText("刷新网络失败");
    expect(screen.getByRole("checkbox", { name: "咖啡馆" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "重新获取公开信息" }));
    await user.click(screen.getByRole("button", { name: "取消" }));
    await user.click(await screen.findByRole("button", { name: "放弃并返回素材库" }));
    await act(async () => resolveLate({ ...preview, preview_id: "stale-page", metadata: { ...preview.metadata, platform_tags: [{ value: "迟到话题", source: "share_text" }] } }));
    await user.type(await screen.findByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    expect(await screen.findByRole("checkbox", { name: "咖啡馆" })).not.toBeChecked();
    expect(screen.queryByRole("checkbox", { name: "迟到话题" })).not.toBeInTheDocument();
  });

  it("启动时从权威列表恢复卡片，并按 ID 读取完整素材详情", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionApi.listItems).mockResolvedValue({
      items: [{
        id: savedItem.id,
        display_title: savedItem.display_title,
        platform: savedItem.platform,
        primary_category: savedItem.organization_confirmation.primary_category,
        cover_url: savedItem.metadata.cover_url.value,
        created_at: savedItem.created_at,
        updated_at: savedItem.updated_at,
      }],
      total: 1,
      limit: 24,
      next_cursor: null,
      facets: {
        platforms: [{ platform: "web", count: 1 }],
        categories: [{ primary_category: "设计", count: 1, children: [{ secondary_category: "空间", count: 1 }] }],
        tags: [{ name: "自然光", source: "organization", count: 1 }],
      },
    });
    renderApp();

    const card = await screen.findByRole("button", { name: "打开素材：东京咖啡馆空间与光线" });
    expect(card.querySelector(".material-cover img")).toHaveAttribute(
      "src",
      "/api/v1/collection-items/collection-1/cover",
    );
    expect(Array.from(document.querySelectorAll("img"), (image) => image.getAttribute("src")))
      .not.toContain(savedItem.metadata.cover_url.value);
    expect(screen.getByRole("button", { name: "设计" })).toBeInTheDocument();
    await user.click(card);

    expect(await screen.findByRole("heading", { name: "我的灵感" })).toBeInTheDocument();
    expect(document.querySelector(".detail-prelude .material-cover img")).toHaveAttribute(
      "src",
      "/api/v1/collection-items/collection-1/cover",
    );
    expect(screen.getByText("想在书房试试这种光线。")).toBeInTheDocument();
    expect(collectionApi.getItem).toHaveBeenCalledWith("collection-1", expect.any(AbortSignal));
  });

  it("D03 从权威详情进入编辑路由，并以 inert 详情承载桌面 sidecar", async () => {
    const user = userEvent.setup();
    const originalMatchMedia = window.matchMedia;
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
    vi.mocked(collectionApi.listItems).mockResolvedValue({
      items: [{
        id: savedItem.id,
        display_title: savedItem.display_title,
        platform: savedItem.platform,
        primary_category: savedItem.organization_confirmation.primary_category,
        cover_url: savedItem.metadata.cover_url.value,
        created_at: savedItem.created_at,
        updated_at: savedItem.updated_at,
      }],
      total: 1,
      limit: 24,
      next_cursor: null,
      facets: { platforms: [], categories: [], tags: [] },
    });
    renderApp();

    await user.click(await screen.findByRole("button", { name: "打开素材：东京咖啡馆空间与光线" }));
    expect(await screen.findByRole("heading", { name: "我的收藏内容" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "编辑收藏" }));

    expect(await screen.findByRole("dialog", { name: "编辑收藏" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/materials/collection-1/edit");
    expect(document.querySelector(".edit-merge-background-shell")).toHaveAttribute("inert");
    expect(await screen.findByRole("textbox", { name: "自定义标题" })).toHaveValue("");
    expect(document.querySelector(".edit-merge-background-shell .material-cover img")).toHaveAttribute(
      "src",
      "/api/v1/collection-items/collection-1/cover?cache=only",
    );
    expect(document.querySelector(".immutable-identity img")).toHaveAttribute(
      "src",
      "/api/v1/collection-items/collection-1/cover?cache=only",
    );
    expect(collectionApi.getDeepAnalysis).not.toHaveBeenCalled();
    Object.defineProperty(window, "matchMedia", { configurable: true, value: originalMatchMedia });
  });

  it("DY-FIX 详情明确平台、作者和分享话题，隔离旧平台CSS类", async () => {
    vi.mocked(collectionApi.getItem).mockResolvedValue({
      ...savedItem, platform: "douyin", source_kind: "video",
      metadata: { ...savedItem.metadata,
        author: { ...savedItem.metadata.author, value: "小羊同学", source: "platform_public" },
        platform_tags: [{ value: "网站上线", source: "share_text" }],
      },
    });
    window.history.replaceState({}, "", "/materials/collection-1");
    renderApp();
    await screen.findByRole("heading", { name: savedItem.display_title });
    expect(document.querySelector(".detail-byline")).toHaveTextContent("抖音·视频作者：小羊同学");
    expect(document.querySelector(".platform-identity")).not.toHaveClass("platform-douyin");
    expect(screen.getByText("分享话题")).toBeInTheDocument();
    expect(collectionApi.createPreview).not.toHaveBeenCalled();
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
  });

  it("DY-FIX 缺作者与缺封面使用未获取提示，不伪称来源没有提供", async () => {
    vi.mocked(collectionApi.getItem).mockResolvedValue({
      ...savedItem, platform: "douyin", source_kind: "video",
      metadata: { ...savedItem.metadata,
        author: { value: "", source: "none", fetched_at: "" },
        cover_url: { value: "", source: "none", fetched_at: "" },
        source_copy: { value: "", source: "none", fetched_at: "" },
      },
    });
    window.history.replaceState({}, "", "/materials/collection-1");
    renderApp();
    await screen.findByRole("heading", { name: savedItem.display_title });
    expect(screen.getByText("视频作者：未获取到作者")).toBeInTheDocument();
    expect(screen.getByText("未获取到视频封面，当前显示占位图。")).toBeInTheDocument();
    expect(screen.getByText("未获取到来源文案；仍可保存原链接，不会自动编写简介。")).toBeInTheDocument();
    expect(screen.queryByText("作者未提供")).not.toBeInTheDocument();
  });

  it("DY-FIX 同源封面失败才显示缺省说明，不增加远程请求或重试循环", async () => {
    window.history.replaceState({}, "", "/materials/collection-1");
    renderApp();
    await screen.findByRole("heading", { name: savedItem.display_title });
    const cover = document.querySelector<HTMLImageElement>(".detail-prelude .material-cover img")!;
    expect(screen.queryByText(/当前显示占位图/)).not.toBeInTheDocument();
    fireEvent.error(cover);
    expect(await screen.findByText("未获取到素材封面，当前显示占位图。")).toBeInTheDocument();
    expect(cover).toHaveAttribute("src", "/assets/material-cover-fallback.webp");
    fireEvent.error(cover);
    expect(cover).toHaveAttribute("src", "/assets/material-cover-fallback.webp");
    expect(collectionApi.createPreview).not.toHaveBeenCalled();
  });

  it("详情 session 首写失败会就地报错并聚焦，重试成功才进入编辑且不写业务接口", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/materials/collection-1");
    renderApp();

    expect(await screen.findByRole("heading", { name: savedItem.display_title })).toBeInTheDocument();
    const storageWrite = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("storage unavailable", "QuotaExceededError");
    });
    let storageRestored = false;
    try {
      await user.click(screen.getByRole("button", { name: "编辑收藏" }));
      expect(window.location.pathname).toBe("/materials/collection-1");
      expect(screen.queryByRole("dialog", { name: "编辑收藏" })).not.toBeInTheDocument();
      expect(document.querySelector(".edit-merge-background-shell")).not.toBeInTheDocument();
      expect(window.sessionStorage.length).toBe(0);
      const error = screen.getByRole("alert");
      expect(error).toHaveTextContent("当前标签页无法建立安全编辑会话，请检查浏览器存储后重试。");
      expect(error).toHaveAttribute("tabindex", "-1");
      await waitFor(() => expect(error).toHaveFocus());

      storageWrite.mockRestore();
      storageRestored = true;
      await user.click(screen.getByRole("button", { name: "重试编辑" }));
      expect(await screen.findByRole("dialog", { name: "编辑收藏" })).toBeInTheDocument();
      expect(window.location.pathname).toBe("/materials/collection-1/edit");
      expect(window.sessionStorage.length).toBe(1);
      expect(collectionApi.createItem).not.toHaveBeenCalled();
      expect(collectionApi.updateItem).not.toHaveBeenCalled();
    } finally {
      if (!storageRestored) storageWrite.mockRestore();
    }
  });

  it("直达 edit 生成稳定 entry，GET 后写入草稿并在刷新重挂时恢复", async () => {
    const user = userEvent.setup();
    const originalMatchMedia = window.matchMedia;
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
    window.history.replaceState({}, "", "/materials/collection-1/edit");
    try {
      const first = renderApp();
      const title = await screen.findByRole("textbox", { name: "自定义标题" });
      const firstEntryId = window.history.state?.collectionRoute?.entryId as string | undefined;
      expect(firstEntryId).toEqual(expect.any(String));
      expect(firstEntryId).not.toBe("");
      await user.clear(title);
      await user.type(title, "刷新后继续编辑");
      await waitFor(() => expect(readEditMergeSession("edit", savedItem.id, firstEntryId)?.desired?.user_title)
        .toBe("刷新后继续编辑"));

      first.unmount();
      renderApp();

      expect(await screen.findByRole("textbox", { name: "自定义标题" })).toHaveValue("刷新后继续编辑");
      expect(window.history.state?.collectionRoute?.entryId).toBe(firstEntryId);
      expect(collectionApi.getItem).toHaveBeenCalledTimes(2);
      expect(collectionApi.updateItem).not.toHaveBeenCalled();
    } finally {
      Object.defineProperty(window, "matchMedia", { configurable: true, value: originalMatchMedia });
    }
  });

  it("PATCH 成功后进入权威详情，并以新 generation 从列表第一页刷新搜索投影", async () => {
    const user = userEvent.setup();
    const originalMatchMedia = window.matchMedia;
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
    const updatedItem: CollectionItem = {
      ...savedItem,
      user_title: "更新后的标题",
      display_title: "更新后的标题",
      revision: 2,
      updated_at: "2026-08-29T11:00:00Z",
    };
    const oldListItem = {
      id: savedItem.id,
      display_title: savedItem.display_title,
      platform: savedItem.platform,
      primary_category: savedItem.organization_confirmation.primary_category,
      cover_url: savedItem.metadata.cover_url.value,
      created_at: savedItem.created_at,
      updated_at: savedItem.updated_at,
    };
    vi.mocked(collectionApi.listItems)
      .mockResolvedValueOnce({
        items: [oldListItem],
        total: 1,
        limit: 24,
        next_cursor: "stale-cursor",
        facets: { platforms: [], categories: [], tags: [] },
      })
      .mockResolvedValueOnce({
        items: [{ ...oldListItem, display_title: updatedItem.display_title, updated_at: updatedItem.updated_at }],
        total: 1,
        limit: 24,
        next_cursor: null,
        facets: { platforms: [], categories: [], tags: [] },
      });
    vi.mocked(collectionApi.getItem)
      .mockResolvedValueOnce(savedItem)
      .mockResolvedValueOnce(savedItem)
      .mockResolvedValueOnce(updatedItem);
    vi.mocked(collectionApi.updateItem).mockResolvedValueOnce(updatedItem);
    renderApp();

    await user.click(await screen.findByRole("button", { name: "打开素材：东京咖啡馆空间与光线" }));
    await user.click(await screen.findByRole("button", { name: "编辑收藏" }));
    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.type(title, "更新后的标题");
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    expect(await screen.findByRole("heading", { name: "更新后的标题" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "编辑收藏" })).toHaveFocus());
    await waitFor(() => expect(collectionApi.listItems).toHaveBeenCalledTimes(2));
    expect(vi.mocked(collectionApi.listItems).mock.calls[1][0]).not.toHaveProperty("cursor");
    expect(vi.mocked(collectionApi.listItems).mock.calls[1][0]).toMatchObject({ limit: 24 });
    expect(window.location.pathname).toBe("/materials/collection-1");
    expect(window.sessionStorage.length).toBe(0);
    act(() => window.history.back());
    await waitFor(() => expect(window.location.pathname).toBe("/"));
    expect(await screen.findByRole("button", { name: "打开素材：更新后的标题" })).toBeInTheDocument();
    Object.defineProperty(window, "matchMedia", { configurable: true, value: originalMatchMedia });
  });

  it("direct edit 成功在视觉退出计时前 Back 仍收束到权威详情，query generation 只刷新一次", async () => {
    const user = userEvent.setup();
    const updatedItem: CollectionItem = {
      ...savedItem,
      user_title: "直达完成标题",
      display_title: "直达完成标题",
      revision: 2,
      updated_at: "2026-08-29T11:01:00Z",
    };
    let resolveUpdate!: (value: CollectionItem) => void;
    vi.mocked(collectionApi.updateItem).mockReturnValueOnce(new Promise((resolve) => { resolveUpdate = resolve; }));
    window.history.replaceState({}, "", "/materials/collection-1/edit");
    renderApp();

    const title = await screen.findByRole("textbox", { name: "自定义标题" });
    await user.type(title, "直达完成标题");
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(collectionApi.updateItem).toHaveBeenCalledOnce());
    const listCallsBeforeCommit = vi.mocked(collectionApi.listItems).mock.calls.length;

    vi.useFakeTimers();
    try {
      await act(async () => {
        resolveUpdate(updatedItem);
        await Promise.resolve();
      });
      expect(window.location.pathname).toBe("/materials/collection-1/edit");
      expect(window.sessionStorage.length).toBe(0);
      expect(collectionApi.listItems).toHaveBeenCalledTimes(listCallsBeforeCommit + 1);

      act(() => window.dispatchEvent(new PopStateEvent("popstate", {
        state: { collectionRoute: { name: "library", entryId: "old-library" } },
      })));
      expect(window.location.pathname).toBe("/materials/collection-1");
      expect(screen.getByRole("heading", { name: "直达完成标题" })).toBeInTheDocument();
      act(() => vi.advanceTimersByTime(200));
      expect(window.location.pathname).toBe("/materials/collection-1");
      expect(collectionApi.listItems).toHaveBeenCalledTimes(listCallsBeforeCommit + 1);
      expect(collectionApi.updateItem).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });

  it("D02 重复态保留打开已有项，并用比较并合并建立当前标签页 merge 会话", async () => {
    const user = userEvent.setup();
    const originalMatchMedia = window.matchMedia;
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
    vi.mocked(collectionApi.createItem).mockRejectedValueOnce(
      new CollectionApiError("素材已存在", "COLLECTION_EXISTS", 409, savedItem.id),
    );
    renderApp();

    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));

    expect(await screen.findByRole("button", { name: "打开已有项" })).toBeInTheDocument();
    const merge = screen.getByRole("button", { name: "比较并合并" });
    expect(merge).toBeInTheDocument();
    await user.click(merge);

    expect(await screen.findByRole("dialog", { name: "合并重复收藏" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/materials/collection-1/merge");
    expect(await screen.findByText(/默认保留已有标量和灵感/)).toBeInTheDocument();
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
    Object.defineProperty(window, "matchMedia", { configurable: true, value: originalMatchMedia });
  });

  it("Review 创建合同以 code point 接受 500/64/4000 边界，并就地阻断 501/65/4001 且零 POST", async () => {
    const user = userEvent.setup();
    const untitledPreview: CollectionPreview = {
      ...preview,
      metadata: {
        ...preview.metadata,
        title: { ...preview.metadata.title, value: "" },
      },
    };
    vi.mocked(collectionApi.createPreview).mockResolvedValueOnce(untitledPreview);
    renderApp();

    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    const title = await screen.findByRole("textbox", { name: "收藏标题" });
    const primary = screen.getByRole("textbox", { name: "一级分类" });
    const secondary = screen.getByRole("textbox", { name: "二级分类" });
    const inspiration = screen.getByRole("textbox", { name: "我的灵感" });
    const save = screen.getByRole("button", { name: "保存到素材库" });
    const astral = "😀";

    fireEvent.change(title, { target: { value: astral.repeat(500) } });
    fireEvent.change(primary, { target: { value: astral.repeat(64) } });
    fireEvent.change(secondary, { target: { value: astral.repeat(64) } });
    fireEvent.change(inspiration, { target: { value: astral.repeat(4000) } });
    expect(title).toHaveAttribute("aria-invalid", "false");
    expect(primary).toHaveAttribute("aria-invalid", "false");
    expect(secondary).toHaveAttribute("aria-invalid", "false");
    expect(inspiration).toHaveAttribute("aria-invalid", "false");
    expect(save).toBeEnabled();

    fireEvent.change(title, { target: { value: astral.repeat(501) } });
    expect(title).toHaveAttribute("aria-invalid", "true");
    expect(title).toHaveAttribute("aria-describedby", "review-user-title-error");
    expect(save).toBeDisabled();
    expect(screen.getAllByRole("alert").filter((node) => node.textContent?.includes("自定义标题最多 500 个字符"))).toHaveLength(1);
    fireEvent.click(save);
    expect(collectionApi.createItem).not.toHaveBeenCalled();

    fireEvent.change(title, { target: { value: astral.repeat(500) } });
    fireEvent.change(primary, { target: { value: astral.repeat(65) } });
    expect(primary).toHaveAttribute("aria-invalid", "true");
    expect(primary).toHaveAttribute("aria-describedby", "review-primary-category-error");
    expect(save).toBeDisabled();
    fireEvent.change(primary, { target: { value: astral.repeat(64) } });

    fireEvent.change(secondary, { target: { value: astral.repeat(65) } });
    expect(secondary).toHaveAttribute("aria-invalid", "true");
    expect(secondary).toHaveAttribute("aria-describedby", "review-secondary-category-error");
    expect(save).toBeDisabled();
    fireEvent.change(secondary, { target: { value: astral.repeat(64) } });

    fireEvent.change(inspiration, { target: { value: astral.repeat(4001) } });
    expect(inspiration).toHaveAttribute("aria-invalid", "true");
    expect(inspiration).toHaveAttribute("aria-describedby", "review-inspiration-error");
    expect(save).toBeDisabled();
    fireEvent.click(save);
    expect(collectionApi.createItem).not.toHaveBeenCalled();

    fireEvent.change(inspiration, { target: { value: astral.repeat(4000) } });
    expect(save).toBeEnabled();
    expect(collectionApi.createItem).not.toHaveBeenCalled();
  });

  it("duplicate 后改成非法分类会禁用比较并合并，不写 session 且零 PATCH", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionApi.createItem).mockRejectedValueOnce(
      new CollectionApiError("素材已存在", "COLLECTION_EXISTS", 409, savedItem.id),
    );
    renderApp();

    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));

    const merge = await screen.findByRole("button", { name: "比较并合并" });
    const primary = screen.getByRole("textbox", { name: "一级分类" });
    fireEvent.change(primary, { target: { value: "😀".repeat(65) } });
    expect(primary).toHaveAttribute("aria-invalid", "true");
    expect(primary).toHaveAttribute("aria-describedby", "review-primary-category-error");
    expect(merge).toBeDisabled();
    expect(screen.getAllByRole("alert").filter((node) => node.textContent?.includes("一级分类最多 64 个字符"))).toHaveLength(1);
    fireEvent.click(merge);
    expect(window.location.pathname).not.toContain("/merge");
    expect(window.sessionStorage.length).toBe(0);
    expect(collectionApi.createItem).toHaveBeenCalledOnce();
    expect(collectionApi.updateItem).not.toHaveBeenCalled();

    fireEvent.change(primary, { target: { value: "😀".repeat(64) } });
    expect(merge).toBeEnabled();
  });

  it("元信息刷新补出来源标题后仍保留并暴露已填写 user_title 的可修复错误", async () => {
    const user = userEvent.setup();
    const untitledPreview: CollectionPreview = {
      ...preview,
      metadata: {
        ...preview.metadata,
        title: { ...preview.metadata.title, value: "" },
      },
    };
    vi.mocked(collectionApi.createPreview)
      .mockResolvedValueOnce(untitledPreview)
      .mockResolvedValueOnce({ ...preview, preview_id: "preview-with-title" });
    renderApp();

    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    const title = await screen.findByRole("textbox", { name: "收藏标题" });
    fireEvent.change(title, { target: { value: "😀".repeat(501) } });
    expect(title).toHaveAttribute("aria-invalid", "true");

    await user.click(screen.getByRole("button", { name: "重新获取公开信息" }));
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    const retained = screen.getByRole("textbox", { name: "收藏标题" });
    expect(retained).toHaveValue("😀".repeat(501));
    expect(retained).toHaveAttribute("aria-invalid", "true");
    expect(retained).toHaveAttribute("aria-describedby", "review-user-title-error");
    expect(screen.getByRole("button", { name: "保存到素材库" })).toBeDisabled();
    expect(collectionApi.createItem).not.toHaveBeenCalled();
  });

  it("D02 重复态的标签草稿、转写草稿与活动录音都阻断合并，清理后才建立零外部请求会话", async () => {
    const user = userEvent.setup();
    usePhoneDevice();
    const originalMatchMedia = window.matchMedia;
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
    const stopTrack = vi.fn();
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: stopTrack }] }) },
    });
    class FakeMediaRecorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(_stream: MediaStream) {}
      start() { this.state = "recording"; }
      stop() {
        this.state = "inactive";
        this.ondataavailable?.({ data: new Blob(["voice"]) } as BlobEvent);
        this.onstop?.();
      }
    }
    Object.defineProperty(globalThis, "MediaRecorder", { configurable: true, value: FakeMediaRecorder });
    vi.mocked(collectionApi.transcribeInspiration).mockResolvedValue({ content: "待处理转写", input_mode: "voice", transcription_status: "draft" });
    vi.mocked(collectionApi.createItem).mockRejectedValueOnce(
      new CollectionApiError("素材已存在", "COLLECTION_EXISTS", 409, savedItem.id),
    );
    try {
      renderApp();
      await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
      await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
      await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
      await user.click(screen.getByRole("button", { name: "保存到素材库" }));

      const merge = await screen.findByRole("button", { name: "比较并合并" });
      const organizationDraft = screen.getByRole("textbox", { name: "整理标签" });
      await user.type(organizationDraft, "尚未添加");
      expect(merge).toBeDisabled();
      expect(screen.getByText(/尚未提交的整理标签/)).toBeInTheDocument();
      expect(window.sessionStorage.length).toBe(0);
      await user.clear(organizationDraft);

      await prepareTapVoice(user);
    await user.click(screen.getByRole("button", { name: "开始录音" }));
      expect(await screen.findByRole("button", { name: "取消录音" })).toBeInTheDocument();
      await waitFor(() => expect(merge).toBeDisabled());
      expect(screen.getByText(/结束或取消当前录音处理/)).toBeInTheDocument();
      expect(collectionApi.transcribeInspiration).not.toHaveBeenCalled();
      expect(window.sessionStorage.length).toBe(0);
      await user.click(screen.getByRole("button", { name: "取消录音" }));
      await waitFor(() => expect(merge).toBeEnabled());

      await user.click(screen.getByRole("button", { name: "开始录音" }));
      await user.click(await screen.findByRole("button", { name: "结束录音" }));
      expect(await screen.findByDisplayValue("待处理转写")).toBeInTheDocument();
      expect(merge).toBeDisabled();
      expect(screen.getByText(/追加、替换或丢弃独立转写草稿/)).toBeInTheDocument();
      expect(window.sessionStorage.length).toBe(0);
      await user.click(screen.getByRole("button", { name: "丢弃草稿" }));
      await waitFor(() => expect(merge).toBeEnabled());

      await user.click(merge);
      expect(await screen.findByRole("dialog", { name: "合并重复收藏" })).toBeInTheDocument();
      expect(window.location.pathname).toBe(`/materials/${savedItem.id}/merge`);
      expect(collectionApi.transcribeInspiration).toHaveBeenCalledOnce();
      expect(collectionApi.createPreview).toHaveBeenCalledOnce();
      expect(collectionApi.updateItem).not.toHaveBeenCalled();
      expect(stopTrack).toHaveBeenCalled();
    } finally {
      Object.defineProperty(window, "matchMedia", { configurable: true, value: originalMatchMedia });
    }
  });

  it("D02 刷新公开信息在途时阻断合并，完成后才冻结已接受 preview", async () => {
    const user = userEvent.setup();
    const originalMatchMedia = window.matchMedia;
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
    let resolveRefresh!: (value: CollectionPreview) => void;
    vi.mocked(collectionApi.createPreview)
      .mockResolvedValueOnce(preview)
      .mockReturnValueOnce(new Promise((resolve) => { resolveRefresh = resolve; }));
    vi.mocked(collectionApi.createItem).mockRejectedValueOnce(
      new CollectionApiError("素材已存在", "COLLECTION_EXISTS", 409, savedItem.id),
    );
    try {
      renderApp();
      await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
      await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
      await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
      await user.click(screen.getByRole("button", { name: "保存到素材库" }));
      const merge = await screen.findByRole("button", { name: "比较并合并" });

      await user.click(screen.getByRole("button", { name: "重新获取公开信息" }));
      await waitFor(() => expect(merge).toBeDisabled());
      expect(screen.getByText(/来源信息仍在更新/)).toBeInTheDocument();
      expect(window.sessionStorage.length).toBe(0);

      const refreshed = { ...preview, preview_id: "preview-2" };
      await act(async () => resolveRefresh(refreshed));
      await waitFor(() => expect(merge).toBeEnabled());
      await user.click(merge);

      expect(await screen.findByRole("dialog", { name: "合并重复收藏" })).toBeInTheDocument();
      expect(collectionApi.createPreview).toHaveBeenCalledTimes(2);
      expect(collectionApi.updateItem).not.toHaveBeenCalled();
    } finally {
      Object.defineProperty(window, "matchMedia", { configurable: true, value: originalMatchMedia });
    }
  });

  it("D02 duplicate 只绑定原 identity；刷新为新 identity 后清入口并重新 POST 判重", async () => {
    const user = userEvent.setup();
    const changedPreview: CollectionPreview = {
      ...preview,
      preview_id: "preview-new-identity",
      canonical_url: "https://example.com/another",
      identity_url: "https://example.com/another",
    };
    vi.mocked(collectionApi.createPreview)
      .mockResolvedValueOnce(preview)
      .mockResolvedValueOnce(changedPreview);
    vi.mocked(collectionApi.createItem)
      .mockRejectedValueOnce(new CollectionApiError("素材已存在", "COLLECTION_EXISTS", 409, savedItem.id))
      .mockResolvedValueOnce({ ...savedItem, ...changedPreview, revision: 1 });

    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));
    expect(await screen.findByRole("button", { name: "比较并合并" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "重新获取公开信息" }));
    expect(await screen.findByText("公开来源身份已变化，请重新保存以确认是否重复。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "比较并合并" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "打开已有项" })).not.toBeInTheDocument();
    const save = screen.getByRole("button", { name: "保存到素材库" });
    expect(save).toBeEnabled();
    expect(window.sessionStorage.length).toBe(0);
    expect(collectionApi.updateItem).not.toHaveBeenCalled();

    await user.click(save);
    await waitFor(() => expect(collectionApi.createItem).toHaveBeenCalledTimes(2));
    expect(vi.mocked(collectionApi.createItem).mock.calls[1][0].preview_id).toBe(changedPreview.preview_id);
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
  });

  it("M02 比较并合并进入 route-level 表面，返回放弃后恢复 duplicate 入口焦点", async () => {
    const user = userEvent.setup();
    const originalMatchMedia = window.matchMedia;
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn((query: string) => ({
        matches: query.includes("max-width"),
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });
    vi.mocked(collectionApi.createItem).mockRejectedValueOnce(
      new CollectionApiError("素材已存在", "COLLECTION_EXISTS", 409, savedItem.id),
    );
    try {
      renderApp();
      await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
      await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
      await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
      await user.type(screen.getByRole("textbox", { name: "个人标签" }), "手机返回焦点");
      await user.click(screen.getByRole("button", { name: "添加个人标签" }));
      await user.click(screen.getByRole("button", { name: "保存到素材库" }));
      await user.click(await screen.findByRole("button", { name: "比较并合并" }));

      const heading = await screen.findByRole("heading", { name: "合并重复收藏" });
      await waitFor(() => expect(heading).toHaveFocus());
      await user.click(screen.getByRole("button", { name: "返回保存前整理" }));
      await user.click(await screen.findByRole("button", { name: "放弃并退出" }));

      const merge = await screen.findByRole("button", { name: "比较并合并" });
      await waitFor(() => expect(merge).toHaveFocus());
      expect(window.sessionStorage.length).toBe(0);
      expect(collectionApi.createPreview).toHaveBeenCalledOnce();
      expect(collectionApi.transcribeInspiration).not.toHaveBeenCalled();
    } finally {
      Object.defineProperty(window, "matchMedia", { configurable: true, value: originalMatchMedia });
    }
  });

  it("merge 成功在视觉退出计时前 Back 也进入权威详情，并同步清理 duplicate review", async () => {
    const user = userEvent.setup();
    const updatedItem: CollectionItem = {
      ...savedItem,
      personal_tags: ["成功标签"],
      revision: 2,
      updated_at: "2026-08-29T11:02:00Z",
    };
    let resolveUpdate!: (value: CollectionItem) => void;
    vi.mocked(collectionApi.createItem).mockRejectedValueOnce(
      new CollectionApiError("素材已存在", "COLLECTION_EXISTS", 409, savedItem.id),
    );
    vi.mocked(collectionApi.updateItem).mockReturnValueOnce(new Promise((resolve) => { resolveUpdate = resolve; }));
    renderApp();

    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    const reviewRoute = structuredClone(window.history.state.collectionRoute);
    await user.type(screen.getByRole("textbox", { name: "个人标签" }), "成功标签");
    await user.click(screen.getByRole("button", { name: "添加个人标签" }));
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));
    await user.click(await screen.findByRole("button", { name: "比较并合并" }));
    await screen.findByRole("heading", { name: "合并重复收藏" });
    await user.click(await screen.findByRole("button", { name: "确认合并" }));
    await waitFor(() => expect(collectionApi.updateItem).toHaveBeenCalledOnce());
    const listCallsBeforeCommit = vi.mocked(collectionApi.listItems).mock.calls.length;

    vi.useFakeTimers();
    try {
      await act(async () => {
        resolveUpdate(updatedItem);
        await Promise.resolve();
      });
      expect(window.location.pathname).toBe("/materials/collection-1/merge");
      expect(window.sessionStorage.length).toBe(0);
      expect(document.querySelector(".review-page")).not.toBeInTheDocument();
      expect(collectionApi.listItems).toHaveBeenCalledTimes(listCallsBeforeCommit + 1);

      act(() => window.dispatchEvent(new PopStateEvent("popstate", { state: { collectionRoute: reviewRoute } })));
      expect(window.location.pathname).toBe("/materials/collection-1");
      expect(screen.getByRole("heading", { name: "东京咖啡馆空间与光线" })).toBeInTheDocument();
      act(() => vi.advanceTimersByTime(200));
      expect(window.location.pathname).toBe("/materials/collection-1");
      expect(document.querySelector(".review-page")).not.toBeInTheDocument();
      expect(collectionApi.listItems).toHaveBeenCalledTimes(listCallsBeforeCommit + 1);
      expect(collectionApi.updateItem).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });

  it("merge item_missing 确认放弃会失效 duplicate review，Back 不再复活旧判重会话", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionApi.createItem).mockRejectedValueOnce(
      new CollectionApiError("素材已存在", "COLLECTION_EXISTS", 409, savedItem.id),
    );
    vi.mocked(collectionApi.getItem).mockRejectedValueOnce(
      new CollectionApiError("素材不存在", "NOT_FOUND", 404),
    );
    renderApp();

    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    const reviewRoute = structuredClone(window.history.state.collectionRoute);
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));
    await user.click(await screen.findByRole("button", { name: "比较并合并" }));

    expect(await screen.findByRole("heading", { name: "素材不存在" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "返回素材库" }));
    expect(await screen.findByRole("dialog", { name: "放弃未保存的内容吗？" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "放弃并退出" }));

    expect(window.location.pathname).toBe("/");
    expect(window.sessionStorage.length).toBe(0);
    expect(document.querySelector(".review-page")).not.toBeInTheDocument();
    act(() => window.dispatchEvent(new PopStateEvent("popstate", { state: { collectionRoute: reviewRoute } })));
    expect(window.location.pathname).toBe("/");
    expect(document.querySelector(".review-page")).not.toBeInTheDocument();
    expect(collectionApi.getItem).toHaveBeenCalledOnce();
    expect(collectionApi.updateItem).not.toHaveBeenCalled();
  });

  it.each(["ready", "queued", "running", "completed", "limited", "failed", "timed_out", "unavailable"] as const)(
    "COLLECT-ONLY1: %s 历史解析不影响核心详情，也不触发深度请求",
    async (state) => {
      window.history.replaceState({}, "", "/materials/collection-1");
      vi.mocked(collectionApi.getDeepAnalysis).mockResolvedValue({ ...unavailableDeepAnalysis, state });
      renderApp();
      const title = await screen.findByRole("heading", { name: savedItem.display_title });
      await waitFor(() => expect(title).toHaveFocus());
      expect(screen.getByRole("button", { name: "编辑收藏" })).toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "我的灵感" })).toBeInTheDocument();
      expect(screen.queryByText(/深度解析|查看完整结果|查看有限结果/)).not.toBeInTheDocument();
      expect(document.querySelector(".deep-analysis-panel")).toBeNull();
      expect(collectionApi.getDeepAnalysis).not.toHaveBeenCalled();
      expect(collectionApi.startDeepAnalysis).not.toHaveBeenCalled();
      expect(collectionApi.retryDeepAnalysis).not.toHaveBeenCalled();
    },
  );

  it("COLLECT-ONLY1: 加载和编辑背景均没有解析占位或请求", async () => {
    const user = userEvent.setup();
    let resolveItem!: (item: CollectionItem) => void;
    vi.mocked(collectionApi.getItem).mockImplementationOnce(() => new Promise((resolve) => { resolveItem = resolve; }));
    window.history.replaceState({}, "", "/materials/collection-1");
    renderApp();
    expect(screen.getByText(/正在从统一后端读取/)).toBeInTheDocument();
    expect(document.querySelector(".deep-analysis-panel")).toBeNull();
    await act(async () => resolveItem({ ...savedItem, inspiration: null }));
    expect(await screen.findByText("还没有留下个人灵感。来源文案不会代替你的想法。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "编辑收藏" }));
    expect(document.querySelector(".is-edit-merge-background")).toBeInTheDocument();
    expect(document.querySelector(".deep-analysis-panel")).toBeNull();
    expect(document.body.textContent).not.toContain("深度解析");
    expect(collectionApi.getDeepAnalysis).not.toHaveBeenCalled();
  });

  it.each([
    ["/?mode=deep-analysis", null, false],
    ["/materials/collection-1?mode=deep-analysis", null, true],
    ["/materials/collection-1/deep-analysis", null, true],
    ["/materials/collection-1/deep-analysis/", { name: "reader", materialId: "wrong-material", resultId: "old-result" }, true],
    ["/materials/collection-1", { name: "reader", materialId: "collection-1", resultId: "old-result" }, true],
  ] as const)("COLLECT-ONLY1: 旧入口 %s 只进入收藏网站", async (url, reader, detail) => {
    window.history.replaceState({ collectionRoute: reader }, "", url);
    render(<PublicApp />);
    await screen.findByRole("heading", { name: detail ? savedItem.display_title : /把看到的/ });
    expect(window.location.pathname).toBe(detail ? "/materials/collection-1" : "/");
    expect(window.location.search).toBe("");
    expect(screen.queryByText(/深度解析|已打开深度结果/)).not.toBeInTheDocument();
    expect(collectionApi.getDeepAnalysis).not.toHaveBeenCalled();
    expect(collectionApi.startDeepAnalysis).not.toHaveBeenCalled();
    expect(collectionApi.retryDeepAnalysis).not.toHaveBeenCalled();
  });

  it("COLLECT-ONLY1: 前进后退遇到旧 reader 状态时归一为对应详情", async () => {
    renderApp();
    await screen.findByRole("heading", { name: /把看到的/ });
    const libraryRoute = window.history.state.collectionRoute;
    const reader = { name: "reader", materialId: savedItem.id, resultId: "old-result", returnToken: 9 };
    for (let visit = 0; visit < 2; visit += 1) {
      act(() => {
        window.history.pushState({ collectionRoute: reader }, "", "/materials/collection-1/deep-analysis");
        window.dispatchEvent(new PopStateEvent("popstate", { state: { collectionRoute: reader } }));
      });
      await screen.findByRole("heading", { name: savedItem.display_title });
      expect(window.history.state.collectionRoute).toMatchObject({ name: "detail", id: savedItem.id });
      expect(window.location.pathname).toBe("/materials/collection-1");
      act(() => {
        window.history.replaceState({ collectionRoute: libraryRoute }, "", "/");
        window.dispatchEvent(new PopStateEvent("popstate", { state: { collectionRoute: libraryRoute } }));
      });
      await screen.findByRole("heading", { name: /把看到的/ });
    }
    expect(collectionApi.getDeepAnalysis).not.toHaveBeenCalled();
    expect(collectionApi.startDeepAnalysis).not.toHaveBeenCalled();
    expect(collectionApi.retryDeepAnalysis).not.toHaveBeenCalled();
  });

  it("COLLECT-ONLY1: 无法解码的旧解析链接安全回收藏库", async () => {
    window.history.replaceState({}, "", "/materials/%E0%A4%A/deep-analysis");
    renderApp();
    await screen.findByRole("heading", { name: /把看到的/ });
    expect(window.location.pathname).toBe("/");
    expect(collectionApi.getItem).not.toHaveBeenCalled();
    expect(collectionApi.getDeepAnalysis).not.toHaveBeenCalled();
  });

  it("来源弹窗被浏览器阻止时就地报告失败", async () => {
    const user = userEvent.setup();
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    vi.mocked(collectionApi.listItems).mockResolvedValue({
      items: [{
        id: savedItem.id,
        display_title: savedItem.display_title,
        platform: savedItem.platform,
        primary_category: savedItem.organization_confirmation.primary_category,
        cover_url: savedItem.metadata.cover_url.value,
        created_at: savedItem.created_at,
        updated_at: savedItem.updated_at,
      }],
      total: 1,
      limit: 24,
      next_cursor: null,
      facets: { platforms: [], categories: [], tags: [] },
    });
    renderApp();

    await user.click(await screen.findByRole("button", { name: "打开素材：东京咖啡馆空间与光线" }));
    await user.click(await screen.findByRole("button", { name: "访问example.com" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("浏览器阻止了来源页面");
    expect(open).toHaveBeenCalledWith("", "_blank");
    open.mockRestore();
  });

  it("来源窗口创建成功时只反馈已请求打开", async () => {
    const user = userEvent.setup();
    const opened = { opener: {}, location: { href: "" } } as unknown as Window;
    const open = vi.spyOn(window, "open").mockReturnValue(opened);
    vi.mocked(collectionApi.listItems).mockResolvedValue({
      items: [{
        id: savedItem.id,
        display_title: savedItem.display_title,
        platform: savedItem.platform,
        primary_category: savedItem.organization_confirmation.primary_category,
        cover_url: savedItem.metadata.cover_url.value,
        created_at: savedItem.created_at,
        updated_at: savedItem.updated_at,
      }],
      total: 1,
      limit: 24,
      next_cursor: null,
      facets: { platforms: [], categories: [], tags: [] },
    });
    renderApp();

    await user.click(await screen.findByRole("button", { name: "打开素材：东京咖啡馆空间与光线" }));
    await user.click(await screen.findByRole("button", { name: "访问example.com" }));

    expect(await screen.findByRole("status")).toHaveTextContent("已请求在隔离的新标签页打开来源");
    expect(opened.opener).toBeNull();
    expect(opened.location.href).toBe(savedItem.canonical_url);
    open.mockRestore();
  });

  it("详情设置返回令牌绑定素材身份且只消费一次", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionApi.listItems).mockResolvedValue({
      items: [{
        id: savedItem.id,
        display_title: savedItem.display_title,
        platform: savedItem.platform,
        primary_category: savedItem.organization_confirmation.primary_category,
        cover_url: savedItem.metadata.cover_url.value,
        created_at: savedItem.created_at,
        updated_at: savedItem.updated_at,
      }],
      total: 1,
      limit: 24,
      next_cursor: null,
      facets: { platforms: [], categories: [], tags: [] },
    });
    renderApp();

    await user.click(await screen.findByRole("button", { name: "打开素材：东京咖啡馆空间与光线" }));
    await screen.findByRole("heading", { name: savedItem.display_title });
    const sourceRoute = window.history.state.collectionRoute;
    await user.click(screen.getByRole("button", { name: "个人偏好" }));
    await user.click(await screen.findByRole("menuitem", { name: "隐私与存储设置" }));
    expect(await screen.findByRole("heading", { name: "隐私与存储设置" })).toBeInTheDocument();
    const settingsRoute = window.history.state.collectionRoute;
    expect(settingsRoute).toMatchObject({
      name: "settings",
      from: "detail",
      materialId: savedItem.id,
      coreRevision: savedItem.updated_at,
      returnToken: expect.any(Number),
    });

    window.dispatchEvent(new PopStateEvent("popstate", { state: { collectionRoute: sourceRoute } }));
    await screen.findByRole("heading", { name: savedItem.display_title });
    await waitFor(() => expect(screen.getByRole("button", { name: "个人偏好" })).toHaveFocus());

    window.dispatchEvent(new PopStateEvent("popstate", { state: { collectionRoute: settingsRoute } }));
    expect(await screen.findByRole("heading", { name: /把看到的/ })).toHaveFocus();
  });

  it("详情设置返回令牌缺失时安全回到素材库", async () => {
    window.history.replaceState({ collectionRoute: { name: "settings", from: "detail" } }, "", "/settings/privacy-storage");
    renderApp();
    expect(await screen.findByRole("heading", { name: "隐私与存储设置" })).toBeInTheDocument();

    window.dispatchEvent(new PopStateEvent("popstate", { state: { collectionRoute: { name: "detail", id: savedItem.id } } }));

    expect(await screen.findByRole("heading", { name: /把看到的/ })).toHaveFocus();
    expect(collectionApi.getItem).not.toHaveBeenCalled();
  });

  it("组合偏好损坏时忽略旧键并整套恢复安全默认", async () => {
    const user = userEvent.setup();
    window.localStorage.setItem("video-knowledge-theme", "dark");
    window.localStorage.setItem("video-knowledge-accent", "orange");
    const invalidCombination = JSON.stringify({ appearance: "dark" });
    window.localStorage.setItem("collection-appearance-v1", invalidCombination);
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    window.history.replaceState({ collectionRoute: { name: "settings", from: "library" } }, "", "/settings/privacy-storage");
    renderApp();

    const safeDefault = await screen.findByRole("radio", { name: "浅色" });
    expect(safeDefault).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "靛蓝" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("status")).toHaveTextContent("当前浏览器的外观偏好损坏、不完整");
    expect(screen.getByRole("alert")).toBeEmptyDOMElement();
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(document.documentElement.dataset.accent).toBe("indigo");
    await user.click(safeDefault);
    expect(setItem).not.toHaveBeenCalled();
    expect(window.localStorage.getItem("collection-appearance-v1")).toBe(invalidCombination);
    expect(window.localStorage.getItem("video-knowledge-theme")).toBe("dark");
    expect(window.localStorage.getItem("video-knowledge-accent")).toBe("orange");
    setItem.mockRestore();
  });

  it("手机偏好单选组使用 roving tabindex 与方向键循环选择", async () => {
    const user = userEvent.setup();
    const originalMatchMedia = window.matchMedia;
    const matchMedia = vi.fn((query: string) => ({
      matches: query.includes("max-width"),
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    Object.defineProperty(window, "matchMedia", { configurable: true, value: matchMedia });
    renderApp();

    await user.click(await screen.findByRole("button", { name: "个人偏好" }));
    const group = await screen.findByRole("radiogroup", { name: "主题" });
    const light = within(group).getByRole("radio", { name: "浅色" });
    const dark = within(group).getByRole("radio", { name: "深色" });
    expect(light).toHaveAttribute("tabindex", "0");
    expect(dark).toHaveAttribute("tabindex", "-1");

    light.focus();
    await user.keyboard("{ArrowRight}");
    expect(dark).toHaveFocus();
    expect(dark).toHaveAttribute("aria-checked", "true");
    expect(dark).toHaveAttribute("tabindex", "0");
    expect(light).toHaveAttribute("tabindex", "-1");
    Object.defineProperty(window, "matchMedia", { configurable: true, value: originalMatchMedia });
  });

  it("详情显式返回消费当前历史项，浏览器后退也恢复素材库滚动和原卡片焦点", async () => {
    const user = userEvent.setup();
    const pushState = vi.spyOn(window.history, "pushState");
    const replaceState = vi.spyOn(window.history, "replaceState");
    Object.defineProperty(window, "scrollY", { configurable: true, value: 420 });
    vi.mocked(collectionApi.listItems).mockResolvedValue({
      items: [{
        id: savedItem.id,
        display_title: savedItem.display_title,
        platform: savedItem.platform,
        primary_category: savedItem.organization_confirmation.primary_category,
        cover_url: savedItem.metadata.cover_url.value,
        created_at: savedItem.created_at,
        updated_at: savedItem.updated_at,
      }],
      total: 1,
      limit: 24,
      next_cursor: null,
      facets: { platforms: [], categories: [], tags: [] },
    });
    renderApp();

    const card = await screen.findByRole("button", { name: "打开素材：东京咖啡馆空间与光线" });
    await user.click(card);
    await screen.findByRole("heading", { name: "我的灵感" });
    const pushesAfterOpen = pushState.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "返回素材库" }));

    const restored = await screen.findByRole("button", { name: "打开素材：东京咖啡馆空间与光线" });
    expect(pushState).toHaveBeenCalledTimes(pushesAfterOpen);
    expect(replaceState).toHaveBeenLastCalledWith(
      { collectionRoute: { name: "library", entryId: expect.any(String) } },
      "",
      "/",
    );
    await waitFor(() => expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 420, behavior: "auto" }));
    expect(restored).toHaveFocus();

    await user.click(restored);
    await screen.findByRole("heading", { name: "我的灵感" });
    window.dispatchEvent(new PopStateEvent("popstate", { state: { collectionRoute: { name: "library" } } }));
    expect(await screen.findByRole("button", { name: "打开素材：东京咖啡馆空间与光线" })).toHaveFocus();
    await waitFor(() => expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 420, behavior: "auto" }));
  });

  it("250ms 防抖后把关键词交给服务端，清空时立即恢复全量查询", async () => {
    const user = userEvent.setup();
    renderApp();
    await waitFor(() => expect(collectionApi.listItems).toHaveBeenCalledTimes(1));
    const search = screen.getByPlaceholderText("搜索素材和我的灵感");

    await user.type(search, "光影");
    expect(collectionApi.listItems).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(collectionApi.listItems).toHaveBeenLastCalledWith(
      expect.objectContaining({ query: "光影" }),
      expect.any(AbortSignal),
    ), { timeout: 1000 });

    await user.clear(search);
    await waitFor(() => expect(collectionApi.listItems).toHaveBeenLastCalledWith(
      expect.objectContaining({ query: undefined }),
      expect.any(AbortSignal),
    ));
  });

  it("桌面筛选取消时丢弃草稿，应用时一次提交平台、二级分类和来源标签", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionApi.listItems).mockResolvedValue({
      items: [], total: 0, limit: 24, next_cursor: null,
      facets: {
        platforms: [{ platform: "youtube", count: 2 }],
        categories: [{ primary_category: "设计", count: 2, children: [{ secondary_category: "动效", count: 2 }] }],
        tags: [{ name: "项目参考", source: "personal", count: 2 }],
      },
    });
    renderApp();
    await screen.findByRole("button", { name: "设计" });
    expect(collectionApi.listItems).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "筛选" }));
    await user.click(await screen.findByRole("radio", { name: "YouTube" }));
    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(collectionApi.listItems).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "筛选" }));
    await user.click(await screen.findByRole("radio", { name: "YouTube" }));
    await user.click(screen.getByRole("radio", { name: "动效" }));
    await user.click(screen.getByRole("radio", { name: /项目参考.*个人/ }));
    await user.click(screen.getByRole("button", { name: "应用筛选" }));
    await waitFor(() => expect(collectionApi.listItems).toHaveBeenLastCalledWith(
      expect.objectContaining({ platform: "youtube", secondaryCategory: "动效", tag: "项目参考", tagSource: "personal" }),
      expect.any(AbortSignal),
    ));

    await user.click(screen.getByRole("button", { name: "筛选" }));
    await user.click(screen.getByRole("button", { name: "清除筛选" }));
    await user.click(screen.getByRole("button", { name: "应用筛选" }));
    await waitFor(() => expect(collectionApi.listItems).toHaveBeenLastCalledWith(
      expect.objectContaining({ platform: undefined, secondaryCategory: undefined, tag: undefined, tagSource: undefined }),
      expect.any(AbortSignal),
    ));
  });

  it("编辑捕获输入时不请求网络，多链接显式提交后保留原文并显示行内错误", async () => {
    const user = userEvent.setup();
    renderApp();
    const input = screen.getByLabelText("粘贴链接或分享文本");

    await user.type(input, "https://example.com/a https://example.com/b");
    expect(collectionApi.createPreview).not.toHaveBeenCalled();
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);

    const errors = await screen.findAllByRole("alert");
    expect(errors[0]).toHaveTextContent("一次只能收一条");
    expect(input).toHaveValue("https://example.com/a https://example.com/b");
    expect(errors[0]).toHaveFocus();
    expect(collectionApi.createPreview).not.toHaveBeenCalled();
  });

  it("单一公开链接立即进入全页整理，再渐进发布来源与建议", async () => {
    const user = userEvent.setup();
    renderApp();
    const input = screen.getByLabelText("粘贴链接或分享文本");
    await user.type(input, "分享 https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);

    expect(screen.getByRole("heading", { name: "保存前整理" })).toBeInTheDocument();
    expect(collectionApi.createPreview).toHaveBeenCalledWith(
      "分享 https://example.com/cafe",
      false,
      expect.any(AbortSignal),
    );
    expect(await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" })).toBeInTheDocument();
    expect(screen.getByDisplayValue("设计")).toBeInTheDocument();
    expect(screen.getByText("阳光透过百叶窗洒进来，空间安静而温柔。")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "我的灵感" })).toBeInTheDocument();
  });

  it("预览封面只使用身份端点，解码失败单次回退并在刷新身份变化后重试", async () => {
    const user = userEvent.setup();
    const refreshedPreview: CollectionPreview = {
      ...preview,
      preview_id: "preview-2",
      metadata: {
        ...preview.metadata,
        cover_url: {
          ...preview.metadata.cover_url,
          value: "https://images.example/cafe-refreshed.jpg",
        },
      },
    };
    vi.mocked(collectionApi.createPreview)
      .mockReset()
      .mockResolvedValueOnce(preview)
      .mockResolvedValueOnce(refreshedPreview);
    renderApp();

    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });

    let cover = document.querySelector<HTMLImageElement>(".review-source .material-cover img");
    expect(cover).toHaveAttribute("src", "/api/v1/collection-previews/preview-1/cover");
    expect(Array.from(document.querySelectorAll("img"), (image) => image.getAttribute("src")))
      .not.toContain(preview.metadata.cover_url.value);

    fireEvent.error(cover!);
    await waitFor(() => {
      cover = document.querySelector<HTMLImageElement>(".review-source .material-cover img");
      expect(cover).toHaveAttribute("src", "/assets/material-cover-fallback.webp");
    });
    fireEvent.error(cover!);
    expect(cover).toHaveAttribute("src", "/assets/material-cover-fallback.webp");

    const inspiration = screen.getByPlaceholderText(/我喜欢这种自然光/);
    await user.type(inspiration, "刷新时保留这段灵感");
    await user.click(screen.getByRole("button", { name: "重新获取公开信息" }));

    await waitFor(() => {
      cover = document.querySelector<HTMLImageElement>(".review-source .material-cover img");
      expect(cover).toHaveAttribute("src", "/api/v1/collection-previews/preview-2/cover");
    });
    expect(collectionApi.createPreview).toHaveBeenNthCalledWith(
      2,
      "https://example.com/cafe",
      true,
      expect.any(AbortSignal),
    );
    expect(inspiration).toHaveValue("刷新时保留这段灵感");
  });

  it("权威封面为空时直接使用正式 fallback，fallback 失败不循环", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionApi.createPreview).mockResolvedValue({
      ...preview,
      metadata: {
        ...preview.metadata,
        cover_url: { ...preview.metadata.cover_url, value: "" },
      },
    });
    renderApp();

    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });

    const cover = document.querySelector<HTMLImageElement>(".review-source .material-cover img");
    expect(cover).toHaveAttribute("src", "/assets/material-cover-fallback.webp");
    fireEvent.error(cover!);
    expect(cover).toHaveAttribute("src", "/assets/material-cover-fallback.webp");
  });

  it("显式元信息刷新失败时保留旧封面身份与当前草稿", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionApi.createPreview)
      .mockReset()
      .mockResolvedValueOnce(preview)
      .mockRejectedValueOnce(new Error("公开信息刷新失败"));
    renderApp();

    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    const inspiration = screen.getByPlaceholderText(/我喜欢这种自然光/);
    await user.type(inspiration, "失败也不能丢失");
    await user.click(screen.getByRole("button", { name: "重新获取公开信息" }));

    expect(await screen.findByText("公开信息刷新失败")).toBeInTheDocument();
    expect(document.querySelector(".review-source .material-cover img")).toHaveAttribute(
      "src",
      "/api/v1/collection-previews/preview-1/cover",
    );
    expect(inspiration).toHaveValue("失败也不能丢失");
  });

  it("来源请求未完成时立即展示精确原链接与可编辑灵感，并解释保存阻断", async () => {
    const user = userEvent.setup();
    let resolvePreview!: (value: CollectionPreview) => void;
    vi.mocked(collectionApi.createPreview).mockReturnValue(new Promise((resolve) => { resolvePreview = resolve; }));
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "分享 https://example.com/cafe。");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);

    expect(screen.getByRole("link", { name: "https://example.com/cafe" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/我喜欢这种自然光/)).toBeEnabled();
    expect(screen.getByRole("button", { name: "保存到素材库" })).toBeDisabled();
    expect(screen.getByText(/来源信息仍在更新/)).toBeInTheDocument();

    resolvePreview(preview);
    expect(await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" })).toBeInTheDocument();
  });

  it("个人标签按 NFKC 与空白规则添加、判重，未提交草稿会阻断保存", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    const tagInput = screen.getByRole("textbox", { name: "个人标签" });

    await user.type(tagInput, "Ｆｏｏ   Bar{Enter}");
    expect(screen.getByRole("button", { name: "删除个人标签：Foo Bar" })).toBeInTheDocument();
    await user.type(tagInput, "foo bar{Enter}");
    expect(await screen.findByText(/已经在个人标签中/)).toBeInTheDocument();
    await user.clear(tagInput);
    await user.type(tagInput, "尚未添加");
    expect(screen.getByRole("button", { name: "保存到素材库" })).toBeDisabled();
    expect(screen.getByText(/先添加或清空尚未提交/)).toBeInTheDocument();
  });

  it("保存冻结整理与灵感 payload，成功后返回素材库并聚焦新卡片", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionApi.listItems)
      .mockResolvedValueOnce({ items: [], total: 0, limit: 24, next_cursor: null, facets: { platforms: [], categories: [], tags: [] } })
      .mockResolvedValue({
        items: [{
          id: savedItem.id,
          display_title: savedItem.display_title,
          platform: savedItem.platform,
          primary_category: savedItem.organization_confirmation.primary_category,
          cover_url: savedItem.metadata.cover_url.value,
          created_at: savedItem.created_at,
          updated_at: savedItem.updated_at,
        }],
        total: 1,
        limit: 24,
        next_cursor: null,
        facets: { platforms: [{ platform: "web", count: 1 }], categories: [{ primary_category: "设计", count: 1, children: [{ secondary_category: "空间", count: 1 }] }], tags: [] },
      });
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "分享 https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    await user.type(screen.getByPlaceholderText(/我喜欢这种自然光/), "想在书房试试这种光线。");
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));

    await waitFor(() => expect(collectionApi.createItem).toHaveBeenCalledOnce());
    const [payload, key] = vi.mocked(collectionApi.createItem).mock.calls[0];
    expect(key).toEqual(expect.any(String));
    expect(payload).toMatchObject({
      preview_id: "preview-1",
      organization_confirmation: {
        primary_category: "设计",
        secondary_category: "空间",
        organization_tags: ["自然光", "日式风格"],
      },
      inspiration: {
        content: "想在书房试试这种光线。",
        input_mode: "text",
      },
    });
    const card = await screen.findByRole("button", { name: "打开素材：东京咖啡馆空间与光线" });
    expect(card).toHaveFocus();
    expect(screen.getByLabelText("素材库")).toContainElement(card);
  });

  it("保存成功后的权威刷新失败时保留旧列表、滚动与可打开的成功提示", async () => {
    const user = userEvent.setup();
    Object.defineProperty(window, "scrollY", { configurable: true, value: 360 });
    vi.mocked(collectionApi.listItems)
      .mockResolvedValueOnce({
        items: [{
          id: "collection-old",
          display_title: "旧素材",
          platform: "web",
          primary_category: "设计",
          cover_url: "",
          created_at: "2026-08-20T00:00:00Z",
          updated_at: "2026-08-20T00:00:00Z",
        }],
        total: 1,
        limit: 24,
        next_cursor: null,
        facets: { platforms: [], categories: [], tags: [] },
      })
      .mockRejectedValueOnce(new CollectionApiError("列表网络失败", "NETWORK_ERROR", 0));
    renderApp();
    await screen.findByRole("button", { name: "打开素材：旧素材" });
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));

    expect(await screen.findByRole("button", { name: "打开素材：旧素材" })).toBeInTheDocument();
    expect(await screen.findByText(/已保存；素材库刷新暂未完成/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "按原条件重试" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "打开已保存素材" })).toBeInTheDocument();
    await waitFor(() => {
      expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 360, behavior: "auto" });
    });
  });

  it("结果未知后的显式重试复用冻结 payload 与同一 Idempotency-Key", async () => {
    const user = userEvent.setup();
    vi.mocked(collectionApi.createItem)
      .mockRejectedValueOnce(new CollectionApiError("网络暂时中断", "NETWORK_ERROR", 0))
      .mockResolvedValueOnce(savedItem);
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    await user.type(screen.getByPlaceholderText(/我喜欢这种自然光/), "冻结草稿");
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));
    const retry = await screen.findByRole("button", { name: "重试保存" });
    expect(screen.getByRole("button", { name: "保存到素材库" })).toBeDisabled();
    await user.click(retry);

    await waitFor(() => expect(collectionApi.createItem).toHaveBeenCalledTimes(2));
    expect(vi.mocked(collectionApi.createItem).mock.calls[1]).toEqual(vi.mocked(collectionApi.createItem).mock.calls[0]);
  });

  it.each([
    ["narrow touch desktop", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140.0"],
    ["iPad", "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) Mobile/15E148"],
    ["Android tablet", "Mozilla/5.0 (Linux; Android 14; Tablet) Safari/537.36"],
    ["unknown", "unknown browser"],
  ])("HOLD-VOICE1 %s has no mounted or focusable recording entry even at phone width", async (_device, userAgent) => {
    const user = userEvent.setup();
    const { getUserMedia } = mockPhoneRecorder();
    Object.defineProperty(navigator, "userAgent", { configurable: true, value: userAgent });
    Object.defineProperty(navigator, "maxTouchPoints", { configurable: true, value: 5 });
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    expect(document.querySelector(".voice-capture")).toBeNull();
    expect(screen.queryByRole("button", { name: /录音|按住说话/ })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "我的灵感" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "保存到素材库" })).toBeEnabled();
    expect(getUserMedia).not.toHaveBeenCalled();
  });

  it("HOLD-VOICE1 old edited draft and main input survive failed/cancelled re-recording and viewport changes", async () => {
    const user = userEvent.setup(); mockPhoneRecorder();
    vi.mocked(collectionApi.transcribeInspiration)
      .mockResolvedValueOnce({ content: "第一份草稿", input_mode: "voice", transcription_status: "draft" })
      .mockRejectedValueOnce(new Error("转写暂时失败，请重新录制。"))
      .mockResolvedValueOnce({ content: "新的有效草稿", input_mode: "voice", transcription_status: "draft" });
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    const original = screen.getByRole("textbox", { name: "我的灵感" });
    await user.type(original, "原有文字"); await prepareTapVoice(user);
    await user.click(screen.getByRole("button", { name: "开始录音" }));
    await user.click(await screen.findByRole("button", { name: "结束录音" }));
    const draft = await screen.findByLabelText("独立转写草稿");
    await user.clear(draft);
    expect(screen.getByLabelText("独立转写草稿")).toBe(draft);
    expect(screen.getByRole("button", { name: "追加到灵感" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "替换现有内容" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "保存到素材库" })).toBeDisabled();
    await user.type(draft, "编辑过的草稿");
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 844 }); fireEvent(window, new Event("resize"));
    expect(screen.getByRole("textbox", { name: "我的灵感" })).toBe(original);
    expect(screen.getByLabelText("独立转写草稿")).toBe(draft);
    expect(draft).toHaveValue("编辑过的草稿");
    await user.click(screen.getByRole("button", { name: "开始录音" }));
    await user.click(await screen.findByRole("button", { name: "结束录音" }));
    await screen.findByText("转写暂时失败，请重新录制。");
    expect(original).toHaveValue("原有文字"); expect(draft).toHaveValue("编辑过的草稿");
    await user.click(screen.getByRole("button", { name: "开始录音" }));
    await user.click(await screen.findByRole("button", { name: "取消录音" }));
    expect(draft).toHaveValue("编辑过的草稿"); expect(screen.getByRole("button", { name: "保存到素材库" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "开始录音" }));
    await user.click(await screen.findByRole("button", { name: "结束录音" }));
    await screen.findByDisplayValue("新的有效草稿"); expect(original).toHaveValue("原有文字");
    const focus = vi.spyOn(original, "focus");
    await user.click(screen.getByRole("button", { name: "追加到灵感" }));
    expect(original).toHaveValue("原有文字\n新的有效草稿"); expect(focus).toHaveBeenCalledWith({ preventScroll: true });
    expect(screen.queryByLabelText("独立转写草稿")).not.toBeInTheDocument();
    expect(screen.queryByText(/转写草稿已就绪/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存到素材库" })).toBeEnabled();
    expect(collectionApi.transcribeInspiration).toHaveBeenCalledTimes(3);
  });

  it("HOLD-VOICE1 cancelling pending permission immediately restores text saving and disposes a late stream", async () => {
    const user = userEvent.setup(); const { getUserMedia, stopTrack } = mockPhoneRecorder();
    let resolve!: (value: unknown) => void;
    getUserMedia.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    await user.type(screen.getByRole("textbox", { name: "我的灵感" }), "仅保留文字");
    await user.click(screen.getByRole("button", { name: "点按录音" }));
    await user.click(screen.getByRole("button", { name: "开始录音" }));
    expect(screen.getByRole("button", { name: "保存到素材库" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "取消录音" }));
    expect(screen.getByRole("button", { name: "保存到素材库" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));
    await waitFor(() => expect(collectionApi.createItem).toHaveBeenCalledOnce());
    await act(async () => resolve({ getTracks: () => [{ stop: stopTrack }] }));
    expect(stopTrack).toHaveBeenCalledOnce(); expect(collectionApi.transcribeInspiration).not.toHaveBeenCalled();
    expect(vi.mocked(collectionApi.createItem).mock.calls[0][0].inspiration?.content).toBe("仅保留文字");
  });

  it("麦克风权限拒绝后保留文字灵感并保持保存可用", async () => {
    const user = userEvent.setup();
    usePhoneDevice();
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    Object.defineProperty(globalThis, "MediaRecorder", {
      configurable: true,
      value: class {},
    });
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    const inspiration = screen.getByRole("textbox", { name: "我的灵感" });
    await user.type(inspiration, "文字灵感不会丢失");
    await user.click(screen.getByRole("button", { name: "点按录音" }));
    await user.click(screen.getByRole("button", { name: "开始录音" }));

    expect(await screen.findByText(/麦克风权限未开放/)).toBeInTheDocument();
    expect(inspiration).toHaveValue("文字灵感不会丢失");
    expect(screen.getByRole("button", { name: "保存到素材库" })).toBeEnabled();
  });

  it("录音器构造失败也关闭已获麦克风且不上传", async () => {
    const user = userEvent.setup();
    usePhoneDevice();
    const stopTrack = vi.fn();
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: {
      getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: stopTrack }] }),
    } });
    Object.defineProperty(globalThis, "MediaRecorder", { configurable: true,
      value: class { constructor() { throw new DOMException("unsupported", "NotSupportedError"); } },
    });
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    await prepareTapVoice(user);
    await user.click(screen.getByRole("button", { name: "开始录音" }));
    await screen.findByText(/麦克风权限未开放/);
    expect(stopTrack).toHaveBeenCalledTimes(2);
    expect(collectionApi.transcribeInspiration).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "保存到素材库" })).toBeEnabled();
  });

  it("ASR-ERR1 显示后端安全错误码并保留文字保存，不自动重试录音", async () => {
    const user = userEvent.setup();
    usePhoneDevice();
    const stopTrack = vi.fn();
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: {
      getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: stopTrack }] }),
    } });
    class Recorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      start() { this.state = "recording"; }
      stop() { this.state = "inactive"; this.ondataavailable?.({ data: new Blob(["synthetic audio"]) } as BlobEvent); this.onstop?.(); }
    }
    Object.defineProperty(globalThis, "MediaRecorder", { configurable: true, value: Recorder });
    const message = "语音服务访问被拒，请维护者核对鉴权及识别资源权限；可先用文字记录。（HTTP 403，业务码 45000030）";
    vi.mocked(collectionApi.transcribeInspiration).mockRejectedValue(new Error(message));
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    await user.type(screen.getByRole("textbox", { name: "我的灵感" }), "保留我的文字灵感");
    await prepareTapVoice(user);
    await user.click(screen.getByRole("button", { name: "开始录音" }));
    await user.click(await screen.findByRole("button", { name: "结束录音" }));
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(stopTrack).toHaveBeenCalledTimes(2);
    expect(collectionApi.transcribeInspiration).toHaveBeenCalledOnce();
    expect(screen.getByRole("textbox", { name: "我的灵感" })).toHaveValue("保留我的文字灵感");
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));
    await waitFor(() => expect(collectionApi.createItem).toHaveBeenCalledOnce());
    expect(vi.mocked(collectionApi.createItem).mock.calls[0][0].inspiration?.content).toBe("保留我的文字灵感");
    expect(collectionApi.getDeepAnalysis).not.toHaveBeenCalled();
  });

  it("取消转写后立即重录，旧请求迟到不会停止新麦克风或覆盖草稿", async () => {
    const user = userEvent.setup();
    usePhoneDevice();
    const stops = [vi.fn(), vi.fn()];
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: {
      getUserMedia: vi.fn().mockResolvedValueOnce({ getTracks: () => [{ stop: vi.fn() }] }).mockResolvedValueOnce({ getTracks: () => [{ stop: stops[0] }] })
        .mockResolvedValueOnce({ getTracks: () => [{ stop: stops[1] }] }),
    } });
    class Recorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      start() { this.state = "recording"; }
      stop() { this.state = "inactive"; this.ondataavailable?.({ data: new Blob(["audio"]) } as BlobEvent); this.onstop?.(); }
    }
    Object.defineProperty(globalThis, "MediaRecorder", { configurable: true, value: Recorder });
    let resolveOld!: (value: { content: string; input_mode: "voice"; transcription_status: "draft" }) => void;
    vi.mocked(collectionApi.transcribeInspiration).mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce({ content: "新录音", input_mode: "voice", transcription_status: "draft" });
    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    await prepareTapVoice(user);
    await user.click(screen.getByRole("button", { name: "开始录音" }));
    await user.click(await screen.findByRole("button", { name: "结束录音" }));
    await user.click(await screen.findByRole("button", { name: "取消转写" }));
    await user.click(screen.getByRole("button", { name: "开始录音" }));
    await screen.findByRole("button", { name: "结束录音" });
    await act(async () => resolveOld({ content: "过期结果", input_mode: "voice", transcription_status: "draft" }));
    expect(stops[1]).not.toHaveBeenCalled();
    expect(screen.queryByDisplayValue("过期结果")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "结束录音" }));
    expect(await screen.findByDisplayValue("新录音")).toBeInTheDocument();
    expect(stops[1]).toHaveBeenCalledOnce();
  });

  it("用户录音转写只有明确替换后才进入 voice/completed 保存语义", async () => {
    const user = userEvent.setup();
    usePhoneDevice();
    const stopTrack = vi.fn();
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: stopTrack }] }) },
    });
    class FakeMediaRecorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(_stream: MediaStream) {}
      start() { this.state = "recording"; }
      stop() {
        this.state = "inactive";
        this.ondataavailable?.({ data: new Blob(["voice"]) } as BlobEvent);
        this.onstop?.();
      }
    }
    Object.defineProperty(globalThis, "MediaRecorder", { configurable: true, value: FakeMediaRecorder });
    vi.mocked(collectionApi.transcribeInspiration).mockResolvedValue({ content: "语音转写灵感", input_mode: "voice", transcription_status: "draft" });

    renderApp();
    await user.type(screen.getByLabelText("粘贴链接或分享文本"), "https://example.com/cafe");
    await user.click(screen.getAllByRole("button", { name: "收进来" })[0]);
    await screen.findByRole("heading", { name: "东京咖啡馆空间与光线" });
    await prepareTapVoice(user);
    await user.click(screen.getByRole("button", { name: "开始录音" }));
    await user.click(await screen.findByRole("button", { name: "结束录音" }));
    expect(await screen.findByDisplayValue("语音转写灵感")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存到素材库" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "替换现有内容" }));
    await user.click(screen.getByRole("button", { name: "保存到素材库" }));

    await waitFor(() => expect(collectionApi.createItem).toHaveBeenCalledOnce());
    expect(vi.mocked(collectionApi.createItem).mock.calls[0][0].inspiration).toEqual({
      content: "语音转写灵感",
      input_mode: "voice",
      transcription_status: "completed",
    });
    expect(stopTrack).toHaveBeenCalled();
  });
});
