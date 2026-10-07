import type {
  AutomaticTag,
  AutomaticTagging,
  Evidence as UiEvidence,
  KnowledgePoint,
  OutlineItem,
  PlaybackCapability,
  ParseStatus,
  TagSource,
  VideoFacets,
  VideoRecord,
  VideoQuestion,
} from "./types";

export type ApiPlatform = "local_upload" | "bilibili" | "douyin";
export type SubtitleSource =
  | "official"
  | "public_page"
  | "asr"
  | "user_upload"
  | "none";
export type ClaimType =
  | "video_fact"
  | "creator_opinion"
  | "model_inference";
export type ItemType =
  | "key_point"
  | "important_data"
  | "case"
  | "argument"
  | "step"
  | "risk"
  | "quote"
  | "supplementary_context";
export type MentionStatus =
  | "explicit"
  | "inferred"
  | "not_mentioned"
  | "unknown_incomplete_transcript";
export type JobStatus =
  | "queued"
  | "resolving"
  | "fetching_metadata"
  | "fetching_subtitles"
  | "transcribing"
  | "cleaning"
  | "extracting"
  | "saving"
  | "completed"
  | "completed_with_warnings"
  | "failed"
  | "cancelled";

export interface ApiJob {
  id: string;
  status: JobStatus;
  progress: number;
  error_code: string | null;
  message: string;
  video_id: string | null;
  retry_count: number;
  max_retries: number;
  retryable: boolean;
  cancel_requested: boolean;
  warnings: string[];
  checkpoint: string;
  heartbeat_at: string;
  revision: number;
  lease_owner: string;
  lease_expires_at: string;
}

export type JobBatchStatus =
  | "queued"
  | "running"
  | "completed"
  | "completed_with_issues";

export interface ApiJobBatchItem {
  position: number;
  title: string;
  platform: ApiPlatform;
  job: ApiJob;
}

export interface ApiJobBatch {
  id: string;
  status: JobBatchStatus;
  total: number;
  completed: number;
  active: number;
  failed: number;
  cancelled: number;
  created_at: string;
  updated_at: string;
  items: ApiJobBatchItem[];
}

export interface CreateJobBatchItem {
  job_id: string;
  title: string;
  platform: ApiPlatform;
}

export interface ApiLibraryVideo {
  platform: ApiPlatform;
  video_id: string;
  source_url: string;
  title: string;
  author: string;
  description: string;
  summary: string;
  cover_url: string;
  subtitle_source: SubtitleSource;
  source_tags: string[];
  warnings: string[];
  personal_tags: string[];
  automatic_tagging?: ApiAutomaticTagging;
  classification?: ApiVideoClassification;
  spark: ApiPersonalSpark | null;
  favorite: boolean;
  created_at: string;
  updated_at: string;
}

export interface ApiAutomaticTag {
  name: string;
  confidence: number;
  generation_method: "llm" | "deterministic";
}

export interface ApiAutomaticTagging {
  status:
    | "generated"
    | "skipped_no_transcript"
    | "failed"
    | "not_generated";
  tags: ApiAutomaticTag[];
  generator_id: string;
  generator_version: string;
  transcript_hash: string;
  generated_at: string;
  warning: string;
}

export interface ApiVideoClassification {
  primary_category: string;
  secondary_category: string;
  updated_at: string;
}

export interface ApiTagFacet {
  name: string;
  source: TagSource;
  count: number;
}

export interface ApiSecondaryCategoryFacet {
  secondary_category: string;
  count: number;
}

export interface ApiCategoryFacet {
  primary_category: string;
  count: number;
  children: ApiSecondaryCategoryFacet[];
}

export interface ApiLibraryFacets {
  tags: ApiTagFacet[];
  categories: ApiCategoryFacet[];
}

export interface ApiVideoSearchPage {
  items: ApiLibraryVideo[];
  total: number;
  limit: number;
  offset: number;
  facets: ApiLibraryFacets;
}

export interface ApiSegment {
  id: string;
  start: number | null;
  end: number | null;
  text: string;
}

export interface ApiEvidence {
  id: string;
  claim: string;
  claim_type: ClaimType;
  evidence: string;
  segment_ids: string[];
  start_time: number | null;
  end_time: number | null;
  confidence: number;
}

export interface ApiExtractionItem {
  text: string;
  item_type: ItemType;
  claim_type: ClaimType;
  evidence_refs: string[];
  confidence: number;
}

export interface ApiFullExtraction {
  key_points: ApiExtractionItem[];
  important_data: ApiExtractionItem[];
  cases_and_arguments: ApiExtractionItem[];
  steps: ApiExtractionItem[];
  risks: ApiExtractionItem[];
  quotes: ApiExtractionItem[];
}

export interface ApiFocusedAnswer {
  mention_status: MentionStatus;
  is_mentioned: boolean | null;
  direct_answer: string;
  key_points: ApiExtractionItem[];
  supporting_segments: ApiEvidence[];
  supplementary_context: ApiExtractionItem[];
  missing_information: string[];
}

export interface ApiVideoResult {
  platform: ApiPlatform;
  source_url: string;
  canonical_url: string;
  video_id: string;
  author: string;
  title: string;
  description: string;
  tags: string[];
  duration: number;
  cover_url: string;
  subtitle_source: SubtitleSource;
  raw_transcript: string;
  clean_transcript: string;
  segments: ApiSegment[];
  focus_query: string;
  extraction_mode: "full" | "focused";
  summary: string;
  full_extraction: ApiFullExtraction;
  evidence: ApiEvidence[];
  focused_answer: ApiFocusedAnswer | null;
  automatic_tagging?: ApiAutomaticTagging;
  warnings: string[];
}

export interface ApiFocusedHistoryItem {
  focus_query: string;
  query_hash: string;
  focused_answer: ApiFocusedAnswer;
  created_at: string;
}

export interface ApiVideoQuestion {
  id: number;
  question: string;
  question_hash: string;
  answer: ApiFocusedAnswer;
  created_at: string;
}

export interface ApiPersonalSpark {
  id: number;
  kind: "spark";
  target_key: null;
  content: string;
  author: string;
  created_at: string;
  updated_at: string;
}

export interface ApiPersonalAnnotation {
  id: number;
  kind: "annotation";
  target_key: string;
  target_type: "claim" | "step";
  content: string;
  author: string;
  created_at: string;
  updated_at: string;
}

export interface ApiPersonalNotes {
  spark: ApiPersonalSpark | null;
  annotations: ApiPersonalAnnotation[];
}

export interface ApiAnnotationTarget {
  target_key: string;
  display_key: string;
  target_type: "claim" | "step";
  mode: "full" | "focused";
  focus_query: string;
  query_hash: string;
}

export interface ApiVideoDetail {
  result: ApiVideoResult;
  favorite: boolean;
  personal_tags: string[];
  automatic_tagging?: ApiAutomaticTagging;
  classification?: ApiVideoClassification;
  personal_notes: ApiPersonalNotes;
  annotation_targets: ApiAnnotationTarget[];
  focused_history: ApiFocusedHistoryItem[];
  question_history: ApiVideoQuestion[];
  playback: ApiPlaybackCapability;
}

export interface ApiPlaybackCapability {
  availability: "available" | "unavailable";
  kind: "retained_local" | "none";
  stream_url: string;
  mime_type: string;
  size_bytes: number;
  supports_range: boolean;
  reason:
    | "available"
    | "not_retained"
    | "unsupported_source"
    | "unsupported_browser_format"
    | "missing_file";
}

export interface ApiResolutionPreview {
  platform: ApiPlatform;
  source_url: string;
  canonical_url: string;
  video_id: string;
  author: string;
  title: string;
  description: string;
  tags: string[];
  duration: number;
  cover_url: string;
  warnings: string[];
}

