import {
  ArrowsClockwise,
  Check,
  CircleNotch,
  FloppyDisk,
  FolderOpen,
  WarningCircle,
} from "@phosphor-icons/react";
import { Button } from "@radix-ui/themes";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  CollectionImportApiError,
  collectionImportApi,
  type CollectionImportBatchSnapshot,
  type CollectionImportDecision,
  type CollectionImportItemDetail,
} from "./collection-import-api";
import { editableTagIdentity, normalizeEditableTag } from "./collection-edit-model";
import {
  canRecordCollectionImportDecision,
  canRepreviewCollectionImportItem,
  cloneCollectionImportReviewDraft,
  collectionImportReviewFieldLabels,
  collectionImportReviewValue,
  formatCollectionImportReviewValue,
  isCollectionImportReviewPaused,
  normalizeCollectionImportReviewDraft,
  sameCollectionImportReviewDraft,
  setCollectionImportReviewValue,
  validateCollectionImportReviewDraft,
  type CollectionImportReviewDraft,
  type CollectionImportReviewField,
} from "./collection-import-review-model";
import { collectionImportStatusLabel, unicodeScalarLength } from "./collection-import-model";
import { TagEditor } from "./collection-tag-editor";

export interface CollectionImportGuardIntent {
  run: () => void;
  history?: boolean;
  returnFocus?: HTMLElement | null;
}

export type CollectionImportLeaveGuard = (intent: CollectionImportGuardIntent) => boolean;

interface ConflictState {
  command: "patch" | "repreview" | "refresh";
  latestBatch: CollectionImportBatchSnapshot;
  latest: CollectionImportItemDetail;
  mineDraft: CollectionImportReviewDraft;
  mineDecision: CollectionImportDecision;
  choices: Partial<Record<CollectionImportReviewField, "latest" | "mine">>;
  explicitDecision?: "save" | "skip";
}

const conflictFields: CollectionImportReviewField[] = [
  "decision",
  "user_title",
  "untitled_confirmed",
  "primary_category",
  "secondary_category",
  "organization_tags",
  "personal_tags",
  "inspiration",
];

function orderedSnapshot(snapshot: CollectionImportBatchSnapshot): CollectionImportBatchSnapshot {
  return { ...snapshot, items: [...snapshot.items].sort((left, right) => left.position - right.position) };
}

function summaryDetail(
  detail: CollectionImportItemDetail,
  snapshot: CollectionImportBatchSnapshot,
  draft: CollectionImportReviewDraft,
): CollectionImportItemDetail {
  const summary = snapshot.items.find((item) => item.batch_item_id === detail.batch_item_id);
  return summary ? { ...detail, ...summary, batch_revision: snapshot.revision, draft } : detail;
}

function reviewStateNotice(detail: CollectionImportItemDetail) {
  if (detail.state === "queued" || detail.state === "previewing") {
    return { title: "来源正在重新检查", body: "审核暂时冻结。普通状态更新不会移动焦点或替你做决定。" };
  }
  if (detail.state === "preview_expired") {
    return { title: "预览已过期", body: "必须显式重新检查来源后，才能基于新预览重新确认审核。" };
  }
  if (detail.state === "duplicate_in_batch") {
    return { title: "这是批内重复项", body: "系统只标记最早位置为代表项，不会自动晋升、合并或保存本项。" };
  }
  if (detail.state === "already_exists") {
    return { title: "这项已在素材库", body: "不会覆盖或合并既有收藏；如需修改，请打开既有收藏后走原编辑流程。" };
  }
  if (detail.state === "failed") {
    if (detail.error_stage === "preview" && detail.preview_generation >= 5) {
      return {
        title: "来源检查次数已达上限",
        body: "不能再次检查；可跳过本项、取消批次，或在新批次重新导入。",
      };
    }
    if (detail.error_stage === "save") {
      return { title: "上次保存确定未写入", body: "可以重新记录保存决定并再次确认，或明确跳过本项。" };
    }
    return detail.error_stage === "preview"
      ? { title: "来源检查失败", body: "可以显式重新检查，或明确跳过本项。" }
      : { title: "本项处理失败", body: "当前只能明确跳过，或刷新权威状态。" };
  }
  if (detail.state === "outcome_unknown") {
    return { title: "结果仍待核对", body: "在本地对账收敛前，不能记录决定或重新检查来源；请在批次动作区刷新或显式恢复。" };
  }
  if (detail.state === "save_queued" || detail.state === "saving") {
    return { title: "保存阶段已冻结审核", body: "正在提交的项不能强制中止；可在批次动作区按合同请求取消，或等待权威状态收敛。" };
  }
  if (["saved", "skipped", "cancelled"].includes(detail.state)) {
    return { title: collectionImportStatusLabel(detail.state), body: "这是权威终态，本页不会改写结果。" };
  }
  return null;
}

