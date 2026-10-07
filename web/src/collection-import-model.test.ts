import { describe, expect, it } from "vitest";
import {
  canCancelCollectionImportBatch,
  canConfirmCollectionImportBatch,
  canResumeCollectionImportBatch,
  collectionImportConfirmationCounts,
  collectionImportCreateItems,
  collectionImportDisplayPosition,
  collectionImportRequestFingerprint,
  hasCollectionImportOutcomeUnknown,
  isCollectionImportSaveActive,
  shouldPollCollectionImportBatch,
  unicodeScalarLength,
  utf8Length,
  validateCollectionImportDraft,
} from "./collection-import-model";
import type { CollectionImportBatchSnapshot, CollectionImportItemStatus } from "./collection-import-api";
import {
  canRecordCollectionImportDecision,
  canRepreviewCollectionImportItem,
  formatCollectionImportReviewValue,
  sameCollectionImportReviewDraft,
  validateCollectionImportReviewDraft,
} from "./collection-import-review-model";

function item(localId: string, inputText: string) {
  return { localId, inputText };
}

describe("collection import model", () => {
  it("按 Unicode scalar 计字符并按 UTF-8 计字节", () => {
    expect(unicodeScalarLength("A😀中")).toBe(3);
    expect(utf8Length("A😀中")).toBe(8);
  });

  it("只在可见层把后端零基 position 转为一基序号", () => {
    expect(collectionImportDisplayPosition(0)).toBe(1);
    expect(collectionImportDisplayPosition(9)).toBe(10);
  });

  it("在提交前整体约束 2 至 10 项、非空、单项字符与总字节", () => {
    expect(validateCollectionImportDraft([item("a", "https://example.com")])).toMatchObject({ valid: false, summary: "每个批次需要 2 至 10 项。" });
    expect(validateCollectionImportDraft([item("a", ""), item("b", "https://example.com")])).toMatchObject({ valid: false, itemErrors: [expect.stringContaining("完整分享文本"), ""] });
    expect(validateCollectionImportDraft([item("a", "a".repeat(10_001)), item("b", "b")])).toMatchObject({ valid: false, itemErrors: [expect.stringContaining("10,001"), ""] });
    expect(validateCollectionImportDraft([item("a", "中".repeat(10_000)), item("b", "中".repeat(10_000)), item("c", "中".repeat(2_000))])).toMatchObject({ valid: false, summary: expect.stringContaining("66,000") });
  });

  it("创建请求保持输入原序与精确文本，客户端身份不含输入", () => {
    const draft = [item("private-a", " 第一条\nhttps://example.com/a "), item("private-b", "第二条 https://example.com/b")];
    const payload = collectionImportCreateItems(draft);
    expect(payload).toEqual([
      { client_item_id: "item-01", input_text: " 第一条\nhttps://example.com/a " },
      { client_item_id: "item-02", input_text: "第二条 https://example.com/b" },
    ]);
    expect(payload[0].client_item_id).not.toContain("private");
  });

  it("相同有序载荷指纹稳定，文本或顺序变化会改变指纹", () => {
    const first = [item("a", "A"), item("b", "B")];
    expect(collectionImportRequestFingerprint(first)).toBe(collectionImportRequestFingerprint(first.map((entry) => ({ ...entry }))));
    expect(collectionImportRequestFingerprint(first)).not.toBe(collectionImportRequestFingerprint([first[1], first[0]]));
    expect(collectionImportRequestFingerprint(first)).not.toBe(collectionImportRequestFingerprint([item("a", "A "), item("b", "B")]));
  });

  it("确认资格同时要求权威审核完整、无 dirty、无 preview active 与 outcome_unknown", () => {
    const makeBatch = (states: CollectionImportItemStatus[] = ["ready", "failed"]): CollectionImportBatchSnapshot => ({
      batch_id: "batch-1",
      status: "awaiting_review",
      revision: 8,
      total: states.length,
      queued: 0,
      previewing: 0,
      ready: states.filter((state) => state === "ready").length,
      needs_review: 0,
      duplicates: 0,
      already_exists: 0,
      failed: states.filter((state) => state === "failed").length,
      selected: 1,
      created_at: "2026-08-31T10:00:00Z",
      updated_at: "2026-08-31T10:01:00Z",
      terminal_at: null,
      items: states.map((state, index) => ({
        batch_item_id: `item-${index + 1}`,
        client_item_id: `item-0${index + 1}`,
        position: index,
        display_label: `第 ${index + 1} 项`,
        state,
        decision: index === 0 ? "save" : "skip",
        item_revision: 3,
        preview_generation: 1,
        duplicate_of_batch_item_id: null,
        collection_item_id: null,
        error_code: null,
        terminal_reason: null,
      })),
    });
    const ready = makeBatch();

    expect(canConfirmCollectionImportBatch(ready)).toBe(true);
    expect(canConfirmCollectionImportBatch(ready, true)).toBe(false);
    expect(canConfirmCollectionImportBatch({ ...ready, items: ready.items.map((item, index) => index === 1 ? { ...item, decision: "pending" } : item) })).toBe(false);
    expect(canConfirmCollectionImportBatch(makeBatch(["ready", "previewing"]))).toBe(false);
    expect(canConfirmCollectionImportBatch(makeBatch(["ready", "outcome_unknown"]))).toBe(false);
    expect(canConfirmCollectionImportBatch({ ...ready, status: "saving" })).toBe(false);
  });

  it("确认摘要只计算安全类别，串行保存活跃状态只认 save_queued/saving", () => {
    const fixture = {
      batch_id: "batch-1",
      status: "awaiting_review",
      revision: 9,
      total: 6,
      queued: 0,
      previewing: 0,
      ready: 2,
      needs_review: 0,
      duplicates: 1,
      already_exists: 1,
      failed: 1,
      selected: 1,
      created_at: "2026-08-31T10:00:00Z",
      updated_at: "2026-08-31T10:01:00Z",
      terminal_at: null,
      items: ["ready", "ready", "duplicate_in_batch", "already_exists", "failed", "skipped"].map((state, index) => ({
        batch_item_id: `item-${index}`,
        client_item_id: `client-${index}`,
        position: index,
        display_label: `第 ${index + 1} 项`,
        state: state as CollectionImportItemStatus,
        decision: index === 0 ? "save" as const : index === 1 || index === 4 ? "skip" as const : "pending" as const,
        item_revision: 2,
        preview_generation: 1,
        duplicate_of_batch_item_id: state === "duplicate_in_batch" ? "item-0" : null,
        collection_item_id: state === "already_exists" ? "material-1" : null,
        error_code: null,
        terminal_reason: null,
      })),
    } satisfies CollectionImportBatchSnapshot;

    expect(collectionImportConfirmationCounts(fixture)).toEqual({ save: 1, skip: 2, duplicate: 1, existing: 1, failed: 1 });
    expect(isCollectionImportSaveActive(fixture)).toBe(false);
    expect(isCollectionImportSaveActive({ ...fixture, items: fixture.items.map((item, index) => index === 0 ? { ...item, state: "save_queued" } : item) })).toBe(true);
  });

  it("partial retry 计数只包含本轮可冻结决定，不重复计算历史 saved/already_exists/skipped", () => {
    const states: CollectionImportItemStatus[] = ["failed", "ready", "saved", "already_exists", "skipped"];
    const partial = {
      batch_id: "batch-1",
      status: "awaiting_review",
      revision: 12,
      total: states.length,
      queued: 0,
      previewing: 0,
      ready: 1,
      needs_review: 0,
      duplicates: 0,
      already_exists: 1,
      failed: 1,
      selected: 3,
      created_at: "2026-08-31T10:00:00Z",
      updated_at: "2026-08-31T10:01:00Z",
      terminal_at: null,
      items: states.map((state, index) => ({
        batch_item_id: `item-${index}`,
        client_item_id: `client-${index}`,
        position: index,
        display_label: `第 ${index + 1} 项`,
        state,
        decision: index === 1 || index === 4 ? "skip" as const : "save" as const,
        item_revision: 4,
        preview_generation: 1,
        duplicate_of_batch_item_id: null,
        collection_item_id: state === "already_exists" ? "material-1" : null,
        error_code: null,
        terminal_reason: null,
      })),
    } satisfies CollectionImportBatchSnapshot;

    expect(collectionImportConfirmationCounts(partial)).toEqual({ save: 1, skip: 1, duplicate: 0, existing: 1, failed: 1 });
    expect(canConfirmCollectionImportBatch(partial)).toBe(true);
  });

  it("批次动作与轮询只按权威生命周期开放，不把终态伪装成可恢复或可取消", () => {
    const fixture: CollectionImportBatchSnapshot = {
      batch_id: "batch-1",
      status: "interrupted",
      revision: 4,
      total: 1,
      queued: 0,
      previewing: 0,
      ready: 0,
      needs_review: 0,
      duplicates: 0,
      already_exists: 0,
      failed: 0,
      selected: 1,
      created_at: "2026-08-31T10:00:00Z",
      updated_at: "2026-08-31T10:01:00Z",
      terminal_at: null,
      items: [{
        batch_item_id: "item-1",
        client_item_id: "client-1",
        position: 0,
        display_label: "第一项",
        state: "outcome_unknown",
        decision: "save",
        item_revision: 3,
        preview_generation: 1,
        duplicate_of_batch_item_id: null,
        collection_item_id: null,
        error_code: null,
        terminal_reason: null,
      }],
    };

    expect(canResumeCollectionImportBatch(fixture)).toBe(true);
    expect(canCancelCollectionImportBatch(fixture)).toBe(true);
    expect(hasCollectionImportOutcomeUnknown(fixture)).toBe(true);
    expect(shouldPollCollectionImportBatch(fixture)).toBe(false);
    expect(canResumeCollectionImportBatch({ ...fixture, status: "awaiting_review" })).toBe(false);
    expect(shouldPollCollectionImportBatch({ ...fixture, status: "cancelling" })).toBe(true);
    for (const status of ["cancelling", "completed", "completed_with_issues", "cancelled"] as const) {
      expect(canCancelCollectionImportBatch({ ...fixture, status })).toBe(false);
    }
  });
});

