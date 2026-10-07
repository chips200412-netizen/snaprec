export type CollectionSourceKind =
  | "video"
  | "article"
  | "webpage"
  | "audio"
  | "other";

export type CollectionPlatform =
  | "douyin"
  | "bilibili"
  | "xiaohongshu"
  | "youtube"
  | "web"
  | "other"
  | "local_upload";

export type MetadataStatus =
  | "recognized"
  | "generic"
  | "metadata_unavailable";

export type MetadataProvenance =
  | "open_graph"
  | "page_metadata"
  | "platform_public"
  | "page_description"
  | "platform_description"
  | "share_text"
  | "user"
  | "legacy_import"
  | "none";

export interface MetadataField {
  value: string;
  source: MetadataProvenance;
  fetched_at: string;
}

export interface SourceMetadata {
  title: MetadataField;
  author: MetadataField;
  cover_url: MetadataField;
  source_copy: MetadataField;
  platform_tags: Array<{
    value: string;
    source: "platform_public" | "page_metadata" | "share_text" | "legacy_import";
  }>;
  warnings: string[];
}

export interface OrganizationSuggestion {
  primary_category: string;
  secondary_category: string;
  tags: string[];
  basis: "public_metadata";
  method: "deterministic" | "llm";
  status: "generated" | "insufficient_metadata" | "failed";
}

export interface OrganizationConfirmation {
  primary_category: string;
  secondary_category: string;
  organization_tags: string[];
}

export interface InspirationDraft {
  content: string;
  input_mode: "text" | "voice";
  transcription_status: "not_applicable" | "completed";
}

export interface CollectionPreview {
  preview_id: string;
  original_input: string;
  source_url: string;
  canonical_url: string;
  identity_url: string;
  source_kind: CollectionSourceKind;
  platform: CollectionPlatform;
  metadata_status: MetadataStatus;
  metadata: SourceMetadata;
  organization_suggestion: OrganizationSuggestion;
  created_at: string;
  expires_at: string;
}

export interface InspirationTranscriptionDraft {
  content: string;
  input_mode: "voice";
  transcription_status: "draft";
}

export interface CollectionItemCreate {
  preview_id: string;
  selected_source_topic_indices?: number[];
  user_title: string | null;
  user_author?: string | null;
  user_cover_asset_id?: string | null;
  untitled_confirmed: boolean;
  organization_confirmation: OrganizationConfirmation;
  personal_tags: string[];
  inspiration: InspirationDraft | null;
}

export interface CollectionItemUpdate {
  expected_revision: number;
  user_title: string | null;
  user_author?: string | null;
  user_cover_asset_id?: string | null;
  organization_confirmation: OrganizationConfirmation;
  personal_tags: string[];
  inspiration: InspirationDraft | null;
}

export interface CollectionItem extends Omit<CollectionPreview, "preview_id" | "expires_at"> {
  id: string;
  selected_source_topic_indices?: number[] | null;
  user_title: string | null;
  user_author?: string | null;
  user_cover_asset_id?: string | null;
  display_title: string;
  organization_confirmation: OrganizationConfirmation;
  personal_tags: string[];
  inspiration: null | (InspirationDraft & {
    id: string;
    collection_item_id: string;
    created_at: string;
    updated_at: string;
  });
  deep_analysis_resource_key: string | null;
  revision: number;
  updated_at: string;
}

export interface CollectionListItem {
  id: string;
  display_title: string;
  platform: CollectionPlatform;
  primary_category: string;
  source_author?: string;
  user_author?: string | null;
  has_user_cover?: boolean;
  cover_url: string;
  created_at: string;
  updated_at: string;
}

export function previewCoverUrl(previewId: string): string {
  return previewId
    ? `/api/v1/collection-previews/${encodeURIComponent(previewId)}/cover`
    : "";
}

export function itemCoverUrl(itemId: string): string {
  return itemId
    ? `/api/v1/collection-items/${encodeURIComponent(itemId)}/cover`
    : "";
}

export function cachedItemCoverUrl(itemId: string): string {
  const url = itemCoverUrl(itemId);
  return url ? `${url}?cache=only` : "";
}

export function sourceItemCoverUrl(itemId: string): string {
  const url = itemCoverUrl(itemId);
  return url ? `${url}?source=only` : "";
}

export function userItemCoverUrl(itemId: string): string {
  const url = itemCoverUrl(itemId);
  return url ? `${url}?user=only` : "";
}

export interface CollectionListFacets {
  platforms: Array<{ platform: CollectionPlatform; count: number }>;
  categories: Array<{
    primary_category: string;
    count: number;
    children: Array<{ secondary_category: string; count: number }>;
  }>;
  tags: Array<{
    name: string;
    source: "platform" | "organization" | "personal";
    count: number;
  }>;
}

export interface CollectionSearchPage {
  items: CollectionListItem[];
  total: number;
  limit: number;
  next_cursor: string | null;
  facets: CollectionListFacets;
}