export interface ListVideosParams {
  query?: string;
  platform?: ApiPlatform;
  tag?: string;
  tagSource?: TagSource;
  primaryCategory?: string;
  secondaryCategory?: string;
  favorite?: boolean;
  limit?: number;
  offset?: number;
  signal?: AbortSignal;
}

export interface RequestOptions {
  signal?: AbortSignal;
}

export interface CreateUploadJobOptions extends RequestOptions {
  retainMedia?: boolean;
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(message: string, code: string, status: number, options?: ErrorOptions) {
    super(message, options);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

type FetchLike = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export interface ApiClient {
  listVideos(params?: ListVideosParams): Promise<ApiVideoSearchPage>;
  getVideoDetail(
    videoId: string,
    platform?: ApiPlatform,
    options?: RequestOptions,
  ): Promise<ApiVideoDetail>;
  createResolutionJob(
    inputText: string,
    focusQuery?: string,
    options?: RequestOptions,
  ): Promise<ApiJob>;
  previewResolution(
    inputText: string,
    options?: RequestOptions,
  ): Promise<ApiResolutionPreview>;
  createUploadJob(
    file: File,
    subtitle?: File,
    options?: CreateUploadJobOptions,
  ): Promise<ApiJob>;
  deleteRetainedMedia(
    videoId: string,
    platform?: ApiPlatform,
    options?: RequestOptions,
  ): Promise<ApiPlaybackCapability>;
  getJob(jobId: string, options?: RequestOptions): Promise<ApiJob>;
  retryJob(jobId: string, options?: RequestOptions): Promise<ApiJob>;
  cancelJob(jobId: string, options?: RequestOptions): Promise<ApiJob>;
  createJobBatch(
    items: CreateJobBatchItem[],
    options?: RequestOptions,
  ): Promise<ApiJobBatch>;
  listJobBatches(
    limit?: number,
    options?: RequestOptions,
  ): Promise<ApiJobBatch[]>;
  getJobBatch(
    batchId: string,
    options?: RequestOptions,
  ): Promise<ApiJobBatch>;
  cancelJobBatchItem(
    batchId: string,
    jobId: string,
    options?: RequestOptions,
  ): Promise<ApiJobBatch>;
  setFavorite(
    videoId: string,
    favorite: boolean,
    platform?: ApiPlatform,
    options?: RequestOptions,
  ): Promise<ApiVideoDetail>;
  updateTags(
    videoId: string,
    tags: string[],
    operation: "add" | "remove",
    platform?: ApiPlatform,
    options?: RequestOptions,
  ): Promise<ApiVideoDetail>;
  updateClassification(
    videoId: string,
    primaryCategory: string,
    secondaryCategory?: string,
    platform?: ApiPlatform,
    options?: RequestOptions,
  ): Promise<ApiVideoDetail>;
  generateAutomaticTags(
    videoId: string,
    platform?: ApiPlatform,
    options?: RequestOptions,
  ): Promise<ApiVideoDetail>;
  getPersonalNotes(
    videoId: string,
    platform?: ApiPlatform,
    options?: RequestOptions,
  ): Promise<ApiPersonalNotes>;
  upsertSpark(
    videoId: string,
    content: string,
    author?: string,
    platform?: ApiPlatform,
    options?: RequestOptions,
  ): Promise<ApiPersonalNotes>;
  deleteSpark(
    videoId: string,
    platform?: ApiPlatform,
    options?: RequestOptions,
  ): Promise<ApiPersonalNotes>;
  createAnnotation(
    videoId: string,
    targetKey: string,
    content: string,
    author?: string,
    platform?: ApiPlatform,
    options?: RequestOptions,
  ): Promise<ApiPersonalNotes>;
  updateAnnotation(
    videoId: string,
    noteId: string,
    content: string,
    author?: string,
    platform?: ApiPlatform,
    options?: RequestOptions,
  ): Promise<ApiPersonalNotes>;
  deleteAnnotation(
    videoId: string,
    noteId: string,
    platform?: ApiPlatform,
    options?: RequestOptions,
  ): Promise<ApiPersonalNotes>;
  createFocusedExtraction(
    videoId: string,
    focusQuery: string,
    platform?: ApiPlatform,
    options?: RequestOptions,
  ): Promise<ApiVideoResult>;
  askQuestion(
    videoId: string,
    question: string,
    platform?: ApiPlatform,
    options?: RequestOptions,
  ): Promise<ApiVideoQuestion>;
  exportMarkdown(
    videoId: string,
    platform?: ApiPlatform,
    exportOptions?: {
      view?: "all" | "overview" | "steps" | "transcript";
      includePersonal?: boolean;
      focusQueryHash?: string;
    },
    options?: RequestOptions,
  ): Promise<string>;
}

export interface CreateApiClientOptions {
  baseUrl?: string;
  fetchImpl?: FetchLike;
}

function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw invalidResponse(`${label} 必须是对象。`);
  }
  return value as Record<string, unknown>;
}

function requiredString(
  value: Record<string, unknown>,
  key: string,
  label: string,
): string {
  if (typeof value[key] !== "string") {
    throw invalidResponse(`${label}.${key} 必须是字符串。`);
  }
  return value[key];
}

function nullableString(
  value: Record<string, unknown>,
  key: string,
  label: string,
): string | null {
  if (value[key] !== null && typeof value[key] !== "string") {
    throw invalidResponse(`${label}.${key} 必须是字符串或 null。`);
  }
  return value[key] as string | null;
}

function requiredNumber(
  value: Record<string, unknown>,
  key: string,
  label: string,
): number {
  if (typeof value[key] !== "number" || !Number.isFinite(value[key])) {
    throw invalidResponse(`${label}.${key} 必须是有限数字。`);
  }
  return value[key];
}

function nullableNumber(
  value: Record<string, unknown>,
  key: string,
  label: string,
): number | null {
  if (
    value[key] !== null &&
    (typeof value[key] !== "number" || !Number.isFinite(value[key]))
  ) {
    throw invalidResponse(`${label}.${key} 必须是有限数字或 null。`);
  }
  return value[key] as number | null;
}

function requiredBoolean(
  value: Record<string, unknown>,
  key: string,
  label: string,
): boolean {
  if (typeof value[key] !== "boolean") {
    throw invalidResponse(`${label}.${key} 必须是布尔值。`);
  }
  return value[key];
}

function requiredArray(
  value: Record<string, unknown>,
  key: string,
  label: string,
): unknown[] {
  if (!Array.isArray(value[key])) {
    throw invalidResponse(`${label}.${key} 必须是数组。`);
  }
  return value[key];
}

function stringArray(
  value: Record<string, unknown>,
  key: string,
  label: string,
): string[] {
  const items = requiredArray(value, key, label);
  if (!items.every((item) => typeof item === "string")) {
    throw invalidResponse(`${label}.${key} 必须是字符串数组。`);
  }
  return items as string[];
}

function enumValue<T extends string>(
  value: unknown,
  allowed: readonly T[],
  label: string,
): T {
  if (typeof value !== "string" || !allowed.includes(value as T)) {
    throw invalidResponse(`${label} 含有未知枚举值。`);
  }
  return value as T;
}

function invalidResponse(message: string): ApiError {
  return new ApiError(message, "INVALID_RESPONSE", 0);
}

