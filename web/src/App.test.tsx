import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Theme } from "@radix-ui/themes";
import { beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    apiClient: {
      listVideos: vi.fn().mockRejectedValue(new Error("test offline")),
      previewResolution: vi.fn().mockResolvedValue({
        platform: "bilibili",
        source_url: "https://www.bilibili.com/video/BV1test",
        canonical_url: "https://www.bilibili.com/video/BV1test",
        video_id: "BV1test",
        author: "测试作者",
        title: "公开课程",
        description: "公开简介",
        tags: ["AI", "教程"],
        duration: 60,
        cover_url: "",
        warnings: [],
      }),
      getVideoDetail: vi.fn(),
      createResolutionJob: vi.fn(),
      createUploadJob: vi.fn(),
      deleteRetainedMedia: vi.fn(),
      getJob: vi.fn(),
      retryJob: vi.fn(),
      cancelJob: vi.fn(),
      createJobBatch: vi.fn(),
      listJobBatches: vi.fn().mockResolvedValue([]),
      getJobBatch: vi.fn(),
      cancelJobBatchItem: vi.fn(),
      setFavorite: vi.fn(),
      updateTags: vi.fn(),
      updateClassification: vi.fn(),
      generateAutomaticTags: vi.fn(),
      getPersonalNotes: vi.fn(),
      upsertSpark: vi.fn(),
      deleteSpark: vi.fn(),
      createAnnotation: vi.fn(),
      updateAnnotation: vi.fn(),
      deleteAnnotation: vi.fn(),
      createFocusedExtraction: vi.fn(),
      askQuestion: vi.fn(),
      exportMarkdown: vi.fn(),
    },
  };
});
import {
  ApiError,
  apiClient,
  type ApiJob,
  type ApiJobBatch,
  type ApiLibraryVideo,
  type ApiPersonalNotes,
  type ApiVideoDetail,
  type ApiVideoSearchPage,
} from "./api";
import { LegacyApp as App, resolveSeekTarget } from "./legacy-app";

const notesLibraryVideo: ApiLibraryVideo = {
  platform: "bilibili",
  video_id: "BVnotes",
  source_url: "https://www.bilibili.com/video/BVnotes",
  title: "持久化笔记样例",
  author: "测试作者",
  description: "",
  summary: "先验证输入，再执行下一步。",
  cover_url: "",
  subtitle_source: "official",
  source_tags: ["教程"],
  warnings: [],
  personal_tags: [],
  automatic_tagging: {
    status: "generated",
    tags: [
      {
        name: "教程",
        confidence: 0.91,
        generation_method: "deterministic",
      },
    ],
    generator_id: "offline-keyword",
    generator_version: "1",
    transcript_hash: "transcript-hash",
    generated_at: "2026-07-29T08:00:00Z",
    warning: "",
  },
  classification: {
    primary_category: "AI 与工具",
    secondary_category: "教程",
    updated_at: "2026-07-29T08:00:00Z",
  },
  spark: null,
  favorite: true,
  created_at: "2026-07-29T08:00:00Z",
  updated_at: "2026-07-29T08:00:00Z",
};

const notesDetail: ApiVideoDetail = {
  result: {
    platform: "bilibili",
    source_url: notesLibraryVideo.source_url,
    canonical_url: notesLibraryVideo.source_url,
    video_id: notesLibraryVideo.video_id,
    author: notesLibraryVideo.author,
    title: notesLibraryVideo.title,
    description: "",
    tags: ["教程"],
    duration: 80,
    cover_url: "",
    subtitle_source: "official",
    raw_transcript: "先验证输入，再执行下一步。",
    clean_transcript: "先验证输入，再执行下一步。",
    segments: [
      {
        id: "segment-notes",
        start: 12,
        end: 18,
        text: "先验证输入，再执行下一步。",
      },
    ],
    focus_query: "",
    extraction_mode: "full",
    summary: "先验证输入，再执行下一步。",
    full_extraction: {
      key_points: [
        {
          text: "先验证输入",
          item_type: "key_point",
          claim_type: "video_fact",
          evidence_refs: ["evidence-notes"],
          confidence: 0.95,
        },
      ],
      important_data: [],
      cases_and_arguments: [],
      steps: [],
      risks: [],
      quotes: [],
    },
    evidence: [
      {
        id: "evidence-notes",
        claim: "先验证输入",
        claim_type: "video_fact",
        evidence: "先验证输入，再执行下一步。",
        segment_ids: ["segment-notes"],
        start_time: 12,
        end_time: 18,
        confidence: 0.95,
      },
    ],
    focused_answer: null,
    warnings: [],
  },
  favorite: true,
  personal_tags: [],
  automatic_tagging: notesLibraryVideo.automatic_tagging,
  classification: notesLibraryVideo.classification,
  focused_history: [],
  question_history: [],
  playback: {
    availability: "unavailable",
    kind: "none",
    stream_url: "",
    mime_type: "",
    size_bytes: 0,
    supports_range: false,
    reason: "unsupported_source",
  },
  personal_notes: { spark: null, annotations: [] },
  annotation_targets: [
    {
      target_key: "extraction:18:item:stable-target",
      display_key: "key_points-0",
      target_type: "claim",
      mode: "full",
      focus_query: "",
      query_hash: "",
    },
  ],
};

const notesSearchPage = {
  items: [notesLibraryVideo],
  total: 1,
  limit: 100,
  offset: 0,
  facets: {
    tags: [
      { name: "教程", source: "platform" as const, count: 1 },
      { name: "教程", source: "automatic" as const, count: 1 },
    ],
    categories: [
      {
        primary_category: "AI 与工具",
        count: 1,
        children: [{ secondary_category: "教程", count: 1 }],
      },
    ],
  },
};

function batchJob(id: string, status: ApiJob["status"] = "queued"): ApiJob {
  return {
    id,
    status,
    progress: 0,
    error_code: null,
    message: status === "queued" ? "等待解析" : "正在获取字幕",
    video_id: null,
    retry_count: 0,
    max_retries: 2,
    retryable: status === "failed",
    cancel_requested: false,
    warnings: [],
    checkpoint: "none",
    heartbeat_at: "",
    revision: 0,
    lease_owner: "",
    lease_expires_at: "",
  };
}

