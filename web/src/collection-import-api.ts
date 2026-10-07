import type { CollectionItemCreate, CollectionPreview } from "./collection-api";

export type CollectionImportBatchStatus =
  | "previewing"
  | "awaiting_review"
  | "saving"
  | "cancelling"
  | "interrupted"
  | "completed"
  | "completed_with_issues"
  | "cancelled";

export type CollectionImportItemStatus =
  | "queued"
  | "previewing"
  | "ready"
  | "needs_review"
  | "preview_expired"
  | "duplicate_in_batch"
  | "save_queued"
  | "saving"
  | "saved"
  | "already_exists"
  | "skipped"
  | "failed"
  | "cancelled"
  | "interrupted"
  | "outcome_unknown";

export type CollectionImportDecision = "pending" | "save" | "skip";

export interface CollectionImportBatchItemSummary {
  batch_item_id: string;
  client_item_id: string;
  position: number;
  display_label: string;
  state: CollectionImportItemStatus;
  decision: CollectionImportDecision;
  item_revision: number;
  preview_generation: number;
  duplicate_of_batch_item_id: string | null;
  collection_item_id: string | null;
  error_code: string | null;
  terminal_reason: string | null;
}

export interface CollectionImportBatchSnapshot {
  batch_id: string;
  status: CollectionImportBatchStatus;
  revision: number;
  total: number;
  queued: number;
  previewing: number;
  ready: number;
  needs_review: number;
  duplicates: number;
  already_exists: number;
  failed: number;
  selected: number;
  created_at: string;
  updated_at: string;
  terminal_at: string | null;
  items: CollectionImportBatchItemSummary[];
}

export interface CollectionImportItemDetail extends CollectionImportBatchItemSummary {
  batch_id: string;
  batch_revision: number;
  input_available: boolean;
  input_text: string | null;
  preview: CollectionPreview | null;
  draft: Omit<CollectionItemCreate, "preview_id">;
  error_stage: string | null;
}

export interface CollectionImportCreateRequest {
  items: Array<{ client_item_id: string; input_text: string }>;
}

export interface CollectionImportItemUpdateRequest extends Omit<CollectionItemCreate, "preview_id"> {
  expected_batch_revision: number;
  expected_item_revision: number;
  decision: Exclude<CollectionImportDecision, "pending">;
}

export interface CollectionImportItemRepreviewRequest {
  expected_batch_revision: number;
  expected_item_revision: number;
  input_text?: string;
}

export interface CollectionImportBatchConfirmRequest {
  expected_batch_revision: number;
}

export interface CollectionImportBatchCommandRequest {
  expected_batch_revision: number;
}

export interface CollectionImportBatchCommandResult {
  snapshot: CollectionImportBatchSnapshot;
  status: 200 | 202;
}

export class CollectionImportApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly batchId: string | null;
  readonly currentBatchRevision: number | null;
  readonly currentItemRevision: number | null;
  readonly currentRevision: number | null;

  constructor(
    message: string,
    code: string,
    status: number,
    batchId: string | null = null,
    currentBatchRevision: number | null = null,
    currentItemRevision: number | null = null,
    currentRevision: number | null = null,
    options?: ErrorOptions,
  ) {
    super(message, options);
    this.name = "CollectionImportApiError";
    this.code = code;
    this.status = status;
    this.batchId = batchId;
    this.currentBatchRevision = currentBatchRevision;
    this.currentItemRevision = currentItemRevision;
    this.currentRevision = currentRevision;
  }
}

type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function responseError(response: Response): Promise<CollectionImportApiError> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return new CollectionImportApiError("批量导入服务暂时不可用，请稍后重试。", "HTTP_ERROR", response.status);
  }
  const error = isRecord(body) && isRecord(body.error) ? body.error : null;
  return new CollectionImportApiError(
    error && typeof error.message === "string" ? error.message : "批量请求没有完成，请稍后重试。",
    error && typeof error.code === "string" ? error.code : "HTTP_ERROR",
    response.status,
    error && typeof error.batch_id === "string" ? error.batch_id : null,
    error && typeof error.current_batch_revision === "number" ? error.current_batch_revision : null,
    error && typeof error.current_item_revision === "number" ? error.current_item_revision : null,
    error && typeof error.current_revision === "number" ? error.current_revision : null,
  );
}

async function requestJsonWithStatus<T>(fetchImpl: FetchLike, baseUrl: string, path: string, init?: RequestInit): Promise<{ body: T; status: number }> {
  let response: Response;
  try {
    response = await fetchImpl(`${baseUrl}${path}`, init);
  } catch (error) {
    throw new CollectionImportApiError(
      "无法连接批量导入服务，请确认后端已经启动。",
      "NETWORK_ERROR",
      0,
      null,
      null,
      null,
      null,
      { cause: error },
    );
  }
  if (!response.ok) throw await responseError(response);
  try {
    return { body: await response.json() as T, status: response.status };
  } catch (error) {
    throw new CollectionImportApiError(
      "服务返回了无法读取的批次数据。",
      "INVALID_RESPONSE",
      response.status,
      null,
      null,
      null,
      null,
      { cause: error },
    );
  }
}

async function requestJson<T>(fetchImpl: FetchLike, baseUrl: string, path: string, init?: RequestInit): Promise<T> {
  return (await requestJsonWithStatus<T>(fetchImpl, baseUrl, path, init)).body;
}