const platforms = ["local_upload", "bilibili", "douyin"] as const;
const subtitleSources = [
  "official",
  "public_page",
  "asr",
  "user_upload",
  "none",
] as const;
const claimTypes = [
  "video_fact",
  "creator_opinion",
  "model_inference",
] as const;
const itemTypes = [
  "key_point",
  "important_data",
  "case",
  "argument",
  "step",
  "risk",
  "quote",
  "supplementary_context",
] as const;
const mentionStatuses = [
  "explicit",
  "inferred",
  "not_mentioned",
  "unknown_incomplete_transcript",
] as const;
const jobStatuses = [
  "queued",
  "resolving",
  "fetching_metadata",
  "fetching_subtitles",
  "transcribing",
  "cleaning",
  "extracting",
  "saving",
  "completed",
  "completed_with_warnings",
  "failed",
  "cancelled",
] as const;
const jobBatchStatuses = [
  "queued",
  "running",
  "completed",
  "completed_with_issues",
] as const;
const automaticTagStatuses = [
  "generated",
  "skipped_no_transcript",
  "failed",
  "not_generated",
] as const;
const tagSources = ["platform", "automatic", "personal"] as const;
const defaultAutomaticTagging: ApiAutomaticTagging = {
  status: "not_generated",
  tags: [],
  generator_id: "",
  generator_version: "",
  transcript_hash: "",
  generated_at: "",
  warning: "",
};
const defaultClassification: ApiVideoClassification = {
  primary_category: "暂未分类",
  secondary_category: "",
  updated_at: "",
};

function parseJob(value: unknown): ApiJob {
  const object = asRecord(value, "job");
  return {
    id: requiredString(object, "id", "job"),
    status: enumValue(object.status, jobStatuses, "job.status"),
    progress: requiredNumber(object, "progress", "job"),
    error_code: nullableString(object, "error_code", "job"),
    message: requiredString(object, "message", "job"),
    video_id: nullableString(object, "video_id", "job"),
    retry_count: requiredNumber(object, "retry_count", "job"),
    max_retries: requiredNumber(object, "max_retries", "job"),
    retryable: requiredBoolean(object, "retryable", "job"),
    cancel_requested: requiredBoolean(object, "cancel_requested", "job"),
    warnings: stringArray(object, "warnings", "job"),
    checkpoint: requiredString(object, "checkpoint", "job"),
    heartbeat_at: requiredString(object, "heartbeat_at", "job"),
    revision: requiredNumber(object, "revision", "job"),
    lease_owner: requiredString(object, "lease_owner", "job"),
    lease_expires_at: requiredString(object, "lease_expires_at", "job"),
  };
}

function parseJobBatch(value: unknown): ApiJobBatch {
  const object = asRecord(value, "job_batch");
  const items = requiredArray(object, "items", "job_batch").map(
    (item, index) => {
      const label = `job_batch.items[${index}]`;
      const itemObject = asRecord(item, label);
      return {
        position: requiredNumber(itemObject, "position", label),
        title: requiredString(itemObject, "title", label),
        platform: enumValue(
          itemObject.platform,
          platforms,
          `${label}.platform`,
        ),
        job: parseJob(itemObject.job),
      };
    },
  );
  const batch: ApiJobBatch = {
    id: requiredString(object, "id", "job_batch"),
    status: enumValue(
      object.status,
      jobBatchStatuses,
      "job_batch.status",
    ),
    total: requiredNumber(object, "total", "job_batch"),
    completed: requiredNumber(object, "completed", "job_batch"),
    active: requiredNumber(object, "active", "job_batch"),
    failed: requiredNumber(object, "failed", "job_batch"),
    cancelled: requiredNumber(object, "cancelled", "job_batch"),
    created_at: requiredString(object, "created_at", "job_batch"),
    updated_at: requiredString(object, "updated_at", "job_batch"),
    items,
  };
  if (
    batch.total !== batch.items.length ||
    batch.completed +
      batch.active +
      batch.failed +
      batch.cancelled !==
      batch.total
  ) {
    throw invalidResponse("job_batch 计数与任务数量不一致。");
  }
  return batch;
}

function parseJobBatchList(value: unknown): ApiJobBatch[] {
  if (!Array.isArray(value)) {
    throw invalidResponse("job_batches 必须是数组。");
  }
  return value.map(parseJobBatch);
}

function parseAutomaticTag(
  value: unknown,
  label: string,
): ApiAutomaticTag {
  const object = asRecord(value, label);
  return {
    name: requiredString(object, "name", label),
    confidence: requiredNumber(object, "confidence", label),
    generation_method: enumValue(
      object.generation_method,
      ["llm", "deterministic"] as const,
      `${label}.generation_method`,
    ),
  };
}

function parseAutomaticTagging(
  value: unknown,
  label: string,
): ApiAutomaticTagging {
  const object = asRecord(value, label);
  return {
    status: enumValue(
      object.status,
      automaticTagStatuses,
      `${label}.status`,
    ),
    tags: requiredArray(object, "tags", label).map((item, index) =>
      parseAutomaticTag(item, `${label}.tags[${index}]`),
    ),
    generator_id: requiredString(object, "generator_id", label),
    generator_version: requiredString(object, "generator_version", label),
    transcript_hash: requiredString(object, "transcript_hash", label),
    generated_at: requiredString(object, "generated_at", label),
    warning: requiredString(object, "warning", label),
  };
}

function parseVideoClassification(
  value: unknown,
  label: string,
): ApiVideoClassification {
  const object = asRecord(value, label);
  return {
    primary_category: requiredString(object, "primary_category", label),
    secondary_category: requiredString(object, "secondary_category", label),
    updated_at: requiredString(object, "updated_at", label),
  };
}

function parseLibraryVideo(value: unknown, index: number): ApiLibraryVideo {
  const label = `videos.items[${index}]`;
  const object = asRecord(value, label);
  return {
    platform: enumValue(object.platform, platforms, `${label}.platform`),
    video_id: requiredString(object, "video_id", label),
    source_url: requiredString(object, "source_url", label),
    title: requiredString(object, "title", label),
    author: requiredString(object, "author", label),
    description: requiredString(object, "description", label),
    summary: requiredString(object, "summary", label),
    cover_url: requiredString(object, "cover_url", label),
    subtitle_source: enumValue(
      object.subtitle_source,
      subtitleSources,
      `${label}.subtitle_source`,
    ),
    source_tags: stringArray(object, "source_tags", label),
    warnings: stringArray(object, "warnings", label),
    personal_tags: stringArray(object, "personal_tags", label),
    automatic_tagging:
      object.automatic_tagging === undefined
        ? { ...defaultAutomaticTagging, tags: [] }
        : parseAutomaticTagging(
            object.automatic_tagging,
            `${label}.automatic_tagging`,
          ),
    classification:
      object.classification === undefined
        ? { ...defaultClassification }
        : parseVideoClassification(
            object.classification,
            `${label}.classification`,
          ),
    spark:
      object.spark === null
        ? null
        : parsePersonalSpark(object.spark, `${label}.spark`),
    favorite: requiredBoolean(object, "favorite", label),
    created_at: requiredString(object, "created_at", label),
    updated_at: requiredString(object, "updated_at", label),
  };
}

function parseLibraryFacets(value: unknown): ApiLibraryFacets {
  const label = "videos.facets";
  const object = asRecord(value, label);
  return {
    tags: requiredArray(object, "tags", label).map((item, index) => {
      const itemLabel = `${label}.tags[${index}]`;
      const tag = asRecord(item, itemLabel);
      return {
        name: requiredString(tag, "name", itemLabel),
        source: enumValue(tag.source, tagSources, `${itemLabel}.source`),
        count: requiredNumber(tag, "count", itemLabel),
      };
    }),
    categories: requiredArray(object, "categories", label).map(
      (item, index) => {
        const itemLabel = `${label}.categories[${index}]`;
        const category = asRecord(item, itemLabel);
        return {
          primary_category: requiredString(
            category,
            "primary_category",
            itemLabel,
          ),
          count: requiredNumber(category, "count", itemLabel),
          children: requiredArray(category, "children", itemLabel).map(
            (child, childIndex) => {
              const childLabel = `${itemLabel}.children[${childIndex}]`;
              const childObject = asRecord(child, childLabel);
              return {
                secondary_category: requiredString(
                  childObject,
                  "secondary_category",
                  childLabel,
                ),
                count: requiredNumber(childObject, "count", childLabel),
              };
            },
          ),
        };
      },
    ),
  };
}

