import { beforeEach, describe, expect, it } from "vitest";
import {
  appendInspiration,
  clearEditMergeSession,
  defaultMergeSnapshot,
  editableTagIdentity,
  normalizeEditableTag,
  normalizeSnapshot,
  normalizedTagUnion,
  readEditMergeSession,
  sameSnapshot,
  setSnapshotField,
  threeWayRebase,
  validateSnapshot,
  writeEditMergeSession,
  type CollectionUserSnapshot,
  type EditMergeSession,
} from "./collection-edit-model";
import {
  PYTHON_CASEFOLD_MAPPING_COUNT,
  PYTHON_CASEFOLD_UNICODE_VERSION,
} from "./unicode-casefold";
import {
  isPythonWhitespaceCodePoint,
  normalizeUnicode15NFKC,
  pythonCollapseWhitespace,
  pythonTrim,
  PYTHON_UNICODE_VERSION,
  PYTHON_WHITESPACE_CODE_POINT_COUNT,
  UNICODE15_ASSIGNED_RANGE_COUNT,
} from "./unicode-normalize";

function snapshot(overrides: Partial<CollectionUserSnapshot> = {}): CollectionUserSnapshot {
  return {
    user_title: "已有标题",
    user_author: null,
    user_cover_asset_id: null,
    organization_confirmation: {
      primary_category: "设计",
      secondary_category: "空间",
      organization_tags: ["自然光"],
    },
    personal_tags: ["参考"],
    inspiration: { content: "已有灵感", input_mode: "voice", transcription_status: "completed" },
    ...overrides,
  };
}

function maximumValidSnapshot(marker: string): CollectionUserSnapshot {
  const fixedLength = (prefix: string, length: number) => `${prefix}${"\0".repeat(length - [...prefix].length)}`;
  const tags = (prefix: string) => Array.from(
    { length: 50 },
    (_, index) => fixedLength(`${prefix}${index.toString().padStart(2, "0")}`, 64),
  );
  return {
    user_title: fixedLength(marker, 500),
    user_author: fixedLength(`${marker}A`, 200),
    user_cover_asset_id: "a".repeat(32),
    organization_confirmation: {
      primary_category: fixedLength(`${marker}P`, 64),
      secondary_category: fixedLength(`${marker}S`, 64),
      organization_tags: tags(`${marker}O`),
    },
    personal_tags: tags(`${marker}U`),
    inspiration: {
      content: fixedLength(`${marker}I`, 4000),
      input_mode: "text",
      transcription_status: "not_applicable",
    },
  };
}

