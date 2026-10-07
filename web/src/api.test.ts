import { describe, expect, it, vi } from "vitest";
import {
  ApiError,
  createApiClient,
  mapJobStatus,
  mapLibraryVideoToRecord,
  mapVideoFacets,
  mapVideoDetailToRecord,
  type ApiJob,
  type ApiJobBatch,
  type ApiLibraryVideo,
  type ApiVideoDetail,
} from "./api";

const generatedTagging = {
  status: "generated" as const,
  tags: [
    {
      name: "AI 编程",
      confidence: 0.86,
      generation_method: "deterministic" as const,
    },
  ],
  generator_id: "offline-keywords",
  generator_version: "1",
  transcript_hash: "transcript-sha256",
  generated_at: "2026-07-29T10:00:00Z",
  warning: "",
};

const classification = {
  primary_category: "AI 与工具",
  secondary_category: "AI 编程",
  updated_at: "2026-07-29T10:00:00Z",
};

function responseJson(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const job: ApiJob = {
  id: "job-1",
  status: "queued",
  progress: 0,
  error_code: null,
  message: "",
  video_id: null,
  retry_count: 0,
  max_retries: 2,
  retryable: false,
  cancel_requested: false,
  warnings: [],
  checkpoint: "none",
  heartbeat_at: "",
  revision: 0,
  lease_owner: "",
  lease_expires_at: "",
};

const jobBatch: ApiJobBatch = {
  id: "batch-1",
  status: "running",
  total: 2,
  completed: 0,
  active: 2,
  failed: 0,
  cancelled: 0,
  created_at: "2026-07-30T08:00:00Z",
  updated_at: "2026-07-30T08:00:00Z",
  items: [
    { position: 0, title: "任务一", platform: "bilibili", job },
    {
      position: 1,
      title: "任务二",
      platform: "douyin",
      job: { ...job, id: "job-2", status: "fetching_subtitles" },
    },
  ],
};

const libraryVideo: ApiLibraryVideo = {
  platform: "bilibili",
  video_id: "BV1test",
  source_url: "https://www.bilibili.com/video/BV1test",
  title: "测试视频",
  author: "作者",
  description: "简介",
  summary: "视频说明先验证再继续。",
  cover_url: "",
  subtitle_source: "official",
  source_tags: ["平台标签"],
  warnings: [],
  personal_tags: ["个人标签"],
  automatic_tagging: generatedTagging,
  classification,
  spark: {
    id: 7,
    kind: "spark",
    target_key: null,
    content: "稍后复盘验证步骤",
    author: "我",
    created_at: "2026-07-29T09:00:00Z",
    updated_at: "2026-07-29T09:00:00Z",
  },
  favorite: true,
  created_at: "2026-07-28T10:00:00Z",
  updated_at: "2026-07-29T10:00:00Z",
};

const detail: ApiVideoDetail = {
  result: {
    platform: "bilibili",
    source_url: "https://www.bilibili.com/video/BV1test",
    canonical_url: "https://www.bilibili.com/video/BV1test",
    video_id: "BV1test",
    author: "作者",
    title: "测试视频",
    description: "简介",
    tags: ["平台标签"],
    duration: 90,
    cover_url: "",
    subtitle_source: "official",
    raw_transcript: "第一步先验证。",
    clean_transcript: "第一步先验证。",
    segments: [{ id: "s1", start: 12, end: 18, text: "第一步先验证。" }],
    focus_query: "",
    extraction_mode: "full",
    summary: "视频说明先验证再继续。",
    full_extraction: {
      key_points: [
        {
          text: "先验证再继续",
          item_type: "key_point",
          claim_type: "video_fact",
          evidence_refs: ["ev1"],
          confidence: 0.9,
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
        id: "ev1",
        claim: "先验证再继续",
        claim_type: "video_fact",
        evidence: "第一步先验证。",
        segment_ids: ["s1"],
        start_time: 12,
        end_time: 18,
        confidence: 0.9,
      },
    ],
    focused_answer: null,
    automatic_tagging: generatedTagging,
    warnings: [],
  },
  favorite: true,
  personal_tags: ["个人标签"],
  automatic_tagging: generatedTagging,
  classification,
  personal_notes: {
    spark: null,
    annotations: [],
  },
  annotation_targets: [
    {
      target_key: "extraction:1:item:stable",
      display_key: "key_points-0",
      target_type: "claim",
      mode: "full",
      focus_query: "",
      query_hash: "",
    },
  ],
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
};

describe("真实后端 API 客户端", () => {
  it("保存前预览只读取公开元信息", async () => {
    const preview = {
      platform: "bilibili",
      source_url: libraryVideo.source_url,
      canonical_url: libraryVideo.source_url,
      video_id: libraryVideo.video_id,
      author: libraryVideo.author,
      title: libraryVideo.title,
      description: libraryVideo.description,
      tags: libraryVideo.source_tags,
      duration: 90,
      cover_url: libraryVideo.cover_url,
      warnings: [],
    };
    const fetchImpl = vi.fn(async () => responseJson(preview));
    const client = createApiClient({ fetchImpl });
    await expect(
      client.previewResolution("分享 https://www.bilibili.com/video/BV1test"),
    ).resolves.toEqual(preview);
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/v1/resolution-preview",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          input_text:
            "分享 https://www.bilibili.com/video/BV1test",
          focus_query: "",
        }),
      }),
    );
  });

  it("创建链接任务时使用异步 job-first 契约", async () => {
    const fetchImpl = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        responseJson(job, 202),
    );
    const client = createApiClient({
      baseUrl: "http://127.0.0.1:8000/",
      fetchImpl,
    });

    await expect(
      client.createResolutionJob("分享文本 https://example.com", "只看步骤"),
    ).resolves.toEqual(job);
    expect(fetchImpl).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/v1/resolution-jobs",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          input_text: "分享文本 https://example.com",
          focus_query: "只看步骤",
        }),
      }),
    );
  });

  it("列表查询正确编码过滤参数并校验响应", async () => {
    const fetchImpl = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        responseJson({
          items: [libraryVideo],
          total: 1,
          limit: 20,
          offset: 0,
          facets: {
            tags: [
              { name: "AI 编程", source: "automatic", count: 3 },
              { name: "AI 编程", source: "personal", count: 1 },
            ],
            categories: [
              {
                primary_category: "AI 与工具",
                count: 8,
                children: [
                  { secondary_category: "AI 编程", count: 5 },
                ],
              },
            ],
          },
        }),
    );
    const client = createApiClient({ fetchImpl });

    const page = await client.listVideos({
      query: "AI 编程",
      platform: "bilibili",
      tag: "AI 编程",
      tagSource: "automatic",
      primaryCategory: "AI 与工具",
      secondaryCategory: "AI 编程",
      favorite: false,
    });

    expect(page.items[0].video_id).toBe("BV1test");
    expect(page.facets.tags).toHaveLength(2);
    const url = String(fetchImpl.mock.calls[0][0]);
    expect(url).toContain("query=AI+%E7%BC%96%E7%A8%8B");
    expect(url).toContain("platform=bilibili");
    expect(url).toContain("tag=AI+%E7%BC%96%E7%A8%8B");
    expect(url).toContain("tag_source=automatic");
    expect(url).toContain(
      "primary_category=AI+%E4%B8%8E%E5%B7%A5%E5%85%B7",
    );
    expect(url).toContain(
      "secondary_category=AI+%E7%BC%96%E7%A8%8B",
    );
    expect(url).toContain("favorite=false");
  });

  it("分类与自动标签接口使用平台限定强类型请求", async () => {
    const fetchImpl = vi.fn(async () => responseJson(detail));
    const client = createApiClient({ fetchImpl });

    await client.updateClassification(
      "BV/分类",
      "AI 与工具",
      "AI 编程",
      "bilibili",
    );
    await client.generateAutomaticTags("BV/分类", "bilibili");

    expect(fetchImpl).toHaveBeenNthCalledWith(
      1,
      "/api/v1/videos/BV%2F%E5%88%86%E7%B1%BB/classification",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          primary_category: "AI 与工具",
          secondary_category: "AI 编程",
          platform: "bilibili",
        }),
      }),
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      2,
      "/api/v1/videos/BV%2F%E5%88%86%E7%B1%BB/automatic-tags",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ platform: "bilibili" }),
      }),
    );
  });

  it("详情、重试、取消和 Markdown 导出使用平台限定路径", async () => {
    const fetchImpl = vi.fn(async (
      input: RequestInfo | URL,
      _init?: RequestInit,
    ) => {
      const url = String(input);
      if (url.endsWith("/export.md?platform=bilibili")) {
        return new Response("# 测试视频", { status: 200 });
      }
      if (url.includes("/retry") || url.includes("/cancel")) {
        return responseJson(job);
      }
      if (url.includes("/favorite") || url.includes("/tags")) {
        return responseJson(detail);
      }
      if (url.includes("/extractions")) {
        return responseJson(detail.result);
      }
      return responseJson(detail);
    });
    const client = createApiClient({ fetchImpl });

    await expect(
      client.getVideoDetail("BV/特殊", "bilibili"),
    ).resolves.toEqual(detail);
    await client.retryJob("job/1");
    await client.cancelJob("job/1");
    await client.setFavorite("BV1test", true, "bilibili");
    await client.updateTags(
      "BV1test",
      ["稍后实践"],
      "add",
      "bilibili",
    );
    await expect(
      client.createFocusedExtraction(
        "BV1test",
        "只看步骤",
        "bilibili",
      ),
    ).resolves.toEqual(detail.result);
    await expect(
      client.exportMarkdown("BV1test", "bilibili"),
    ).resolves.toBe("# 测试视频");

    const urls = fetchImpl.mock.calls.map((call) => String(call[0]));
    expect(urls).toContain(
      "/api/v1/videos/BV%2F%E7%89%B9%E6%AE%8A?platform=bilibili",
    );
    expect(urls).toContain("/api/v1/jobs/job%2F1/retry");
    expect(urls).toContain("/api/v1/jobs/job%2F1/cancel");
    expect(urls).toContain("/api/v1/videos/BV1test/favorite");
    expect(urls).toContain("/api/v1/videos/BV1test/tags");
    expect(urls).toContain("/api/v1/videos/BV1test/extractions");
  });

  it("继续提问和模式化导出传递强类型范围", async () => {
    const question = {
      id: 9,
      question: "收费标准是什么？",
      question_hash: "a".repeat(64),
      answer: {
        mention_status: "explicit" as const,
        is_mentioned: true,
        direct_answer: "视频明确提到收费标准。",
        key_points: [],
        supporting_segments: [],
        supplementary_context: [],
        missing_information: [],
      },
      created_at: "2026-08-09T08:00:00Z",
    };
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      return String(input).includes("/questions")
        ? responseJson(question)
        : new Response("# 仅步骤", { status: 200 });
    });
    const client = createApiClient({ fetchImpl });

    await expect(
      client.askQuestion("BV/问答", "收费标准是什么？", "bilibili"),
    ).resolves.toEqual(question);
    await expect(
      client.exportMarkdown("BV/问答", "bilibili", {
        view: "steps",
        includePersonal: false,
        focusQueryHash: "b".repeat(64),
      }),
    ).resolves.toBe("# 仅步骤");

    expect(fetchImpl).toHaveBeenNthCalledWith(
      1,
      "/api/v1/videos/BV%2F%E9%97%AE%E7%AD%94/questions",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          question: "收费标准是什么？",
          platform: "bilibili",
        }),
      }),
    );
    expect(String(fetchImpl.mock.calls[1][0])).toBe(
      `/api/v1/videos/BV%2F%E9%97%AE%E7%AD%94/export.md?platform=bilibili&view=steps&include_personal=false&focus_query_hash=${"b".repeat(64)}`,
    );
  });

  it("上传显式传递媒体保留选择，删除媒体使用受控视频路径", async () => {
    const fetchImpl = vi.fn(async (
      input: RequestInfo | URL,
      _init?: RequestInit,
    ) => {
      if (String(input).includes("/media")) {
        return responseJson({
          availability: "unavailable",
          kind: "none",
          stream_url: "",
          mime_type: "",
          size_bytes: 0,
          supports_range: false,
          reason: "not_retained",
        });
      }
      return responseJson(job);
    });
    const client = createApiClient({ fetchImpl });

    await client.createUploadJob(
      new File(["media"], "sample.mp4", { type: "video/mp4" }),
      undefined,
      { retainMedia: true },
    );
    const uploadBody = fetchImpl.mock.calls[0][1]?.body as FormData;
    expect(uploadBody.get("retain_media")).toBe("true");
    await expect(
      client.deleteRetainedMedia("local/id", "local_upload"),
    ).resolves.toMatchObject({ availability: "unavailable", reason: "not_retained" });
    expect(fetchImpl.mock.calls[1][0]).toBe(
      "/api/v1/videos/local%2Fid/media?platform=local_upload",
    );
    expect(fetchImpl.mock.calls[1][1]).toEqual(
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("批次接口提交既有任务并解析服务端真实计数", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/job-batches?limit=1")) {
        return responseJson([jobBatch]);
      }
      return responseJson(jobBatch, url.endsWith("/job-batches") ? 201 : 200);
    });
    const client = createApiClient({ fetchImpl });
    const items = [
      { job_id: "job-1", title: "任务一", platform: "bilibili" as const },
      { job_id: "job-2", title: "任务二", platform: "douyin" as const },
    ];

    await expect(client.createJobBatch(items)).resolves.toEqual(jobBatch);
    await expect(client.listJobBatches(1)).resolves.toEqual([jobBatch]);
    await expect(client.getJobBatch("batch/1")).resolves.toEqual(jobBatch);
    await expect(
      client.cancelJobBatchItem("batch/1", "job/2"),
    ).resolves.toEqual(jobBatch);

    expect(fetchImpl).toHaveBeenNthCalledWith(
      1,
      "/api/v1/job-batches",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ items }),
      }),
    );
    const urls = fetchImpl.mock.calls.map((call) => String(call[0]));
    expect(urls).toContain("/api/v1/job-batches?limit=1");
    expect(urls).toContain("/api/v1/job-batches/batch%2F1");
    expect(urls).toContain(
      "/api/v1/job-batches/batch%2F1/jobs/job%2F2/cancel",
    );
  });

  it("批次响应计数与成员数量不一致时拒绝展示", async () => {
    const fetchImpl = vi.fn(async () =>
      responseJson({ ...jobBatch, total: 3 }),
    );
    const client = createApiClient({ fetchImpl });

    await expect(client.getJobBatch("batch-1")).rejects.toMatchObject({
      code: "INVALID_RESPONSE",
    });
  });

  it("后端结构化错误保持稳定错误码，不伪装成功", async () => {
    const fetchImpl = vi.fn(async () =>
      responseJson(
        {
          error: {
            code: "UNSUPPORTED_PLATFORM",
            message: "链接解析服务未配置，请上传本地媒体。",
          },
        },
        400,
      ),
    );
    const client = createApiClient({ fetchImpl });

    await expect(client.getJob("missing")).rejects.toMatchObject({
      name: "ApiError",
      code: "UNSUPPORTED_PLATFORM",
      status: 400,
      message: "链接解析服务未配置，请上传本地媒体。",
    });
  });

  it("个人批注更新使用稳定资源路径并原样提交文本", async () => {
    const notes = {
      spark: null,
      annotations: [
        {
          id: 31,
          kind: "annotation",
          target_key: "extraction:18:item:stable-target",
          target_type: "claim",
          content: "  更新后的备注  ",
          author: "我",
          created_at: "2026-07-29T10:00:00Z",
          updated_at: "2026-07-29T10:05:00Z",
        },
      ],
    };
    const fetchImpl = vi.fn(async () => responseJson(notes));
    const client = createApiClient({ fetchImpl });

    await client.updateAnnotation(
      "BVnotes",
      "31",
      "  更新后的备注  ",
      "我",
      "bilibili",
    );

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/v1/videos/BVnotes/annotations/31",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({
          content: "  更新后的备注  ",
          author: "我",
          platform: "bilibili",
        }),
      }),
    );
  });

  it("网络不可用与畸形响应都返回明确错误", async () => {
    const offlineClient = createApiClient({
      fetchImpl: vi.fn(async () => {
        throw new TypeError("fetch failed");
      }),
    });
    await expect(offlineClient.listVideos()).rejects.toMatchObject({
      code: "NETWORK_ERROR",
      status: 0,
    });

    const malformedClient = createApiClient({
      fetchImpl: vi.fn(async () => responseJson({ items: "not-an-array" })),
    });
    await expect(malformedClient.listVideos()).rejects.toBeInstanceOf(ApiError);
    await expect(malformedClient.listVideos()).rejects.toMatchObject({
      code: "INVALID_RESPONSE",
    });
  });
});