function parseVideoSearchPage(value: unknown): ApiVideoSearchPage {
  const object = asRecord(value, "videos");
  return {
    items: requiredArray(object, "items", "videos").map(parseLibraryVideo),
    total: requiredNumber(object, "total", "videos"),
    limit: requiredNumber(object, "limit", "videos"),
    offset: requiredNumber(object, "offset", "videos"),
    facets:
      object.facets === undefined
        ? { tags: [], categories: [] }
        : parseLibraryFacets(object.facets),
  };
}

function parseSegment(value: unknown, index: number): ApiSegment {
  const label = `result.segments[${index}]`;
  const object = asRecord(value, label);
  return {
    id: requiredString(object, "id", label),
    start: nullableNumber(object, "start", label),
    end: nullableNumber(object, "end", label),
    text: requiredString(object, "text", label),
  };
}

function parseEvidence(value: unknown, label: string): ApiEvidence {
  const object = asRecord(value, label);
  return {
    id: requiredString(object, "id", label),
    claim: requiredString(object, "claim", label),
    claim_type: enumValue(object.claim_type, claimTypes, `${label}.claim_type`),
    evidence: requiredString(object, "evidence", label),
    segment_ids: stringArray(object, "segment_ids", label),
    start_time: nullableNumber(object, "start_time", label),
    end_time: nullableNumber(object, "end_time", label),
    confidence: requiredNumber(object, "confidence", label),
  };
}

function parseExtractionItem(
  value: unknown,
  label: string,
): ApiExtractionItem {
  const object = asRecord(value, label);
  return {
    text: requiredString(object, "text", label),
    item_type: enumValue(object.item_type, itemTypes, `${label}.item_type`),
    claim_type: enumValue(object.claim_type, claimTypes, `${label}.claim_type`),
    evidence_refs: stringArray(object, "evidence_refs", label),
    confidence: requiredNumber(object, "confidence", label),
  };
}

function extractionItems(
  object: Record<string, unknown>,
  key: string,
  label: string,
): ApiExtractionItem[] {
  return requiredArray(object, key, label).map((item, index) =>
    parseExtractionItem(item, `${label}.${key}[${index}]`),
  );
}

function parseFullExtraction(value: unknown): ApiFullExtraction {
  const label = "result.full_extraction";
  const object = asRecord(value, label);
  return {
    key_points: extractionItems(object, "key_points", label),
    important_data: extractionItems(object, "important_data", label),
    cases_and_arguments: extractionItems(object, "cases_and_arguments", label),
    steps: extractionItems(object, "steps", label),
    risks: extractionItems(object, "risks", label),
    quotes: extractionItems(object, "quotes", label),
  };
}

function parseFocusedAnswer(
  value: unknown,
  label: string,
): ApiFocusedAnswer {
  const object = asRecord(value, label);
  const isMentioned = object.is_mentioned;
  if (
    isMentioned !== null &&
    typeof isMentioned !== "boolean"
  ) {
    throw invalidResponse(`${label}.is_mentioned 必须是布尔值或 null。`);
  }
  return {
    mention_status: enumValue(
      object.mention_status,
      mentionStatuses,
      `${label}.mention_status`,
    ),
    is_mentioned: isMentioned,
    direct_answer: requiredString(object, "direct_answer", label),
    key_points: extractionItems(object, "key_points", label),
    supporting_segments: requiredArray(
      object,
      "supporting_segments",
      label,
    ).map((item, index) =>
      parseEvidence(item, `${label}.supporting_segments[${index}]`),
    ),
    supplementary_context: extractionItems(
      object,
      "supplementary_context",
      label,
    ),
    missing_information: stringArray(object, "missing_information", label),
  };
}

function parseVideoResult(value: unknown): ApiVideoResult {
  const label = "result";
  const object = asRecord(value, label);
  return {
    platform: enumValue(object.platform, platforms, `${label}.platform`),
    source_url: requiredString(object, "source_url", label),
    canonical_url: requiredString(object, "canonical_url", label),
    video_id: requiredString(object, "video_id", label),
    author: requiredString(object, "author", label),
    title: requiredString(object, "title", label),
    description: requiredString(object, "description", label),
    tags: stringArray(object, "tags", label),
    duration: requiredNumber(object, "duration", label),
    cover_url: requiredString(object, "cover_url", label),
    subtitle_source: enumValue(
      object.subtitle_source,
      subtitleSources,
      `${label}.subtitle_source`,
    ),
    raw_transcript: requiredString(object, "raw_transcript", label),
    clean_transcript: requiredString(object, "clean_transcript", label),
    segments: requiredArray(object, "segments", label).map(parseSegment),
    focus_query: requiredString(object, "focus_query", label),
    extraction_mode: enumValue(
      object.extraction_mode,
      ["full", "focused"] as const,
      `${label}.extraction_mode`,
    ),
    summary: requiredString(object, "summary", label),
    full_extraction: parseFullExtraction(object.full_extraction),
    evidence: requiredArray(object, "evidence", label).map((item, index) =>
      parseEvidence(item, `${label}.evidence[${index}]`),
    ),
    focused_answer:
      object.focused_answer === null
        ? null
        : parseFocusedAnswer(object.focused_answer, `${label}.focused_answer`),
    automatic_tagging:
      object.automatic_tagging === undefined
        ? { ...defaultAutomaticTagging, tags: [] }
        : parseAutomaticTagging(
            object.automatic_tagging,
            `${label}.automatic_tagging`,
          ),
    warnings: stringArray(object, "warnings", label),
  };
}

function parseFocusedHistoryItem(
  value: unknown,
  index: number,
): ApiFocusedHistoryItem {
  const label = `detail.focused_history[${index}]`;
  const object = asRecord(value, label);
  return {
    focus_query: requiredString(object, "focus_query", label),
    query_hash: requiredString(object, "query_hash", label),
    focused_answer: parseFocusedAnswer(
      object.focused_answer,
      `${label}.focused_answer`,
    ),
    created_at: requiredString(object, "created_at", label),
  };
}

function parseVideoQuestion(
  value: unknown,
  index = 0,
): ApiVideoQuestion {
  const label = `detail.question_history[${index}]`;
  const object = asRecord(value, label);
  return {
    id: requiredNumber(object, "id", label),
    question: requiredString(object, "question", label),
    question_hash: requiredString(object, "question_hash", label),
    answer: parseFocusedAnswer(object.answer, `${label}.answer`),
    created_at: requiredString(object, "created_at", label),
  };
}

function parsePersonalSpark(
  value: unknown,
  label: string,
): ApiPersonalSpark {
  const object = asRecord(value, label);
  if (object.kind !== "spark" || object.target_key !== null) {
    throw invalidResponse(`${label} is not a spark note`);
  }
  return {
    id: requiredNumber(object, "id", label),
    kind: "spark",
    target_key: null,
    content: requiredString(object, "content", label),
    author: requiredString(object, "author", label),
    created_at: requiredString(object, "created_at", label),
    updated_at: requiredString(object, "updated_at", label),
  };
}

function parsePersonalAnnotation(
  value: unknown,
  label: string,
): ApiPersonalAnnotation {
  const object = asRecord(value, label);
  if (object.kind !== "annotation") {
    throw invalidResponse(`${label} is not an annotation`);
  }
  return {
    id: requiredNumber(object, "id", label),
    kind: "annotation",
    target_key: requiredString(object, "target_key", label),
    target_type: enumValue(
      object.target_type,
      ["claim", "step"] as const,
      `${label}.target_type`,
    ),
    content: requiredString(object, "content", label),
    author: requiredString(object, "author", label),
    created_at: requiredString(object, "created_at", label),
    updated_at: requiredString(object, "updated_at", label),
  };
}

