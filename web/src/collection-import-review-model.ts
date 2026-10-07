import type { CollectionImportDecision, CollectionImportItemDetail } from "./collection-import-api";
import type { CollectionItemCreate } from "./collection-api";
import {
  normalizeSnapshot,
  validateSnapshot,
  type CollectionUserSnapshot,
} from "./collection-edit-model";

export type CollectionImportReviewDraft = Omit<CollectionItemCreate, "preview_id">;
export type CollectionImportReviewField =
  | "decision"
  | "user_title"
  | "untitled_confirmed"
  | "primary_category"
  | "secondary_category"
  | "organization_tags"
  | "personal_tags"
  | "inspiration";

export const collectionImportReviewFieldLabels: Record<CollectionImportReviewField, string> = {
  decision: "审核决定",
  user_title: "自定义标题",
  untitled_confirmed: "无标题确认",
  primary_category: "一级分类",
  secondary_category: "二级分类",
  organization_tags: "整理标签",
  personal_tags: "个人标签",
  inspiration: "我的灵感",
};

function snapshotFromDraft(draft: CollectionImportReviewDraft): CollectionUserSnapshot {
  return {
    user_title: draft.user_title,
    organization_confirmation: {
      primary_category: draft.organization_confirmation.primary_category,
      secondary_category: draft.organization_confirmation.secondary_category,
      organization_tags: [...draft.organization_confirmation.organization_tags],
    },
    personal_tags: [...draft.personal_tags],
    inspiration: draft.inspiration ? { ...draft.inspiration } : null,
  };
}

export function normalizeCollectionImportReviewDraft(
  draft: CollectionImportReviewDraft,
): CollectionImportReviewDraft {
  const normalized = normalizeSnapshot(snapshotFromDraft(draft));
  return {
    user_title: normalized.user_title,
    untitled_confirmed: draft.untitled_confirmed,
    organization_confirmation: {
      primary_category: normalized.organization_confirmation.primary_category,
      secondary_category: normalized.organization_confirmation.secondary_category,
      organization_tags: [...normalized.organization_confirmation.organization_tags],
    },
    personal_tags: [...normalized.personal_tags],
    inspiration: normalized.inspiration ? { ...normalized.inspiration } : null,
  };
}

export function cloneCollectionImportReviewDraft(
  draft: CollectionImportReviewDraft,
): CollectionImportReviewDraft {
  return structuredClone(draft);
}

export function sameCollectionImportReviewDraft(
  left: CollectionImportReviewDraft,
  right: CollectionImportReviewDraft,
): boolean {
  return JSON.stringify(normalizeCollectionImportReviewDraft(left))
    === JSON.stringify(normalizeCollectionImportReviewDraft(right));
}

export function validateCollectionImportReviewDraft(
  draft: CollectionImportReviewDraft,
  decision: Exclude<CollectionImportDecision, "pending">,
  state: CollectionImportItemDetail["state"],
) {
  const normalized = normalizeCollectionImportReviewDraft(draft);
  const validation = validateSnapshot(snapshotFromDraft(normalized));
  const errors: Record<string, string> = { ...validation.errors };
  if (decision === "save" && state === "needs_review" && !normalized.user_title && !normalized.untitled_confirmed) {
    errors.user_title = "请补充自定义标题，或明确确认按无标题普通书签保存。";
  }
  return { valid: Object.keys(errors).length === 0, errors, value: normalized };
}

export function isCollectionImportReviewPaused(
  items: ReadonlyArray<Pick<CollectionImportItemDetail, "state">>,
): boolean {
  return items.some((item) => item.state === "queued" || item.state === "previewing");
}

export function canRecordCollectionImportDecision(
  detail: Pick<CollectionImportItemDetail, "state"> & Partial<Pick<CollectionImportItemDetail, "error_stage">>,
  decision: Exclude<CollectionImportDecision, "pending">,
  reviewPaused: boolean,
): boolean {
  if (reviewPaused) return false;
  if (decision === "save") return detail.state === "ready"
    || detail.state === "needs_review"
    || (detail.state === "failed" && detail.error_stage === "save");
  return detail.state === "ready"
    || detail.state === "needs_review"
    || detail.state === "preview_expired"
    || detail.state === "failed";
}

export function canRepreviewCollectionImportItem(
  detail: Pick<CollectionImportItemDetail, "state" | "error_stage" | "preview_generation">,
  reviewPaused: boolean,
): boolean {
  if (reviewPaused || detail.preview_generation >= 5) return false;
  if (detail.state === "ready" || detail.state === "needs_review" || detail.state === "preview_expired") return true;
  return detail.state === "failed" && detail.error_stage === "preview";
}

export function collectionImportReviewValue(
  draft: CollectionImportReviewDraft,
  decision: CollectionImportDecision,
  field: CollectionImportReviewField,
): unknown {
  if (field === "decision") return decision;
  if (field === "primary_category") return draft.organization_confirmation.primary_category;
  if (field === "secondary_category") return draft.organization_confirmation.secondary_category;
  if (field === "organization_tags") return draft.organization_confirmation.organization_tags;
  return draft[field];
}

export function setCollectionImportReviewValue(
  draft: CollectionImportReviewDraft,
  decision: CollectionImportDecision,
  field: CollectionImportReviewField,
  sourceDraft: CollectionImportReviewDraft,
  sourceDecision: CollectionImportDecision,
): { draft: CollectionImportReviewDraft; decision: CollectionImportDecision } {
  const next = cloneCollectionImportReviewDraft(draft);
  if (field === "decision") return { draft: next, decision: sourceDecision };
  if (field === "primary_category") next.organization_confirmation.primary_category = sourceDraft.organization_confirmation.primary_category;
  else if (field === "secondary_category") next.organization_confirmation.secondary_category = sourceDraft.organization_confirmation.secondary_category;
  else if (field === "organization_tags") next.organization_confirmation.organization_tags = [...sourceDraft.organization_confirmation.organization_tags];
  else if (field === "personal_tags") next.personal_tags = [...sourceDraft.personal_tags];
  else if (field === "inspiration") next.inspiration = sourceDraft.inspiration ? { ...sourceDraft.inspiration } : null;
  else next[field] = sourceDraft[field] as never;
  return { draft: next, decision };
}

export function formatCollectionImportReviewValue(value: unknown): string {
  if (value === null || value === "" || (Array.isArray(value) && value.length === 0)) return "未填写";
  if (typeof value === "boolean") return value ? "已确认" : "未确认";
  if (Array.isArray(value)) return value.join("、");
  if (typeof value === "object" && value && "content" in value) {
    const content = (value as { content?: unknown }).content;
    return typeof content === "string" && content ? content : "未填写";
  }
  if (value === "pending") return "尚未决定";
  if (value === "save") return "保存";
  if (value === "skip") return "跳过";
  return String(value);
}