function pendingBatchDescriptor(
  id: string,
  title: string,
  job: ApiJob,
  platform: "bilibili" | "local_upload" = "bilibili",
) {
  const isLocalUpload = platform === "local_upload";
  return {
    job,
    platform,
    record: {
      id,
      platform: isLocalUpload ? "local" : "bilibili",
      sourceUrl: isLocalUpload ? null : `https://www.bilibili.com/video/${id}`,
      mediaType: "视频",
      title,
      author: "测试作者",
      duration: 0,
      status: "processing",
      statusText: "AI 解析中",
      summary: job.message,
      tags: [],
      userTags: [],
      primaryCategory: "暂未分类",
      secondaryCategory: null,
      sparkNote: null,
      sparkCreatedAt: null,
      updatedAt: "刚刚",
      integrity: "暂不可判断",
      subtitleSource: "正在获取",
      outline: [],
      points: [],
      evidence: [],
      notes: [],
    },
    meta: {
      primaryCategory: "暂未分类",
      secondaryCategory: null,
      userTags: [],
      sparkNote: null,
      sparkCreatedAt: null,
    },
  };
}

function apiBatch(
  items: Array<{
    title: string;
    job: ApiJob;
    platform?: "bilibili" | "local_upload";
  }>,
): ApiJobBatch {
  const completed = items.filter((item) =>
    ["completed", "completed_with_warnings"].includes(item.job.status),
  ).length;
  const failed = items.filter((item) => item.job.status === "failed").length;
  const cancelled = items.filter((item) => item.job.status === "cancelled").length;
  const active = items.length - completed - failed - cancelled;
  return {
    id: "batch-restore",
    status: active ? "running" : failed || cancelled ? "completed_with_issues" : "completed",
    total: items.length,
    completed,
    active,
    failed,
    cancelled,
    created_at: "2026-07-30T08:00:00Z",
    updated_at: "2026-07-30T08:00:00Z",
    items: items.map((item, position) => ({
      position,
      title: item.title,
      platform: item.platform ?? "bilibili",
      job: item.job,
    })),
  };
}

function renderApp() {
  return render(
    <Theme appearance="dark">
      <App />
    </Theme>,
  );
}

async function openDesktopDetail(user: ReturnType<typeof userEvent.setup>) {
  const library = document.querySelector(".desktop-library-view") as HTMLElement;
  await user.click(
    within(library).getByRole("button", {
      name: /打开解析详情：普通人如何用 AI 编程/,
    }),
  );
  return document.querySelector(".desktop-workspace") as HTMLElement;
}

async function openLiveNotesDetail(
  user: ReturnType<typeof userEvent.setup>,
  detail: ApiVideoDetail = notesDetail,
  page: ApiVideoSearchPage = notesSearchPage,
) {
  vi.mocked(apiClient.listVideos).mockResolvedValue(page);
  vi.mocked(apiClient.getVideoDetail).mockResolvedValue(detail);
  renderApp();
  const library = document.querySelector(".desktop-library-view") as HTMLElement;
  await waitFor(() =>
    expect(
      within(library).getByRole("heading", {
        name: notesLibraryVideo.title,
      }),
    ).toBeInTheDocument(),
  );
  await waitFor(() =>
    expect(apiClient.listVideos).toHaveBeenCalledTimes(2),
  );
  await user.click(
    within(library).getByRole("button", {
      name: `打开解析详情：${notesLibraryVideo.title}`,
    }),
  );
  const workspace = document.querySelector(
    ".desktop-workspace",
  ) as HTMLElement;
  await waitFor(() =>
    expect(
      within(workspace).getByRole("heading", {
        name: "先验证输入",
      }),
    ).toBeInTheDocument(),
  );
  return workspace;
}