function parsePersonalNotes(value: unknown): ApiPersonalNotes {
  const label = "personal_notes";
  const object = asRecord(value, label);
  return {
    spark:
      object.spark === null
        ? null
        : parsePersonalSpark(object.spark, `${label}.spark`),
    annotations: requiredArray(object, "annotations", label).map(
      (item, index) =>
        parsePersonalAnnotation(item, `${label}.annotations[${index}]`),
    ),
  };
}

function parseAnnotationTarget(
  value: unknown,
  index: number,
): ApiAnnotationTarget {
  const label = `detail.annotation_targets[${index}]`;
  const object = asRecord(value, label);
  return {
    target_key: requiredString(object, "target_key", label),
    display_key: requiredString(object, "display_key", label),
    target_type: enumValue(
      object.target_type,
      ["claim", "step"] as const,
      `${label}.target_type`,
    ),
    mode: enumValue(
      object.mode,
      ["full", "focused"] as const,
      `${label}.mode`,
    ),
    focus_query: requiredString(object, "focus_query", label),
    query_hash: requiredString(object, "query_hash", label),
  };
}

function parsePlaybackCapability(value: unknown): ApiPlaybackCapability {
  const object = asRecord(value, "playback");
  return {
    availability: enumValue(
      object.availability,
      ["available", "unavailable"] as const,
      "playback.availability",
    ),
    kind: enumValue(
      object.kind,
      ["retained_local", "none"] as const,
      "playback.kind",
    ),
    stream_url: requiredString(object, "stream_url", "playback"),
    mime_type: requiredString(object, "mime_type", "playback"),
    size_bytes: requiredNumber(object, "size_bytes", "playback"),
    supports_range: requiredBoolean(object, "supports_range", "playback"),
    reason: enumValue(
      object.reason,
      [
        "available",
        "not_retained",
        "unsupported_source",
        "unsupported_browser_format",
        "missing_file",
      ] as const,
      "playback.reason",
    ),
  };
}

function parseVideoDetail(value: unknown): ApiVideoDetail {
  const object = asRecord(value, "detail");
  const result = parseVideoResult(object.result);
  return {
    result,
    favorite: requiredBoolean(object, "favorite", "detail"),
    personal_tags: stringArray(object, "personal_tags", "detail"),
    automatic_tagging:
      object.automatic_tagging === undefined
        ? result.automatic_tagging
        : parseAutomaticTagging(
            object.automatic_tagging,
            "detail.automatic_tagging",
          ),
    classification:
      object.classification === undefined
        ? { ...defaultClassification }
        : parseVideoClassification(
            object.classification,
            "detail.classification",
          ),
    personal_notes: parsePersonalNotes(object.personal_notes),
    annotation_targets: requiredArray(
      object,
      "annotation_targets",
      "detail",
    ).map(parseAnnotationTarget),
    focused_history: requiredArray(
      object,
      "focused_history",
      "detail",
    ).map(parseFocusedHistoryItem),
    question_history: requiredArray(
      object,
      "question_history",
      "detail",
    ).map(parseVideoQuestion),
    playback: parsePlaybackCapability(object.playback),
  };
}

function parseResolutionPreview(value: unknown): ApiResolutionPreview {
  const object = asRecord(value, "resolution_preview");
  return {
    platform: enumValue(
      requiredString(object, "platform", "resolution_preview"),
      ["local_upload", "bilibili", "douyin"] as const,
      "resolution_preview.platform",
    ),
    source_url: requiredString(object, "source_url", "resolution_preview"),
    canonical_url: requiredString(
      object,
      "canonical_url",
      "resolution_preview",
    ),
    video_id: requiredString(object, "video_id", "resolution_preview"),
    author: requiredString(object, "author", "resolution_preview"),
    title: requiredString(object, "title", "resolution_preview"),
    description: requiredString(object, "description", "resolution_preview"),
    tags: stringArray(object, "tags", "resolution_preview"),
    duration: requiredNumber(object, "duration", "resolution_preview"),
    cover_url: requiredString(object, "cover_url", "resolution_preview"),
    warnings: stringArray(object, "warnings", "resolution_preview"),
  };
}

async function parseErrorResponse(response: Response): Promise<ApiError> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return new ApiError(
      `请求失败（HTTP ${response.status}）。`,
      `HTTP_${response.status}`,
      response.status,
    );
  }
  if (typeof body === "object" && body !== null && "error" in body) {
    const error = (body as { error?: unknown }).error;
    if (typeof error === "object" && error !== null) {
      const code =
        "code" in error && typeof error.code === "string"
          ? error.code
          : `HTTP_${response.status}`;
      const message =
        "message" in error && typeof error.message === "string"
          ? error.message
          : `请求失败（HTTP ${response.status}）。`;
      return new ApiError(message, code, response.status);
    }
  }
  return new ApiError(
    `请求失败（HTTP ${response.status}）。`,
    `HTTP_${response.status}`,
    response.status,
  );
}

function pathSegment(value: string): string {
  return encodeURIComponent(value);
}

function withPlatform(path: string, platform?: ApiPlatform): string {
  if (!platform) return path;
  const params = new URLSearchParams({ platform });
  return `${path}?${params.toString()}`;
}