export interface CollectionImportApiClient {
  createBatch(payload: CollectionImportCreateRequest, idempotencyKey: string, signal?: AbortSignal): Promise<CollectionImportBatchSnapshot>;
  getActiveBatch(signal?: AbortSignal): Promise<CollectionImportBatchSnapshot | null>;
  getBatch(batchId: string, signal?: AbortSignal): Promise<CollectionImportBatchSnapshot>;
  getItem(batchId: string, batchItemId: string, signal?: AbortSignal): Promise<CollectionImportItemDetail>;
  updateItem(batchId: string, batchItemId: string, payload: CollectionImportItemUpdateRequest, signal?: AbortSignal): Promise<CollectionImportBatchSnapshot>;
  repreviewItem(batchId: string, batchItemId: string, payload: CollectionImportItemRepreviewRequest, signal?: AbortSignal): Promise<CollectionImportBatchSnapshot>;
  confirmBatch(batchId: string, payload: CollectionImportBatchConfirmRequest, signal?: AbortSignal): Promise<CollectionImportBatchSnapshot>;
  resumeBatch(batchId: string, payload: CollectionImportBatchCommandRequest, signal?: AbortSignal): Promise<CollectionImportBatchSnapshot>;
  cancelBatch(batchId: string, payload: CollectionImportBatchCommandRequest, signal?: AbortSignal): Promise<CollectionImportBatchCommandResult>;
}

export function createCollectionImportApiClient(options: { baseUrl?: string; fetchImpl?: FetchLike } = {}): CollectionImportApiClient {
  const baseUrl = (options.baseUrl ?? "").replace(/\/+$/, "");
  const fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  return {
    createBatch(payload, idempotencyKey, signal) {
      return requestJson<CollectionImportBatchSnapshot>(fetchImpl, baseUrl, "/api/v1/collection-import-batches", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify(payload),
        signal,
      });
    },
    async getActiveBatch(signal) {
      let response: Response;
      try {
        response = await fetchImpl(`${baseUrl}/api/v1/collection-import-batches/active`, { signal });
      } catch (error) {
        throw new CollectionImportApiError(
          "无法连接批量导入服务，请确认后端已经启动。",
          "NETWORK_ERROR",
          0,
          null,
          null,
          null,
          null,
          { cause: error },
        );
      }
      if (response.status === 204) return null;
      if (!response.ok) throw await responseError(response);
      try {
        return await response.json() as CollectionImportBatchSnapshot;
      } catch (error) {
        throw new CollectionImportApiError(
          "服务返回了无法读取的活动批次数据。",
          "INVALID_RESPONSE",
          response.status,
          null,
          null,
          null,
          null,
          { cause: error },
        );
      }
    },
    getBatch(batchId, signal) {
      return requestJson<CollectionImportBatchSnapshot>(
        fetchImpl,
        baseUrl,
        `/api/v1/collection-import-batches/${encodeURIComponent(batchId)}`,
        { signal },
      );
    },
    getItem(batchId, batchItemId, signal) {
      return requestJson<CollectionImportItemDetail>(
        fetchImpl,
        baseUrl,
        `/api/v1/collection-import-batches/${encodeURIComponent(batchId)}/items/${encodeURIComponent(batchItemId)}`,
        { signal },
      );
    },
    updateItem(batchId, batchItemId, payload, signal) {
      return requestJson<CollectionImportBatchSnapshot>(
        fetchImpl,
        baseUrl,
        `/api/v1/collection-import-batches/${encodeURIComponent(batchId)}/items/${encodeURIComponent(batchItemId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
          signal,
        },
      );
    },
    repreviewItem(batchId, batchItemId, payload, signal) {
      return requestJson<CollectionImportBatchSnapshot>(
        fetchImpl,
        baseUrl,
        `/api/v1/collection-import-batches/${encodeURIComponent(batchId)}/items/${encodeURIComponent(batchItemId)}/repreview`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
          signal,
        },
      );
    },
    confirmBatch(batchId, payload, signal) {
      return requestJson<CollectionImportBatchSnapshot>(
        fetchImpl,
        baseUrl,
        `/api/v1/collection-import-batches/${encodeURIComponent(batchId)}/confirm`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
          signal,
        },
      );
    },
    async resumeBatch(batchId, payload, signal) {
      const result = await requestJsonWithStatus<CollectionImportBatchSnapshot>(
        fetchImpl,
        baseUrl,
        `/api/v1/collection-import-batches/${encodeURIComponent(batchId)}/resume`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
          signal,
        },
      );
      if (result.status !== 202) {
        throw new CollectionImportApiError("恢复响应状态不符合批次合同。", "INVALID_RESPONSE", result.status);
      }
      return result.body;
    },
    async cancelBatch(batchId, payload, signal) {
      const result = await requestJsonWithStatus<CollectionImportBatchSnapshot>(
        fetchImpl,
        baseUrl,
        `/api/v1/collection-import-batches/${encodeURIComponent(batchId)}/cancel`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
          signal,
        },
      );
      if (result.status !== 200 && result.status !== 202) {
        throw new CollectionImportApiError("取消响应状态不符合批次合同。", "INVALID_RESPONSE", result.status);
      }
      return { snapshot: result.body, status: result.status };
    },
  };
}

export const collectionImportApi = createCollectionImportApiClient();
