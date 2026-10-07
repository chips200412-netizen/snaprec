import type {
  CollectionItem,
  CollectionItemCreate,
  InspirationDraft,
} from "./collection-api";
import { unicodeCaseFold } from "./unicode-casefold";
import {
  normalizeUnicode15NFKC,
  pythonCollapseWhitespace,
  pythonTrim,
} from "./unicode-normalize";

export type EditMergeMode = "edit" | "merge";
export type EditMergeReturnKind = "detail" | "review";
export type EditableField =
  | "user_title"
  | "user_author"
  | "user_cover_asset_id"
  | "primary_category"
  | "secondary_category"
  | "organization_tags"
  | "personal_tags"
  | "inspiration";

export interface CollectionUserSnapshot {
  user_title: string | null;
  user_author?: string | null;
  user_cover_asset_id?: string | null;
  organization_confirmation: {
    primary_category: string;
    secondary_category: string;
    organization_tags: string[];
  };
  personal_tags: string[];
  inspiration: InspirationDraft | null;
}

export interface ConflictRecoveryState {
  latest: CollectionUserSnapshot;
  mine: CollectionUserSnapshot;
  conflictFields: EditableField[];
  choices: Partial<Record<EditableField, "latest" | "mine">>;
  latestRevision: number;
}

export interface EditMergeSession {
  version: 1;
  mode: EditMergeMode;
  itemId: string;
  routeEntryId: string;
  returnKind: EditMergeReturnKind;
  reviewEntryId: string | null;
  base: CollectionUserSnapshot | null;
  desired: CollectionUserSnapshot | null;
  incoming: CollectionUserSnapshot | null;
  expectedRevision: number | null;
  requestState: "draft" | "saving" | "outcome_unknown";
  conflictRecovery: ConflictRecoveryState | null;
  updatedAt: number;
  userCoverClaimToken?: string | null;
}

export interface SnapshotValidation {
  valid: boolean;
  errors: Partial<Record<EditableField, string>>;
}

export interface ThreeWayResult {
  desired: CollectionUserSnapshot;
  conflicts: EditableField[];
}

const sessionPrefix = "instant-record:r25:edit-merge:v1";
// A fully valid merge conflict can persist five independent maximum-size
// snapshots (base, desired, incoming, latest, and mine). JSON escaping can
// expand each code point to six bytes, so that state can exceed 300 KiB.
// 512 KiB keeps the abuse boundary finite while leaving deterministic room
// for the schema keys, conflict choices, identifiers, and future v1 metadata.
const maxSessionBytes = 512 * 1024;