export function createApiClient(
  options: CreateApiClientOptions = {},
): ApiClient {
  const baseUrl = (options.baseUrl ?? "").replace(/\/+$/, "");
  const fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);

  async function requestJson<T>(
    path: string,
    decoder: (value: unknown) => T,
    init?: RequestInit,
  ): Promise<T> {
    let response: Response;
    try {
      response = await fetchImpl(`${baseUrl}${path}`, init);
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(
        "无法连接视频解析服务，请确认后端已经启动。",
        "NETWORK_ERROR",
        0,
        { cause: error },
      );
    }
    if (!response.ok) throw await parseErrorResponse(response);
    let body: unknown;
    try {
      body = await response.json();
    } catch (error) {
      throw new ApiError(
        "服务返回了无法解析的 JSON。",
        "INVALID_RESPONSE",
        response.status,
        { cause: error },
      );
    }
    return decoder(body);
  }

  return {
    listVideos(params = {}) {
      const search = new URLSearchParams();
      if (params.query) search.set("query", params.query);
      if (params.platform) search.set("platform", params.platform);
      if (params.tag) search.set("tag", params.tag);
      if (params.tagSource) search.set("tag_source", params.tagSource);
      if (params.primaryCategory) {
        search.set("primary_category", params.primaryCategory);
      }
      if (params.secondaryCategory) {
        search.set("secondary_category", params.secondaryCategory);
      }
      if (params.favorite !== undefined) {
        search.set("favorite", String(params.favorite));
      }
      if (params.limit !== undefined) search.set("limit", String(params.limit));
      if (params.offset !== undefined) {
        search.set("offset", String(params.offset));
      }
      const suffix = search.size ? `?${search.toString()}` : "";
      return requestJson(
        `/api/v1/videos${suffix}`,
        parseVideoSearchPage,
        { signal: params.signal },
      );
    },

    getVideoDetail(videoId, platform, requestOptions) {
      return requestJson(
        withPlatform(
          `/api/v1/videos/${pathSegment(videoId)}`,
          platform,
        ),
        parseVideoDetail,
        { signal: requestOptions?.signal },
      );
    },

    createResolutionJob(inputText, focusQuery = "", requestOptions) {
      return requestJson(
        "/api/v1/resolution-jobs",
        parseJob,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            input_text: inputText,
            focus_query: focusQuery,
          }),
          signal: requestOptions?.signal,
        },
      );
    },

    previewResolution(inputText, requestOptions) {
      return requestJson(
        "/api/v1/resolution-preview",
        parseResolutionPreview,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            input_text: inputText,
            focus_query: "",
          }),
          signal: requestOptions?.signal,
        },
      );
    },

    createUploadJob(file, subtitle, requestOptions) {
      const body = new FormData();
      body.append("file", file);
      if (subtitle) body.append("subtitle", subtitle);
      body.append("retain_media", String(requestOptions?.retainMedia === true));
      return requestJson(
        "/api/v1/upload-jobs",
        parseJob,
        {
          method: "POST",
          body,
          signal: requestOptions?.signal,
        },
      );
    },

    deleteRetainedMedia(videoId, platform = "local_upload", requestOptions) {
      return requestJson(
        withPlatform(`/api/v1/videos/${pathSegment(videoId)}/media`, platform),
        parsePlaybackCapability,
        { method: "DELETE", signal: requestOptions?.signal },
      );
    },

    getJob(jobId, requestOptions) {
      return requestJson(
        `/api/v1/jobs/${pathSegment(jobId)}`,
        parseJob,
        { signal: requestOptions?.signal },
      );
    },

    retryJob(jobId, requestOptions) {
      return requestJson(
        `/api/v1/jobs/${pathSegment(jobId)}/retry`,
        parseJob,
        { method: "POST", signal: requestOptions?.signal },
      );
    },

    cancelJob(jobId, requestOptions) {
      return requestJson(
        `/api/v1/jobs/${pathSegment(jobId)}/cancel`,
        parseJob,
        { method: "POST", signal: requestOptions?.signal },
      );
    },

    createJobBatch(items, requestOptions) {
      return requestJson(
        "/api/v1/job-batches",
        parseJobBatch,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ items }),
          signal: requestOptions?.signal,
        },
      );
    },

    listJobBatches(limit = 10, requestOptions) {
      const search = new URLSearchParams({ limit: String(limit) });
      return requestJson(
        `/api/v1/job-batches?${search.toString()}`,
        parseJobBatchList,
        { signal: requestOptions?.signal },
      );
    },

    getJobBatch(batchId, requestOptions) {
      return requestJson(
        `/api/v1/job-batches/${pathSegment(batchId)}`,
        parseJobBatch,
        { signal: requestOptions?.signal },
      );
    },

    cancelJobBatchItem(batchId, jobId, requestOptions) {
      return requestJson(
        `/api/v1/job-batches/${pathSegment(batchId)}/jobs/${pathSegment(jobId)}/cancel`,
        parseJobBatch,
        { method: "POST", signal: requestOptions?.signal },
      );
    },

    setFavorite(videoId, favorite, platform, requestOptions) {
      return requestJson(
        `/api/v1/videos/${pathSegment(videoId)}/favorite`,
        parseVideoDetail,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ favorite, platform: platform ?? null }),
          signal: requestOptions?.signal,
        },
      );
    },

    updateTags(videoId, tags, operation, platform, requestOptions) {
      return requestJson(
        `/api/v1/videos/${pathSegment(videoId)}/tags`,
        parseVideoDetail,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            operation,
            tags,
            platform: platform ?? null,
          }),
          signal: requestOptions?.signal,
        },
      );
    },

    updateClassification(
      videoId,
      primaryCategory,
      secondaryCategory = "",
      platform,
      requestOptions,
    ) {
      return requestJson(
        `/api/v1/videos/${pathSegment(videoId)}/classification`,
        parseVideoDetail,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            primary_category: primaryCategory,
            secondary_category: secondaryCategory,
            platform: platform ?? null,
          }),
          signal: requestOptions?.signal,
        },
      );
    },

    generateAutomaticTags(videoId, platform, requestOptions) {
      return requestJson(
        `/api/v1/videos/${pathSegment(videoId)}/automatic-tags`,
        parseVideoDetail,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ platform: platform ?? null }),
          signal: requestOptions?.signal,
        },
      );
    },

    getPersonalNotes(videoId, platform, requestOptions) {
      return requestJson(
        withPlatform(
          `/api/v1/videos/${pathSegment(videoId)}/personal-notes`,
          platform,
        ),
        parsePersonalNotes,
        { signal: requestOptions?.signal },
      );
    },

    upsertSpark(
      videoId,
      content,
      author = "我",
      platform,
      requestOptions,
    ) {
      return requestJson(
        `/api/v1/videos/${pathSegment(videoId)}/spark`,
        parsePersonalNotes,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            content,
            author,
            platform: platform ?? null,
          }),
          signal: requestOptions?.signal,
        },
      );
    },

    deleteSpark(videoId, platform, requestOptions) {
      return requestJson(
        withPlatform(`/api/v1/videos/${pathSegment(videoId)}/spark`, platform),
        parsePersonalNotes,
        { method: "DELETE", signal: requestOptions?.signal },
      );
    },

    createAnnotation(
      videoId,
      targetKey,
      content,
      author = "我",
      platform,
      requestOptions,
    ) {
      return requestJson(
        `/api/v1/videos/${pathSegment(videoId)}/annotations`,
        parsePersonalNotes,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            target_key: targetKey,
            content,
            author,
            platform: platform ?? null,
          }),
          signal: requestOptions?.signal,
        },
      );
    },

    updateAnnotation(
      videoId,
      noteId,
      content,
      author = "我",
      platform,
      requestOptions,
    ) {
      return requestJson(
        `/api/v1/videos/${pathSegment(videoId)}/annotations/${pathSegment(noteId)}`,
        parsePersonalNotes,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            content,
            author,
            platform: platform ?? null,
          }),
          signal: requestOptions?.signal,
        },
      );
    },

    deleteAnnotation(videoId, noteId, platform, requestOptions) {
      return requestJson(
        withPlatform(
          `/api/v1/videos/${pathSegment(videoId)}/annotations/${pathSegment(noteId)}`,
          platform,
        ),
        parsePersonalNotes,
        { method: "DELETE", signal: requestOptions?.signal },
      );
    },

    createFocusedExtraction(videoId, focusQuery, platform, requestOptions) {
      return requestJson(
        `/api/v1/videos/${pathSegment(videoId)}/extractions`,
        parseVideoResult,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            focus_query: focusQuery,
            platform: platform ?? null,
          }),
          signal: requestOptions?.signal,
        },
      );
    },

    askQuestion(videoId, question, platform, requestOptions) {
      return requestJson(
        `/api/v1/videos/${pathSegment(videoId)}/questions`,
        (value) => parseVideoQuestion(value),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            question,
            platform: platform ?? null,
          }),
          signal: requestOptions?.signal,
        },
      );
    },

    async exportMarkdown(videoId, platform, exportOptions, requestOptions) {
      let response: Response;
      const search = new URLSearchParams();
      if (platform) search.set("platform", platform);
      if (exportOptions?.view) search.set("view", exportOptions.view);
      if (exportOptions?.includePersonal !== undefined) {
        search.set("include_personal", String(exportOptions.includePersonal));
      }
      if (exportOptions?.focusQueryHash) {
        search.set("focus_query_hash", exportOptions.focusQueryHash);
      }
      const query = search.toString();
      try {
        response = await fetchImpl(
          `${baseUrl}/api/v1/videos/${pathSegment(videoId)}/export.md${
            query ? `?${query}` : ""
          }`,
          { signal: requestOptions?.signal },
        );
      } catch (error) {
        throw new ApiError(
          "无法连接视频解析服务，请确认后端已经启动。",
          "NETWORK_ERROR",
          0,
          { cause: error },
        );
      }
      if (!response.ok) throw await parseErrorResponse(response);
      return response.text();
    },
  };
}

