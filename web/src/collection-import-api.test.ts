import { describe, expect, it, vi } from "vitest";
import { CollectionImportApiError, createCollectionImportApiClient } from "./collection-import-api";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const batch = {
  batch_id: "batch /一",
  status: "previewing",
  revision: 1,
  total: 2,
  queued: 2,
  previewing: 0,
  ready: 0,
  needs_review: 0,
  duplicates: 0,
  already_exists: 0,
  failed: 0,
  selected: 0,
  created_at: "2026-08-31T10:00:00Z",
  updated_at: "2026-08-31T10:00:00Z",
  terminal_at: null,
  items: [],
};

describe("collection import api", () => {
  it("创建批次发送有序载荷、JSON 媒体类型、幂等键和 AbortSignal", async () => {
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse(batch, 202));
    const client = createCollectionImportApiClient({ baseUrl: "http://service.test/", fetchImpl });
    const controller = new AbortController();
    const payload = {
      items: [
        { client_item_id: "item-01", input_text: "分享 A https://example.com/a" },
        { client_item_id: "item-02", input_text: "分享 B https://example.com/b" },
      ],
    };

    await client.createBatch(payload, "attempt-1", controller.signal);

    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("http://service.test/api/v1/collection-import-batches");
    expect(init?.method).toBe("POST");
    expect(init?.signal).toBe(controller.signal);
    expect(new Headers(init?.headers).get("Content-Type")).toBe("application/json");
    expect(new Headers(init?.headers).get("Idempotency-Key")).toBe("attempt-1");
    expect(JSON.parse(String(init?.body))).toEqual(payload);
  });

  it("active 204 明确返回 null，200 返回权威批次", async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse(batch));
    const client = createCollectionImportApiClient({ fetchImpl });

    await expect(client.getActiveBatch()).resolves.toBeNull();
    await expect(client.getActiveBatch()).resolves.toMatchObject({ batch_id: "batch /一" });
  });

  it("batch 与 item GET 只编码权威路径段，兼容正常 200 响应", async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(jsonResponse(batch))
      .mockResolvedValueOnce(jsonResponse({ batch_id: "batch /一", batch_item_id: "item /二" }));
    const client = createCollectionImportApiClient({ fetchImpl });

    await client.getBatch("batch /一");
    await client.getItem("batch /一", "item /二");

    expect(fetchImpl.mock.calls[0][0]).toBe("/api/v1/collection-import-batches/batch%20%2F%E4%B8%80");
    expect(fetchImpl.mock.calls[1][0]).toBe("/api/v1/collection-import-batches/batch%20%2F%E4%B8%80/items/item%20%2F%E4%BA%8C");
  });

  it("审核 PATCH 只发送扁平 draft 与两级 revision，成功读取有序 BatchSnapshot", async () => {
    const response = { ...batch, revision: 2, items: [{ batch_item_id: "item /二", position: 0, item_revision: 3 }] };
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse(response));
    const client = createCollectionImportApiClient({ fetchImpl });
    const payload = {
      expected_batch_revision: 1,
      expected_item_revision: 2,
      decision: "save" as const,
      user_title: "我的标题",
      untitled_confirmed: false,
      organization_confirmation: {
        primary_category: "文章",
        secondary_category: "产品",
        organization_tags: ["研究"],
      },
      personal_tags: ["待读"],
      inspiration: {
        content: "我的灵感",
        input_mode: "text" as const,
        transcription_status: "not_applicable" as const,
      },
    };

    await expect(client.updateItem("batch /一", "item /二", payload)).resolves.toEqual(response);

    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("/api/v1/collection-import-batches/batch%20%2F%E4%B8%80/items/item%20%2F%E4%BA%8C");
    expect(init?.method).toBe("PATCH");
    expect(new Headers(init?.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(String(init?.body))).toEqual(payload);
    expect(String(init?.body)).not.toContain("preview_id");
  });

  it("repreview 只在用户提供替换输入时发送 input_text，并读取 202 BatchSnapshot", async () => {
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse(batch, 202));
    const client = createCollectionImportApiClient({ fetchImpl });

    await client.repreviewItem("batch /一", "item /二", {
      expected_batch_revision: 1,
      expected_item_revision: 2,
    });
    await client.repreviewItem("batch /一", "item /二", {
      expected_batch_revision: 2,
      expected_item_revision: 3,
      input_text: "替换分享 https://example.com/new",
    });

    expect(JSON.parse(String(fetchImpl.mock.calls[0][1]?.body))).toEqual({
      expected_batch_revision: 1,
      expected_item_revision: 2,
    });
    expect(JSON.parse(String(fetchImpl.mock.calls[1][1]?.body))).toEqual({
      expected_batch_revision: 2,
      expected_item_revision: 3,
      input_text: "替换分享 https://example.com/new",
    });
    expect(fetchImpl.mock.calls[0][0]).toBe("/api/v1/collection-import-batches/batch%20%2F%E4%B8%80/items/item%20%2F%E4%BA%8C/repreview");
  });

  it("批次确认只 POST expected_batch_revision 到编码后的 confirm 路径", async () => {
    const saving = { ...batch, status: "saving", revision: 2 };
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse(saving, 202));
    const client = createCollectionImportApiClient({ fetchImpl });
    const controller = new AbortController();

    await expect(client.confirmBatch("batch /一", { expected_batch_revision: 1 }, controller.signal)).resolves.toEqual(saving);

    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("/api/v1/collection-import-batches/batch%20%2F%E4%B8%80/confirm");
    expect(init?.method).toBe("POST");
    expect(init?.signal).toBe(controller.signal);
    expect(new Headers(init?.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(String(init?.body))).toEqual({ expected_batch_revision: 1 });
  });

  it("恢复只接受 202；取消保留并区分 200/202 合同状态", async () => {
    const resumed = { ...batch, status: "interrupted", revision: 1 };
    const cancelled = { ...batch, status: "cancelled", revision: 2 };
    const cancelling = { ...batch, status: "cancelling", revision: 3 };
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(jsonResponse(resumed, 202))
      .mockResolvedValueOnce(jsonResponse(cancelled, 200))
      .mockResolvedValueOnce(jsonResponse(cancelling, 202));
    const client = createCollectionImportApiClient({ fetchImpl });
    const controller = new AbortController();

    await expect(client.resumeBatch("batch /一", { expected_batch_revision: 1 }, controller.signal)).resolves.toEqual(resumed);
    await expect(client.cancelBatch("batch /一", { expected_batch_revision: 1 })).resolves.toEqual({ snapshot: cancelled, status: 200 });
    await expect(client.cancelBatch("batch /一", { expected_batch_revision: 2 })).resolves.toEqual({ snapshot: cancelling, status: 202 });

    expect(fetchImpl.mock.calls[0][0]).toBe("/api/v1/collection-import-batches/batch%20%2F%E4%B8%80/resume");
    expect(fetchImpl.mock.calls[0][1]).toMatchObject({ method: "POST", signal: controller.signal });
    expect(JSON.parse(String(fetchImpl.mock.calls[0][1]?.body))).toEqual({ expected_batch_revision: 1 });
    expect(fetchImpl.mock.calls[1][0]).toBe("/api/v1/collection-import-batches/batch%20%2F%E4%B8%80/cancel");
    expect(JSON.parse(String(fetchImpl.mock.calls[2][1]?.body))).toEqual({ expected_batch_revision: 2 });
  });

  it("恢复误回 200、取消误回其他 2xx 都不包装成成功", async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(jsonResponse(batch, 200))
      .mockResolvedValueOnce(jsonResponse(batch, 201));
    const client = createCollectionImportApiClient({ fetchImpl });

    await expect(client.resumeBatch("batch-1", { expected_batch_revision: 1 })).rejects.toEqual(expect.objectContaining<Partial<CollectionImportApiError>>({
      code: "INVALID_RESPONSE",
      status: 200,
    }));
    await expect(client.cancelBatch("batch-1", { expected_batch_revision: 1 })).rejects.toEqual(expect.objectContaining<Partial<CollectionImportApiError>>({
      code: "INVALID_RESPONSE",
      status: 201,
    }));
  });

  it("稳定错误只暴露安全字段并保留活动批次身份", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({
      error: {
        code: "BATCH_ACTIVE",
        message: "已有批次正在处理",
        batch_id: "active-1",
        current_batch_revision: 7,
      },
    }, 409));
    const client = createCollectionImportApiClient({ fetchImpl });

    await expect(client.createBatch({ items: [] }, "attempt-1")).rejects.toMatchObject({
      code: "BATCH_ACTIVE",
      status: 409,
      batchId: "active-1",
      currentBatchRevision: 7,
    });
  });

  it("两级 CAS 错误只读取通用 current_revision，不依赖旧 envelope 字段", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({
      error: {
        code: "BATCH_ITEM_REVISION_CONFLICT",
        message: "项目审核已更新",
        current_revision: 9,
      },
    }, 409));
    const client = createCollectionImportApiClient({ fetchImpl });

    await expect(client.updateItem("b1", "i1", {
      expected_batch_revision: 1,
      expected_item_revision: 2,
      decision: "skip",
      user_title: null,
      untitled_confirmed: false,
      organization_confirmation: { primary_category: "", secondary_category: "", organization_tags: [] },
      personal_tags: [],
      inspiration: null,
    })).rejects.toMatchObject({
      code: "BATCH_ITEM_REVISION_CONFLICT",
      currentRevision: 9,
    });
  });

  it("网络与非 JSON 错误不把底层异常或响应正文带进可见消息", async () => {
    const offline = createCollectionImportApiClient({ fetchImpl: vi.fn(async () => { throw new Error("private path C:/secret"); }) });
    await expect(offline.getBatch("b1")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    await expect(offline.getBatch("b1")).rejects.not.toMatchObject({ message: expect.stringContaining("C:/secret") });

    const invalid = createCollectionImportApiClient({ fetchImpl: vi.fn(async () => new Response("private draft", { status: 500 })) });
    await expect(invalid.getBatch("b1")).rejects.toMatchObject({ code: "HTTP_ERROR" });
    await expect(invalid.getBatch("b1")).rejects.not.toMatchObject({ message: expect.stringContaining("private draft") });
  });
});