function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function sessionKey(mode: EditMergeMode, itemId: string): string {
  return `${sessionPrefix}:${mode}:${encodeURIComponent(itemId)}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.length <= 50 && value.every((entry) => typeof entry === "string");
}

function isInspiration(value: unknown): value is InspirationDraft | null {
  if (value === null) return true;
  if (!isRecord(value) || typeof value.content !== "string"
    || !pythonTrim(value.content)) return false;
  return (value.input_mode === "text" && value.transcription_status === "not_applicable")
    || (value.input_mode === "voice" && value.transcription_status === "completed");
}

function isSnapshot(value: unknown): value is CollectionUserSnapshot {
  if (!isRecord(value) || !isRecord(value.organization_confirmation)) return false;
  const confirmation = value.organization_confirmation;
  return (value.user_title === null || typeof value.user_title === "string")
    && (value.user_author === undefined || value.user_author === null || typeof value.user_author === "string")
    && (value.user_cover_asset_id === undefined || value.user_cover_asset_id === null || typeof value.user_cover_asset_id === "string")
    && typeof confirmation.primary_category === "string"
    && typeof confirmation.secondary_category === "string"
    && stringArray(confirmation.organization_tags)
    && stringArray(value.personal_tags)
    && isInspiration(value.inspiration);
}

const editableFields: EditableField[] = [
  "user_title",
  "user_author",
  "user_cover_asset_id",
  "primary_category",
  "secondary_category",
  "organization_tags",
  "personal_tags",
  "inspiration",
];

function isEditableField(value: unknown): value is EditableField {
  return typeof value === "string" && editableFields.includes(value as EditableField);
}

function isConflictRecovery(
  value: unknown,
  base: CollectionUserSnapshot,
  desired: CollectionUserSnapshot,
  expectedRevision: number,
  requestState: EditMergeSession["requestState"],
): value is ConflictRecoveryState {
  if (!isRecord(value) || !isSnapshot(value.latest) || !isSnapshot(value.mine)
    || !Array.isArray(value.conflictFields) || value.conflictFields.length === 0
    || !value.conflictFields.every(isEditableField)
    || new Set(value.conflictFields).size !== value.conflictFields.length
    || !isRecord(value.choices)
    || !Number.isInteger(value.latestRevision) || Number(value.latestRevision) < 1
    || value.latestRevision !== expectedRevision
    || requestState !== "draft"
    || !sameSnapshot(base, value.latest)) return false;
  const fields = value.conflictFields as EditableField[];
  const latest = value.latest as CollectionUserSnapshot;
  const mine = value.mine as CollectionUserSnapshot;
  const choices = value.choices as Record<string, unknown>;
  for (const [field, choice] of Object.entries(choices)) {
    if (!isEditableField(field) || !fields.includes(field) || (choice !== "latest" && choice !== "mine")) return false;
  }
  return fields.every((field) => {
    const choice = choices[field];
    const source = choice === "latest" ? latest : mine;
    return sameSnapshotField(desired, source, field);
  });
}

function isSession(value: unknown): value is EditMergeSession {
  if (!isRecord(value)) return false;
  const common = value.version === 1
    && (value.mode === "edit" || value.mode === "merge")
    && typeof value.itemId === "string" && Boolean(pythonTrim(value.itemId))
    && typeof value.routeEntryId === "string" && Boolean(pythonTrim(value.routeEntryId))
    && (value.returnKind === "detail" || value.returnKind === "review")
    && (value.reviewEntryId === null || typeof value.reviewEntryId === "string")
    && (value.base === null || isSnapshot(value.base))
    && (value.desired === null || isSnapshot(value.desired))
    && (value.incoming === null || isSnapshot(value.incoming))
    && (value.expectedRevision === null || (Number.isInteger(value.expectedRevision) && Number(value.expectedRevision) >= 1))
    && (value.requestState === "draft" || value.requestState === "saving" || value.requestState === "outcome_unknown")
    && (value.conflictRecovery === null || isRecord(value.conflictRecovery))
    && typeof value.updatedAt === "number";
  if (value.userCoverClaimToken !== undefined && value.userCoverClaimToken !== null
    && typeof value.userCoverClaimToken !== "string") return false;
  if (!common) return false;
  const initialized = isSnapshot(value.base) && isSnapshot(value.desired)
    && typeof value.expectedRevision === "number";
  const uninitialized = value.base === null && value.desired === null && value.expectedRevision === null;
  const validRecovery = initialized && (value.conflictRecovery === null || isConflictRecovery(
    value.conflictRecovery,
    value.base as CollectionUserSnapshot,
    value.desired as CollectionUserSnapshot,
    value.expectedRevision as number,
    value.requestState as EditMergeSession["requestState"],
  ));
  if (value.mode === "merge") {
    return value.returnKind === "review"
      && typeof value.reviewEntryId === "string"
      && Boolean(pythonTrim(value.reviewEntryId))
      && value.incoming !== null
      && ((uninitialized && value.requestState === "draft" && value.conflictRecovery === null) || validRecovery);
  }
  return value.returnKind === "detail" && value.reviewEntryId === null && value.incoming === null && validRecovery;
}

export function readEditMergeSession(
  mode: EditMergeMode,
  itemId: string,
  routeEntryId?: string,
): EditMergeSession | null {
  const key = sessionKey(mode, itemId);
  let raw: string | null = null;
  try { raw = window.sessionStorage.getItem(key); } catch { return null; }
  if (!raw) return null;
  if (utf8ByteLength(raw) > maxSessionBytes) {
    clearEditMergeSession(mode, itemId);
    return null;
  }
  try {
    const value: unknown = JSON.parse(raw);
    if (!isSession(value) || value.mode !== mode || value.itemId !== itemId
      || (routeEntryId !== undefined && value.routeEntryId !== routeEntryId)) {
      clearEditMergeSession(mode, itemId);
      return null;
    }
    return value;
  } catch {
    clearEditMergeSession(mode, itemId);
    return null;
  }
}

export function storedEditMergeEntryId(mode: EditMergeMode, itemId: string): string | null {
  return readEditMergeSession(mode, itemId)?.routeEntryId ?? null;
}

export function writeEditMergeSession(session: EditMergeSession): boolean {
  const normalized = { ...session, updatedAt: Date.now() };
  const serialized = JSON.stringify(normalized);
  if (utf8ByteLength(serialized) > maxSessionBytes) return false;
  try {
    window.sessionStorage.setItem(sessionKey(session.mode, session.itemId), serialized);
    return true;
  } catch {
    return false;
  }
}

export function clearEditMergeSession(mode: EditMergeMode, itemId: string): void {
  try { window.sessionStorage.removeItem(sessionKey(mode, itemId)); } catch { /* unavailable storage */ }
}

function normalizedText(value: string): string {
  return pythonTrim(normalizeUnicode15NFKC(value));
}

export function normalizeEditableTag(value: string): string {
  return pythonCollapseWhitespace(normalizeUnicode15NFKC(value));
}

export function editableTagIdentity(value: string): string {
  return unicodeCaseFold(normalizeEditableTag(value));
}

function normalizedTags(values: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const raw of values) {
    const value = normalizeEditableTag(raw);
    if (!value) continue;
    const identity = editableTagIdentity(value);
    if (seen.has(identity)) continue;
    seen.add(identity);
    result.push(value);
  }
  return result;
}

function normalizedInspiration(value: InspirationDraft | null): InspirationDraft | null {
  if (!value || !pythonTrim(value.content)) return null;
  return { ...value };
}

export function normalizeSnapshot(snapshot: CollectionUserSnapshot): CollectionUserSnapshot {
  const title = snapshot.user_title === null ? null : pythonTrim(snapshot.user_title);
  const author = typeof snapshot.user_author === "string"
    ? pythonCollapseWhitespace(normalizeUnicode15NFKC(snapshot.user_author))
    : null;
  return {
    user_title: title || null,
    user_author: author || null,
    user_cover_asset_id: typeof snapshot.user_cover_asset_id === "string" && /^[0-9a-f]{32}$/.test(snapshot.user_cover_asset_id)
      ? snapshot.user_cover_asset_id
      : null,
    organization_confirmation: {
      primary_category: normalizedText(snapshot.organization_confirmation.primary_category),
      secondary_category: normalizedText(snapshot.organization_confirmation.secondary_category),
      organization_tags: normalizedTags(snapshot.organization_confirmation.organization_tags),
    },
    personal_tags: normalizedTags(snapshot.personal_tags),
    inspiration: normalizedInspiration(snapshot.inspiration),
  };
}

export function snapshotFromItem(item: CollectionItem): CollectionUserSnapshot {
  return normalizeSnapshot({
    user_title: item.user_title,
    user_author: item.user_author ?? null,
    user_cover_asset_id: item.user_cover_asset_id ?? null,
    organization_confirmation: {
      primary_category: item.organization_confirmation.primary_category,
      secondary_category: item.organization_confirmation.secondary_category,
      organization_tags: [...item.organization_confirmation.organization_tags],
    },
    personal_tags: [...item.personal_tags],
    inspiration: item.inspiration ? {
      content: item.inspiration.content,
      input_mode: item.inspiration.input_mode,
      transcription_status: item.inspiration.transcription_status,
    } : null,
  });
}

export function snapshotFromCreate(payload: CollectionItemCreate): CollectionUserSnapshot {
  return normalizeSnapshot({
    user_title: payload.user_title,
    user_author: payload.user_author ?? null,
    user_cover_asset_id: payload.user_cover_asset_id ?? null,
    organization_confirmation: {
      primary_category: payload.organization_confirmation.primary_category,
      secondary_category: payload.organization_confirmation.secondary_category,
      organization_tags: [...payload.organization_confirmation.organization_tags],
    },
    personal_tags: [...payload.personal_tags],
    inspiration: payload.inspiration ? { ...payload.inspiration } : null,
  });
}

export function sameSnapshot(left: CollectionUserSnapshot, right: CollectionUserSnapshot): boolean {
  return JSON.stringify(normalizeSnapshot(left)) === JSON.stringify(normalizeSnapshot(right));
}

export function snapshotFieldValue(snapshot: CollectionUserSnapshot, field: EditableField): unknown {
  if (field === "primary_category") return snapshot.organization_confirmation.primary_category;
  if (field === "secondary_category") return snapshot.organization_confirmation.secondary_category;
  if (field === "organization_tags") return snapshot.organization_confirmation.organization_tags;
  return snapshot[field];
}

export function sameSnapshotField(left: CollectionUserSnapshot, right: CollectionUserSnapshot, field: EditableField): boolean {
  return JSON.stringify(snapshotFieldValue(normalizeSnapshot(left), field))
    === JSON.stringify(snapshotFieldValue(normalizeSnapshot(right), field));
}

export function setSnapshotField(
  snapshot: CollectionUserSnapshot,
  field: EditableField,
  source: CollectionUserSnapshot,
): CollectionUserSnapshot {
  const next = structuredClone(snapshot);
  if (field === "primary_category") next.organization_confirmation.primary_category = source.organization_confirmation.primary_category;
  else if (field === "secondary_category") next.organization_confirmation.secondary_category = source.organization_confirmation.secondary_category;
  else if (field === "organization_tags") next.organization_confirmation.organization_tags = [...source.organization_confirmation.organization_tags];
  else if (field === "personal_tags") next.personal_tags = [...source.personal_tags];
  else if (field === "inspiration") next.inspiration = source.inspiration ? { ...source.inspiration } : null;
  else if (field === "user_author") next.user_author = source.user_author;
  else if (field === "user_cover_asset_id") next.user_cover_asset_id = source.user_cover_asset_id;
  else next.user_title = source.user_title;
  return next;
}

export function differingFields(left: CollectionUserSnapshot, right: CollectionUserSnapshot): EditableField[] {
  const fields: EditableField[] = [
    "user_title",
    "user_author",
    "user_cover_asset_id",
    "primary_category",
    "secondary_category",
    "organization_tags",
    "personal_tags",
    "inspiration",
  ];
  return fields.filter((field) => !sameSnapshotField(left, right, field));
}

export function normalizedTagUnion(existing: string[], incoming: string[]): string[] {
  return normalizedTags([...existing, ...incoming]);
}

export function defaultMergeSnapshot(
  base: CollectionUserSnapshot,
  incoming: CollectionUserSnapshot,
): CollectionUserSnapshot {
  return normalizeSnapshot({
    ...structuredClone(base),
    organization_confirmation: {
      ...base.organization_confirmation,
      organization_tags: normalizedTagUnion(
        base.organization_confirmation.organization_tags,
        incoming.organization_confirmation.organization_tags,
      ),
    },
    personal_tags: normalizedTagUnion(base.personal_tags, incoming.personal_tags),
    inspiration: base.inspiration ? { ...base.inspiration } : null,
  });
}

export function appendInspiration(
  existing: InspirationDraft | null,
  incoming: InspirationDraft | null,
): InspirationDraft | null {
  const oldContent = existing?.content ?? "";
  const newContent = incoming?.content ?? "";
  const hasOldContent = Boolean(pythonTrim(oldContent));
  const hasNewContent = Boolean(pythonTrim(newContent));
  if (!hasOldContent && !hasNewContent) return null;
  return {
    content: hasOldContent && hasNewContent
      ? `${oldContent}\n\n${newContent}`
      : hasOldContent ? oldContent : newContent,
    input_mode: "text",
    transcription_status: "not_applicable",
  };
}

export function threeWayRebase(
  base: CollectionUserSnapshot,
  desired: CollectionUserSnapshot,
  latest: CollectionUserSnapshot,
): ThreeWayResult {
  let rebased = structuredClone(desired);
  const conflicts: EditableField[] = [];
  for (const field of differingFields(base, latest)) {
    const userChanged = !sameSnapshotField(base, desired, field);
    const reached = sameSnapshotField(desired, latest, field);
    if (!userChanged || reached) {
      rebased = setSnapshotField(rebased, field, latest);
    } else {
      conflicts.push(field);
    }
  }
  return { desired: normalizeSnapshot(rebased), conflicts };
}

function characterLength(value: string): number {
  return [...value].length;
}

export function validateSnapshot(snapshot: CollectionUserSnapshot): SnapshotValidation {
  const value = normalizeSnapshot(snapshot);
  const errors: SnapshotValidation["errors"] = {};
  if (value.user_title && characterLength(value.user_title) > 500) errors.user_title = "自定义标题最多 500 个字符。";
  if (value.user_author && characterLength(value.user_author) > 200) errors.user_author = "我补充的作者最多 200 个字符。";
  if (characterLength(value.organization_confirmation.primary_category) > 64) errors.primary_category = "一级分类最多 64 个字符。";
  if (characterLength(value.organization_confirmation.secondary_category) > 64) errors.secondary_category = "二级分类最多 64 个字符。";
  const validateTags = (tags: string[], field: "organization_tags" | "personal_tags", label: string) => {
    if (tags.length > 50) errors[field] = `${label}最多 50 个。`;
    else if (tags.some((tag) => characterLength(tag) > 64)) errors[field] = `${label}单项最多 64 个字符。`;
  };
  validateTags(value.organization_confirmation.organization_tags, "organization_tags", "整理标签");
  validateTags(value.personal_tags, "personal_tags", "个人标签");
  if (value.inspiration && characterLength(value.inspiration.content) > 4000) errors.inspiration = "我的灵感最多 4000 个字符。";
  return { valid: Object.keys(errors).length === 0, errors };
}

export const editableFieldLabels: Record<EditableField, string> = {
  user_title: "自定义标题",
  user_author: "我补充的作者",
  user_cover_asset_id: "我补充的封面",
  primary_category: "一级分类",
  secondary_category: "二级分类",
  organization_tags: "整理标签",
  personal_tags: "个人标签",
  inspiration: "我的灵感",
};
