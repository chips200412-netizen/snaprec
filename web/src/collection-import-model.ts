import type {
  CollectionImportBatchItemSummary,
  CollectionImportBatchSnapshot,
  CollectionImportItemStatus,
} from "./collection-import-api";

export const COLLECTION_IMPORT_MIN_ITEMS = 2;
export const COLLECTION_IMPORT_MAX_ITEMS = 10;
export const COLLECTION_IMPORT_MAX_ITEM_CHARS = 10_000;
export const COLLECTION_IMPORT_MAX_UTF8_BYTES = 65_536;

export interface CollectionImportDraftItem {
  localId: string;
  inputText: string;
}

export interface CollectionImportDraftValidation {
  valid: boolean;
  totalBytes: number;
  itemErrors: string[];
  summary: string;
}

const encoder = new TextEncoder();

export function unicodeScalarLength(value: string): number {
  return Array.from(value).length;
}

export function utf8Length(value: string): number {
  return encoder.encode(value).byteLength;
}

export function validateCollectionImportDraft(
  items: readonly CollectionImportDraftItem[],
): CollectionImportDraftValidation {
  const itemErrors = items.map((item) => {
    if (!item.inputText.trim()) return "请粘贴这一项的完整分享文本。";
    const length = unicodeScalarLength(item.inputText);
    return length > COLLECTION_IMPORT_MAX_ITEM_CHARS
      ? `这一项有 ${length.toLocaleString("zh-CN")} 个字符，最多 10,000 个。`
      : "";
  });
  const totalBytes = items.reduce((total, item) => total + utf8Length(item.inputText), 0);
  let summary = "";
  if (items.length < COLLECTION_IMPORT_MIN_ITEMS || items.length > COLLECTION_IMPORT_MAX_ITEMS) {
    summary = "每个批次需要 2 至 10 项。";
  } else if (itemErrors.some(Boolean)) {
    summary = "请先修正标出的分享文本。";
  } else if (totalBytes > COLLECTION_IMPORT_MAX_UTF8_BYTES) {
    summary = `全部分享文本共 ${totalBytes.toLocaleString("zh-CN")} bytes，最多 65,536 bytes。`;
  }
  return {
    valid: !summary,
    totalBytes,
    itemErrors,
    summary,
  };
}

export function collectionImportRequestFingerprint(items: readonly CollectionImportDraftItem[]): string {
  return JSON.stringify(items.map((item, index) => ({
    client_item_id: `item-${String(index + 1).padStart(2, "0")}`,
    input_text: item.inputText,
  })));
}

export function collectionImportCreateItems(items: readonly CollectionImportDraftItem[]) {
  return items.map((item, index) => ({
    client_item_id: `item-${String(index + 1).padStart(2, "0")}`,
    input_text: item.inputText,
  }));
}

export function collectionImportItemId(item: CollectionImportBatchItemSummary): string {
  return item.batch_item_id;
}

export function collectionImportDisplayPosition(position: number): number {
  return position + 1;
}

export function collectionImportStatusLabel(status: CollectionImportItemStatus): string {
  const labels: Record<CollectionImportItemStatus, string> = {
    queued: "等待检查",
    previewing: "正在检查",
    ready: "待审核",
    needs_review: "需要补充",
    preview_expired: "预览已过期",
    duplicate_in_batch: "批内重复",
    save_queued: "等待保存",
    saving: "正在保存",
    saved: "已保存",
    already_exists: "已在素材库",
    skipped: "已跳过",
    failed: "处理失败",
    cancelled: "已取消",
    interrupted: "需要恢复",
    outcome_unknown: "结果待核对",
  };
  return labels[status];
}

export function collectionImportBatchLabel(status: CollectionImportBatchSnapshot["status"]): string {
  const labels: Record<CollectionImportBatchSnapshot["status"], string> = {
    previewing: "正在逐项检查",
    awaiting_review: "等待逐项审核",
    saving: "正在按原序保存",
    cancelling: "正在取消",
    interrupted: "需要恢复与对账",
    completed: "批次已完成",
    completed_with_issues: "批次已完成，部分项目未导入",
    cancelled: "批次已取消",
  };
  return labels[status];
}

export function isCollectionImportPreviewActive(batch: CollectionImportBatchSnapshot): boolean {
  return batch.status === "previewing"
    || batch.items.some((item) => item.state === "queued" || item.state === "previewing");
}

export interface CollectionImportConfirmationCounts {
  save: number;
  skip: number;
  duplicate: number;
  existing: number;
  failed: number;
}

export function collectionImportConfirmationCounts(
  batch: CollectionImportBatchSnapshot,
): CollectionImportConfirmationCounts {
  const saveStates = new Set(["ready", "needs_review", "failed"]);
  const skipStates = new Set(["ready", "needs_review", "preview_expired", "failed"]);
  return {
    save: batch.items.filter((item) => item.decision === "save" && saveStates.has(item.state)).length,
    skip: batch.items.filter((item) => item.decision === "skip" && skipStates.has(item.state)).length,
    duplicate: batch.items.filter((item) => item.state === "duplicate_in_batch").length,
    existing: batch.items.filter((item) => item.state === "already_exists").length,
    failed: batch.items.filter((item) => item.state === "failed").length,
  };
}

export function isCollectionImportReviewComplete(batch: CollectionImportBatchSnapshot): boolean {
  if (batch.status !== "awaiting_review") return false;
  return batch.items.every((item) => {
    if (item.state === "duplicate_in_batch"
      || item.state === "already_exists"
      || item.state === "saved"
      || item.state === "skipped") return true;
    if (item.state === "ready" || item.state === "needs_review") {
      return item.decision === "save" || item.decision === "skip";
    }
    if (item.state === "preview_expired") {
      return item.decision === "skip";
    }
    if (item.state === "failed") return item.decision === "save" || item.decision === "skip";
    return false;
  });
}

export function canConfirmCollectionImportBatch(
  batch: CollectionImportBatchSnapshot,
  dirty = false,
): boolean {
  if (dirty || isCollectionImportPreviewActive(batch)) return false;
  if (batch.items.some((item) => item.state === "outcome_unknown")) return false;
  return isCollectionImportReviewComplete(batch);
}

export function isCollectionImportSaveActive(batch: CollectionImportBatchSnapshot): boolean {
  return batch.status === "saving"
    || batch.items.some((item) => item.state === "save_queued" || item.state === "saving");
}

export function canResumeCollectionImportBatch(batch: CollectionImportBatchSnapshot): boolean {
  return batch.status === "interrupted";
}

export function canCancelCollectionImportBatch(batch: CollectionImportBatchSnapshot): boolean {
  return batch.status !== "cancelling"
    && batch.status !== "completed"
    && batch.status !== "completed_with_issues"
    && batch.status !== "cancelled";
}

export function hasCollectionImportOutcomeUnknown(batch: CollectionImportBatchSnapshot): boolean {
  return batch.items.some((item) => item.state === "outcome_unknown");
}

export function shouldPollCollectionImportBatch(batch: CollectionImportBatchSnapshot): boolean {
  return batch.status === "cancelling"
    || isCollectionImportPreviewActive(batch)
    || isCollectionImportSaveActive(batch);
}

export function safeCollectionImportCounts(batch: CollectionImportBatchSnapshot) {
  return {
    total: batch.total,
    ready: batch.ready + batch.needs_review,
    selected: batch.selected,
    failed: batch.failed,
    duplicate: batch.duplicates,
    existing: batch.already_exists,
  };
}