describe("collection import review model", () => {
  const draft = {
    user_title: null,
    untitled_confirmed: false,
    organization_confirmation: { primary_category: "", secondary_category: "", organization_tags: [] as string[] },
    personal_tags: [] as string[],
    inspiration: null,
  };

  it("needs_review 保存要求标题或明确无标题，skip 不会伪造字段要求", () => {
    expect(validateCollectionImportReviewDraft(draft, "save", "needs_review")).toMatchObject({
      valid: false,
      errors: { user_title: expect.stringContaining("明确确认") },
    });
    expect(validateCollectionImportReviewDraft({ ...draft, untitled_confirmed: true }, "save", "needs_review").valid).toBe(true);
    expect(validateCollectionImportReviewDraft(draft, "skip", "needs_review").valid).toBe(true);
  });

  it("draft 比较包含 untitled_confirmed 且不改变调用方值", () => {
    const same = structuredClone(draft);
    expect(sameCollectionImportReviewDraft(draft, same)).toBe(true);
    same.untitled_confirmed = true;
    expect(sameCollectionImportReviewDraft(draft, same)).toBe(false);
    expect(draft.untitled_confirmed).toBe(false);
  });

  it("冲突中的灵感显示可比较原文，不把不同内容折叠成同一占位", () => {
    expect(formatCollectionImportReviewValue({ content: "最新灵感", input_mode: "text" })).toBe("最新灵感");
    expect(formatCollectionImportReviewValue({ content: "我的灵感", input_mode: "text" })).toBe("我的灵感");
  });

  it("全批 preview 活跃时暂停 PATCH，危险权威状态不开放 repreview", () => {
    expect(canRecordCollectionImportDecision({ state: "ready" }, "save", true)).toBe(false);
    expect(canRecordCollectionImportDecision({ state: "failed" }, "skip", false)).toBe(true);
    for (const state of ["duplicate_in_batch", "save_queued", "saving", "saved", "already_exists", "skipped", "cancelled", "outcome_unknown"] as const) {
      expect(canRepreviewCollectionImportItem({ state, error_stage: "preview", preview_generation: 1 }, false)).toBe(false);
    }
    expect(canRepreviewCollectionImportItem({ state: "failed", error_stage: "save", preview_generation: 1 }, false)).toBe(false);
    expect(canRepreviewCollectionImportItem({ state: "failed", error_stage: "preview", preview_generation: 1 }, false)).toBe(true);
    expect(canRepreviewCollectionImportItem({ state: "ready", error_stage: null, preview_generation: 5 }, false)).toBe(false);
  });

  it("已知未写入的 save failure 可重新记录保存，preview failure 仍只能重试或跳过", () => {
    expect(canRecordCollectionImportDecision({ state: "failed", error_stage: "save" }, "save", false)).toBe(true);
    expect(canRecordCollectionImportDecision({ state: "failed", error_stage: "preview" }, "save", false)).toBe(false);
    expect(canRecordCollectionImportDecision({ state: "failed", error_stage: "save" }, "skip", false)).toBe(true);
  });
});