export function CollectionImportReviewControls({
  batch,
  detail: incomingDetail,
  onBatchSnapshot,
  onAuthoritativeDetail,
  onOpenExisting,
  registerGuard,
  batchConfirmEnabled = false,
  batchConfirmCount = 0,
  onOpenBatchConfirm,
  onDirtyChange,
  onAnnouncement,
  confirmationOpen = false,
  authorityBlocked = false,
  batchCommandBlocked = false,
}: {
  batch: CollectionImportBatchSnapshot;
  detail: CollectionImportItemDetail;
  onBatchSnapshot: (snapshot: CollectionImportBatchSnapshot) => void;
  onAuthoritativeDetail: (detail: CollectionImportItemDetail) => void;
  onOpenExisting: (collectionItemId: string) => void;
  registerGuard: (guard: CollectionImportLeaveGuard | null) => void;
  batchConfirmEnabled?: boolean;
  batchConfirmCount?: number;
  onOpenBatchConfirm?: (trigger: HTMLButtonElement) => void;
  onDirtyChange?: (dirty: boolean) => void;
  onAnnouncement?: (message: string) => void;
  confirmationOpen?: boolean;
  authorityBlocked?: boolean;
  batchCommandBlocked?: boolean;
}) {
  const identity = `${incomingDetail.batch_id}:${incomingDetail.batch_item_id}`;
  const [authority, setAuthority] = useState(incomingDetail);
  const [baseDraft, setBaseDraft] = useState(() => cloneCollectionImportReviewDraft(incomingDetail.draft));
  const [draft, setDraft] = useState(() => cloneCollectionImportReviewDraft(incomingDetail.draft));
  const [baseDecision, setBaseDecision] = useState<CollectionImportDecision>(incomingDetail.decision);
  const [pendingTagDraft, setPendingTagDraft] = useState({ organization: "", personal: "" });
  const [tagEditorKey, setTagEditorKey] = useState(0);
  const [busy, setBusy] = useState<"patch" | "repreview" | "refresh" | null>(null);
  const [message, setMessage] = useState("");
  const [blockingError, setBlockingError] = useState("");
  const [validationErrors, setValidationErrors] = useState<Record<string, string>>({});
  const [pendingIntent, setPendingIntent] = useState<CollectionImportGuardIntent | null>(null);
  const [repreviewOpen, setRepreviewOpen] = useState(false);
  const [replacementInput, setReplacementInput] = useState("");
  const [replacementTouched, setReplacementTouched] = useState(false);
  const [conflict, setConflict] = useState<ConflictState | null>(null);
  const [authorityUnknown, setAuthorityUnknown] = useState(false);
  const identityRef = useRef(identity);
  const bypassHistory = useRef(false);
  const pendingIntentRef = useRef<CollectionImportGuardIntent | null>(null);
  const errorRef = useRef<HTMLDivElement | null>(null);
  const statusRef = useRef<HTMLHeadingElement | null>(null);
  const authorityRefreshTriggerRef = useRef<HTMLButtonElement | null>(null);
  const authorityRefreshTriggerIdentityRef = useRef("");
  const authorityRefreshFocusIdentityRef = useRef("");
  const repreviewTriggerRef = useRef<HTMLButtonElement | null>(null);
  const repreviewHeadingRef = useRef<HTMLHeadingElement | null>(null);
  const conflictHeadingRef = useRef<HTMLHeadingElement | null>(null);
  const dirtyHeadingRef = useRef<HTMLHeadingElement | null>(null);
  const stateAnnouncementIdentity = useRef("");
  const messageAnnouncementIdentity = useRef("");
  const dirtyAnnouncementIdentity = useRef("");

  const reviewDraftDirty = useMemo(() => (
    !sameCollectionImportReviewDraft(baseDraft, draft)
    || Boolean(normalizeEditableTag(pendingTagDraft.organization))
    || Boolean(normalizeEditableTag(pendingTagDraft.personal))
  ), [baseDraft, draft, pendingTagDraft]);
  const dirty = reviewDraftDirty || replacementTouched;
  const summary = batch.items.find((item) => item.batch_item_id === authority.batch_item_id);
  const current = summary ? { ...authority, ...summary, batch_revision: batch.revision } : authority;
  const reviewPaused = isCollectionImportReviewPaused(batch.items);
  const formDisabled = busy !== null
    || conflict !== null
    || repreviewOpen
    || confirmationOpen
    || authorityBlocked
    || batchCommandBlocked
    || authorityUnknown
    || reviewPaused
    || !["ready", "needs_review", "preview_expired", "failed"].includes(current.state);

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  useEffect(() => {
    if (identityRef.current !== identity) {
      identityRef.current = identity;
      setAuthority(incomingDetail);
      setBaseDraft(cloneCollectionImportReviewDraft(incomingDetail.draft));
      setDraft(cloneCollectionImportReviewDraft(incomingDetail.draft));
      setBaseDecision(incomingDetail.decision);
      setPendingTagDraft({ organization: "", personal: "" });
      setTagEditorKey((value) => value + 1);
      setBusy(null);
      setMessage("");
      setBlockingError("");
      setValidationErrors({});
      setPendingIntent(null);
      pendingIntentRef.current = null;
      authorityRefreshTriggerIdentityRef.current = "";
      authorityRefreshFocusIdentityRef.current = "";
      setRepreviewOpen(false);
      setReplacementInput("");
      setReplacementTouched(false);
      setConflict(null);
      setAuthorityUnknown(false);
      return;
    }
    if (incomingDetail.item_revision <= authority.item_revision || dirty || conflict || busy) return;
    setAuthority(incomingDetail);
    setBaseDraft(cloneCollectionImportReviewDraft(incomingDetail.draft));
    setDraft(cloneCollectionImportReviewDraft(incomingDetail.draft));
    setBaseDecision(incomingDetail.decision);
  }, [authority.item_revision, busy, conflict, dirty, identity, incomingDetail]);

  useEffect(() => {
    if (!dirty) return undefined;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  useEffect(() => {
    if (blockingError) errorRef.current?.focus();
  }, [blockingError]);

  const conflictOpen = conflict !== null;
  useEffect(() => {
    if (conflictOpen) conflictHeadingRef.current?.focus();
  }, [conflictOpen]);

  useEffect(() => {
    if (pendingIntent) dirtyHeadingRef.current?.focus();
  }, [pendingIntent]);

  useLayoutEffect(() => {
    if (busy !== null) return;
    const focusIdentity = authorityRefreshFocusIdentityRef.current;
    if (!focusIdentity) return;
    authorityRefreshFocusIdentityRef.current = "";
    if (focusIdentity !== identity) return;
    const trigger = authorityRefreshTriggerIdentityRef.current === focusIdentity
      ? authorityRefreshTriggerRef.current
      : null;
    if (trigger?.isConnected) trigger.focus();
    else statusRef.current?.focus();
    authorityRefreshTriggerIdentityRef.current = "";
  }, [authorityUnknown, busy, current.state, identity]);

  const guard = useCallback<CollectionImportLeaveGuard>((intent) => {
    if (intent.history && bypassHistory.current) {
      bypassHistory.current = false;
      return false;
    }
    if (!dirty) return false;
    if (pendingIntentRef.current) return true;
    pendingIntentRef.current = intent;
    setPendingIntent(intent);
    return true;
  }, [dirty]);

  useEffect(() => {
    registerGuard(guard);
    return () => registerGuard(null);
  }, [guard, registerGuard]);

  const runOnce = (intent: CollectionImportGuardIntent | null) => {
    if (!intent) return;
    if (intent.history) bypassHistory.current = true;
    intent.run();
  };

  const withPendingTags = (source: CollectionImportReviewDraft) => {
    const candidate = cloneCollectionImportReviewDraft(source);
    const append = (values: string[], pending: string) => {
      const normalized = normalizeEditableTag(pending);
      if (!normalized) return values;
      const identity = editableTagIdentity(normalized);
      return values.some((value) => editableTagIdentity(value) === identity)
        ? values
        : [...values, normalized];
    };
    candidate.organization_confirmation.organization_tags = append(
      candidate.organization_confirmation.organization_tags,
      pendingTagDraft.organization,
    );
    candidate.personal_tags = append(candidate.personal_tags, pendingTagDraft.personal);
    return candidate;
  };

  const acceptSnapshot = (
    snapshot: CollectionImportBatchSnapshot,
    submittedDraft: CollectionImportReviewDraft,
    submittedDecision: CollectionImportDecision,
  ) => {
    const ordered = orderedSnapshot(snapshot);
    const normalized = normalizeCollectionImportReviewDraft(submittedDraft);
    const nextDetail = summaryDetail(authority, ordered, normalized);
    setAuthority(nextDetail);
    setBaseDraft(cloneCollectionImportReviewDraft(normalized));
    setDraft(cloneCollectionImportReviewDraft(normalized));
    setBaseDecision(submittedDecision);
    setPendingTagDraft({ organization: "", personal: "" });
    setTagEditorKey((value) => value + 1);
    setValidationErrors({});
    setConflict(null);
    setAuthorityUnknown(false);
    onBatchSnapshot(ordered);
    onAuthoritativeDetail(nextDetail);
  };

  const getAuthority = async () => {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const latestBatch = orderedSnapshot(await collectionImportApi.getBatch(authority.batch_id));
      const latest = await collectionImportApi.getItem(authority.batch_id, authority.batch_item_id);
      if (latestBatch.batch_id !== authority.batch_id
        || latest.batch_id !== authority.batch_id
        || latest.batch_item_id !== authority.batch_item_id) {
        throw new Error("authority identity mismatch");
      }
      if (latestBatch.revision === latest.batch_revision) return { latestBatch, latest };
    }
    throw new Error("authority revision mismatch");
  };

  const reconcilePatch = async (
    error: unknown,
    submittedDraft: CollectionImportReviewDraft,
    submittedDecision: Exclude<CollectionImportDecision, "pending">,
    expectedBatchRevision: number,
    expectedItemRevision: number,
    conflictCommand: "patch" | "repreview" = "patch",
  ) => {
    const apiError = error instanceof CollectionImportApiError ? error : null;
    const isConflict = apiError?.code === "BATCH_REVISION_CONFLICT" || apiError?.code === "BATCH_ITEM_REVISION_CONFLICT";
    const isUnknown = apiError?.code === "NETWORK_ERROR"
      || apiError?.code === "INVALID_RESPONSE"
      || apiError?.status === 0
      || !apiError;
    const isStateConflict = apiError?.code === "BATCH_STATE_CONFLICT";
    if (!isConflict && !isUnknown && !isStateConflict) throw error;
    try {
      const { latestBatch, latest } = await getAuthority();
      onBatchSnapshot(latestBatch);
      onAuthoritativeDetail(latest);
      const commandObserved = latest.decision === submittedDecision
        && sameCollectionImportReviewDraft(latest.draft, submittedDraft)
        && (latest.batch_revision > expectedBatchRevision || latest.item_revision > expectedItemRevision);
      if (isUnknown && commandObserved) {
        acceptSnapshot(latestBatch, latest.draft, latest.decision);
        setMessage(submittedDecision === "save" ? "已记录保存决定" : "已记录跳过决定");
        return "observed" as const;
      }
      setAuthority(latest);
      if (isStateConflict) {
        const advanced = latest.batch_revision > expectedBatchRevision || latest.item_revision > expectedItemRevision;
        const latestPaused = isCollectionImportReviewPaused(latestBatch.items);
        if (advanced && canRecordCollectionImportDecision(latest, submittedDecision, latestPaused)) {
          setConflict({
            command: conflictCommand,
            latestBatch,
            latest,
            mineDraft: cloneCollectionImportReviewDraft(submittedDraft),
            mineDecision: submittedDecision,
            choices: {},
          });
          setBlockingError("");
          return "conflict" as const;
        }
        setConflict(null);
        setAuthorityUnknown(false);
        setBlockingError("权威状态已改变，本次决定没有记录。已保留本地修改，请按最新状态处理。 ");
        return "refreshed" as const;
      }
      const exactUnchanged = latest.batch_revision === expectedBatchRevision
        && latest.item_revision === expectedItemRevision;
      if (isUnknown && exactUnchanged) {
        setBlockingError("权威状态证明上次请求未生效。已保留本地修改；如仍要记录，请再次明确操作。 ");
        return "not_observed" as const;
      }
      const advanced = latest.batch_revision > expectedBatchRevision || latest.item_revision > expectedItemRevision;
      if (!advanced) {
        setAuthorityUnknown(true);
        setBlockingError("权威 revision 无法与上次请求一致对账。已保留本地修改，危险操作继续阻断。 ");
        return "blocked" as const;
      }
      const latestPaused = isCollectionImportReviewPaused(latestBatch.items);
      if (!canRecordCollectionImportDecision(latest, submittedDecision, latestPaused)) {
        setConflict(null);
        setAuthorityUnknown(false);
        setBlockingError("权威 revision 已前进，但当前状态不允许记录这项决定。无法把本次请求认定为已生效；本地修改仍保留，请按最新状态处理。 ");
        return "refreshed" as const;
      }
      setConflict({
        command: conflictCommand,
        latestBatch,
        latest,
        mineDraft: cloneCollectionImportReviewDraft(submittedDraft),
        mineDecision: submittedDecision,
        choices: {},
      });
      setBlockingError("");
      return "conflict" as const;
    } catch {
      setAuthorityUnknown(true);
      setBlockingError("无法确认上次审核请求是否生效。已保留本地修改；请先刷新权威状态。 ");
      return "blocked" as const;
    }
  };

  const submitDecision = async (
    decision: Exclude<CollectionImportDecision, "pending">,
    afterSuccess: CollectionImportGuardIntent | null = null,
    override?: { draft: CollectionImportReviewDraft; batchRevision: number; itemRevision: number },
  ) => {
    if (busy) return;
    if (!canRecordCollectionImportDecision(current, decision, reviewPaused) || authorityUnknown) {
      setBlockingError("最新权威状态不允许记录这项决定。已保留本地修改。 ");
      return;
    }
    const candidate = override?.draft ?? withPendingTags(draft);
    const validation = validateCollectionImportReviewDraft(candidate, decision, current.state);
    if (!validation.valid) {
      setValidationErrors(validation.errors);
      setBlockingError("请先修正标出的审核字段，再记录决定。 ");
      return;
    }
    const expectedBatchRevision = override?.batchRevision ?? authority.batch_revision;
    const expectedItemRevision = override?.itemRevision ?? authority.item_revision;
    setBusy("patch");
    setBlockingError("");
    setMessage("");
    try {
      const snapshot = await collectionImportApi.updateItem(authority.batch_id, authority.batch_item_id, {
        expected_batch_revision: expectedBatchRevision,
        expected_item_revision: expectedItemRevision,
        decision,
        ...validation.value,
      });
      acceptSnapshot(snapshot, validation.value, decision);
      setMessage(decision === "save" ? "已记录保存决定" : "已记录跳过决定");
      setPendingIntent(null);
      pendingIntentRef.current = null;
      runOnce(afterSuccess);
    } catch (error) {
      try {
        const result = await reconcilePatch(error, validation.value, decision, expectedBatchRevision, expectedItemRevision);
        if (result === "observed") {
          setPendingIntent(null);
          pendingIntentRef.current = null;
          runOnce(afterSuccess);
        }
      } catch {
        setBlockingError("这次没有记录决定。请按提示检查权威状态后重试。 ");
      }
    } finally {
      setBusy(null);
    }
  };

  const refreshAuthority = async () => {
    if (busy) return;
    authorityRefreshFocusIdentityRef.current = "";
    const expectedBatchRevision = authority.batch_revision;
    const expectedItemRevision = authority.item_revision;
    const mineDraft = withPendingTags(draft);
    const mineDecision = baseDecision;
    setBusy("refresh");
    setBlockingError("");
    try {
      const { latestBatch, latest } = await getAuthority();
      const advanced = latest.batch_revision > expectedBatchRevision || latest.item_revision > expectedItemRevision;
      const regressed = latest.batch_revision < expectedBatchRevision || latest.item_revision < expectedItemRevision;
      if (regressed) {
        setAuthorityUnknown(true);
        setBlockingError("刷新结果无法与当前 revision 安全对账。已保留本地修改，危险操作继续阻断。 ");
        return;
      }
      if (dirty && advanced) {
        setConflict({
          command: "refresh",
          latestBatch,
          latest,
          mineDraft,
          mineDecision,
          choices: {},
        });
        setAuthorityUnknown(false);
        setMessage("");
        return;
      }
      setAuthority(latest);
      onBatchSnapshot(latestBatch);
      onAuthoritativeDetail(latest);
      if (!dirty) {
        setBaseDraft(cloneCollectionImportReviewDraft(latest.draft));
        setDraft(cloneCollectionImportReviewDraft(latest.draft));
        setBaseDecision(latest.decision);
      }
      setAuthorityUnknown(latest.state === "outcome_unknown");
      setMessage("已刷新权威状态");
      if (authorityRefreshTriggerIdentityRef.current === identity) {
        authorityRefreshFocusIdentityRef.current = identity;
      }
    } catch {
      setAuthorityUnknown(true);
      setBlockingError("权威状态仍无法读取。已保留本地修改，危险操作继续阻断。 ");
    } finally {
      setBusy(null);
    }
  };

  const closeRepreview = () => {
    setRepreviewOpen(false);
    setReplacementInput("");
    setReplacementTouched(false);
    window.requestAnimationFrame(() => repreviewTriggerRef.current?.focus());
  };

  useEffect(() => {
    if (!repreviewOpen) return undefined;
    const close = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeRepreview();
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [repreviewOpen]);

  const openRepreview = () => {
    setBlockingError("");
    setRepreviewOpen(true);
    window.requestAnimationFrame(() => repreviewHeadingRef.current?.focus());
  };

  const restoreRepreviewConfirmation = () => {
    setRepreviewOpen(true);
    window.requestAnimationFrame(() => repreviewHeadingRef.current?.focus());
  };

  const requestRepreview = () => {
    const intent = { run: openRepreview, returnFocus: repreviewTriggerRef.current };
    if (!guard(intent)) openRepreview();
  };

  const submitRepreview = async (override?: {
    batchRevision: number;
    itemRevision: number;
    retainedDraft: CollectionImportReviewDraft;
    retainedDecision: CollectionImportDecision;
  }) => {
    if (busy && !override) return;
    if (!canRepreviewCollectionImportItem(current, reviewPaused) || authorityUnknown) {
      setBlockingError("最新权威状态不允许重新检查来源。替换输入仍保留。 ");
      restoreRepreviewConfirmation();
      return;
    }
    const replacement = replacementInput;
    const replacementLength = unicodeScalarLength(replacement);
    if (replacement && !replacement.trim()) {
      setBlockingError("替换输入不能只包含空白；不替换时请保持为空。 ");
      return;
    }
    if (replacementLength > 10_000) {
      setBlockingError("替换输入最多 10,000 个字符。 ");
      return;
    }
    const expectedBatchRevision = override?.batchRevision ?? authority.batch_revision;
    const expectedItemRevision = override?.itemRevision ?? authority.item_revision;
    const retainedSource = override?.retainedDraft ?? withPendingTags(draft);
    const retainedDecision = override?.retainedDecision ?? baseDecision;
    setBusy("repreview");
    setBlockingError("");
    setMessage("");
    try {
      const snapshot = await collectionImportApi.repreviewItem(authority.batch_id, authority.batch_item_id, {
        expected_batch_revision: expectedBatchRevision,
        expected_item_revision: expectedItemRevision,
        ...(replacement ? { input_text: replacement } : {}),
      });
      const retained = normalizeCollectionImportReviewDraft({ ...retainedSource, untitled_confirmed: false });
      acceptSnapshot(snapshot, retained, "pending");
      setRepreviewOpen(false);
      setReplacementInput("");
      setReplacementTouched(false);
      setMessage("已排队重新检查来源；审核暂时冻结");
      window.requestAnimationFrame(() => statusRef.current?.focus());
    } catch (error) {
      const apiError = error instanceof CollectionImportApiError ? error : null;
      const isCas = apiError?.code === "BATCH_REVISION_CONFLICT"
        || apiError?.code === "BATCH_ITEM_REVISION_CONFLICT";
      const isStateConflict = apiError?.code === "BATCH_STATE_CONFLICT";
      const isUnknown = apiError?.code === "NETWORK_ERROR"
        || apiError?.code === "INVALID_RESPONSE"
        || apiError?.status === 0
        || !apiError;
      const reconcile = isCas || isStateConflict || isUnknown;
      if (!reconcile) {
        setBlockingError("这次没有排队重新检查。请核对当前权威状态。 ");
        restoreRepreviewConfirmation();
      } else {
        try {
          const { latestBatch, latest } = await getAuthority();
          setAuthority(latest);
          onBatchSnapshot(latestBatch);
          onAuthoritativeDetail(latest);
          if (isCas) {
            const latestPaused = isCollectionImportReviewPaused(latestBatch.items);
            if (!canRepreviewCollectionImportItem(latest, latestPaused)) {
              setConflict(null);
              setBlockingError("重预览已被 revision 冲突拒绝，且最新权威状态暂不允许再次提交。替换输入仍保留。 ");
              restoreRepreviewConfirmation();
            } else {
              setRepreviewOpen(false);
              setConflict({
                command: "repreview",
                latestBatch,
                latest,
                mineDraft: cloneCollectionImportReviewDraft(retainedSource),
                mineDecision: retainedDecision,
                choices: {},
              });
            }
          } else if (isStateConflict) {
            setConflict(null);
            setBlockingError("权威状态已改变，本次重预览已被拒绝。替换输入仍保留，请按最新状态处理。 ");
            restoreRepreviewConfirmation();
          } else if (latest.batch_revision === expectedBatchRevision && latest.item_revision === expectedItemRevision) {
            setBlockingError("权威 revision 未变，已证明上次重预览未生效；请检查后再次明确确认。 ");
            restoreRepreviewConfirmation();
          } else if (latest.batch_revision > expectedBatchRevision || latest.item_revision > expectedItemRevision) {
            const latestPaused = isCollectionImportReviewPaused(latestBatch.items);
            if (!canRepreviewCollectionImportItem(latest, latestPaused)) {
              setConflict(null);
              setBlockingError("权威 revision 已前进，但当前状态不允许重新检查来源。无法把本次请求归因为已生效；替换输入仍保留，请按最新状态处理。 ");
              restoreRepreviewConfirmation();
            } else {
              setRepreviewOpen(false);
              setConflict({
                command: "repreview",
                latestBatch,
                latest,
                mineDraft: cloneCollectionImportReviewDraft(retainedSource),
                mineDecision: retainedDecision,
                choices: {},
              });
            }
          } else {
            setAuthorityUnknown(true);
            setBlockingError("权威 revision 无法与重预览请求一致对账。已保留替换输入，危险操作继续阻断。 ");
          }
        } catch {
          setAuthorityUnknown(true);
          setBlockingError("无法确认重预览是否生效。已保留替换输入；请先刷新权威状态。 ");
        }
      }
    } finally {
      setBusy(null);
    }
  };

  const resolveConflict = async () => {
    if (authorityUnknown) {
      setBlockingError("权威状态仍无法确认。已保留冲突取舍，请先刷新权威状态。 ");
      return;
    }
    if (!conflict || conflictFields.some((field) => field === "decision"
      ? !conflict.choices.decision && !conflict.explicitDecision
      : !conflict.choices[field])) return;
    let mergedDraft = cloneCollectionImportReviewDraft(conflict.latest.draft);
    let mergedDecision: CollectionImportDecision = conflict.latest.decision;
    for (const field of conflictFields) {
      if (field === "decision" && conflict.explicitDecision) {
        mergedDecision = conflict.explicitDecision;
        continue;
      }
      const source = conflict.choices[field] === "mine"
        ? { draft: conflict.mineDraft, decision: conflict.mineDecision }
        : { draft: conflict.latest.draft, decision: conflict.latest.decision };
      const merged = setCollectionImportReviewValue(
        mergedDraft,
        mergedDecision,
        field,
        source.draft,
        source.decision,
      );
      mergedDraft = merged.draft;
      mergedDecision = merged.decision;
    }
    if (conflict.command === "patch" && mergedDecision === "pending") {
      setBlockingError("请在冲突中明确选择保存或跳过决定。 ");
      return;
    }
    if (conflict.command === "refresh") {
      const normalized = normalizeCollectionImportReviewDraft(mergedDraft);
      const ordered = orderedSnapshot(conflict.latestBatch);
      setAuthority(conflict.latest);
      setBaseDraft(cloneCollectionImportReviewDraft(conflict.latest.draft));
      setDraft(cloneCollectionImportReviewDraft(normalized));
      setBaseDecision(conflict.latest.decision);
      setPendingTagDraft({ organization: "", personal: "" });
      setTagEditorKey((value) => value + 1);
      setConflict(null);
      setAuthorityUnknown(false);
      setBlockingError("");
      setMessage(mergedDecision === conflict.latest.decision
        ? "已按最新权威状态应用逐字段取舍"
        : "字段取舍已应用；决定仍需用下方动作明确记录");
      onBatchSnapshot(ordered);
      onAuthoritativeDetail(conflict.latest);
      return;
    }
    if (conflict.command === "repreview") {
      const needsPatch = mergedDecision !== conflict.latest.decision
        || !sameCollectionImportReviewDraft(mergedDraft, conflict.latest.draft);
      if (needsPatch && mergedDecision === "pending") {
        setBlockingError("保留了我的字段时，请明确选择保存或跳过，才能先记录取舍再重新检查来源。 ");
        return;
      }
      if (needsPatch && !canRecordCollectionImportDecision(
        conflict.latest,
        mergedDecision as Exclude<CollectionImportDecision, "pending">,
        isCollectionImportReviewPaused(conflict.latestBatch.items),
      )) {
        setBlockingError("最新权威状态不允许先记录这项取舍。替换输入仍保留。 ");
        return;
      }
      if (!needsPatch) {
        setConflict(null);
        setBlockingError("");
        await submitRepreview({
          batchRevision: conflict.latest.batch_revision,
          itemRevision: conflict.latest.item_revision,
          retainedDraft: conflict.latest.draft,
          retainedDecision: conflict.latest.decision,
        });
        return;
      }
      const validation = validateCollectionImportReviewDraft(
        mergedDraft,
        mergedDecision as Exclude<CollectionImportDecision, "pending">,
        conflict.latest.state,
      );
      if (!validation.valid) {
        setValidationErrors(validation.errors);
        setBlockingError("请先修正冲突取舍后的审核字段。 ");
        return;
      }
      setBusy("patch");
      setBlockingError("");
      try {
        const patchSnapshot = orderedSnapshot(await collectionImportApi.updateItem(
          authority.batch_id,
          authority.batch_item_id,
          {
            expected_batch_revision: conflict.latest.batch_revision,
            expected_item_revision: conflict.latest.item_revision,
            decision: mergedDecision as Exclude<CollectionImportDecision, "pending">,
            ...validation.value,
          },
        ));
        const patchedSummary = patchSnapshot.items.find((item) => item.batch_item_id === authority.batch_item_id);
        if (!patchedSummary) throw new Error("patched item missing");
        acceptSnapshot(patchSnapshot, validation.value, mergedDecision);
        await submitRepreview({
          batchRevision: patchSnapshot.revision,
          itemRevision: patchedSummary.item_revision,
          retainedDraft: validation.value,
          retainedDecision: mergedDecision,
        });
      } catch (error) {
        try {
          const result = await reconcilePatch(
            error,
            validation.value,
            mergedDecision as Exclude<CollectionImportDecision, "pending">,
            conflict.latest.batch_revision,
            conflict.latest.item_revision,
            "repreview",
          );
          if (result === "observed") {
            setMessage("取舍已记录；重预览尚未发送，请再次明确确认");
            restoreRepreviewConfirmation();
          } else if (result === "refreshed") {
            restoreRepreviewConfirmation();
          }
        } catch {
          setAuthorityUnknown(true);
          setBlockingError("无法确认冲突取舍是否记录。替换输入仍保留，未提交重预览。 ");
        } finally {
          setBusy(null);
        }
      }
      return;
    }
    if (mergedDecision === "pending") return;
    await submitDecision(mergedDecision, pendingIntent, {
      draft: mergedDraft,
      batchRevision: conflict.latest.batch_revision,
      itemRevision: conflict.latest.item_revision,
    });
  };

  const updateDraft = (updater: (value: CollectionImportReviewDraft) => void) => {
    setDraft((value) => {
      const next = cloneCollectionImportReviewDraft(value);
      updater(next);
      return next;
    });
    setMessage("");
    setValidationErrors({});
  };

  const discardAndContinue = () => {
    const intent = pendingIntent;
    setDraft(cloneCollectionImportReviewDraft(baseDraft));
    setPendingTagDraft({ organization: "", personal: "" });
    setTagEditorKey((value) => value + 1);
    setRepreviewOpen(false);
    setReplacementInput("");
    setReplacementTouched(false);
    setPendingIntent(null);
    pendingIntentRef.current = null;
    setMessage("");
    runOnce(intent);
  };

  const stateNotice = reviewStateNotice(current);
  const visibleStateNotice = authorityBlocked ? {
    title: "批次权威状态待确认",
    body: "恢复或取消命令的结果尚未读清。请先刷新权威状态，系统不会自动重放命令。",
  } : batchCommandBlocked ? {
    title: "批次动作尚未结束",
    body: "当前审核暂时冻结。系统不会在批次动作期间提交或丢弃本项修改。",
  } : stateNotice ?? (authorityUnknown ? {
    title: "权威状态待确认",
    body: "当前无法证明上一请求的结果；本地修改仍保留，危险操作继续阻断。",
  } : null);
  const canSave = canRecordCollectionImportDecision(current, "save", reviewPaused) && !authorityUnknown && !authorityBlocked && !batchCommandBlocked;
  const canSkip = canRecordCollectionImportDecision(current, "skip", reviewPaused) && !authorityUnknown && !authorityBlocked && !batchCommandBlocked;
  const canRepreview = canRepreviewCollectionImportItem(current, reviewPaused) && !authorityUnknown && !authorityBlocked && !batchCommandBlocked;
  const canUpdateThenContinue = baseDecision !== "pending"
    && reviewDraftDirty
    && !replacementTouched
    && canRecordCollectionImportDecision(current, baseDecision, reviewPaused)
    && !authorityUnknown
    && !authorityBlocked
    && !batchCommandBlocked;
  const suggestion = current.preview?.organization_suggestion;
  const replacementError = replacementInput && !replacementInput.trim()
    ? "替换输入不能只包含空白。"
    : unicodeScalarLength(replacementInput) > 10_000
      ? "替换输入最多 10,000 个字符。"
      : "";

  const stateAnnouncement = visibleStateNotice
    ? `${visibleStateNotice.title}。${visibleStateNotice.body}`
    : "";
  useEffect(() => {
    const announcementIdentity = stateAnnouncement ? `${identity}:${stateAnnouncement}` : "";
    if (!announcementIdentity) {
      stateAnnouncementIdentity.current = "";
      return;
    }
    if (stateAnnouncementIdentity.current === announcementIdentity) return;
    stateAnnouncementIdentity.current = announcementIdentity;
    onAnnouncement?.(stateAnnouncement);
  }, [identity, onAnnouncement, stateAnnouncement]);

  useEffect(() => {
    const announcementIdentity = message ? `${identity}:${message}` : "";
    if (!announcementIdentity) {
      messageAnnouncementIdentity.current = "";
      return;
    }
    if (messageAnnouncementIdentity.current === announcementIdentity) return;
    messageAnnouncementIdentity.current = announcementIdentity;
    onAnnouncement?.(message);
  }, [identity, message, onAnnouncement]);

  useEffect(() => {
    const announcementIdentity = dirty ? `${identity}:dirty` : "";
    if (!announcementIdentity) {
      dirtyAnnouncementIdentity.current = "";
      return;
    }
    if (dirtyAnnouncementIdentity.current === announcementIdentity) return;
    dirtyAnnouncementIdentity.current = announcementIdentity;
    onAnnouncement?.("有未提交修改");
  }, [dirty, identity, onAnnouncement]);

  return (
    <>
      <section className="batch-import-authority-state" aria-labelledby="batch-item-status-title">
        <div>
          <span>权威状态</span>
          <h3 ref={statusRef} id="batch-item-status-title" tabIndex={-1}>{collectionImportStatusLabel(current.state)}</h3>
        </div>
        <p>决定：{current.decision === "save" ? "保存" : current.decision === "skip" ? "跳过" : "尚未决定"}</p>
      </section>

      {visibleStateNotice && (
        <div className="batch-import-state-notice" data-state={current.state}>
          <WarningCircle size={21} weight="fill" aria-hidden />
          <div><strong>{visibleStateNotice.title}</strong><p>{visibleStateNotice.body}</p></div>
          {current.state === "already_exists" && current.collection_item_id && (
            <Button type="button" variant="outline" color="gray" data-batch-existing-trigger={current.batch_item_id} onClick={(event) => {
              const intent = { run: () => onOpenExisting(current.collection_item_id!), returnFocus: event.currentTarget };
              if (!guard(intent)) intent.run();
            }}><FolderOpen size={18} aria-hidden />打开既有收藏</Button>
          )}
          {(current.state === "outcome_unknown" || authorityUnknown) && (
            <Button
              ref={authorityRefreshTriggerRef}
              type="button"
              variant="outline"
              color="gray"
              disabled={busy !== null}
              onClick={(event) => {
                authorityRefreshTriggerRef.current = event.currentTarget;
                authorityRefreshTriggerIdentityRef.current = identity;
                void refreshAuthority();
              }}
            >
              {busy === "refresh" ? <CircleNotch className="batch-import-spin" size={18} aria-hidden /> : <ArrowsClockwise size={18} aria-hidden />}
              刷新权威状态
            </Button>
          )}
        </div>
      )}

      {blockingError && (
        <div ref={errorRef} className="batch-import-review-error-summary" role="alert" tabIndex={-1}>
          <WarningCircle size={21} weight="fill" aria-hidden />
          <div><strong>当前操作被阻断</strong><p>{blockingError}</p></div>
        </div>
      )}

      {message && <p className="batch-import-review-success"><Check size={18} weight="bold" aria-hidden />{message}</p>}
      {dirty && <p className="batch-import-dirty-state">有未提交修改</p>}

      {pendingIntent && (
        <section className="batch-import-inline-gate" aria-labelledby="batch-import-dirty-title">
          <h3 ref={dirtyHeadingRef} id="batch-import-dirty-title" tabIndex={-1}>先处理未提交修改</h3>
          <p>修改只在当前页面内存中。放弃后无法恢复，原动作尚未执行。</p>
          <div>
            <Button type="button" variant="outline" color="gray" onClick={() => {
              const intent = pendingIntent;
              setPendingIntent(null);
              pendingIntentRef.current = null;
              window.requestAnimationFrame(() => intent?.returnFocus?.focus());
            }}>继续编辑</Button>
            <Button type="button" variant="outline" color="red" onClick={discardAndContinue}>放弃未提交修改并继续原动作</Button>
            <Button type="button" disabled={!canUpdateThenContinue || busy !== null} onClick={() => {
              if (baseDecision === "pending") return;
              void submitDecision(baseDecision, pendingIntent);
            }}>更新后继续原动作</Button>
          </div>
        </section>
      )}

      {conflict && (
        <section className="batch-import-conflict" aria-labelledby="batch-import-conflict-title">
          <h3 ref={conflictHeadingRef} id="batch-import-conflict-title" tabIndex={-1}>审核已在别处更新</h3>
          <p>下面每个字段都要明确选择。即使值看起来相同，也不会自动覆盖或重试。</p>
          <div className="batch-import-conflict-fields">
            {conflictFields.map((field) => {
              const label = collectionImportReviewFieldLabels[field];
              const latestValue = formatCollectionImportReviewValue(collectionImportReviewValue(conflict.latest.draft, conflict.latest.decision, field));
              const mineValue = formatCollectionImportReviewValue(collectionImportReviewValue(conflict.mineDraft, conflict.mineDecision, field));
              return (
                <fieldset key={field}>
                  <legend>{label}</legend>
                  <label>
                    <input type="radio" name={`batch-conflict-${field}`} aria-label={`${label}：使用最新值`} checked={conflict.choices[field] === "latest"} onChange={() => setConflict({ ...conflict, choices: { ...conflict.choices, [field]: "latest" }, explicitDecision: field === "decision" ? undefined : conflict.explicitDecision })} />
                    <span><strong>使用最新值</strong><small>{latestValue}</small></span>
                  </label>
                  {field === "decision" && conflict.command === "repreview" && (
                    <>
                      <label>
                        <input type="radio" name="batch-conflict-decision" aria-label="审核决定：选择保存" checked={conflict.explicitDecision === "save"} onChange={() => setConflict({ ...conflict, choices: { ...conflict.choices, decision: undefined }, explicitDecision: "save" })} />
                        <span><strong>明确选择保存</strong><small>先记录取舍，再重新检查来源</small></span>
                      </label>
                      <label>
                        <input type="radio" name="batch-conflict-decision" aria-label="审核决定：选择跳过" checked={conflict.explicitDecision === "skip"} onChange={() => setConflict({ ...conflict, choices: { ...conflict.choices, decision: undefined }, explicitDecision: "skip" })} />
                        <span><strong>明确选择跳过</strong><small>先记录取舍，再重新检查来源</small></span>
                      </label>
                    </>
                  )}
                  <label>
                    <input type="radio" name={`batch-conflict-${field}`} aria-label={`${label}：保留我的值`} checked={conflict.choices[field] === "mine"} onChange={() => setConflict({ ...conflict, choices: { ...conflict.choices, [field]: "mine" }, explicitDecision: field === "decision" ? undefined : conflict.explicitDecision })} />
                    <span><strong>保留我的值</strong><small>{mineValue}</small></span>
                  </label>
                </fieldset>
              );
            })}
          </div>
          <Button type="button" disabled={authorityUnknown || conflictFields.some((field) => field === "decision" ? !conflict.choices.decision && !conflict.explicitDecision : !conflict.choices[field]) || busy !== null} onClick={() => void resolveConflict()}>
            {conflict.command === "patch"
              ? "用最新 revision 重新记录决定"
              : conflict.command === "repreview"
                ? "记录取舍并重新检查来源"
                : "应用逐字段取舍"}
          </Button>
        </section>
      )}

      <form className="batch-import-review-form" onSubmit={(event) => event.preventDefault()} noValidate>
        <div className="batch-import-review-section-heading">
          <div><h3>本项审核</h3><p>字段只在明确记录决定后持久化。</p></div>
        </div>
        <div className="batch-import-field">
          <label htmlFor="batch-import-user-title">自定义标题</label>
          <input
            id="batch-import-user-title"
            value={draft.user_title ?? ""}
            disabled={formDisabled}
            maxLength={500}
            aria-invalid={Boolean(validationErrors.user_title)}
            aria-describedby={validationErrors.user_title ? "batch-import-title-error" : undefined}
            onChange={(event) => updateDraft((next) => { next.user_title = event.target.value || null; })}
          />
          {validationErrors.user_title && <p id="batch-import-title-error" className="inline-error" role="alert">{validationErrors.user_title}</p>}
        </div>
        <label className="batch-import-untitled">
          <input type="checkbox" checked={draft.untitled_confirmed} disabled={formDisabled} onChange={(event) => updateDraft((next) => { next.untitled_confirmed = event.target.checked; })} />
          <span>没有可用标题，按无标题普通书签保存</span>
        </label>

        {suggestion?.status === "generated" && (
          <section className="batch-import-suggestions" aria-labelledby="batch-import-suggestions-title">
            <div><h4 id="batch-import-suggestions-title">整理建议</h4><p>逐条采用；不会自动写入或批量采用。</p></div>
            <ul>
              {suggestion.primary_category && <li><span>一级分类：{suggestion.primary_category}</span><Button type="button" variant="outline" color="gray" disabled={formDisabled} aria-label={`采用一级分类建议：${suggestion.primary_category}`} onClick={() => updateDraft((next) => { next.organization_confirmation.primary_category = suggestion.primary_category; })}>采用此建议</Button></li>}
              {suggestion.secondary_category && <li><span>二级分类：{suggestion.secondary_category}</span><Button type="button" variant="outline" color="gray" disabled={formDisabled} aria-label={`采用二级分类建议：${suggestion.secondary_category}`} onClick={() => updateDraft((next) => { next.organization_confirmation.secondary_category = suggestion.secondary_category; })}>采用此建议</Button></li>}
              {suggestion.tags.map((tag) => <li key={tag}><span>整理标签：{tag}</span><Button type="button" variant="outline" color="gray" disabled={formDisabled || draft.organization_confirmation.organization_tags.includes(tag)} aria-label={`采用整理标签建议：${tag}`} onClick={() => updateDraft((next) => { next.organization_confirmation.organization_tags = [...next.organization_confirmation.organization_tags, tag]; })}>采用此建议</Button></li>)}
            </ul>
          </section>
        )}

        <div className="batch-import-category-fields">
          <div className="batch-import-field">
            <label htmlFor="batch-import-primary-category">一级分类</label>
            <input id="batch-import-primary-category" value={draft.organization_confirmation.primary_category} disabled={formDisabled} maxLength={64} onChange={(event) => updateDraft((next) => { next.organization_confirmation.primary_category = event.target.value; })} />
          </div>
          <div className="batch-import-field">
            <label htmlFor="batch-import-secondary-category">二级分类</label>
            <input id="batch-import-secondary-category" value={draft.organization_confirmation.secondary_category} disabled={formDisabled} maxLength={64} onChange={(event) => updateDraft((next) => { next.organization_confirmation.secondary_category = event.target.value; })} />
          </div>
        </div>

        <TagEditor key={`organization-${tagEditorKey}`} idPrefix="batch-import-organization-tags" label="整理标签" values={draft.organization_confirmation.organization_tags} setValues={(values) => updateDraft((next) => { next.organization_confirmation.organization_tags = values; })} disabled={formDisabled} onDraftChange={(value) => setPendingTagDraft((currentValue) => ({ ...currentValue, organization: value }))} validationError={validationErrors.organization_tags} />
        <TagEditor key={`personal-${tagEditorKey}`} idPrefix="batch-import-personal-tags" label="个人标签" personal values={draft.personal_tags} setValues={(values) => updateDraft((next) => { next.personal_tags = values; })} disabled={formDisabled} onDraftChange={(value) => setPendingTagDraft((currentValue) => ({ ...currentValue, personal: value }))} validationError={validationErrors.personal_tags} />

        <div className="batch-import-field">
          <label htmlFor="batch-import-inspiration">我的灵感</label>
          <textarea
            id="batch-import-inspiration"
            rows={6}
            value={draft.inspiration?.content ?? ""}
            disabled={formDisabled}
            maxLength={4000}
            aria-invalid={Boolean(validationErrors.inspiration)}
            onChange={(event) => updateDraft((next) => {
              next.inspiration = event.target.value ? {
                content: event.target.value,
                input_mode: "text",
                transcription_status: "not_applicable",
              } : null;
            })}
          />
          <span>{unicodeScalarLength(draft.inspiration?.content ?? "").toLocaleString("zh-CN")}/4,000 字符</span>
        </div>
      </form>

      {canRepreview && !repreviewOpen && !confirmationOpen && (
        <div className="batch-import-repreview-trigger">
          <Button ref={repreviewTriggerRef} type="button" variant="outline" color="gray" disabled={busy !== null} onClick={requestRepreview}>
            <ArrowsClockwise size={18} aria-hidden />重新检查来源
          </Button>
        </div>
      )}

      {repreviewOpen && (
        <section className="batch-import-inline-repreview" aria-labelledby="batch-import-repreview-title">
          <h3 ref={repreviewHeadingRef} id="batch-import-repreview-title" tabIndex={-1}>重新检查来源</h3>
          <p>可能重新访问公开来源。保留标题、分类、标签与灵感，但清除当前决定和无标题确认，必须重新审核。</p>
          <label htmlFor="batch-import-replacement-input">可选替换输入</label>
          <textarea id="batch-import-replacement-input" rows={4} value={replacementInput} disabled={busy !== null} aria-invalid={Boolean(replacementError)} aria-describedby="batch-import-replacement-count" placeholder="留空则复用当前预览的原始输入" onChange={(event) => { setReplacementInput(event.target.value); setReplacementTouched(true); setBlockingError(""); }} />
          <div id="batch-import-replacement-count" className="batch-import-input-feedback"><span>{unicodeScalarLength(replacementInput).toLocaleString("zh-CN")}/10,000 字符</span>{replacementError && <span className="batch-import-field-error">{replacementError}</span>}</div>
          <div>
            <Button type="button" variant="outline" color="gray" disabled={busy !== null} onClick={closeRepreview}>撤回重预览</Button>
            <Button type="button" disabled={busy !== null || Boolean(replacementError) || authorityUnknown || conflict !== null || !canRepreview} onClick={() => void submitRepreview()}>
              {busy === "repreview" ? <CircleNotch className="batch-import-spin" size={18} aria-hidden /> : <ArrowsClockwise size={18} aria-hidden />}
              确认重新检查来源
            </Button>
          </div>
        </section>
      )}

      <section className="batch-import-action-dock" aria-label="当前项审核动作" aria-hidden={confirmationOpen || undefined}>
        <div><strong>{dirty ? "有未提交修改" : baseDecision === "pending" ? "尚未记录决定" : "本项决定已记录"}</strong><span>记录决定不会创建收藏</span></div>
        <div>
          <Button type="button" variant="outline" color="gray" disabled={confirmationOpen || !canSkip || busy !== null || conflict !== null || repreviewOpen} onClick={() => void submitDecision("skip")}>
            {baseDecision === "save" ? "改为跳过" : "跳过本项"}
          </Button>
          <Button type="button" disabled={confirmationOpen || !canSave || busy !== null || conflict !== null || repreviewOpen} onClick={() => void submitDecision("save")}>
            {busy === "patch" ? <CircleNotch className="batch-import-spin" size={18} aria-hidden /> : <FloppyDisk size={18} aria-hidden />}
            {baseDecision === "pending" ? "标记保存" : baseDecision === "skip" ? "改为保存" : "更新本项审核"}
          </Button>
          {onOpenBatchConfirm && (
            <Button
              className="batch-import-desktop-confirm-action"
              type="button"
              data-batch-confirm-trigger
              disabled={confirmationOpen || authorityBlocked || batchCommandBlocked || !batchConfirmEnabled || busy !== null || conflict !== null || repreviewOpen}
              onClick={(event) => onOpenBatchConfirm(event.currentTarget)}
            >
              <Check size={18} weight="bold" aria-hidden />核对并保存已选 {batchConfirmCount} 项
            </Button>
          )}
        </div>
      </section>
    </>
  );
}