describe("后端领域模型到界面模型的纯映射", () => {
  it("轻量收藏列表返回缓存摘要并保留字幕状态", () => {
    const mapped = mapLibraryVideoToRecord(libraryVideo);
    expect(mapped.summary).toBe("视频说明先验证再继续。");
    expect(mapped.integrity).toBe("视频明确陈述");
    expect(mapped.sourceUrl).toBe(
      "https://www.bilibili.com/video/BV1test",
    );
    expect(mapped.userTags).toEqual(["个人标签"]);
    expect(mapped.automaticTags).toEqual([
      {
        name: "AI 编程",
        confidence: 0.86,
        generationMethod: "deterministic",
      },
    ]);
    expect(mapped.automaticTagging?.status).toBe("generated");
    expect(mapped.primaryCategory).toBe("AI 与工具");
    expect(mapped.secondaryCategory).toBe("AI 编程");
    expect(mapped.classificationUpdatedAt).toBe(
      "2026-07-29T10:00:00Z",
    );
    expect(mapped.sparkNote).toBe("稍后复盘验证步骤");
    expect(mapped.sparkId).toBe("7");
    expect(mapped.status).toBe("complete");
  });

  it("自动标签四种状态保持独立且不混入平台或个人标签", () => {
    const statuses = [
      "generated",
      "skipped_no_transcript",
      "failed",
      "not_generated",
    ] as const;

    for (const status of statuses) {
      const mapped = mapLibraryVideoToRecord({
        ...libraryVideo,
        source_tags: ["同名标签"],
        personal_tags: ["同名标签"],
        automatic_tagging: {
          ...generatedTagging,
          status,
          tags: status === "generated" ? generatedTagging.tags : [],
          warning: status === "failed" ? "自动标签生成失败" : "",
        },
      });

      expect(mapped.tags).toEqual(["同名标签"]);
      expect(mapped.userTags).toEqual(["同名标签"]);
      expect(mapped.automaticTagging?.status).toBe(status);
      expect(mapped.automaticTags).toEqual(
        status === "generated"
          ? [
              {
                name: "AI 编程",
                confidence: 0.86,
                generationMethod: "deterministic",
              },
            ]
          : [],
      );
    }
  });

  it("同名不同来源 facet 不合并，并映射分类层级", () => {
    const mapped = mapVideoFacets({
      tags: [
        { name: "AI 编程", source: "platform", count: 4 },
        { name: "AI 编程", source: "automatic", count: 3 },
        { name: "AI 编程", source: "personal", count: 2 },
      ],
      categories: [
        {
          primary_category: "AI 与工具",
          count: 8,
          children: [{ secondary_category: "AI 编程", count: 5 }],
        },
      ],
    });

    expect(mapped.tags.map(({ source, count }) => [source, count])).toEqual([
      ["platform", 4],
      ["automatic", 3],
      ["personal", 2],
    ]);
    expect(mapped.categories).toEqual([
      {
        primaryCategory: "AI 与工具",
        count: 8,
        children: [{ secondaryCategory: "AI 编程", count: 5 }],
      },
    ]);
  });

  it("详情将结论关联到原始证据和真实时间戳", () => {
    const mapped = mapVideoDetailToRecord(detail);
    expect(mapped.summary).toBe("视频说明先验证再继续。");
    expect(mapped.points[0]).toMatchObject({
      title: "先验证再继续",
      evidenceIds: ["ev1"],
      annotationTargetKey: "extraction:1:item:stable",
    });
    expect(mapped.evidence[0]).toEqual({
      id: "ev1",
      start: 12,
      end: 18,
      text: "第一步先验证。",
      segmentIds: ["s1"],
    });
    expect(mapped.transcript).toEqual([
      { id: "s1", start: 12, end: 18, text: "第一步先验证。" },
    ]);
    expect(mapped.outline[0]).toMatchObject({
      title: "核心观点",
      start: 12,
      end: 18,
    });
    expect(mapped.playback).toMatchObject({
      availability: "unavailable",
      reason: "unsupported_source",
    });
  });

  it("不同定向问题只绑定各自的稳定批注目标", () => {
    const targetA = "extraction:21:item:focus-a";
    const targetB = "extraction:22:item:focus-b";
    const focused = {
      mention_status: "explicit" as const,
      is_mentioned: true,
      direct_answer: "视频明确回答了问题 A。",
      key_points: [
        {
          text: "问题 A 的回答",
          item_type: "key_point" as const,
          claim_type: "video_fact" as const,
          evidence_refs: ["ev1"],
          confidence: 0.9,
        },
      ],
      supporting_segments: detail.result.evidence,
      supplementary_context: [],
      missing_information: [],
    };
    const mapped = mapVideoDetailToRecord({
      ...detail,
      result: {
        ...detail.result,
        extraction_mode: "focused",
        focus_query: "问题 A",
        focused_answer: focused,
      },
      annotation_targets: [
        {
          target_key: targetA,
          display_key: "focused-key-points-0",
          target_type: "claim",
          mode: "focused",
          focus_query: "问题 A",
          query_hash: "hash-a",
        },
        {
          target_key: targetB,
          display_key: "focused-key-points-0",
          target_type: "claim",
          mode: "focused",
          focus_query: "问题 B",
          query_hash: "hash-b",
        },
      ],
      personal_notes: {
        spark: null,
        annotations: [
          {
            id: 9,
            kind: "annotation",
            target_key: targetB,
            target_type: "claim",
            content: "只属于问题 B",
            author: "我",
            created_at: "2026-07-29T10:00:00Z",
            updated_at: "2026-07-29T10:00:00Z",
          },
        ],
      },
    });

    expect(mapped.points[0].annotationTargetKey).toBe(targetA);
    expect(mapped.notes[0]).toMatchObject({
      pointId: "",
      targetKey: targetB,
    });
  });

  it("不安全原链接不会映射成可点击地址", () => {
    const mapped = mapLibraryVideoToRecord({
      ...libraryVideo,
      source_url: "javascript:alert(1)",
    });
    expect(mapped.sourceUrl).toBeNull();
  });

  it("任务状态映射保留警告、失败和真实处理阶段", () => {
    expect(mapJobStatus(job)).toEqual({
      status: "processing",
      statusText: "等待解析",
    });
    expect(
      mapJobStatus({
        ...job,
        status: "completed_with_warnings",
      }),
    ).toEqual({
      status: "warning",
      statusText: "解析完成，有警告",
    });
    expect(
      mapJobStatus({ ...job, status: "failed", message: "字幕获取失败" }),
    ).toEqual({
      status: "failed",
      statusText: "字幕获取失败",
    });
  });
});