export interface UserCoverDraft {
  asset_id: string;
  claim_token: string;
  media_type: "image/webp";
  width: number;
  height: number;
  expires_at: string;
}

export type CollectionDeepAnalysisState =
  | "unavailable"
  | "ready"
  | "queued"
  | "running"
  | "completed"
  | "limited"
  | "failed"
  | "timed_out";

export interface CollectionDeepAnalysisSnapshot {
  material_id: string;
  analysis_job_id: string | null;
  state: CollectionDeepAnalysisState;
  config_version: string;
  job_revision: number;
  failed_stage: string;
  can_start: boolean;
  can_retry: boolean;
  can_view_result: boolean;
  result_id: string | null;
  result_revision: string;
  result_kind: "none" | "complete" | "limited";
  updated_at: string;
  limitation: string;
  error_code: string | null;
  error_message: string;
}

export interface CollectionListQuery {
  query?: string;
  platform?: CollectionPlatform;
  primaryCategory?: string;
  secondaryCategory?: string;
  tag?: string;
  tagSource?: "platform" | "organization" | "personal";
  limit?: number;
  cursor?: string;
}

export class CollectionApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly collectionItemId: string | null;
  readonly expectedRevision: number | null;
  readonly currentRevision: number | null;

  constructor(
    message: string,
    code: string,
    status: number,
    collectionItemId: string | null = null,
    options?: ErrorOptions,
    expectedRevision: number | null = null,
    currentRevision: number | null = null,
  ) {
    super(message, options);
    this.name = "CollectionApiError";
    this.code = code;
    this.status = status;
    this.collectionItemId = collectionItemId;
    this.expectedRevision = expectedRevision;
    this.currentRevision = currentRevision;
  }
}

type FetchLike = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function responseError(response: Response): Promise<CollectionApiError> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return new CollectionApiError("服务暂时不可用，请稍后重试。", "HTTP_ERROR", response.status);
  }
  const error = isObject(body) && isObject(body.error) ? body.error : null;
  const expectedRevision = error && typeof error.expected_revision === "number"
    ? error.expected_revision
    : null;
  const currentRevision = error && typeof error.current_revision === "number"
    ? error.current_revision
    : null;
  return new CollectionApiError(
    error && typeof error.message === "string" ? error.message : "请求没有完成，请稍后重试。",
    error && typeof error.code === "string" ? error.code : "HTTP_ERROR",
    response.status,
    error && typeof error.collection_item_id === "string"
      ? error.collection_item_id
      : null,
    undefined,
    expectedRevision,
    currentRevision,
  );
}

async function jsonRequest<T>(
  fetchImpl: FetchLike,
  baseUrl: string,
  path: string,
  init?: RequestInit,
): Promise<T> {
  let response: Response;
  try {
    response = await fetchImpl(`${baseUrl}${path}`, init);
  } catch (error) {
    throw new CollectionApiError(
      "无法连接收藏服务，请确认后端已经启动。",
      "NETWORK_ERROR",
      0,
      null,
      { cause: error },
    );
  }
  if (!response.ok) throw await responseError(response);
  try {
    return (await response.json()) as T;
  } catch (error) {
    throw new CollectionApiError(
      "服务返回了无法读取的数据。",
      "INVALID_RESPONSE",
      response.status,
      null,
      { cause: error },
    );
  }
}

export interface CollectionApiClient {
  createPreview(inputText: string, refreshMetadata?: boolean, signal?: AbortSignal): Promise<CollectionPreview>;
  transcribeInspiration(file: Blob, signal?: AbortSignal): Promise<InspirationTranscriptionDraft>;
  createItem(payload: CollectionItemCreate, idempotencyKey: string, signal?: AbortSignal, userCoverClaimToken?: string): Promise<CollectionItem>;
  updateItem(id: string, payload: CollectionItemUpdate, signal?: AbortSignal, userCoverClaimToken?: string): Promise<CollectionItem>;
  uploadUserCover(file: Blob, signal?: AbortSignal): Promise<UserCoverDraft>;
  getUserCover(assetId: string, claimToken: string, signal?: AbortSignal): Promise<Blob>;
  deleteUserCover(assetId: string, claimToken: string, signal?: AbortSignal): Promise<void>;
  getItem(id: string, signal?: AbortSignal): Promise<CollectionItem>;
  listItems(query?: CollectionListQuery, signal?: AbortSignal): Promise<CollectionSearchPage>;
  getDeepAnalysis(id: string, signal?: AbortSignal): Promise<CollectionDeepAnalysisSnapshot>;
  startDeepAnalysis(id: string, signal?: AbortSignal): Promise<CollectionDeepAnalysisSnapshot>;
  retryDeepAnalysis(id: string, idempotencyKey: string, signal?: AbortSignal): Promise<CollectionDeepAnalysisSnapshot>;
}