describe("视频知识库界面", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/?mode=deep-analysis");
    window.localStorage.clear();
    vi.mocked(apiClient.getJob).mockReset();
    vi.mocked(apiClient.createJobBatch).mockReset();
    vi.mocked(apiClient.listJobBatches).mockReset();
    vi.mocked(apiClient.listJobBatches).mockResolvedValue([]);
    vi.mocked(apiClient.getJobBatch).mockReset();
    vi.mocked(apiClient.cancelJobBatchItem).mockReset();
    vi.mocked(apiClient.listVideos).mockReset();
    vi.mocked(apiClient.listVideos).mockRejectedValue(
      new Error("test offline"),
    );
    vi.mocked(apiClient.getVideoDetail).mockReset();
    vi.mocked(apiClient.deleteRetainedMedia).mockReset();
    vi.mocked(apiClient.upsertSpark).mockReset();
    vi.mocked(apiClient.deleteSpark).mockReset();
    vi.mocked(apiClient.createAnnotation).mockReset();
    vi.mocked(apiClient.updateAnnotation).mockReset();
    vi.mocked(apiClient.deleteAnnotation).mockReset();
    vi.mocked(apiClient.askQuestion).mockReset();
    vi.mocked(apiClient.exportMarkdown).mockReset();
    vi.mocked(apiClient.exportMarkdown).mockResolvedValue("# 导出结果");
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => "blob:w11-export"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
  });

  it("刷新后按持久任务终态恢复失败卡片，不继续显示解析中", () => {
    window.localStorage.setItem(
      "video-knowledge.pending-jobs.v1",
      JSON.stringify([
        {
          job: {
            id: "job-failed",
            status: "failed",
            progress: 10,
            error_code: "ASR_FAILED",
            message: "无法生成字幕，请上传字幕后重新提交。",
            video_id: null,
            retry_count: 0,
            max_retries: 2,
            retryable: false,
            cancel_requested: false,
            warnings: [],
            checkpoint: "none",
            heartbeat_at: "",
            revision: 2,
            lease_owner: "",
            lease_expires_at: "",
          },
          platform: "local_upload",
          record: {
            id: "saved-failed",
            platform: "local",
            sourceUrl: null,
            mediaType: "音频",
            title: "失败样例",
            author: "本地上传",
            duration: 0,
            status: "processing",
            statusText: "AI 解析中",
            summary: "处理中",
            tags: [],
            userTags: [],
            primaryCategory: "暂未分类",
            secondaryCategory: null,
            sparkNote: null,
            sparkCreatedAt: null,
            updatedAt: "刚刚",
            integrity: "暂不可判断",
            subtitleSource: "正在获取",
            outline: [],
            points: [],
            evidence: [],
            notes: [],
          },
          meta: {
            primaryCategory: "暂未分类",
            secondaryCategory: null,
            userTags: [],
            sparkNote: null,
            sparkCreatedAt: null,
          },
        },
      ]),
    );
    renderApp();
    const desktop = document.querySelector(
      ".desktop-library-view",
    ) as HTMLElement;
    const failedCard = within(desktop)
      .getByRole("heading", { name: "失败样例" })
      .closest("article") as HTMLElement;
    expect(
      within(failedCard).getAllByText(
        "无法生成字幕，请上传字幕后重新提交。",
      )
        .length,
    ).toBeGreaterThan(0);
    expect(
      within(failedCard).queryByText("AI 解析中"),
    ).not.toBeInTheDocument();
    expect(within(failedCard).getByText("请重新提交")).toBeInTheDocument();
  });

  it("后端找不到持久任务时转为失败终态并停止恢复轮询", async () => {
    vi.mocked(apiClient.getJob).mockRejectedValue(
      new ApiError("任务不存在", "NOT_FOUND", 404),
    );
    window.localStorage.setItem(
      "video-knowledge.pending-jobs.v1",
      JSON.stringify([
        {
          job: {
            id: "job-lost",
            status: "processing",
            progress: 45,
            error_code: null,
            message: "正在整理字幕",
            video_id: null,
            retry_count: 0,
            max_retries: 2,
            retryable: true,
            cancel_requested: false,
            warnings: [],
            checkpoint: "subtitles",
            heartbeat_at: "",
            revision: 1,
            lease_owner: "",
            lease_expires_at: "",
          },
          platform: "local_upload",
          record: {
            id: "saved-lost",
            platform: "local",
            sourceUrl: null,
            mediaType: "音频",
            title: "已丢失任务",
            author: "本地上传",
            duration: 0,
            status: "processing",
            statusText: "AI 解析中",
            summary: "正在整理字幕",
            tags: [],
            userTags: [],
            primaryCategory: "暂未分类",
            secondaryCategory: null,
            sparkNote: null,
            sparkCreatedAt: null,
            updatedAt: "刚刚",
            integrity: "暂不可判断",
            subtitleSource: "正在获取",
            outline: [],
            points: [],
            evidence: [],
            notes: [],
          },
          meta: {
            primaryCategory: "暂未分类",
            secondaryCategory: null,
            userTags: [],
            sparkNote: null,
            sparkCreatedAt: null,
          },
        },
      ]),
    );

    renderApp();

    await waitFor(() => {
      expect(screen.getAllByText("任务已失效").length).toBeGreaterThan(0);
    });
    expect(
      screen.getAllByText(
        "后端已找不到这个任务，可能更换了数据实例。请重新提交解析。",
      ).length,
    ).toBeGreaterThan(0);
    await waitFor(() => {
      expect(
        JSON.parse(
          window.localStorage.getItem(
            "video-knowledge.pending-jobs.v1",
          ) ?? "[]",
        ),
      ).toEqual([]);
    });
  });

  it("刷新后从服务端恢复最近批次并单独取消成员任务", async () => {
    const first = batchJob("job-batch-1");
    const second = batchJob("job-batch-2", "fetching_subtitles");
    const restored = apiBatch([
      { title: "恢复任务一", job: first },
      { title: "恢复任务二", job: second },
    ]);
    const cancelled = apiBatch([
      { title: "恢复任务一", job: { ...first, status: "cancelled" } },
      { title: "恢复任务二", job: second },
    ]);
    vi.mocked(apiClient.listJobBatches).mockResolvedValue([restored]);
    vi.mocked(apiClient.getJobBatch).mockResolvedValue(restored);
    vi.mocked(apiClient.cancelJobBatchItem).mockResolvedValue(cancelled);
    const user = userEvent.setup();

    renderApp();
    const desktop = document.querySelector(
      ".desktop-library-view",
    ) as HTMLElement;
    const queue = await waitFor(() =>
      within(desktop).getByLabelText("批量解析队列"),
    );
    expect(within(queue).getByText("已完成 0/2")).toBeInTheDocument();
    expect(queue.textContent).not.toContain("%");

    await user.click(
      within(queue).getAllByRole("button", { name: "取消任务" })[0],
    );
    await waitFor(() =>
      expect(apiClient.cancelJobBatchItem).toHaveBeenCalledWith(
        "batch-restore",
        "job-batch-1",
      ),
    );
    expect(within(queue).getAllByText("已结束").length).toBeGreaterThan(0);
  });

  it("桌面收藏库只把显式选择的两个既有任务提交为批次", async () => {
    const first = batchJob("job-create-1");
    const second = batchJob("job-create-2", "cleaning");
    window.localStorage.setItem(
      "video-knowledge.pending-jobs.v1",
      JSON.stringify([
        pendingBatchDescriptor("saved-create-1", "创建任务一", first),
        pendingBatchDescriptor("saved-create-2", "创建任务二", second),
      ]),
    );
    const created = apiBatch([
      { title: "创建任务一", job: first },
      { title: "创建任务二", job: second },
    ]);
    vi.mocked(apiClient.createJobBatch).mockResolvedValue(created);
    const user = userEvent.setup();

    renderApp();
    const desktop = document.querySelector(
      ".desktop-library-view",
    ) as HTMLElement;
    await user.click(within(desktop).getByRole("button", { name: "批量选择" }));
    await user.click(
      within(desktop).getByRole("button", { name: "勾选任务：创建任务一" }),
    );
    await user.click(
      within(desktop).getByRole("button", { name: "勾选任务：创建任务二" }),
    );
    expect(within(desktop).getByText(/已选择 2\/10 项/)).toBeInTheDocument();
    await user.click(within(desktop).getByRole("button", { name: "创建批次" }));

    await waitFor(() =>
      expect(apiClient.createJobBatch).toHaveBeenCalledWith([
        { job_id: "job-create-1", title: "创建任务一", platform: "bilibili" },
        { job_id: "job-create-2", title: "创建任务二", platform: "bilibili" },
      ]),
    );
    expect(
      within(desktop).getByLabelText("批量解析队列"),
    ).toBeInTheDocument();
  });

  it("本地上传任务无需公开原链接也能与平台任务一起提交批次", async () => {
    const localJob = batchJob("job-local-upload", "transcribing");
    const platformJob = batchJob("job-platform-link", "cleaning");
    const localDescriptor = pendingBatchDescriptor(
      "saved-local-upload",
      "本地上传教程.mp4",
      localJob,
      "local_upload",
    );
    expect(localDescriptor.record.sourceUrl).toBeNull();
    window.localStorage.setItem(
      "video-knowledge.pending-jobs.v1",
      JSON.stringify([
        localDescriptor,
        pendingBatchDescriptor(
          "saved-platform-link",
          "平台视频",
          platformJob,
        ),
      ]),
    );
    vi.mocked(apiClient.createJobBatch).mockResolvedValue(
      apiBatch([
        {
          title: "本地上传教程.mp4",
          job: localJob,
          platform: "local_upload",
        },
        { title: "平台视频", job: platformJob },
      ]),
    );
    const user = userEvent.setup();

    renderApp();
    const desktop = document.querySelector(
      ".desktop-library-view",
    ) as HTMLElement;
    await user.click(within(desktop).getByRole("button", { name: "批量选择" }));
    await user.click(
      within(desktop).getByRole("button", {
        name: "勾选任务：本地上传教程.mp4",
      }),
    );
    await user.click(
      within(desktop).getByRole("button", { name: "勾选任务：平台视频" }),
    );
    await user.click(within(desktop).getByRole("button", { name: "创建批次" }));

    await waitFor(() =>
      expect(apiClient.createJobBatch).toHaveBeenCalledWith([
        {
          job_id: "job-local-upload",
          title: "本地上传教程.mp4",
          platform: "local_upload",
        },
        {
          job_id: "job-platform-link",
          title: "平台视频",
          platform: "bilibili",
        },
      ]),
    );
  });

  it("收藏网格可以进入结论优先详情和主题目录", async () => {
    const user = userEvent.setup();
    renderApp();
    const desktop = await openDesktopDetail(user);

    expect(within(desktop).getByText("一句话看懂")).toBeInTheDocument();
    expect(within(desktop).getByText("本视频目录")).toBeInTheDocument();
    expect(
      within(desktop).getAllByText(
        "视频用一个小项目说明 AI 编程的入门路径，重点是缩小任务、即时验证，以及始终保留人工判断。",
      ).length,
    ).toBeGreaterThan(0);
  });

  it("三种阅读模式只切换展示内容", async () => {
    const user = userEvent.setup();
    renderApp();

    const desktop = await openDesktopDetail(user);
    await user.click(
      within(desktop).getByRole("tab", { name: /操作步骤/ }),
    );

    expect(
      within(desktop).getByText("写清输入、输出和验收条件"),
    ).toBeInTheDocument();
    expect(
      within(desktop).queryByText("先把目标缩小到一个可验证结果"),
    ).not.toBeInTheDocument();
    const evidencePanel = within(desktop)
      .getByText("字幕依据")
      .closest("details") as HTMLDetailsElement;
    expect(
      within(evidencePanel).queryByText(
        "如果你第一次就想做一个完整产品，通常很快会卡住。先选一个一两个小时能看到结果的小项目。",
      ),
    ).not.toBeInTheDocument();
    expect(
      within(evidencePanel).getByText(
        "你要先告诉工具输入是什么，最后想得到什么，还要把你怎么判断它完成了写清楚。",
      ),
    ).toBeInTheDocument();
  });

  it("原始字幕支持局部搜索、高亮和匹配定位", async () => {
    const user = userEvent.setup();
    renderApp();

    const desktop = await openDesktopDetail(user);
    await user.click(within(desktop).getByRole("tab", { name: /原始字幕/ }));
    await user.type(
      within(desktop).getByRole("textbox", { name: "搜索当前字幕" }),
      "输入是什么",
    );

    expect(within(desktop).getByText("1 / 1 条匹配")).toBeInTheDocument();
    const highlighted = within(desktop).getByText("输入是什么", {
      selector: "mark",
    });
    expect(highlighted.closest("article")).toHaveClass("is-active");
  });

  it("字幕依据默认折叠，时间戳会展开依据", async () => {
    const user = userEvent.setup();
    renderApp();

    const desktop = await openDesktopDetail(user);
    const evidencePanel = within(desktop)
      .getByText("字幕依据")
      .closest("details") as HTMLDetailsElement;
    expect(evidencePanel.open).toBe(false);

    await user.click(
      within(desktop).getByRole("button", {
        name: "查看 01:02 的字幕依据",
      }),
    );
    expect(evidencePanel.open).toBe(true);
  });

  it("服务端提供播放能力时只挂载一个媒体元素并打开时间戳证据", async () => {
    const user = userEvent.setup();
    const workspace = await openLiveNotesDetail(user, {
      ...notesDetail,
      playback: {
        availability: "available",
        kind: "retained_local",
        stream_url: "/api/v1/videos/notes-video/media?platform=local_upload",
        mime_type: "video/mp4",
        size_bytes: 128,
        supports_range: true,
        reason: "available",
      },
    });
    const media = workspace.querySelector("video") as HTMLVideoElement;
    expect(media).toBeInTheDocument();
    expect(document.querySelectorAll("video")).toHaveLength(1);
    expect(media).toHaveAttribute(
      "src",
      "/api/v1/videos/notes-video/media?platform=local_upload",
    );
    Object.defineProperty(media, "duration", { configurable: true, value: 80 });
    Object.defineProperty(media, "readyState", { configurable: true, value: 1 });
    Object.defineProperty(media, "currentTime", {
      configurable: true,
      writable: true,
      value: 0,
    });
    expect(within(workspace).getByText("证据时间线")).toBeInTheDocument();

    await user.click(
      within(workspace).getByRole("button", {
        name: "查看 00:12 的字幕依据",
      }),
    );
    expect(
      within(workspace).getByText("字幕依据").closest("details"),
    ).toHaveAttribute("open");
  });

  it("keeps compact media deletion reachable without timestamps", async () => {
    const user = userEvent.setup();
    const localSearchPage: ApiVideoSearchPage = {
      ...notesSearchPage,
      items: [
        {
          ...notesLibraryVideo,
          platform: "local_upload",
          source_url: "",
        },
      ],
    };
    await openLiveNotesDetail(
      user,
      {
        ...notesDetail,
        result: {
          ...notesDetail.result,
          platform: "local_upload",
          source_url: "",
          canonical_url: "",
          evidence: notesDetail.result.evidence.map((evidence) => ({
            ...evidence,
            start_time: null,
            end_time: null,
          })),
        },
        playback: {
          availability: "available",
          kind: "retained_local",
          stream_url: "/api/v1/videos/notes-video/media?platform=local_upload",
          mime_type: "video/mp4",
          size_bytes: 128,
          supports_range: true,
          reason: "available",
        },
      },
      localSearchPage,
    );

    await waitFor(() =>
      expect(screen.getByText("已保留可播放媒体")).toBeInTheDocument(),
    );
    const currentWorkspace = document.querySelector(".desktop-workspace") as HTMLElement;
    const deleteButton = within(currentWorkspace).getByRole("button", {
      name: "删除可播放媒体",
    });
    expect(deleteButton).toBeInTheDocument();
    vi.mocked(apiClient.deleteRetainedMedia).mockResolvedValue({
      availability: "unavailable",
      kind: "none",
      stream_url: "",
      mime_type: "",
      size_bytes: 0,
      supports_range: false,
      reason: "not_retained",
    });
    deleteButton.focus();
    fireEvent.click(deleteButton);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "确认只删除媒体" }),
      ).toBeInTheDocument(),
    );
    const dialog = screen.getByRole("alertdialog", {
      name: "只删除可播放媒体？",
    });
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(deleteButton).toHaveFocus();

    fireEvent.click(deleteButton);
    await screen.findByRole("button", { name: "确认只删除媒体" });
    await user.click(
      screen.getByRole("button", { name: "确认只删除媒体" }),
    );
    expect(apiClient.deleteRetainedMedia).toHaveBeenCalledWith(
      "BVnotes",
      "local_upload",
    );
  });

  it("时间戳只在媒体时长范围内生成播放目标", () => {
    expect(resolveSeekTarget(12, 80)).toBe(12);
    expect(resolveSeekTarget(80, 80)).toBeNull();
    expect(resolveSeekTarget(-1, 80)).toBeNull();
  });

  it("无播放能力时展示紧凑说明并只定位字幕", async () => {
    const user = userEvent.setup();
    const workspace = await openLiveNotesDetail(user);
    expect(
      within(workspace).getByText("当前仅支持字幕定位"),
    ).toBeInTheDocument();
    expect(workspace.querySelector("video, audio")).not.toBeInTheDocument();
  });

  it("继续提问只提交当前视频并恢复四态回答和字幕依据", async () => {
    vi.mocked(apiClient.askQuestion).mockResolvedValue({
      id: 7,
      question: "视频里的限制是什么？",
      question_hash: "a".repeat(64),
      answer: {
        mention_status: "explicit",
        is_mentioned: true,
        direct_answer: "视频明确提到：先验证输入。",
        key_points: [],
        supporting_segments: [
          {
            id: "question-evidence",
            claim: "先验证输入",
            claim_type: "video_fact",
            evidence: "先验证输入，再执行下一步。",
            segment_ids: ["segment-notes"],
            start_time: 12,
            end_time: 18,
            confidence: 0.95,
          },
        ],
        supplementary_context: [],
        missing_information: [],
      },
      created_at: "2026-08-09T08:00:00Z",
    });
    const user = userEvent.setup();
    const workspace = await openLiveNotesDetail(user);
    const input = within(workspace).getByRole("textbox", {
      name: "围绕当前视频字幕继续提问",
    });

    await user.type(input, "视频里的限制是什么？");
    await user.click(within(workspace).getByRole("button", { name: "提问" }));

    await waitFor(() =>
      expect(apiClient.askQuestion).toHaveBeenCalledWith(
        "BVnotes",
        "视频里的限制是什么？",
        "bilibili",
      ),
    );
    expect(within(workspace).getByText("视频明确提到")).toBeInTheDocument();
    expect(
      within(workspace).getByText("视频明确提到：先验证输入。"),
    ).toBeInTheDocument();
    await user.click(
      within(workspace).getByRole("button", {
        name: /00:12.*先验证输入，再执行下一步/,
      }),
    );
    expect(
      within(workspace).getByRole("tab", { name: /原始字幕/ }),
    ).toHaveAttribute("aria-selected", "true");
  });

  it("没有已存字幕时禁用追问并说明恢复方式", async () => {
    const user = userEvent.setup();
    const workspace = await openLiveNotesDetail(user, {
      ...notesDetail,
      result: {
        ...notesDetail.result,
        subtitle_source: "none",
        raw_transcript: "",
        clean_transcript: "",
        segments: [],
      },
    });
    expect(
      within(workspace).getByPlaceholderText("当前没有可供追问的字幕"),
    ).toBeDisabled();
    expect(
      within(workspace).getByText("上传字幕或重新解析后才能继续提问"),
    ).toBeInTheDocument();
  });

  it("导出菜单传递当前阅读模式和个人内容显式选择", async () => {
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const user = userEvent.setup();
    const workspace = await openLiveNotesDetail(user);
    await user.click(within(workspace).getByRole("tab", { name: /操作步骤/ }));
    await user.click(within(workspace).getByRole("button", { name: /导出/ }));
    await user.click(
      await screen.findByRole("menuitemcheckbox", { name: "包含个人内容" }),
    );
    await user.click(within(workspace).getByRole("button", { name: /导出/ }));
    await user.click(
      await screen.findByRole("menuitem", { name: "当前模式：操作步骤" }),
    );

    await waitFor(() =>
      expect(apiClient.exportMarkdown).toHaveBeenCalledWith(
        "BVnotes",
        "bilibili",
        {
          view: "steps",
          includePersonal: false,
          focusQueryHash: undefined,
        },
      ),
    );
    expect(anchorClick).toHaveBeenCalledTimes(1);
    anchorClick.mockRestore();
  });

  it("主题和强调色切换会持久化并恢复为完整白色主题", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(screen.getAllByRole("button", { name: "切换配色" })[0]);
    await user.click(await screen.findByRole("menuitemradio", { name: /深色/ }));
    expect(document.documentElement.dataset.theme).toBe("dark");

    await user.click(screen.getAllByRole("button", { name: "切换配色" })[0]);
    await user.click(await screen.findByRole("menuitemradio", { name: /白色/ }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(window.localStorage.getItem("video-knowledge-theme")).toBe("light");

    await user.click(screen.getAllByRole("button", { name: "切换配色" })[0]);
    await user.click(await screen.findByRole("menuitemradio", { name: /青绿/ }));
    expect(document.documentElement.dataset.accent).toBe("teal");
    expect(window.localStorage.getItem("video-knowledge-accent")).toBe("teal");
  });

  it("移动端一次只展开一张卡片", async () => {
    const user = userEvent.setup();
    renderApp();

    const feed = screen.getByRole("region", { name: "视频卡片流" });
    const first = within(feed).getByRole("button", {
      name: /普通人如何用 AI 编程/,
    });
    const second = within(feed).getByRole("button", {
      name: /做内容之前先想清楚/,
    });

    expect(first).toHaveAttribute("aria-expanded", "false");
    await user.click(first);
    expect(first).toHaveAttribute("aria-expanded", "true");
    await user.click(second);
    expect(first).toHaveAttribute("aria-expanded", "false");
    expect(second).toHaveAttribute("aria-expanded", "true");
  });

  it("搜索无匹配时给出可恢复空状态", async () => {
    const user = userEvent.setup();
    renderApp();

    const desktop = document.querySelector(".desktop-library-view") as HTMLElement;
    await user.type(
      within(desktop).getByLabelText("搜索收藏库"),
      "不存在的内容",
    );
    expect(
      within(desktop).getByText("没有匹配的视频"),
    ).toBeInTheDocument();
    expect(
      within(desktop).getByText("调整条件或清除筛选，即可查看其他收藏。"),
    ).toBeInTheDocument();
  });

  it("个人标签筛选只保留对应收藏", async () => {
    const user = userEvent.setup();
    renderApp();

    const desktop = document.querySelector(
      ".desktop-library-view",
    ) as HTMLElement;
    await user.selectOptions(
      within(desktop).getByLabelText("按个人标签筛选"),
      "稍后实践",
    );

    expect(
      within(desktop).getByRole("heading", {
        name: "普通人如何用 AI 编程完成第一个真实项目",
      }),
    ).toBeInTheDocument();
    expect(
      within(desktop).queryByRole("heading", {
        name: "做内容之前先想清楚：用户到底在搜什么",
      }),
    ).not.toBeInTheDocument();
  });

  it("同名标签按来源组合筛选并可一键清除", async () => {
    const user = userEvent.setup();
    vi.mocked(apiClient.listVideos).mockResolvedValue(notesSearchPage);
    renderApp();
    const desktop = document.querySelector(
      ".desktop-library-view",
    ) as HTMLElement;
    await waitFor(() =>
      expect(
        within(desktop).getByRole("heading", { name: notesLibraryVideo.title }),
      ).toBeInTheDocument(),
    );

    await user.selectOptions(
      within(desktop).getByLabelText("按标签来源筛选"),
      "automatic",
    );
    await user.selectOptions(
      within(desktop).getByLabelText("按自动标签筛选"),
      "教程",
    );
    await user.click(
      within(desktop).getByRole("button", { name: /AI 与工具/ }),
    );

    await waitFor(() =>
      expect(apiClient.listVideos).toHaveBeenLastCalledWith(
        expect.objectContaining({
          tag: "教程",
          tagSource: "automatic",
          primaryCategory: "AI 与工具",
        }),
      ),
    );

    await user.click(
      within(desktop).getByRole("button", { name: "清除筛选" }),
    );
    expect(within(desktop).getByLabelText("搜索收藏库")).toHaveValue("");
    expect(within(desktop).getByLabelText("按平台筛选")).toHaveValue("all");
    expect(within(desktop).getByLabelText("按个人标签筛选")).toHaveValue("all");
  });

  it("筛选排除再清除后保留已加载详情与个人批注", async () => {
    const user = userEvent.setup();
    const detailWithAnnotation: ApiVideoDetail = {
      ...notesDetail,
      personal_notes: {
        spark: null,
        annotations: [
          {
            id: 41,
            kind: "annotation",
            target_key: "extraction:18:item:stable-target",
            target_type: "claim",
            content: "筛选后仍需保留",
            author: "我",
            created_at: "2026-07-29T10:00:00Z",
            updated_at: "2026-07-29T10:00:00Z",
          },
        ],
      },
    };
    const emptyPage = {
      ...notesSearchPage,
      items: [],
      total: 0,
      facets: { tags: [], categories: [] },
    };
    vi.mocked(apiClient.listVideos).mockImplementation(async (params) =>
      params?.query ? emptyPage : notesSearchPage,
    );
    vi.mocked(apiClient.getVideoDetail).mockResolvedValue(detailWithAnnotation);
    renderApp();

    let library = document.querySelector(
      ".desktop-library-view",
    ) as HTMLElement;
    await waitFor(() =>
      expect(
        within(library).getByRole("heading", {
          name: notesLibraryVideo.title,
        }),
      ).toBeInTheDocument(),
    );
    await user.click(
      within(library).getByRole("button", {
        name: `打开解析详情：${notesLibraryVideo.title}`,
      }),
    );
    let workspace = document.querySelector(
      ".desktop-workspace",
    ) as HTMLElement;
    await waitFor(() =>
      expect(
        within(workspace).getByText("筛选后仍需保留"),
      ).toBeInTheDocument(),
    );

    await user.click(
      within(workspace).getByRole("button", { name: "收藏库" }),
    );
    library = document.querySelector(".desktop-library-view") as HTMLElement;
    await user.type(
      within(library).getByLabelText("搜索收藏库"),
      "排除当前视频",
    );
    await waitFor(() =>
      expect(
        within(library).queryByRole("heading", {
          name: notesLibraryVideo.title,
        }),
      ).not.toBeInTheDocument(),
    );
    const filterSummary = library.querySelector(
      ".filter-summary",
    ) as HTMLElement;
    await user.click(
      within(filterSummary).getByRole("button", { name: "清除筛选" }),
    );
    await waitFor(() =>
      expect(
        within(library).getByRole("heading", {
          name: notesLibraryVideo.title,
        }),
      ).toBeInTheDocument(),
    );
    await user.click(
      within(library).getByRole("button", {
        name: `打开解析详情：${notesLibraryVideo.title}`,
      }),
    );
    workspace = document.querySelector(".desktop-workspace") as HTMLElement;
    expect(
      within(workspace).getByText("筛选后仍需保留"),
    ).toBeInTheDocument();
    expect(apiClient.getVideoDetail).toHaveBeenCalledTimes(1);
  });

  it("生成成功但没有标签时不猜测生成方式", async () => {
    const user = userEvent.setup();
    const emptyGenerated = {
      ...notesLibraryVideo.automatic_tagging!,
      tags: [],
      generator_id: "replaceable-empty-generator",
    };
    const libraryVideo = {
      ...notesLibraryVideo,
      automatic_tagging: emptyGenerated,
    };
    vi.mocked(apiClient.listVideos).mockResolvedValue({
      ...notesSearchPage,
      items: [libraryVideo],
    });
    vi.mocked(apiClient.getVideoDetail).mockResolvedValue({
      ...notesDetail,
      automatic_tagging: emptyGenerated,
      result: {
        ...notesDetail.result,
        automatic_tagging: emptyGenerated,
      },
    });
    renderApp();
    const library = document.querySelector(
      ".desktop-library-view",
    ) as HTMLElement;
    await waitFor(() =>
      expect(
        within(library).getByRole("heading", {
          name: notesLibraryVideo.title,
        }),
      ).toBeInTheDocument(),
    );
    await user.click(
      within(library).getByRole("button", {
        name: `打开解析详情：${notesLibraryVideo.title}`,
      }),
    );
    const workspace = document.querySelector(
      ".desktop-workspace",
    ) as HTMLElement;
    await waitFor(() =>
      expect(
        within(workspace).getByText("生成器已完成，未返回可用标签"),
      ).toBeInTheDocument(),
    );
    expect(
      within(workspace).queryByText("离线规则基于字幕提取"),
    ).not.toBeInTheDocument();
  });

  it("保存前可以分类并记录闪念", async () => {
    const user = userEvent.setup();
    renderApp();

    const desktop = document.querySelector(".desktop-library-view") as HTMLElement;
    await user.click(within(desktop).getByRole("button", { name: "新建收藏" }));
    const overlay = document.querySelector(".review-overlay") as HTMLElement;
    await user.type(
      within(overlay).getByLabelText("链接或分享文本"),
      "https://www.bilibili.com/video/BV1test",
    );
    await user.click(
      within(overlay).getByRole("button", { name: "识别并整理" }),
    );
    expect(
      within(overlay).getByRole("heading", { name: "保存前整理" }),
    ).toBeInTheDocument();
    expect(within(overlay).getByText("有限来源")).toBeInTheDocument();
    expect(within(overlay).getByText("闪念")).toBeInTheDocument();
  });

  it("收藏卡片的原链接使用安全的新标签页入口", () => {
    renderApp();
    const links = screen.getAllByRole("link", { name: "访问原链接" });
    expect(links[0]).toHaveAttribute("target", "_blank");
    expect(links[0]).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("详情入口也使用统一模态框并在关闭后恢复触发按钮焦点", async () => {
    const user = userEvent.setup();
    renderApp();
    const desktop = await openDesktopDetail(user);
    const trigger = within(desktop).getByRole("button", {
      name: "新建解析",
    });

    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "新建收藏" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(document.body.style.overflow).toBe("hidden");
    await user.click(
      within(dialog).getByRole("button", { name: "关闭新建解析" }),
    );
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(document.body.style.overflow).toBe("");
  });

  it("公开预览失败时规范化标点并给出真实后续动作", async () => {
    const user = userEvent.setup();
    vi.mocked(apiClient.previewResolution).mockRejectedValueOnce(
      new Error("访问公开页面失败。"),
    );
    renderApp();
    const desktop = document.querySelector(
      ".desktop-library-view",
    ) as HTMLElement;
    await user.click(within(desktop).getByRole("button", { name: "新建收藏" }));
    const overlay = document.querySelector(".review-overlay") as HTMLElement;
    await user.type(
      within(overlay).getByLabelText("链接或分享文本"),
      "https://v.douyin.com/test/",
    );
    await user.click(
      within(overlay).getByRole("button", { name: "识别并整理" }),
    );
    expect(
      within(overlay).getByText(
        "访问公开页面失败。仍可保存；保存后会创建解析任务。若仍失败，请上传本地媒体。",
      ),
    ).toBeInTheDocument();
    expect(overlay).not.toHaveTextContent("失败。。");
  });

  it("一次输入多个链接时阻止提交", async () => {
    const user = userEvent.setup();
    renderApp();
    const desktop = document.querySelector(
      ".desktop-library-view",
    ) as HTMLElement;
    await user.click(within(desktop).getByRole("button", { name: "新建收藏" }));
    const overlay = document.querySelector(".review-overlay") as HTMLElement;
    await user.type(
      within(overlay).getByLabelText("链接或分享文本"),
      "https://www.bilibili.com/video/BV1one https://v.douyin.com/two",
    );
    expect(within(overlay).getByRole("alert")).toHaveTextContent(
      "一次只能解析一个受支持的视频链接",
    );
    expect(
      within(overlay).getByRole("button", { name: "识别并整理" }),
    ).toBeDisabled();
  });

  it("本地上传可以附加用户字幕并显示来源", async () => {
    const user = userEvent.setup();
    renderApp();
    const desktop = document.querySelector(
      ".desktop-library-view",
    ) as HTMLElement;
    await user.click(within(desktop).getByRole("button", { name: "导入" }));
    const overlay = document.querySelector(".review-overlay") as HTMLElement;
    await user.upload(
      within(overlay).getByLabelText(/选择视频或音频文件/),
      new File(["media"], "sample.mp4", { type: "video/mp4" }),
    );
    await user.upload(
      within(overlay).getByLabelText(/可选：附加字幕/),
      new File(["00:00 示例字幕"], "sample.txt", { type: "text/plain" }),
    );
    await user.click(
      within(overlay).getByRole("button", { name: "识别并整理" }),
    );
    expect(within(overlay).getByText("用户字幕")).toBeInTheDocument();
    expect(within(overlay).getByText(/sample\.txt/)).toBeInTheDocument();
  });

  it("本地上传默认不保留媒体，用户授权后才提交 retainMedia", async () => {
    const user = userEvent.setup();
    vi.mocked(apiClient.createUploadJob).mockResolvedValue(batchJob("upload-job"));
    renderApp();
    const desktop = document.querySelector(
      ".desktop-library-view",
    ) as HTMLElement;
    await user.click(within(desktop).getByRole("button", { name: "导入" }));
    const overlay = document.querySelector(".review-overlay") as HTMLElement;
    await user.upload(
      within(overlay).getByLabelText(/选择视频或音频文件/),
      new File(["media"], "retained.mp4", { type: "video/mp4" }),
    );
    const retain = within(overlay).getByRole("checkbox", {
      name: /保留媒体用于时间戳播放/,
    });
    expect(retain).not.toBeChecked();
    await user.click(retain);
    await user.click(
      within(overlay).getByRole("button", { name: "识别并整理" }),
    );
    expect(within(overlay).getByText("已授权保留媒体")).toBeInTheDocument();
    await user.click(within(overlay).getByRole("button", { name: "保存" }));
    await waitFor(() =>
      expect(apiClient.createUploadJob).toHaveBeenCalledWith(
        expect.any(File),
        undefined,
        { retainMedia: true },
      ),
    );
  });

  it("闪念可以创建、编辑和删除，并保留用户原始空格", async () => {
    const user = userEvent.setup();
    const workspace = await openLiveNotesDetail(user);
    const created: ApiPersonalNotes = {
      spark: {
        id: 21,
        kind: "spark",
        target_key: null,
        content: "  以后复盘  ",
        author: "我",
        created_at: "2026-07-29T10:00:00Z",
        updated_at: "2026-07-29T10:00:00Z",
      },
      annotations: [],
    };
    vi.mocked(apiClient.upsertSpark)
      .mockResolvedValueOnce(created)
      .mockResolvedValueOnce({
        ...created,
        spark: {
          ...created.spark!,
          content: "修改后的闪念",
          updated_at: "2026-07-29T10:05:00Z",
        },
      });
    vi.mocked(apiClient.deleteSpark).mockResolvedValue({
      spark: null,
      annotations: [],
    });

    await user.click(
      within(workspace).getByRole("button", { name: /^添加闪念/ }),
    );
    const createEditor = within(workspace).getByLabelText("编辑闪念");
    await user.type(createEditor, "  以后复盘  ");
    await user.click(
      within(workspace).getByRole("button", { name: "保存闪念" }),
    );
    await waitFor(() =>
      expect(apiClient.upsertSpark).toHaveBeenLastCalledWith(
        "BVnotes",
        "  以后复盘  ",
        "我",
        "bilibili",
      ),
    );
    expect(within(workspace).getByText("以后复盘")).toBeInTheDocument();

    await user.click(
      within(workspace).getByRole("button", { name: "编辑闪念" }),
    );
    const editEditor = within(workspace).getByLabelText("编辑闪念");
    await user.clear(editEditor);
    await user.type(editEditor, "修改后的闪念");
    await user.click(
      within(workspace).getByRole("button", { name: "保存闪念" }),
    );
    expect(
      within(workspace).getByText("修改后的闪念"),
    ).toBeInTheDocument();

    await user.click(
      within(workspace).getByRole("button", { name: "删除闪念" }),
    );
    await user.click(
      within(workspace).getByRole("button", { name: "确认删除" }),
    );
    await waitFor(() =>
      expect(
        within(workspace).getByRole("button", { name: /^添加闪念/ }),
      ).toBeInTheDocument(),
    );
  });

  it("要点批注使用稳定目标键完成创建、编辑和删除", async () => {
    const user = userEvent.setup();
    const workspace = await openLiveNotesDetail(user);
    const getPoint = () =>
      within(workspace)
        .getByRole("heading", { name: "先验证输入" })
        .closest("article") as HTMLElement;
    const point = getPoint();
    const targetKey = "extraction:18:item:stable-target";
    const createdAnnotation = {
      id: 31,
      kind: "annotation" as const,
      target_key: targetKey,
      target_type: "claim" as const,
      content: "补充自己的验收条件",
      author: "我",
      created_at: "2026-07-29T10:10:00Z",
      updated_at: "2026-07-29T10:10:00Z",
    };
    vi.mocked(apiClient.createAnnotation).mockResolvedValue({
      spark: null,
      annotations: [createdAnnotation],
    });
    vi.mocked(apiClient.updateAnnotation).mockResolvedValue({
      spark: null,
      annotations: [
        {
          ...createdAnnotation,
          content: "更新后的验收条件",
          updated_at: "2026-07-29T10:12:00Z",
        },
      ],
    });
    vi.mocked(apiClient.deleteAnnotation).mockResolvedValue({
      spark: null,
      annotations: [],
    });

    await user.click(
      within(point).getByRole("button", { name: "添加个人备注" }),
    );
    fireEvent.change(within(point).getByLabelText("添加个人备注"), {
      target: { value: "补充自己的验收条件" },
    });
    const saveButton = within(point).getByRole("button", {
      name: "保存备注",
    });
    expect(within(point).getByLabelText("添加个人备注")).toHaveValue(
      "补充自己的验收条件",
    );
    expect(saveButton).toBeEnabled();
    await user.click(saveButton);
    await waitFor(() =>
      expect(apiClient.createAnnotation).toHaveBeenCalledWith(
        "BVnotes",
        targetKey,
        "补充自己的验收条件",
        "我",
        "bilibili",
      ),
    );
    await waitFor(() =>
      expect(
        within(getPoint()).getByRole("button", {
          name: "添加个人备注",
        }),
      ).toHaveFocus(),
    );

    let savedPoint = getPoint();
    await user.click(
      within(savedPoint).getByRole("button", { name: "编辑" }),
    );
    savedPoint = getPoint();
    fireEvent.change(
      within(savedPoint).getByLabelText("编辑个人备注"),
      { target: { value: "更新后的验收条件" } },
    );
    await user.click(
      within(savedPoint).getByRole("button", { name: "保存备注" }),
    );
    await waitFor(() =>
      expect(apiClient.updateAnnotation).toHaveBeenCalledWith(
        "BVnotes",
        "31",
        "更新后的验收条件",
        "我",
        "bilibili",
      ),
    );
    await waitFor(() =>
      expect(
        within(getPoint()).getByText("更新后的验收条件"),
      ).toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(
        within(getPoint()).getByRole("button", { name: "编辑" }),
      ).toHaveFocus(),
    );

    savedPoint = getPoint();
    await user.click(
      within(savedPoint).getByRole("button", { name: "删除" }),
    );
    await user.click(
      within(savedPoint).getByRole("button", { name: "确认删除" }),
    );
    await waitFor(() =>
      expect(
        within(getPoint()).queryByText("更新后的验收条件"),
      ).not.toBeInTheDocument(),
    );
    expect(
      within(workspace).getByRole("heading", { name: "先验证输入" }),
    ).toBeInTheDocument();
  });

  it("要点批注保存失败保留草稿，Escape 和取消恢复添加按钮焦点", async () => {
    const user = userEvent.setup();
    const workspace = await openLiveNotesDetail(user);
    const getPoint = () =>
      within(workspace)
        .getByRole("heading", { name: "先验证输入" })
        .closest("article") as HTMLElement;
    vi.mocked(apiClient.createAnnotation).mockRejectedValueOnce(
      new Error("个人备注保存失败。"),
    );

    await user.click(
      within(getPoint()).getByRole("button", { name: "添加个人备注" }),
    );
    const editor = within(getPoint()).getByLabelText("添加个人备注");
    await user.type(editor, "失败后仍要保留");
    await user.click(
      within(getPoint()).getByRole("button", { name: "保存备注" }),
    );

    await waitFor(() =>
      expect(within(getPoint()).getByRole("alert")).toHaveTextContent(
        "个人备注保存失败。",
      ),
    );
    expect(within(getPoint()).getByLabelText("添加个人备注")).toHaveValue(
      "失败后仍要保留",
    );

    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(
        within(getPoint()).getByRole("button", {
          name: "添加个人备注",
        }),
      ).toHaveFocus(),
    );

    await user.click(
      within(getPoint()).getByRole("button", { name: "添加个人备注" }),
    );
    await user.click(
      within(getPoint()).getByRole("button", { name: "取消" }),
    );
    await waitFor(() =>
      expect(
        within(getPoint()).getByRole("button", {
          name: "添加个人备注",
        }),
      ).toHaveFocus(),
    );
  });
});