export const apiClient = createApiClient();

const subtitleLabels: Record<SubtitleSource, string> = {
  official: "官方字幕",
  public_page: "公开页面字幕",
  asr: "ASR 字幕",
  user_upload: "用户字幕",
  none: "无字幕",
};

const claimLabels: Record<ClaimType, string> = {
  video_fact: "视频明确陈述",
  creator_opinion: "博主观点",
  model_inference: "根据上下文归纳",
};

function safeHttpUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:"
      ? url.toString()
      : null;
  } catch {
    return null;
  }
}

export function mapJobStatus(job: ApiJob): {
  status: ParseStatus;
  statusText: string;
} {
  if (job.status === "completed") {
    return { status: "complete", statusText: "解析完成" };
  }
  if (job.status === "completed_with_warnings") {
    return { status: "warning", statusText: "解析完成，有警告" };
  }
  if (job.status === "failed") {
    return { status: "failed", statusText: job.message || "解析失败" };
  }
  if (job.status === "cancelled") {
    return { status: "failed", statusText: "已取消" };
  }
  const labels: Partial<Record<JobStatus, string>> = {
    queued: "等待解析",
    resolving: "正在识别链接",
    fetching_metadata: "正在获取视频信息",
    fetching_subtitles: "正在获取字幕",
    transcribing: "正在语音转写",
    cleaning: "正在清洗字幕",
    extracting: "正在整理内容",
    saving: "正在保存",
  };
  return {
    status: "processing",
    statusText: labels[job.status] ?? "正在处理",
  };
}

function formatUpdatedAt(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}

function mapAutomaticTag(tag: ApiAutomaticTag): AutomaticTag {
  return {
    name: tag.name,
    confidence: tag.confidence,
    generationMethod: tag.generation_method,
  };
}

function mapAutomaticTagging(
  tagging: ApiAutomaticTagging,
): AutomaticTagging {
  return {
    status: tagging.status,
    tags: tagging.tags.map(mapAutomaticTag),
    generatorId: tagging.generator_id,
    generatorVersion: tagging.generator_version,
    transcriptHash: tagging.transcript_hash,
    generatedAt: tagging.generated_at,
    warning: tagging.warning,
  };
}

export function mapVideoFacets(facets: ApiLibraryFacets): VideoFacets {
  return {
    tags: facets.tags.map((tag) => ({ ...tag })),
    categories: facets.categories.map((category) => ({
      primaryCategory: category.primary_category,
      count: category.count,
      children: category.children.map((child) => ({
        secondaryCategory: child.secondary_category,
        count: child.count,
      })),
    })),
  };
}

export function mapLibraryVideoToRecord(video: ApiLibraryVideo): VideoRecord {
  const hasTranscript = video.subtitle_source !== "none";
  const hasWarnings = video.warnings.length > 0;
  const spark = video.spark;
  const automaticTagging = mapAutomaticTagging(
    video.automatic_tagging ?? defaultAutomaticTagging,
  );
  const classification = video.classification ?? defaultClassification;
  return {
    id: `${video.platform}:${video.video_id}`,
    platform: video.platform === "local_upload" ? "local" : video.platform,
    sourceUrl: safeHttpUrl(video.source_url),
    mediaType: "视频",
    title: video.title || "未命名视频",
    author: video.author || "作者未知",
    duration: 0,
    status: !hasTranscript ? "missing" : hasWarnings ? "warning" : "complete",
    statusText: !hasTranscript
      ? "字幕缺失"
      : hasWarnings
        ? "有完整性提示"
        : "字幕完整",
    summary:
      video.summary ||
      "当前没有可由字幕支持的摘要，打开详情查看信息完整性提示。",
    tags: [...video.source_tags],
    userTags: [...video.personal_tags],
    automaticTags: automaticTagging.tags,
    automaticTagging,
    primaryCategory: classification.primary_category,
    secondaryCategory: classification.secondary_category || null,
    classificationUpdatedAt: classification.updated_at,
    sparkNote: spark?.content ?? null,
    sparkCreatedAt: spark?.created_at ?? null,
    sparkId: spark ? String(spark.id) : null,
    sparkAuthor: spark?.author ?? null,
    sparkUpdatedAt: spark?.updated_at ?? null,
    updatedAt: formatUpdatedAt(video.updated_at),
    integrity: !hasTranscript
      ? "字幕残缺，无法判断"
      : hasWarnings
        ? "结果带有完整性警告"
        : "视频明确陈述",
    subtitleSource: subtitleLabels[video.subtitle_source],
    warnings: [...video.warnings],
    coverUrl: safeHttpUrl(video.cover_url),
    outline: [],
    points: [],
    evidence: [],
    transcript: [],
    notes: [],
    questions: [],
    favorite: video.favorite,
    detailLoaded: false,
  };
}

export function mergePersonalNotesIntoRecord(
  record: VideoRecord,
  personalNotes: ApiPersonalNotes,
): VideoRecord {
  const spark = personalNotes.spark;
  const displayKeyByTargetKey = new Map(
    record.points.flatMap((point) =>
      point.annotationTargetKey
        ? [[point.annotationTargetKey, point.id] as const]
        : [],
    ),
  );
  return {
    ...record,
    sparkNote: spark?.content ?? null,
    sparkCreatedAt: spark?.created_at ?? null,
    sparkId: spark ? String(spark.id) : null,
    sparkAuthor: spark?.author ?? null,
    sparkUpdatedAt: spark?.updated_at ?? null,
    notes: personalNotes.annotations.map((note) => ({
      id: String(note.id),
      pointId: displayKeyByTargetKey.get(note.target_key) ?? "",
      targetKey: note.target_key,
      text: note.content,
      targetType: note.target_type,
      author: note.author,
      createdAt: note.created_at,
      updatedAt: note.updated_at,
    })),
    questions: record.questions ?? [],
  };
}

interface PointGroup {
  key: keyof ApiFullExtraction;
  title: string;
  kind: KnowledgePoint["kind"];
}

const fullPointGroups: PointGroup[] = [
  { key: "key_points", title: "核心观点", kind: "point" },
  { key: "important_data", title: "重要数据", kind: "point" },
  { key: "cases_and_arguments", title: "案例和论据", kind: "point" },
  { key: "steps", title: "方法和操作步骤", kind: "step" },
  { key: "risks", title: "注意事项和风险", kind: "risk" },
  { key: "quotes", title: "可复用金句", kind: "point" },
];

function groupTimeRange(
  items: ApiExtractionItem[],
  evidenceById: Map<string, ApiEvidence>,
): Pick<OutlineItem, "start" | "end"> {
  const related = items.flatMap((item) =>
    item.evidence_refs
      .map((id) => evidenceById.get(id))
      .filter((item): item is ApiEvidence => item !== undefined),
  );
  const starts = related
    .map((item) => item.start_time)
    .filter((item): item is number => item !== null);
  const ends = related
    .map((item) => item.end_time)
    .filter((item): item is number => item !== null);
  return {
    start: starts.length ? Math.min(...starts) : null,
    end: ends.length ? Math.max(...ends) : null,
  };
}

function mapFullPoints(
  result: ApiVideoResult,
  evidenceById: Map<string, ApiEvidence>,
  targetKeyByDisplayKey: Map<string, string>,
): { points: KnowledgePoint[]; outline: OutlineItem[] } {
  const points: KnowledgePoint[] = [];
  const outline: OutlineItem[] = [];
  for (const group of fullPointGroups) {
    const items = result.full_extraction[group.key];
    if (!items.length) continue;
    outline.push({
      id: `section-${group.key}`,
      title: group.title,
      ...groupTimeRange(items, evidenceById),
    });
    items.forEach((item, index) => {
      const displayKey = `${group.key}-${index}`;
      points.push({
        id: displayKey,
        title: item.text,
        body: claimLabels[item.claim_type],
        evidenceIds: [...item.evidence_refs],
        kind: group.kind,
        sectionId: `section-${group.key}`,
        annotationTargetKey: targetKeyByDisplayKey.get(displayKey),
      });
    });
  }
  return { points, outline };
}

