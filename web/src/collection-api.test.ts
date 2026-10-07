import { describe, expect, it, vi } from "vitest";
import {
  cachedItemCoverUrl,
  createCollectionApiClient,
  itemCoverUrl,
  previewCoverUrl,
  sourceItemCoverUrl,
  userItemCoverUrl,
  type CollectionItemCreate,
  type CollectionItemUpdate,
} from "./collection-api";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("collection api", () => {
  it("封面展示路径只由权威身份构造并编码路径段", () => {
    expect(previewCoverUrl("preview /?#")).toBe(
      "/api/v1/collection-previews/preview%20%2F%3F%23/cover",
    );
    expect(itemCoverUrl("素材/一")).toBe(
      "/api/v1/collection-items/%E7%B4%A0%E6%9D%90%2F%E4%B8%80/cover",
    );
    expect(cachedItemCoverUrl("素材/一")).toBe(
      "/api/v1/collection-items/%E7%B4%A0%E6%9D%90%2F%E4%B8%80/cover?cache=only",
    );
    expect(sourceItemCoverUrl("素材/一")).toBe(
      "/api/v1/collection-items/%E7%B4%A0%E6%9D%90%2F%E4%B8%80/cover?source=only",
    );
    expect(previewCoverUrl("")).toBe("");
    expect(userItemCoverUrl("素材/一")).toBe("/api/v1/collection-items/%E7%B4%A0%E6%9D%90%2F%E4%B8%80/cover?user=only");
    expect(userItemCoverUrl("")).toBe("");
    expect(itemCoverUrl("")).toBe("");
    expect(cachedItemCoverUrl("")).toBe("");
  });

  it("权威列表编码组合筛选、游标与 AbortSignal", async () => {
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse({
      items: [], total: 0, limit: 24, next_cursor: null,
      facets: { platforms: [], categories: [], tags: [] },
    }));
    const client = createCollectionApiClient({ baseUrl: "http://service.test/", fetchImpl });
    const controller = new AbortController();

    await client.listItems({
      query: "光 影",
      platform: "youtube",
      primaryCategory: "设计",
      secondaryCategory: "动效",
      tag: "项目参考",
      tagSource: "personal",
      limit: 12,
      cursor: "next-page",
    }, controller.signal);

    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("http://service.test/api/v1/collection-items?query=%E5%85%89+%E5%BD%B1&platform=youtube&primary_category=%E8%AE%BE%E8%AE%A1&secondary_category=%E5%8A%A8%E6%95%88&tag=%E9%A1%B9%E7%9B%AE%E5%8F%82%E8%80%83&tag_source=personal&limit=12&cursor=next-page");
    expect(init?.signal).toBe(controller.signal);
  });

  it("只在显式预览请求中提交原始输入和刷新标志", async () => {
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse({ preview_id: "p1" }, 201));
    const client = createCollectionApiClient({ baseUrl: "http://service.test/", fetchImpl });

    await client.createPreview("分享 https://example.com/a", true);

    expect(fetchImpl).toHaveBeenCalledOnce();
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("http://service.test/api/v1/collection-previews");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({
      input_text: "分享 https://example.com/a",
      refresh_metadata: true,
    });
  });

  it("原子保存发送冻结 payload 和幂等键", async () => {
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse({ id: "c1" }, 201));
    const client = createCollectionApiClient({ fetchImpl });
    const payload: CollectionItemCreate = {
      preview_id: "p1",
      user_title: null,
      untitled_confirmed: false,
      organization_confirmation: {
        primary_category: "设计",
        secondary_category: "空间",
        organization_tags: ["自然光"],
      },
      personal_tags: ["书房"],
      inspiration: {
        content: "想试试百叶帘的光影。",
        input_mode: "text",
        transcription_status: "not_applicable",
      },
    };

    await client.createItem(payload, "attempt-1", undefined, "claim-secret");

    const [, init] = fetchImpl.mock.calls[0];
    expect(new Headers(init?.headers).get("Idempotency-Key")).toBe("attempt-1");
    expect(new Headers(init?.headers).get("X-User-Cover-Claim-Token")).toBe("claim-secret");
    expect(JSON.parse(String(init?.body))).toEqual(payload);
  });

  it("编辑按素材身份发送完整 PATCH 快照与 expected_revision", async () => {
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse({ id: "c1", revision: 8 }));
    const client = createCollectionApiClient({ baseUrl: "http://service.test/", fetchImpl });
    const payload: CollectionItemUpdate = {
      expected_revision: 7,
      user_title: "保留的标题",
      organization_confirmation: {
        primary_category: "设计",
        secondary_category: "空间",
        organization_tags: ["自然光"],
      },
      personal_tags: ["书房"],
      inspiration: {
        content: "更新后的灵感",
        input_mode: "text",
        transcription_status: "not_applicable",
      },
    };

    await client.updateItem("素材/一", payload, undefined, "claim-secret");

    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("http://service.test/api/v1/collection-items/%E7%B4%A0%E6%9D%90%2F%E4%B8%80");
    expect(init?.method).toBe("PATCH");
    expect(new Headers(init?.headers).get("Idempotency-Key")).toBeNull();
    expect(new Headers(init?.headers).get("X-User-Cover-Claim-Token")).toBe("claim-secret");
    expect(JSON.parse(String(init?.body))).toEqual(payload);
  });

  it("个人封面只上传文件，并以不透明身份和 claim token 读取或清理", async () => {
    const responses = [
      jsonResponse({
        asset_id: "a".repeat(32), claim_token: "claim-secret", media_type: "image/webp",
        width: 640, height: 480, size_bytes: 1234, expires_at: "2026-09-11T00:00:00Z",
      }, 201),
      new Response(new Blob(["sanitized"]), { status: 200, headers: { "Content-Type": "image/webp" } }),
      new Response(null, { status: 204 }),
    ];
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => responses.shift()!);
    const client = createCollectionApiClient({ baseUrl: "http://service.test", fetchImpl });
    const file = new Blob(["image"], { type: "image/png" });

    const draft = await client.uploadUserCover(file);
    const restored = await client.getUserCover(draft.asset_id, draft.claim_token);
    await client.deleteUserCover(draft.asset_id, draft.claim_token);

    const uploadBody = fetchImpl.mock.calls[0][1]?.body as FormData;
    expect([...uploadBody.keys()]).toEqual(["file"]);
    expect(restored.type).toBe("image/webp");
    expect(fetchImpl.mock.calls[1][0]).toBe(`http://service.test/api/v1/user-cover-assets/${"a".repeat(32)}/content`);
    expect(new Headers(fetchImpl.mock.calls[1][1]?.headers).get("X-User-Cover-Claim-Token")).toBe("claim-secret");
    expect(fetchImpl.mock.calls[2][1]?.method).toBe("DELETE");
    expect(new Headers(fetchImpl.mock.calls[2][1]?.headers).get("X-User-Cover-Claim-Token")).toBe("claim-secret");
  });

  it("revision 冲突保留 expected/current 三方比较信息", async () => {
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse({
      error: {
        code: "COLLECTION_REVISION_CONFLICT",
        message: "收藏已在其他位置更新",
        expected_revision: 4,
        current_revision: 5,
      },
    }, 409));
    const client = createCollectionApiClient({ fetchImpl });

    await expect(client.updateItem("c1", {
      expected_revision: 4,
      user_title: null,
      organization_confirmation: {
        primary_category: "设计",
        secondary_category: "空间",
        organization_tags: [],
      },
      personal_tags: [],
      inspiration: null,
    })).rejects.toMatchObject({
      code: "COLLECTION_REVISION_CONFLICT",
      status: 409,
      expectedRevision: 4,
      currentRevision: 5,
    });
  });

  it("语音转写 multipart 只包含用户录音 file", async () => {
    const fetchImpl = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      const body = init?.body as FormData;
      expect([...body.keys()]).toEqual(["file"]);
      return jsonResponse({ content: "语音灵感", input_mode: "voice", transcription_status: "draft" });
    });
    const client = createCollectionApiClient({ fetchImpl });

    await client.transcribeInspiration(new Blob(["audio"], { type: "audio/webm" }));
    expect(fetchImpl).toHaveBeenCalledOnce();
  });

  it("保留 COLLECTION_EXISTS 的权威素材身份", async () => {
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse({
      error: {
        code: "COLLECTION_EXISTS",
        message: "素材已存在",
        collection_item_id: "existing-1",
      },
    }, 409));
    const client = createCollectionApiClient({ fetchImpl });

    await expect(client.createItem({} as CollectionItemCreate, "attempt-1"))
      .rejects.toMatchObject({
        code: "COLLECTION_EXISTS",
        status: 409,
        collectionItemId: "existing-1",
      });
  });

  it("深度解析查询、显式启动与重试使用素材身份和 attempt 幂等键", async () => {
    const snapshot = {
      material_id: "c1",
      analysis_job_id: "job-1",
      state: "queued",
      config_version: "collection-default-v1",
      job_revision: 1,
      failed_stage: null,
      can_start: false,
      can_retry: false,
      result_id: null,
      result_kind: null,
      result_revision: null,
      updated_at: "2026-08-24T10:00:00Z",
      limitation: null,
      error: null,
    };
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse(snapshot, 202));
    const client = createCollectionApiClient({ fetchImpl });

    await client.getDeepAnalysis("c1");
    await client.startDeepAnalysis("c1");
    await client.retryDeepAnalysis("c1", "retry-attempt-1");

    expect(fetchImpl.mock.calls[0][0]).toBe("/api/v1/collection-items/c1/deep-analysis");
    expect(fetchImpl.mock.calls[0][1]?.method).toBeUndefined();
    expect(fetchImpl.mock.calls[1][0]).toBe("/api/v1/collection-items/c1/deep-analysis");
    expect(fetchImpl.mock.calls[1][1]?.method).toBe("POST");
    expect(fetchImpl.mock.calls[2][0]).toBe("/api/v1/collection-items/c1/deep-analysis/retry");
    expect(fetchImpl.mock.calls[2][1]?.method).toBe("POST");
    expect(new Headers(fetchImpl.mock.calls[2][1]?.headers).get("Idempotency-Key"))
      .toBe("retry-attempt-1");
  });
});