describe("R2.5 编辑与合并模型", () => {
  beforeEach(() => window.sessionStorage.clear());

  it("按 NFKC、空白和大小写身份去重，保留已有顺序后追加新项", () => {
    expect(normalizedTagUnion(["ＡＩ 计划", "Hermes", "Straße"], ["AI   计划", "hermes", "STRASSE", "新标签"]))
      .toEqual(["AI 计划", "Hermes", "Straße", "新标签"]);
  });

  it("个人作者规范化，合并默认保留旧作者与旧封面且三方并发分别冲突", () => {
    const base = snapshot({ user_author: "原作者", user_cover_asset_id: "a".repeat(32) });
    const incoming = snapshot({ user_author: "  新  作者  ", user_cover_asset_id: "b".repeat(32) });
    const merged = defaultMergeSnapshot(base, incoming);
    expect(merged.user_author).toBe("原作者");
    expect(merged.user_cover_asset_id).toBe("a".repeat(32));

    const mine = setSnapshotField(setSnapshotField(base, "user_author", incoming), "user_cover_asset_id", incoming);
    expect(mine.user_author).toBe("  新  作者  ");
    const latest = snapshot({ user_author: "远端作者", user_cover_asset_id: "c".repeat(32) });
    const rebased = threeWayRebase(base, mine, latest);
    expect(rebased.conflicts).toEqual(expect.arrayContaining(["user_author", "user_cover_asset_id"]));

    expect(normalizeSnapshot(incoming)).toMatchObject({
      user_author: "新 作者",
      user_cover_asset_id: "b".repeat(32),
    });
  });

  it("使用 Python Unicode 15 完整 casefold，并覆盖跨脚本与多码点展开", () => {
    expect(PYTHON_CASEFOLD_UNICODE_VERSION).toBe("15.0.0");
    expect(PYTHON_CASEFOLD_MAPPING_COUNT).toBe(1530);
    const equivalents = [
      ["Straße", "STRASSE"],
      ["ᾳ", "αι"],
      ["ΐ", "ι\u0308\u0301"],
      ["Σ", "ς"],
      ["Ꭰ", "ꭰ"],
      ["𐐀", "𐐨"],
      ["Ա", "ա"],
    ] as const;
    for (const [left, right] of equivalents) {
      expect(editableTagIdentity(left)).toBe(editableTagIdentity(right));
    }
    expect(normalizedTagUnion(
      equivalents.map(([left]) => left),
      equivalents.map(([, right]) => right),
    )).toHaveLength(equivalents.length);
  });

  it("冻结 Python Unicode 15 NFKC，隔离新版 Unicode 新分配字符", () => {
    expect(PYTHON_UNICODE_VERSION).toBe("15.0.0");
    expect(UNICODE15_ASSIGNED_RANGE_COUNT).toBe(707);
    expect(normalizeUnicode15NFKC("Ａ①ﬃⅣ")).toBe("A1ffiIV");
    expect(normalizeUnicode15NFKC("A\u030a")).toBe("Å");
    expect(normalizeUnicode15NFKC("\ua7f1")).toBe("\ua7f1");
    expect(normalizeUnicode15NFKC("A\ua7f1\u030a")).toBe("A\ua7f1\u030a");
    expect(editableTagIdentity("\ua7f1")).not.toBe(editableTagIdentity("S"));
  });

  it("完整使用 Python 3.12 whitespace 集合，不把 FEFF 当空白并折叠 NEL", () => {
    const expected = [
      0x09, 0x0a, 0x0b, 0x0c, 0x0d,
      0x1c, 0x1d, 0x1e, 0x1f, 0x20,
      0x85, 0xa0, 0x1680,
      0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006,
      0x2007, 0x2008, 0x2009, 0x200a,
      0x2028, 0x2029, 0x202f, 0x205f, 0x3000,
    ];
    const actual: number[] = [];
    for (let codePoint = 0; codePoint <= 0x10ffff; codePoint += 1) {
      if (isPythonWhitespaceCodePoint(codePoint)) actual.push(codePoint);
    }
    expect(PYTHON_WHITESPACE_CODE_POINT_COUNT).toBe(29);
    expect(actual).toEqual(expected);
    expect(pythonTrim("\u0085标题\u0085")).toBe("标题");
    expect(pythonTrim("\ufeff标题\ufeff")).toBe("\ufeff标题\ufeff");
    expect(pythonCollapseWhitespace("\u0085A\u001cB\ufeffC\u0085")).toBe("A B\ufeffC");
    expect(normalizeEditableTag("\u0085")).toBe("");
    expect(normalizeEditableTag("\ufeff")).toBe("\ufeff");
  });

  it("共享规范器覆盖标题、分类与灵感空白合同", () => {
    const normalized = normalizeSnapshot(snapshot({
      user_title: "\u0085逐字标题\ufeff\u0085",
      organization_confirmation: {
        primary_category: "\u0085ＡＩ\u0085",
        secondary_category: "\u001c工具\u001f",
        organization_tags: [" A\u0085B "],
      },
      inspiration: { content: "\u0085", input_mode: "text", transcription_status: "not_applicable" },
    }));
    expect(normalized.user_title).toBe("逐字标题\ufeff");
    expect(normalized.organization_confirmation.primary_category).toBe("AI");
    expect(normalized.organization_confirmation.secondary_category).toBe("工具");
    expect(normalized.organization_confirmation.organization_tags).toEqual(["A B"]);
    expect(normalized.inspiration).toBeNull();
    expect(normalizeSnapshot(snapshot({
      inspiration: { content: "\ufeff", input_mode: "text", transcription_status: "not_applicable" },
    })).inspiration?.content).toBe("\ufeff");
  });

  it("合并默认保留已有标量和灵感，只将两组标签去重并集", () => {
    const base = snapshot();
    const incoming = snapshot({
      user_title: "新标题",
      organization_confirmation: {
        primary_category: "AI",
        secondary_category: "工具",
        organization_tags: ["自然光", "Agent"],
      },
      personal_tags: ["参考", "效率"],
      inspiration: { content: "新灵感", input_mode: "text", transcription_status: "not_applicable" },
    });
    const desired = defaultMergeSnapshot(base, incoming);
    expect(desired.user_title).toBe("已有标题");
    expect(desired.organization_confirmation.primary_category).toBe("设计");
    expect(desired.organization_confirmation.organization_tags).toEqual(["自然光", "Agent"]);
    expect(desired.personal_tags).toEqual(["参考", "效率"]);
    expect(desired.inspiration).toEqual(base.inspiration);
  });

  it("灵感追加精确使用两个换行，手工合并后改为文字输入", () => {
    const oldContent = "\n 旧内容 \n";
    const newContent = "\n新内容  \n";
    expect(appendInspiration(
      { content: oldContent, input_mode: "voice", transcription_status: "completed" },
      { content: newContent, input_mode: "voice", transcription_status: "completed" },
    )).toEqual({ content: `${oldContent}\n\n${newContent}`, input_mode: "text", transcription_status: "not_applicable" });
    expect(appendInspiration(null, { content: "\n 单侧语音 \n", input_mode: "voice", transcription_status: "completed" }))
      .toEqual({ content: "\n 单侧语音 \n", input_mode: "text", transcription_status: "not_applicable" });
  });

  it("修改其他字段时不改写历史灵感的首尾空白与换行", () => {
    const originalContent = "\n  历史灵感逐字保留  \n\n";
    const base = snapshot({
      inspiration: { content: originalContent, input_mode: "voice", transcription_status: "completed" },
    });
    const changed = setSnapshotField(base, "user_title", snapshot({ user_title: "新标题" }));
    expect(changed.user_title).toBe("新标题");
    expect(changed.inspiration?.content).toBe(originalContent);
  });

  it("三方比较自动吸收未修改字段，只标出 latest/base/desired 真冲突", () => {
    const base = snapshot();
    let desired = setSnapshotField(base, "user_title", snapshot({ user_title: "我的标题" }));
    desired = setSnapshotField(desired, "personal_tags", snapshot({ personal_tags: ["参考", "我的标签"] }));
    const latest = snapshot({
      user_title: "其他位置的标题",
      organization_confirmation: {
        primary_category: "摄影",
        secondary_category: "空间",
        organization_tags: ["自然光"],
      },
    });
    const result = threeWayRebase(base, desired, latest);
    expect(result.conflicts).toEqual(["user_title"]);
    expect(result.desired.organization_confirmation.primary_category).toBe("摄影");
    expect(result.desired.personal_tags).toEqual(["参考", "我的标签"]);
  });

  it("验证长度边界，且标题只裁剪首尾空白、不做 NFKC 改写", () => {
    const base = snapshot();
    const astral = "\u{1f4a1}";
    const normalizedEquivalent = snapshot({ user_title: "  ＩＡ  " });
    const normalizedBase = snapshot({ user_title: "ＩＡ" });
    expect(sameSnapshot(normalizedEquivalent, normalizedBase)).toBe(true);
    expect(sameSnapshot(normalizedEquivalent, snapshot({ user_title: "IA" }))).toBe(false);
    expect(validateSnapshot({ ...base, user_title: astral.repeat(500) }).errors.user_title).toBeUndefined();
    expect(validateSnapshot({ ...base, user_title: astral.repeat(501) }).errors.user_title).toMatch(/500/);
    expect(validateSnapshot({
      ...base,
      organization_confirmation: { ...base.organization_confirmation, primary_category: astral.repeat(64) },
    }).errors.primary_category).toBeUndefined();
    expect(validateSnapshot({
      ...base,
      organization_confirmation: { ...base.organization_confirmation, primary_category: astral.repeat(65) },
    }).errors.primary_category).toMatch(/64/);
    expect(validateSnapshot({ ...base, inspiration: { content: "x".repeat(4001), input_mode: "text", transcription_status: "not_applicable" } }).errors.inspiration)
      .toMatch(/4000/);
  });

  it("只修改其他字段时逐字保留全角标题", () => {
    const base = snapshot({ user_title: "ＩＡ 灵感" });
    const changed = setSnapshotField(
      base,
      "primary_category",
      snapshot({ organization_confirmation: { ...base.organization_confirmation, primary_category: "摄影" } }),
    );
    expect(changed.user_title).toBe("ＩＡ 灵感");
    expect(changed.organization_confirmation.primary_category).toBe("摄影");
  });

  it("当前标签页会话绑定 mode、item 和 route entry，错配时清除", () => {
    const session: EditMergeSession = {
      version: 1,
      mode: "edit",
      itemId: "item-1",
      routeEntryId: "entry-1",
      returnKind: "detail",
      reviewEntryId: null,
      base: snapshot(),
      desired: snapshot(),
      incoming: null,
      expectedRevision: 3,
      requestState: "draft",
      conflictRecovery: null,
      updatedAt: Date.now(),
    };
    expect(writeEditMergeSession(session)).toBe(true);
    expect(readEditMergeSession("edit", "item-1", "entry-1")).toEqual(expect.objectContaining({ expectedRevision: 3 }));
    expect(readEditMergeSession("edit", "item-1", "wrong-entry")).toBeNull();
    expect(readEditMergeSession("edit", "item-1")).toBeNull();
    clearEditMergeSession("edit", "item-1");
  });

  it("current-tab session 强制 mode 关系与灵感合法组合，损坏即清除", () => {
    const base: EditMergeSession = {
      version: 1,
      mode: "merge",
      itemId: "item-1",
      routeEntryId: "merge-entry",
      returnKind: "review",
      reviewEntryId: "review-entry",
      base: snapshot(),
      desired: snapshot(),
      incoming: snapshot(),
      expectedRevision: 3,
      requestState: "draft",
      conflictRecovery: null,
      updatedAt: Date.now(),
    };
    expect(writeEditMergeSession(base)).toBe(true);
    expect(readEditMergeSession("merge", "item-1", "merge-entry")).not.toBeNull();

    const malformed: EditMergeSession[] = [
      { ...base, returnKind: "detail", reviewEntryId: null },
      { ...base, reviewEntryId: "" },
      { ...base, incoming: null },
      { ...base, routeEntryId: "\u0085" },
      { ...base, incoming: snapshot({ inspiration: { content: "非法", input_mode: "voice", transcription_status: "not_applicable" } }) },
      { ...base, incoming: snapshot({ inspiration: { content: "非法", input_mode: "text", transcription_status: "completed" } }) },
      { ...base, incoming: snapshot({ inspiration: { content: "\u0085", input_mode: "text", transcription_status: "not_applicable" } }) },
    ];
    for (const value of malformed) {
      expect(writeEditMergeSession(value)).toBe(true);
      expect(readEditMergeSession("merge", "item-1", value.routeEntryId)).toBeNull();
      expect(window.sessionStorage.length).toBe(0);
    }

    const overlongDraft = {
      ...base,
      incoming: snapshot({ inspiration: { content: "x".repeat(4001), input_mode: "text", transcription_status: "not_applicable" } }),
    };
    expect(writeEditMergeSession(overlongDraft)).toBe(true);
    const restored = readEditMergeSession("merge", "item-1", "merge-entry");
    expect(restored).not.toBeNull();
    expect(validateSnapshot(restored!.incoming!).errors.inspiration).toBe("我的灵感最多 4000 个字符。");
    clearEditMergeSession("merge", "item-1");

    const initialMerge: EditMergeSession = {
      ...base,
      base: null,
      desired: null,
      expectedRevision: null,
      requestState: "draft",
      conflictRecovery: null,
    };
    expect(writeEditMergeSession(initialMerge)).toBe(true);
    expect(readEditMergeSession("merge", "item-1", "merge-entry")).not.toBeNull();
    for (const incomplete of [
      { ...initialMerge, base: snapshot() },
      { ...initialMerge, desired: snapshot() },
      { ...initialMerge, expectedRevision: 3 },
      { ...initialMerge, requestState: "outcome_unknown" as const },
    ]) {
      expect(writeEditMergeSession(incomplete)).toBe(true);
      expect(readEditMergeSession("merge", "item-1", "merge-entry")).toBeNull();
    }

    const incompleteEdit: EditMergeSession = {
      ...base,
      mode: "edit",
      routeEntryId: "edit-incomplete",
      returnKind: "detail",
      reviewEntryId: null,
      incoming: null,
      desired: null,
    };
    expect(writeEditMergeSession(incompleteEdit)).toBe(true);
    expect(readEditMergeSession("edit", "item-1", "edit-incomplete")).toBeNull();

    const malformedEdit: EditMergeSession = {
      ...base,
      mode: "edit",
      routeEntryId: "edit-entry",
      returnKind: "detail",
      reviewEntryId: null,
    };
    expect(writeEditMergeSession(malformedEdit)).toBe(true);
    expect(readEditMergeSession("edit", "item-1", "edit-entry")).toBeNull();
  });

  it("current-tab 容量覆盖合法边界 edit、merge 与完整 conflict recovery，仍拒绝真正超限状态", () => {
    const editSnapshot = maximumValidSnapshot("E");
    expect(validateSnapshot(editSnapshot)).toEqual({ valid: true, errors: {} });
    const editSession: EditMergeSession = {
      version: 1,
      mode: "edit",
      itemId: "maximum-edit-item",
      routeEntryId: "maximum-edit-entry",
      returnKind: "detail",
      reviewEntryId: null,
      base: editSnapshot,
      desired: editSnapshot,
      incoming: null,
      expectedRevision: 3,
      requestState: "draft",
      conflictRecovery: null,
      updatedAt: Date.now(),
    };
    expect(new TextEncoder().encode(JSON.stringify(editSession)).byteLength).toBeGreaterThan(64 * 1024);
    expect(writeEditMergeSession(editSession)).toBe(true);
    expect(readEditMergeSession("edit", editSession.itemId, editSession.routeEntryId)?.desired)
      .toEqual(editSnapshot);

    const incoming = maximumValidSnapshot("I");
    const mergeSession: EditMergeSession = {
      ...editSession,
      mode: "merge",
      itemId: "maximum-merge-item",
      routeEntryId: "maximum-merge-entry",
      returnKind: "review",
      reviewEntryId: "maximum-review-entry",
      incoming,
    };
    expect(writeEditMergeSession(mergeSession)).toBe(true);
    expect(readEditMergeSession("merge", mergeSession.itemId, mergeSession.routeEntryId)?.incoming)
      .toEqual(incoming);

    const latest = maximumValidSnapshot("L");
    const mine = maximumValidSnapshot("M");
    const conflictSession: EditMergeSession = {
      ...mergeSession,
      itemId: "maximum-conflict-item",
      routeEntryId: "maximum-conflict-entry",
      base: latest,
      desired: mine,
      expectedRevision: 8,
      conflictRecovery: {
        latest,
        mine,
        conflictFields: [
          "user_title",
          "primary_category",
          "secondary_category",
          "organization_tags",
          "personal_tags",
          "inspiration",
        ],
        choices: {},
        latestRevision: 8,
      },
    };
    const conflictBytes = new TextEncoder().encode(JSON.stringify(conflictSession)).byteLength;
    expect(conflictBytes).toBeGreaterThan(300 * 1024);
    expect(conflictBytes).toBeLessThan(512 * 1024);
    expect(writeEditMergeSession(conflictSession)).toBe(true);
    expect(readEditMergeSession("merge", conflictSession.itemId, conflictSession.routeEntryId)?.conflictRecovery)
      .toEqual(conflictSession.conflictRecovery);

    window.sessionStorage.clear();
    const oversized = {
      ...editSession,
      itemId: "oversized-item",
      routeEntryId: "oversized-entry",
      desired: snapshot({
        inspiration: {
          content: "\0".repeat(90_000),
          input_mode: "text",
          transcription_status: "not_applicable",
        },
      }),
    };
    expect(new TextEncoder().encode(JSON.stringify(oversized)).byteLength).toBeGreaterThan(512 * 1024);
    expect(writeEditMergeSession(oversized)).toBe(false);
    expect(readEditMergeSession("edit", oversized.itemId, oversized.routeEntryId)).toBeNull();
    expect(window.sessionStorage.length).toBe(0);
  });
});