function mapFocusedPoints(
  result: ApiVideoResult,
  evidenceById: Map<string, ApiEvidence>,
  targetKeyByDisplayKey: Map<string, string>,
): { points: KnowledgePoint[]; outline: OutlineItem[] } {
  const answer = result.focused_answer;
  if (!answer) return { points: [], outline: [] };
  const groups = [
    {
      id: "focused-key-points",
      title: "相关内容",
      items: answer.key_points,
    },
    {
      id: "focused-context",
      title: "可能相关的补充内容",
      items: answer.supplementary_context,
    },
  ];
  const points: KnowledgePoint[] = [];
  const outline: OutlineItem[] = [];
  for (const group of groups) {
    if (!group.items.length) continue;
    outline.push({
      id: group.id,
      title: group.title,
      ...groupTimeRange(group.items, evidenceById),
    });
    group.items.forEach((item, index) => {
      const displayKey = `${group.id}-${index}`;
      points.push({
        id: displayKey,
        title: item.text,
        body: claimLabels[item.claim_type],
        evidenceIds: [...item.evidence_refs],
        kind:
          item.item_type === "step"
            ? "step"
            : item.item_type === "risk"
              ? "risk"
              : "point",
        sectionId: group.id,
        annotationTargetKey: targetKeyByDisplayKey.get(displayKey),
      });
    });
  }
  return { points, outline };
}

export function mapVideoDetailToRecord(detail: ApiVideoDetail): VideoRecord {
  const { result } = detail;
  const allEvidence = [
    ...result.evidence,
    ...(result.focused_answer?.supporting_segments ?? []),
  ];
  const evidenceById = new Map(allEvidence.map((item) => [item.id, item]));
  const currentTargets = detail.annotation_targets.filter(
    (target) =>
      target.mode === result.extraction_mode &&
      (target.mode === "full" || target.focus_query === result.focus_query),
  );
  const targetKeyByDisplayKey = new Map(
    currentTargets.map((target) => [target.display_key, target.target_key]),
  );
  const mapped =
    result.extraction_mode === "focused"
      ? mapFocusedPoints(result, evidenceById, targetKeyByDisplayKey)
      : mapFullPoints(result, evidenceById, targetKeyByDisplayKey);
  const displayKeyByTargetKey = new Map(
    mapped.points.flatMap((point) =>
      point.annotationTargetKey
        ? [[point.annotationTargetKey, point.id] as const]
        : [],
    ),
  );
  const evidence: UiEvidence[] = Array.from(evidenceById.values()).map(
    (item) => ({
      id: item.id,
      start: item.start_time,
      end: item.end_time,
      text: item.evidence,
      segmentIds: [...item.segment_ids],
    }),
  );
  const transcript: UiEvidence[] = result.segments.map((segment) => ({
    id: segment.id,
    start: segment.start,
    end: segment.end,
    text: segment.text,
  }));
  const hasWarnings = result.warnings.length > 0;
  const hasTranscript = result.subtitle_source !== "none";
  const focusedStatus = result.focused_answer?.mention_status;
  const integrity =
    focusedStatus === "explicit"
      ? "视频明确提到"
      : focusedStatus === "inferred"
        ? "根据上下文可以合理归纳"
        : focusedStatus === "not_mentioned"
          ? "视频没有提到"
          : focusedStatus === "unknown_incomplete_transcript"
            ? "字幕残缺，无法判断"
            : hasWarnings
              ? "结果带有完整性警告"
              : "视频明确陈述";
  const status: ParseStatus = !hasTranscript
    ? "missing"
    : hasWarnings
      ? "warning"
      : "complete";
  const statusText =
    status === "missing"
      ? "字幕缺失"
      : status === "warning"
        ? "解析完成，有警告"
        : "字幕完整";
  const spark = detail.personal_notes.spark;
  const automaticTagging = mapAutomaticTagging(
    detail.automatic_tagging ??
      result.automatic_tagging ??
      defaultAutomaticTagging,
  );
  const classification = detail.classification ?? defaultClassification;
  const focusedHistoryItem = detail.focused_history.find(
    (item) => item.focus_query === result.focus_query,
  );
  return {
    id: `${result.platform}:${result.video_id}`,
    platform: result.platform === "local_upload" ? "local" : result.platform,
    sourceUrl: safeHttpUrl(result.source_url),
    mediaType: "视频",
    title: result.title || "未命名视频",
    author: result.author || "作者未知",
    duration: result.duration,
    status,
    statusText,
    summary:
      result.extraction_mode === "focused" && result.focused_answer
        ? result.focused_answer.direct_answer
        : result.summary ||
          "没有可由字幕支持的摘要，请查看信息完整性提示。",
    tags: [...result.tags],
    userTags: [...detail.personal_tags],
    automaticTags: automaticTagging.tags,
    automaticTagging,
    primaryCategory: classification.primary_category,
    secondaryCategory: classification.secondary_category || null,
    classificationUpdatedAt: classification.updated_at,
    sparkNote: spark?.content ?? null,
    sparkCreatedAt: spark?.created_at ?? null,
    sparkId: spark ? String(spark.id) : null,
    sparkAuthor: spark?.author ?? null,
    sparkUpdatedAt: spark?.updated_at ?? null,
    updatedAt: "",
    integrity,
    subtitleSource: subtitleLabels[result.subtitle_source],
    warnings: [...result.warnings],
    coverUrl: safeHttpUrl(result.cover_url),
    focusQuery: result.focus_query.trim() || undefined,
    focusQueryHash: focusedHistoryItem?.query_hash,
    missingInformation:
      result.focused_answer?.missing_information.slice() ?? [],
    outline: mapped.outline,
    points: mapped.points,
    evidence,
    transcript,
    notes: detail.personal_notes.annotations.map((note) => ({
      id: String(note.id),
      pointId: displayKeyByTargetKey.get(note.target_key) ?? "",
      targetKey: note.target_key,
      text: note.content,
      targetType: note.target_type,
      author: note.author,
      createdAt: note.created_at,
      updatedAt: note.updated_at,
    })),
    questions: detail.question_history.map(mapApiVideoQuestion),
    playback: mapPlaybackCapability(detail.playback),
    favorite: detail.favorite,
    detailLoaded: true,
  };
}

export function mapApiVideoQuestion(question: ApiVideoQuestion): VideoQuestion {
  return {
    id: String(question.id),
    question: question.question,
    questionHash: question.question_hash,
    mentionStatus: question.answer.mention_status,
    directAnswer: question.answer.direct_answer,
    evidence: question.answer.supporting_segments.map((item) => ({
      id: `question-${question.id}-${item.id}`,
      start: item.start_time,
      end: item.end_time,
      text: item.evidence,
      segmentIds: [...item.segment_ids],
    })),
    missingInformation: [...question.answer.missing_information],
    createdAt: question.created_at,
  };
}

export function mapPlaybackCapability(
  playback: ApiPlaybackCapability,
): PlaybackCapability {
  return {
    availability: playback.availability,
    kind: playback.kind,
    streamUrl: playback.stream_url,
    mimeType: playback.mime_type,
    sizeBytes: playback.size_bytes,
    supportsRange: playback.supports_range,
    reason: playback.reason,
  };
}

export function mapVideoSearchPageToRecords(
  page: ApiVideoSearchPage,
): VideoRecord[] {
  return page.items.map(mapLibraryVideoToRecord);
}