export function createCollectionApiClient(options: {
  baseUrl?: string;
  fetchImpl?: FetchLike;
} = {}): CollectionApiClient {
  const baseUrl = (options.baseUrl ?? "").replace(/\/+$/, "");
  const fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  return {
    createPreview(inputText, refreshMetadata = false, signal) {
      return jsonRequest<CollectionPreview>(fetchImpl, baseUrl, "/api/v1/collection-previews", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input_text: inputText, refresh_metadata: refreshMetadata }),
        signal,
      });
    },
    transcribeInspiration(file, signal) {
      const body = new FormData();
      body.append("file", file, "inspiration.webm");
      return jsonRequest<InspirationTranscriptionDraft>(
        fetchImpl,
        baseUrl,
        "/api/v1/inspiration-transcriptions",
        { method: "POST", body, signal },
      );
    },
    createItem(payload, idempotencyKey, signal, userCoverClaimToken) {
      return jsonRequest<CollectionItem>(fetchImpl, baseUrl, "/api/v1/collection-items", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
          ...(userCoverClaimToken ? { "X-User-Cover-Claim-Token": userCoverClaimToken } : {}),
        },
        body: JSON.stringify(payload),
        signal,
      });
    },
    updateItem(id, payload, signal, userCoverClaimToken) {
      return jsonRequest<CollectionItem>(
        fetchImpl,
        baseUrl,
        `/api/v1/collection-items/${encodeURIComponent(id)}`,
        {
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
            ...(userCoverClaimToken ? { "X-User-Cover-Claim-Token": userCoverClaimToken } : {}),
          },
          body: JSON.stringify(payload),
          signal,
        },
      );
    },
    uploadUserCover(file, signal) {
      const body = new FormData();
      body.append("file", file, "user-cover");
      return jsonRequest<UserCoverDraft>(
        fetchImpl,
        baseUrl,
        "/api/v1/user-cover-assets",
        { method: "POST", body, signal },
      );
    },
    async getUserCover(assetId, claimToken, signal) {
      let response: Response;
      try {
        response = await fetchImpl(
          `${baseUrl}/api/v1/user-cover-assets/${encodeURIComponent(assetId)}/content`,
          { headers: { "X-User-Cover-Claim-Token": claimToken }, signal },
        );
      } catch (error) {
        throw new CollectionApiError("无法读取暂存图片。", "NETWORK_ERROR", 0, null, { cause: error });
      }
      if (!response.ok) throw await responseError(response);
      return response.blob();
    },
    async deleteUserCover(assetId, claimToken, signal) {
      let response: Response;
      try {
        response = await fetchImpl(
          `${baseUrl}/api/v1/user-cover-assets/${encodeURIComponent(assetId)}`,
          { method: "DELETE", headers: { "X-User-Cover-Claim-Token": claimToken }, signal },
        );
      } catch (error) {
        throw new CollectionApiError("无法清理暂存图片。", "NETWORK_ERROR", 0, null, { cause: error });
      }
      if (!response.ok) throw await responseError(response);
    },
    getItem(id, signal) {
      return jsonRequest<CollectionItem>(
        fetchImpl,
        baseUrl,
        `/api/v1/collection-items/${encodeURIComponent(id)}`,
        { signal },
      );
    },
    listItems(query = {}, signal) {
      const parameters = new URLSearchParams();
      if (query.query) parameters.set("query", query.query);
      if (query.platform) parameters.set("platform", query.platform);
      if (query.primaryCategory) parameters.set("primary_category", query.primaryCategory);
      if (query.secondaryCategory) parameters.set("secondary_category", query.secondaryCategory);
      if (query.tag) parameters.set("tag", query.tag);
      if (query.tagSource) parameters.set("tag_source", query.tagSource);
      if (query.limit !== undefined) parameters.set("limit", String(query.limit));
      if (query.cursor) parameters.set("cursor", query.cursor);
      const suffix = parameters.size ? `?${parameters.toString()}` : "";
      return jsonRequest<CollectionSearchPage>(
        fetchImpl,
        baseUrl,
        `/api/v1/collection-items${suffix}`,
        { signal },
      );
    },
    getDeepAnalysis(id, signal) {
      return jsonRequest<CollectionDeepAnalysisSnapshot>(
        fetchImpl,
        baseUrl,
        `/api/v1/collection-items/${encodeURIComponent(id)}/deep-analysis`,
        { signal },
      );
    },
    startDeepAnalysis(id, signal) {
      return jsonRequest<CollectionDeepAnalysisSnapshot>(
        fetchImpl,
        baseUrl,
        `/api/v1/collection-items/${encodeURIComponent(id)}/deep-analysis`,
        { method: "POST", signal },
      );
    },
    retryDeepAnalysis(id, idempotencyKey, signal) {
      return jsonRequest<CollectionDeepAnalysisSnapshot>(
        fetchImpl,
        baseUrl,
        `/api/v1/collection-items/${encodeURIComponent(id)}/deep-analysis/retry`,
        {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey },
          signal,
        },
      );
    },
  };
}

export const collectionApi = createCollectionApiClient();
