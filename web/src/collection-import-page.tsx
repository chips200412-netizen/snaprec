import {
  ArrowLeft,
  ArrowRight,
  ArrowsClockwise,
  CaretDown,
  CheckCircle,
  CircleNotch,
  ListPlus,
  Plus,
  Trash,
  WarningCircle,
  XCircle,
} from "@phosphor-icons/react";
import { Button, IconButton } from "@radix-ui/themes";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CollectionImportApiError,
  collectionImportApi,
  type CollectionImportBatchItemSummary,
  type CollectionImportBatchSnapshot,
  type CollectionImportItemDetail,
} from "./collection-import-api";
import {
  COLLECTION_IMPORT_MAX_ITEMS,
  collectionImportBatchLabel,
  collectionImportConfirmationCounts,
  collectionImportCreateItems,
  collectionImportDisplayPosition,
  collectionImportItemId,
  collectionImportRequestFingerprint,
  collectionImportStatusLabel,
  canCancelCollectionImportBatch,
  canConfirmCollectionImportBatch,
  canResumeCollectionImportBatch,
  hasCollectionImportOutcomeUnknown,
  isCollectionImportPreviewActive,
  isCollectionImportSaveActive,
  safeCollectionImportCounts,
  shouldPollCollectionImportBatch,
  unicodeScalarLength,
  validateCollectionImportDraft,
  type CollectionImportDraftItem,
} from "./collection-import-model";
import {
  CollectionImportReviewControls,
  type CollectionImportLeaveGuard,
} from "./collection-import-review";
import "./collection-import.css";

export interface CollectionImportRouteState {
  batchId?: string;
  view: "list" | "item" | "confirm";
  itemId?: string;
  focus?: string;
  restoreScrollY?: number;
  restoreToken?: number;
  anchorItemId?: string;
  anchorOffset?: number;
  trigger?: "keyboard" | "pointer";
}

interface CollectionImportPageProps {
  route: CollectionImportRouteState;
  onRouteChange: (route: CollectionImportRouteState, replace?: boolean) => void;
  onBackToLibrary: () => void;
  onBackToList: (batchItemId: string) => void;
  onBackFromConfirm: () => void;
  onOpenExisting: (collectionItemId: string) => void;
  registerLeaveGuard: (guard: (() => boolean) | null) => void;
}

type PageState = "loading" | "create" | "batch" | "error";
type ItemState = "idle" | "loading" | "ready" | "error";
type ConfirmState = "idle" | "loading" | "ready" | "blocked" | "error";
type BatchCommandBusy = "resume" | "cancel" | "refresh" | null;
type BatchCommandTone = "info" | "success" | "warning" | "error";

interface BatchCommandFeedback {
  tone: BatchCommandTone;
  text: string;
}

const collectionImportPlatformNames: Record<NonNullable<CollectionImportItemDetail["preview"]>["platform"], string> = {
  douyin: "抖音",
  bilibili: "哔哩哔哩",
  xiaohongshu: "小红书",
  youtube: "YouTube",
  web: "网页",
  other: "其他来源",
  local_upload: "本地上传",
};

function restoreBatchItemPosition(scrollY: number | undefined, focus: () => HTMLElement | null): () => void {
  let firstFrame = 0;
  let secondFrame = 0;
  let finished = false;
  const stop = () => {
    window.cancelAnimationFrame(firstFrame);
    window.cancelAnimationFrame(secondFrame);
    for (const event of ["wheel", "touchstart", "pointerdown", "keydown"]) window.removeEventListener(event, interrupt);
  };
  const interrupt = () => {
    if (finished) return;
    finished = true;
    stop();
  };
  for (const event of ["wheel", "touchstart", "pointerdown", "keydown"]) window.addEventListener(event, interrupt, { once: true, passive: true });
  firstFrame = window.requestAnimationFrame(() => {
    secondFrame = window.requestAnimationFrame(() => {
      if (finished) return;
      finished = true;
      stop();
      if (typeof scrollY === "number" && Number.isFinite(scrollY)) {
        const max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
        window.scrollTo({ top: Math.min(Math.max(0, scrollY), max), behavior: "auto" });
      }
      focus()?.focus({ preventScroll: true });
    });
  });
  return () => { finished = true; stop(); };
}

function makeLocalId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `batch-input-${Date.now()}-${Math.random()}`;
}

function makeRequestKey(): string {
  return globalThis.crypto?.randomUUID?.() ?? `batch-${Date.now()}-${Math.random()}`;
}

function newDraftItem(): CollectionImportDraftItem {
  return { localId: makeLocalId(), inputText: "" };
}

function safeErrorMessage(error: unknown, fallback: string): string {
  return error instanceof CollectionImportApiError && error.message ? error.message : fallback;
}

function orderedBatchSnapshot(snapshot: CollectionImportBatchSnapshot): CollectionImportBatchSnapshot {
  return { ...snapshot, items: [...snapshot.items].sort((left, right) => left.position - right.position) };
}

function collectionImportConfirmationFrozen(batch: CollectionImportBatchSnapshot): boolean {
  return batch.status === "saving"
    || batch.status === "completed"
    || batch.status === "completed_with_issues"
    || batch.items.some((item) => item.state === "save_queued"
      || item.state === "saving"
      || item.state === "outcome_unknown")
    || (batch.status === "interrupted"
      && batch.items.some((item) => item.state === "interrupted" && item.decision === "save"));
}

function collectionImportConfirmObserved(
  batch: CollectionImportBatchSnapshot,
  expected: CollectionImportBatchSnapshot,
): boolean {
  if (batch.batch_id !== expected.batch_id || batch.revision <= expected.revision) return false;
  const latestById = new Map(batch.items.map((item) => [item.batch_item_id, item]));
  let transitionCount = 0;
  for (const before of expected.items) {
    const after = latestById.get(before.batch_item_id);
    if (!after) return false;
    const saveThisConfirm = before.decision === "save"
      && (before.state === "ready" || before.state === "needs_review" || before.state === "failed");
    const skipThisConfirm = before.decision === "skip"
      && (before.state === "ready" || before.state === "needs_review" || before.state === "preview_expired" || before.state === "failed");
    const duplicateThisConfirm = before.state === "duplicate_in_batch";
    if (!saveThisConfirm && !skipThisConfirm && !duplicateThisConfirm) continue;
    transitionCount += 1;
    if (after.item_revision <= before.item_revision) return false;
    if (saveThisConfirm && after.state === "failed" && after.decision !== "pending") return false;
    if (saveThisConfirm && !["save_queued", "saving", "saved", "already_exists", "failed", "interrupted", "outcome_unknown"].includes(after.state)) return false;
    if ((skipThisConfirm || duplicateThisConfirm) && after.state !== "skipped") return false;
  }
  return transitionCount > 0;
}

function batchResumeFeedback(batch: CollectionImportBatchSnapshot): BatchCommandFeedback {
  if (isCollectionImportPreviewActive(batch)) {
    return { tone: "info", text: "本地对账已完成，正在继续检查公开来源。" };
  }
  if (isCollectionImportSaveActive(batch)) {
    return { tone: "info", text: "本地对账已完成，正在继续按原输入顺序保存。" };
  }
  if (batch.status === "interrupted" || hasCollectionImportOutcomeUnknown(batch)) {
    return { tone: "warning", text: "本地对账已完成，但仍有结果待核对。系统没有自动重试保存。" };
  }
  return { tone: "success", text: "本地对账已完成，批次已按权威状态收敛。" };
}

function shouldReconcileBatchCommand(error: unknown): boolean {
  if (!(error instanceof CollectionImportApiError)) return true;
  return error.code === "BATCH_REVISION_CONFLICT"
    || error.code === "BATCH_STATE_CONFLICT"
    || error.code === "NETWORK_ERROR"
    || error.code === "HTTP_ERROR"
    || error.code === "INVALID_RESPONSE"
    || error.status === 0;
}

function BatchItemStatusIcon({ item }: { item: CollectionImportBatchItemSummary }) {
  if (item.state === "queued" || item.state === "previewing" || item.state === "saving" || item.state === "save_queued") {
    return <CircleNotch className="batch-import-spin" size={18} aria-hidden />;
  }
  if (item.state === "ready" || item.state === "saved" || item.state === "already_exists") {
    return <CheckCircle size={18} weight="fill" aria-hidden />;
  }
  return <WarningCircle size={18} weight="fill" aria-hidden />;
}

function BatchHeader({ onBack }: { onBack: (returnFocus?: HTMLElement | null) => void }) {
  return (
    <header className="batch-import-global-header">
      <span className="collection-wordmark" aria-label="瞬时录">瞬时录</span>
      <Button variant="ghost" color="gray" onClick={(event) => onBack(event.currentTarget)}>
        <ArrowLeft size={18} aria-hidden />返回素材库
      </Button>
    </header>
  );
}

function LoadingSurface({ onBack }: { onBack: () => void }) {
  return (
    <div className="collection-page batch-import-page">
      <BatchHeader onBack={onBack} />
      <main className="batch-import-loading" aria-busy="true" aria-live="polite">
        <CircleNotch className="batch-import-spin" size={28} aria-hidden />
        <h1 tabIndex={-1}>正在读取批量导入</h1>
        <p>这里只核对已有批次，不会自动恢复或重新访问来源。</p>
      </main>
    </div>
  );
}

function CreateSurface({
  draftItems,
  setDraftItems,
  submitting,
  submitError,
  activeBatchId,
  onSubmit,
  onOpenActive,
  onBack,
}: {
  draftItems: CollectionImportDraftItem[];
  setDraftItems: (items: CollectionImportDraftItem[]) => void;
  submitting: boolean;
  submitError: string;
  activeBatchId: string | null;
  onSubmit: () => void;
  onOpenActive: () => void;
  onBack: () => void;
}) {
  const titleRef = useRef<HTMLHeadingElement | null>(null);
  const errorRef = useRef<HTMLDivElement | null>(null);
  const validation = useMemo(() => validateCollectionImportDraft(draftItems), [draftItems]);

  useEffect(() => { titleRef.current?.focus(); }, []);
  useEffect(() => {
    if (submitError) errorRef.current?.focus();
  }, [submitError]);

  const changeItem = (localId: string, inputText: string) => {
    setDraftItems(draftItems.map((item) => item.localId === localId ? { ...item, inputText } : item));
  };

  return (
    <div className="collection-page batch-import-page">
      <BatchHeader onBack={onBack} />
      <main className="batch-import-create-main">
        <section className="batch-import-create-heading">
          <div>
            <h1 ref={titleRef} tabIndex={-1}>批量导入</h1>
            <p>一次提交 2 至 10 条完整分享文本。系统按原顺序逐项检查，检查完成后仍需逐项审核。</p>
          </div>
          <ListPlus size={36} aria-hidden />
        </section>

        <form className="batch-import-form" onSubmit={(event) => { event.preventDefault(); onSubmit(); }} noValidate>
          <div className="batch-import-boundary-note" id="batch-import-boundaries">
            <strong>输入边界</strong>
            <span>每项最多 10,000 个字符，全部文本最多 65,536 UTF-8 bytes。</span>
            <span>{draftItems.length}/10 项，当前 {validation.totalBytes.toLocaleString("zh-CN")}/65,536 bytes</span>
          </div>

          {submitError && (
            <div ref={errorRef} className="batch-import-error-summary" role="alert" tabIndex={-1}>
              <WarningCircle size={21} weight="fill" aria-hidden />
              <div><strong>这次没有创建批次</strong><p>{submitError}</p></div>
              {activeBatchId && <Button type="button" variant="outline" color="gray" onClick={onOpenActive}>打开当前批次</Button>}
            </div>
          )}

          <ol className="batch-import-input-list" aria-describedby="batch-import-boundaries">
            {draftItems.map((item, index) => {
              const error = validation.itemErrors[index];
              const errorId = `batch-input-error-${item.localId}`;
              const countId = `batch-input-count-${item.localId}`;
              return (
                <li key={item.localId}>
                  <div className="batch-import-input-heading">
                    <label htmlFor={`batch-input-${item.localId}`}><span>{index + 1}</span>第 {index + 1} 项完整分享文本</label>
                    <IconButton
                      type="button"
                      variant="ghost"
                      color="gray"
                      aria-label={`移除第 ${index + 1} 项`}
                      disabled={draftItems.length <= 2 || submitting}
                      onClick={() => setDraftItems(draftItems.filter((entry) => entry.localId !== item.localId))}
                    ><Trash size={19} aria-hidden /></IconButton>
                  </div>
                  <textarea
                    id={`batch-input-${item.localId}`}
                    value={item.inputText}
                    rows={4}
                    disabled={submitting}
                    aria-invalid={Boolean(error)}
                    aria-describedby={`${countId}${error ? ` ${errorId}` : ""}`}
                    placeholder="粘贴一条公开链接或包含一个链接的完整分享文本"
                    onChange={(event) => changeItem(item.localId, event.target.value)}
                  />
                  <div className="batch-import-input-feedback">
                    <span id={countId}>{unicodeScalarLength(item.inputText).toLocaleString("zh-CN")}/10,000 字符</span>
                    {error && <span id={errorId} className="batch-import-field-error">{error}</span>}
                  </div>
                </li>
              );
            })}
          </ol>

          <div className="batch-import-create-actions">
            <Button
              type="button"
              variant="outline"
              color="gray"
              disabled={draftItems.length >= COLLECTION_IMPORT_MAX_ITEMS || submitting}
              onClick={() => setDraftItems([...draftItems, newDraftItem()])}
            ><Plus size={18} aria-hidden />添加一项</Button>
            <div>
              {validation.summary && <p aria-live="polite">{validation.summary}</p>}
              <Button type="submit" size="3" disabled={!validation.valid || submitting}>
                {submitting ? <CircleNotch className="batch-import-spin" size={19} aria-hidden /> : <ListPlus size={19} aria-hidden />}
                {submitting ? "正在创建" : "创建批次并开始检查"}
              </Button>
            </div>
          </div>
        </form>
      </main>
    </div>
  );
}

function BatchLifecycleActions({
  batch,
  busy,
  feedback,
  authorityUncertain,
  cancelOpen,
  onResume,
  onOpenCancel,
  onCloseCancel,
  onConfirmCancel,
  onRefresh,
  showOutcomeRefresh,
  placement,
}: {
  batch: CollectionImportBatchSnapshot;
  busy: BatchCommandBusy;
  feedback: BatchCommandFeedback | null;
  authorityUncertain: boolean;
  cancelOpen: boolean;
  onResume: (trigger: HTMLButtonElement) => void;
  onOpenCancel: (trigger: HTMLButtonElement) => void;
  onCloseCancel: () => void;
  onConfirmCancel: (trigger: HTMLButtonElement) => void;
  onRefresh: () => void;
  showOutcomeRefresh: boolean;
  placement: "desktop" | "mobile";
}) {
  const cancelTriggerRef = useRef<HTMLButtonElement | null>(null);
  const cancelHeadingRef = useRef<HTMLHeadingElement | null>(null);
  const errorRef = useRef<HTMLParagraphElement | null>(null);
  const errorIdentity = useRef("");
  const restoreCancelTrigger = useRef(false);
  const canResume = canResumeCollectionImportBatch(batch) && !authorityUncertain;
  const canCancel = canCancelCollectionImportBatch(batch) && !authorityUncertain;
  const canRefresh = authorityUncertain || (showOutcomeRefresh && hasCollectionImportOutcomeUnknown(batch));

  useEffect(() => {
    if (cancelOpen) {
      cancelHeadingRef.current?.focus();
      return;
    }
    if (!restoreCancelTrigger.current) return;
    restoreCancelTrigger.current = false;
    window.requestAnimationFrame(() => cancelTriggerRef.current?.focus());
  }, [cancelOpen]);

  useEffect(() => {
    if (feedback?.tone !== "error") {
      errorIdentity.current = "";
      return;
    }
    if (errorIdentity.current === feedback.text) return;
    errorIdentity.current = feedback.text;
    errorRef.current?.focus();
  }, [feedback]);

  useEffect(() => {
    if (!cancelOpen) return undefined;
    const close = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || busy !== null) return;
      event.preventDefault();
      restoreCancelTrigger.current = true;
      onCloseCancel();
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [busy, cancelOpen, onCloseCancel]);

  if (!canResume && !canCancel && !canRefresh && !feedback && !cancelOpen) return null;

  return (
    <section
      className={`batch-import-lifecycle-actions batch-import-lifecycle-actions-${placement}`}
      aria-label="批次恢复与取消"
      data-batch-lifecycle-placement={placement}
    >
      {feedback && (
        <p
          ref={feedback.tone === "error" ? errorRef : undefined}
          className="batch-import-command-feedback"
          data-tone={feedback.tone}
          role={feedback.tone === "error" ? "alert" : undefined}
          tabIndex={feedback.tone === "error" ? -1 : undefined}
        >{feedback.text}</p>
      )}
      {!cancelOpen && (
        <div className="batch-import-lifecycle-buttons">
          {canRefresh && (
            <Button
              type="button"
              variant="outline"
              color="gray"
              disabled={busy !== null}
              data-batch-lifecycle-focus="refresh"
              onClick={onRefresh}
            >
              {busy === "refresh" ? <CircleNotch className="batch-import-spin" size={18} aria-hidden /> : <ArrowsClockwise size={18} aria-hidden />}
              {busy === "refresh" ? "正在读取" : "刷新权威状态"}
            </Button>
          )}
          {canResume && (
            <Button
              type="button"
              variant="outline"
              color="gray"
              disabled={busy !== null}
              data-batch-lifecycle-focus="resume"
              onClick={(event) => onResume(event.currentTarget)}
            >
              {busy === "resume" ? <CircleNotch className="batch-import-spin" size={18} aria-hidden /> : <ArrowsClockwise size={18} aria-hidden />}
              {busy === "resume" ? "正在本地对账" : "恢复并对账"}
            </Button>
          )}
          {canCancel && (
            <Button
              ref={cancelTriggerRef}
              type="button"
              variant="outline"
              color="red"
              disabled={busy !== null}
              data-batch-lifecycle-focus="cancel"
              onClick={(event) => onOpenCancel(event.currentTarget)}
            ><XCircle size={18} aria-hidden />取消批次</Button>
          )}
        </div>
      )}
      {cancelOpen && (
        <section className="batch-import-cancel-confirm" aria-labelledby={`batch-import-cancel-title-${placement}`}>
          <h3
            ref={cancelHeadingRef}
            id={`batch-import-cancel-title-${placement}`}
            tabIndex={-1}
            data-batch-lifecycle-focus="cancel-heading"
          >确认取消当前批次</h3>
          <p>已保存的项会保留，正在提交的项仍可能保存或继续收敛。系统不会强制中止正在写入的保存。</p>
          <p>若你刚才放弃了未提交修改，这些修改无法恢复。</p>
          <div>
            <Button type="button" variant="outline" color="gray" disabled={busy !== null} onClick={() => {
              restoreCancelTrigger.current = true;
              onCloseCancel();
            }}>撤回取消</Button>
            <Button type="button" color="red" disabled={busy !== null} onClick={(event) => onConfirmCancel(event.currentTarget)}>
              {busy === "cancel" ? <CircleNotch className="batch-import-spin" size={18} aria-hidden /> : <XCircle size={18} aria-hidden />}
              {busy === "cancel" ? "正在提交取消意图" : "确认取消批次"}
            </Button>
          </div>
        </section>
      )}
    </section>
  );
}

function BatchSummaryBand({
  batch,
  titleRef,
  statusRef,
  children,
}: {
  batch: CollectionImportBatchSnapshot;
  titleRef: React.RefObject<HTMLHeadingElement | null>;
  statusRef: React.RefObject<HTMLHeadingElement | null>;
  children?: React.ReactNode;
}) {
  const counts = safeCollectionImportCounts(batch);
  return (
    <section className="batch-import-summary" aria-labelledby="batch-import-title">
      <div>
        <h1 ref={titleRef} id="batch-import-title" tabIndex={-1}>批量导入</h1>
        <div className="batch-import-summary-status">
          <h2 ref={statusRef} id="batch-import-status-title" tabIndex={-1}>{collectionImportBatchLabel(batch.status)}</h2>
          <span>批次状态以服务端记录为准</span>
        </div>
      </div>
      <dl>
        <div><dt>全部</dt><dd>{counts.total}</dd></div>
        <div><dt>可审核</dt><dd>{counts.ready}</dd></div>
        <div><dt>已选保存</dt><dd>{counts.selected}</dd></div>
        <div><dt>失败</dt><dd>{counts.failed}</dd></div>
        <div><dt>重复</dt><dd>{counts.duplicate}</dd></div>
        <div><dt>已存在</dt><dd>{counts.existing}</dd></div>
      </dl>
      {children}
    </section>
  );
}

function BatchConfirmEntry({
  batch,
  dirty,
  authorityBlocked = false,
  commandBlocked = false,
  onOpen,
  compact = false,
}: {
  batch: CollectionImportBatchSnapshot;
  dirty: boolean;
  authorityBlocked?: boolean;
  commandBlocked?: boolean;
  onOpen: (trigger: HTMLButtonElement) => void;
  compact?: boolean;
}) {
  const eligible = !authorityBlocked && !commandBlocked && canConfirmCollectionImportBatch(batch, dirty);
  const counts = collectionImportConfirmationCounts(batch);
  const terminalMessage = batch.status === "completed"
    ? "本批次已结束，无需再次确认。可在列表中查看各项结果。"
    : batch.status === "completed_with_issues"
      ? "未成功的项目已跳过；已保存的素材会保留。"
      : batch.status === "cancelled"
        ? "已保存的素材会保留；其余项目已停止处理。"
        : "";
  const reason = authorityBlocked
    ? "请先刷新权威状态；系统不会自动重放确认。"
    : commandBlocked
      ? "请先完成或撤回当前批次动作。"
    : dirty
    ? "请先处理当前项的未提交修改。"
    : isCollectionImportPreviewActive(batch)
      ? "仍有来源正在检查，审核暂时不能冻结。"
      : batch.items.some((item) => item.state === "outcome_unknown")
        ? "仍有结果待核对，不能再次确认。"
        : batch.status === "saving" || isCollectionImportSaveActive(batch)
          ? "批次已经进入串行保存，审核现已冻结。"
          : terminalMessage || "全部可审核项都要先明确记录保存或跳过决定。";
  return (
    <section className={compact ? "batch-import-confirm-entry batch-import-confirm-entry-compact" : "batch-import-confirm-entry batch-import-action-dock"} aria-label="批次确认动作">
      <div>
        <strong>{eligible ? `已完成审核，可核对 ${counts.save} 项保存决定` : terminalMessage ? collectionImportBatchLabel(batch.status) : "批次审核尚未满足确认条件"}</strong>
        <span>{eligible ? "第一次点击只打开安全计数摘要，不会创建收藏。" : reason}</span>
      </div>
      <Button
        type="button"
        data-batch-confirm-trigger
        disabled={!eligible}
        onClick={(event) => onOpen(event.currentTarget)}
      >
        <CheckCircle size={18} weight="fill" aria-hidden />核对并保存已选 {counts.save} 项
      </Button>
    </section>
  );
}

function BatchConfirmSurface({
  batch,
  state,
  error,
  busy,
  onBack,
  onConfirm,
  onRefresh,
}: {
  batch: CollectionImportBatchSnapshot;
  state: ConfirmState;
  error: string;
  busy: boolean;
  onBack: () => void;
  onConfirm: () => void;
  onRefresh: () => void;
}) {
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const counts = collectionImportConfirmationCounts(batch);
  useEffect(() => { headingRef.current?.focus(); }, [state]);

  if (state === "idle" || state === "loading") {
    return (
      <section className="batch-import-confirm-surface batch-import-confirm-loading" aria-busy="true">
        <CircleNotch className="batch-import-spin" size={26} aria-hidden />
        <h2 ref={headingRef} tabIndex={-1}>正在复核批次确认资格</h2>
        <p>只读取权威批次状态，不会自动确认或保存。</p>
      </section>
    );
  }

  if (state === "blocked" || state === "error") {
    return (
      <section className="batch-import-confirm-surface batch-import-confirm-blocked" aria-labelledby="batch-confirm-blocked-title">
        <WarningCircle size={25} weight="fill" aria-hidden />
        <h2 ref={headingRef} id="batch-confirm-blocked-title" tabIndex={-1}>本次确认已停止</h2>
        <p role="alert">{error}</p>
        <div>
          <Button type="button" variant="outline" color="gray" onClick={onBack}><ArrowLeft size={18} aria-hidden />返回审核</Button>
          {state === "error" && <Button type="button" variant="outline" color="gray" onClick={onRefresh}>刷新权威状态</Button>}
        </div>
      </section>
    );
  }

  return (
    <section className="batch-import-confirm-surface" aria-labelledby="batch-confirm-title">
      <div className="batch-import-confirm-heading">
        <div>
          <h2 ref={headingRef} id="batch-confirm-title" tabIndex={-1}>核对本次批次决定</h2>
          <p>这里只显示有限计数，不包含分享文本、审核草稿或我的灵感。</p>
        </div>
        <CheckCircle size={30} weight="fill" aria-hidden />
      </div>
      <dl className="batch-import-confirm-counts">
        <div><dt>保存</dt><dd>{counts.save}</dd></div>
        <div><dt>跳过</dt><dd>{counts.skip}</dd></div>
        <div><dt>批内重复</dt><dd>{counts.duplicate}</dd></div>
        <div><dt>已存在</dt><dd>{counts.existing}</dd></div>
        <div><dt>失败</dt><dd>{counts.failed}</dd></div>
      </dl>
      <p className="batch-import-confirm-note">确认后，保存决定会被冻结，并严格按原输入顺序逐项提交。已保存项不会因后续单项失败而回滚。</p>
      <div className="batch-import-confirm-actions">
        <Button type="button" variant="outline" color="gray" disabled={busy} onClick={onBack}><ArrowLeft size={18} aria-hidden />返回审核</Button>
        <Button type="button" disabled={busy} onClick={onConfirm}>
          {busy ? <CircleNotch className="batch-import-spin" size={18} aria-hidden /> : <CheckCircle size={18} weight="fill" aria-hidden />}
          {busy ? "正在确认" : `确认保存 ${counts.save} 项`}
        </Button>
      </div>
    </section>
  );
}

function OrderedBatchIndex({
  batch,
  selectedItemId,
  focus,
  anchorOffset,
  onSelect,
}: {
  batch: CollectionImportBatchSnapshot;
  selectedItemId?: string;
  focus?: string;
  anchorOffset?: number;
  onSelect: (item: CollectionImportBatchItemSummary, trigger: HTMLButtonElement, triggerKind: "keyboard" | "pointer") => void;
}) {
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const [rovingId, setRovingId] = useState(() => selectedItemId ?? collectionImportItemId(batch.items[0]));

  useEffect(() => {
    if (selectedItemId) setRovingId(selectedItemId);
  }, [selectedItemId]);

  useEffect(() => {
    if (!focus?.startsWith("item:")) return;
    const button = buttons.current.get(focus.slice(5));
    if (!button) return;
    button.focus({ preventScroll: true });
    if (typeof anchorOffset === "number" && Number.isFinite(anchorOffset)) {
      const nextScroll = window.scrollY + button.getBoundingClientRect().top - anchorOffset;
      window.scrollTo({ top: Math.max(0, nextScroll), behavior: "auto" });
    }
  }, [anchorOffset, focus]);

  const move = (event: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index;
    if (event.key === "ArrowDown") next = Math.min(batch.items.length - 1, index + 1);
    else if (event.key === "ArrowUp") next = Math.max(0, index - 1);
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = batch.items.length - 1;
    else return;
    event.preventDefault();
    const id = collectionImportItemId(batch.items[next]);
    setRovingId(id);
    buttons.current.get(id)?.focus();
  };

  return (
    <section className="batch-import-index" aria-labelledby="batch-index-title">
      <div className="batch-import-index-heading">
        <h2 id="batch-index-title">原序列表</h2>
        <span>{batch.items.length} 项</span>
      </div>
      <ol>
        {batch.items.map((item, index) => {
          const id = collectionImportItemId(item);
          return (
            <li key={id} data-status={item.state}>
              <button
                ref={(node) => { if (node) buttons.current.set(id, node); else buttons.current.delete(id); }}
                type="button"
                aria-current={selectedItemId === id ? "true" : undefined}
                tabIndex={rovingId === id ? 0 : -1}
                onFocus={() => setRovingId(id)}
                onKeyDown={(event) => move(event, index)}
                onClick={(event) => onSelect(item, event.currentTarget, event.detail === 0 ? "keyboard" : "pointer")}
              >
                <span className="batch-import-position">{collectionImportDisplayPosition(item.position)}</span>
                <span className="batch-import-index-copy">
                  <strong>{item.display_label || `第 ${collectionImportDisplayPosition(item.position)} 项`}</strong>
                  <span><BatchItemStatusIcon item={item} />{collectionImportStatusLabel(item.state)}</span>
                </span>
                <ArrowRight size={18} aria-hidden />
              </button>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function ItemReview({
  batch,
  itemId,
  state,
  detail,
  error,
  onRetry,
  onBackToList,
  onBatchSnapshot,
  onAuthoritativeDetail,
  onOpenExisting,
  registerGuard,
  confirmEnabled,
  confirmCount,
  onOpenConfirm,
  onDirtyChange,
  onAnnouncement,
  confirmationOpen,
  authorityBlocked,
  batchCommandBlocked,
}: {
  batch: CollectionImportBatchSnapshot;
  itemId?: string;
  state: ItemState;
  detail: CollectionImportItemDetail | null;
  error: string;
  onRetry: () => void;
  onBackToList: (returnFocus?: HTMLElement | null) => void;
  onBatchSnapshot: (snapshot: CollectionImportBatchSnapshot) => void;
  onAuthoritativeDetail: (detail: CollectionImportItemDetail) => void;
  onOpenExisting: (collectionItemId: string) => void;
  registerGuard: (guard: CollectionImportLeaveGuard | null) => void;
  confirmEnabled: boolean;
  confirmCount: number;
  onOpenConfirm: (trigger: HTMLButtonElement) => void;
  onDirtyChange: (dirty: boolean) => void;
  onAnnouncement: (message: string) => void;
  confirmationOpen: boolean;
  authorityBlocked: boolean;
  batchCommandBlocked: boolean;
}) {
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const disclosureRef = useRef<HTMLButtonElement | null>(null);
  const focusedItem = useRef<string | null>(null);
  const [inputOpen, setInputOpen] = useState(false);
  const summary = batch.items.find((item) => collectionImportItemId(item) === itemId);

  useEffect(() => { setInputOpen(false); }, [itemId]);
  useEffect(() => {
    const identity = itemId ? `${batch.batch_id}:${itemId}` : null;
    if (!detail || focusedItem.current === identity || !window.matchMedia?.("(max-width: 900px)").matches) return;
    focusedItem.current = identity;
    headingRef.current?.focus();
  }, [batch.batch_id, detail, itemId]);
  useEffect(() => {
    if (!inputOpen) return undefined;
    const close = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setInputOpen(false);
      window.requestAnimationFrame(() => disclosureRef.current?.focus());
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [inputOpen]);

  if (!itemId || !summary) {
    return (
      <section className="batch-import-review batch-import-review-empty" aria-labelledby="batch-empty-review-title" tabIndex={-1}>
        <ListPlus size={34} aria-hidden />
        <h2 id="batch-empty-review-title">选择一项开始审核</h2>
        <p>列表会保持原始顺序。状态更新不会自动打开第一项，也不会替你选择保存。</p>
      </section>
    );
  }
  if (state === "loading") {
    return <section className="batch-import-review batch-import-review-loading" aria-busy="true" tabIndex={-1}><CircleNotch className="batch-import-spin" size={26} aria-hidden /><h2>正在读取第 {collectionImportDisplayPosition(summary.position)} 项</h2><p>只读取这一项的权威预览和状态。</p></section>;
  }
  if (state === "error" || !detail) {
    return <section className="batch-import-review batch-import-review-error" role="alert" tabIndex={-1}><WarningCircle size={27} weight="fill" aria-hidden /><h2>这一项暂时无法读取</h2><p>{error}</p><Button variant="outline" color="gray" onClick={onRetry}>重新读取</Button></section>;
  }

  const preview = detail.preview;
  const title = preview?.metadata.title.value || detail.display_label || `第 ${collectionImportDisplayPosition(detail.position)} 项`;
  return (
    <section className="batch-import-review" aria-labelledby="batch-item-title" tabIndex={-1}>
      <Button className="batch-import-mobile-back" variant="ghost" color="gray" onClick={(event) => onBackToList(event.currentTarget)}>
        <ArrowLeft size={18} aria-hidden />返回批次
      </Button>
      {error && (
        <div className="batch-import-item-notice" role="alert">
          <WarningCircle size={21} weight="fill" aria-hidden />
          <div><strong>最新项目状态暂时没有读到</strong><p>{error}</p></div>
          <Button variant="outline" color="gray" onClick={onRetry}>重新读取</Button>
        </div>
      )}
      <div className="batch-import-review-heading">
        <div>
          <span>第 {collectionImportDisplayPosition(detail.position)}/{batch.items.length} 项</span>
          <h2 ref={headingRef} id="batch-item-title" tabIndex={-1}>{title}</h2>
        </div>
        <p><BatchItemStatusIcon item={detail} />{collectionImportStatusLabel(detail.state)}</p>
      </div>

      <div className="batch-import-source-facts">
        <h3>来源预览</h3>
        {preview ? (
          <>
            <dl>
              <div><dt>来源</dt><dd>{collectionImportPlatformNames[preview.platform]}</dd></div>
              <div><dt>作者</dt><dd>{preview.metadata.author.value || "未提供"}</dd></div>
              <div><dt>元信息</dt><dd>{preview.metadata_status === "recognized" ? "平台公开信息" : preview.metadata_status === "generic" ? "普通网页信息" : "元信息不可用"}</dd></div>
            </dl>
            {preview.metadata.source_copy.value && <p className="batch-import-source-copy">{preview.metadata.source_copy.value}</p>}
          </>
        ) : (
          <p>这项尚未生成可审核预览。原链接仍保留在批次中，检查失败不会阻止后续普通书签降级。</p>
        )}
      </div>

      {detail.input_available && detail.input_text !== null && (
        <div className="batch-import-input-disclosure">
          <button ref={disclosureRef} type="button" aria-expanded={inputOpen} onClick={() => setInputOpen((open) => !open)}>
            查看原始输入<CaretDown size={18} aria-hidden />
          </button>
          {inputOpen && <pre>{detail.input_text}</pre>}
        </div>
      )}

      <CollectionImportReviewControls
        batch={batch}
        detail={detail}
        onBatchSnapshot={onBatchSnapshot}
        onAuthoritativeDetail={onAuthoritativeDetail}
        onOpenExisting={onOpenExisting}
        registerGuard={registerGuard}
        batchConfirmEnabled={confirmEnabled}
        batchConfirmCount={confirmCount}
        onOpenBatchConfirm={onOpenConfirm}
        onDirtyChange={onDirtyChange}
        onAnnouncement={onAnnouncement}
        confirmationOpen={confirmationOpen}
        authorityBlocked={authorityBlocked}
        batchCommandBlocked={batchCommandBlocked}
      />
    </section>
  );
}

function BatchSurface({
  route,
  batch,
  batchError,
  itemState,
  itemDetail,
  itemError,
  onRefresh,
  onRetryItem,
  onRouteChange,
  onBackToLibrary,
  onBackToList,
  onOpenExisting,
  registerLeaveGuard,
  onBatchSnapshot,
  onAuthoritativeDetail,
  confirmState,
  confirmError,
  confirmBusy,
  onConfirm,
  onRefreshConfirm,
  onBackFromConfirm,
}: {
  route: CollectionImportRouteState;
  batch: CollectionImportBatchSnapshot;
  batchError: string;
  itemState: ItemState;
  itemDetail: CollectionImportItemDetail | null;
  itemError: string;
  onRefresh: () => void;
  onRetryItem: () => void;
  onRouteChange: (route: CollectionImportRouteState, replace?: boolean) => void;
  onBackToLibrary: () => void;
  onBackToList: (batchItemId: string) => void;
  onOpenExisting: (collectionItemId: string) => void;
  registerLeaveGuard: (guard: (() => boolean) | null) => void;
  onBatchSnapshot: (snapshot: CollectionImportBatchSnapshot) => void;
  onAuthoritativeDetail: (detail: CollectionImportItemDetail) => void;
  confirmState: ConfirmState;
  confirmError: string;
  confirmBusy: boolean;
  onConfirm: () => void;
  onRefreshConfirm: () => void;
  onBackFromConfirm: () => void;
}) {
  const titleRef = useRef<HTMLHeadingElement | null>(null);
  const statusRef = useRef<HTMLHeadingElement | null>(null);
  const latestBatchRef = useRef(batch);
  const liveRef = useRef("");
  const liveBatchId = useRef("");
  const pageRef = useRef<HTMLDivElement | null>(null);
  const restoredItemFocus = useRef("");
  const [announcement, setAnnouncement] = useState("");
  const [reviewDirty, setReviewDirty] = useState(false);
  const [mobileLayout, setMobileLayout] = useState(() => window.matchMedia?.("(max-width: 900px)").matches ?? false);
  const [batchCommandBusy, setBatchCommandBusy] = useState<BatchCommandBusy>(null);
  const [batchCommandFeedback, setBatchCommandFeedback] = useState<BatchCommandFeedback | null>(null);
  const [batchAuthorityUncertain, setBatchAuthorityUncertain] = useState(false);
  const [cancelOpen, setCancelOpen] = useState(false);
  const commandAnnouncementIdentity = useRef("");
  const lifecycleFocusAfterLayout = useRef("");
  const lifecycleFocusAfterCommand = useRef("");
  const itemGuard = useRef<CollectionImportLeaveGuard | null>(null);
  latestBatchRef.current = batch;

  const publishBatchSnapshot = useCallback((snapshot: CollectionImportBatchSnapshot) => {
    const ordered = orderedBatchSnapshot(snapshot);
    latestBatchRef.current = ordered;
    onBatchSnapshot(ordered);
  }, [onBatchSnapshot]);

  const attempt = useCallback((run: () => void, returnFocus?: HTMLElement | null) => {
    const intent = { run, returnFocus };
    if (!itemGuard.current?.(intent)) run();
  }, []);

  const focusLifecycleTarget = useCallback((token: string, fallbackToStatus = false) => {
    window.requestAnimationFrame(() => {
      const target = pageRef.current?.querySelector<HTMLElement>(`[data-batch-lifecycle-focus="${token}"]`);
      if (target) target.focus({ preventScroll: true });
      else if (fallbackToStatus) statusRef.current?.focus({ preventScroll: true });
    });
  }, []);

  const validateCommandSnapshot = useCallback((
    snapshot: CollectionImportBatchSnapshot,
    expected: CollectionImportBatchSnapshot,
    allowSameRevision = false,
  ) => {
    const ordered = orderedBatchSnapshot(snapshot);
    const expectedOrdered = orderedBatchSnapshot(expected);
    const sameRevisionChangedAuthority = allowSameRevision
      && ordered.revision === expectedOrdered.revision
      && (
        ordered.status !== expectedOrdered.status
        || ordered.total !== expectedOrdered.total
        || ordered.queued !== expectedOrdered.queued
        || ordered.previewing !== expectedOrdered.previewing
        || ordered.ready !== expectedOrdered.ready
        || ordered.needs_review !== expectedOrdered.needs_review
        || ordered.duplicates !== expectedOrdered.duplicates
        || ordered.already_exists !== expectedOrdered.already_exists
        || ordered.failed !== expectedOrdered.failed
        || ordered.selected !== expectedOrdered.selected
        || ordered.created_at !== expectedOrdered.created_at
        || ordered.updated_at !== expectedOrdered.updated_at
        || ordered.terminal_at !== expectedOrdered.terminal_at
        || JSON.stringify(ordered.items) !== JSON.stringify(expectedOrdered.items)
      );
    if (
      ordered.batch_id !== expected.batch_id
      || ordered.revision < expected.revision
      || (!allowSameRevision && ordered.revision === expected.revision)
      || sameRevisionChangedAuthority
    ) {
      throw new CollectionImportApiError("批次命令响应无法与当前权威状态对账。", "INVALID_RESPONSE", 0);
    }
    return ordered;
  }, []);

  const reconcileBatchCommand = useCallback(async (
    command: "resume" | "cancel",
    expected: CollectionImportBatchSnapshot,
    error: unknown,
  ) => {
    if (!shouldReconcileBatchCommand(error)) throw error;
    try {
      const latest = validateCommandSnapshot(
        await collectionImportApi.getBatch(expected.batch_id),
        expected,
        true,
      );
      publishBatchSnapshot(latest);
      setBatchAuthorityUncertain(false);
      setCancelOpen(false);
      if (command === "cancel" && latest.status === "cancelled") {
        setBatchCommandFeedback({ tone: "success", text: "权威状态显示批次已取消。已保存项仍会保留。" });
        statusRef.current?.focus();
        return;
      }
      if (command === "cancel" && latest.status === "cancelling") {
        setBatchCommandFeedback({ tone: "info", text: "权威状态显示正在取消。已保存及正在提交的项可能保留。" });
        statusRef.current?.focus();
        return;
      }
      if (command === "resume" && latest.status !== "interrupted") {
        const stage = batchResumeFeedback(latest);
        setBatchCommandFeedback({ ...stage, text: `权威状态已更新。${stage.text}` });
        if (!canResumeCollectionImportBatch(latest)) statusRef.current?.focus();
        return;
      }
      setBatchCommandFeedback({
        tone: "warning",
        text: `已读取最新权威状态，但系统没有自动重放${command === "resume" ? "恢复" : "取消"}。如仍需执行，请按最新状态重新点击。`,
      });
      focusLifecycleTarget(command, true);
    } catch {
      setBatchAuthorityUncertain(true);
      setCancelOpen(false);
      setBatchCommandFeedback({
        tone: "error",
        text: `无法确认上次${command === "resume" ? "恢复" : "取消"}是否生效。系统没有重放请求；请先刷新权威状态。`,
      });
    }
  }, [focusLifecycleTarget, publishBatchSnapshot, validateCommandSnapshot]);

  const resumeBatch = useCallback(async (trigger: HTMLButtonElement) => {
    const expected = latestBatchRef.current;
    if (batchCommandBusy !== null || batchAuthorityUncertain || !canResumeCollectionImportBatch(expected)) return;
    setBatchCommandBusy("resume");
    commandAnnouncementIdentity.current = "";
    setBatchCommandFeedback({ tone: "info", text: "正在执行本地对账。确认可继续后，才会恢复缺失的检查或保存。" });
    try {
      const snapshot = validateCommandSnapshot(await collectionImportApi.resumeBatch(expected.batch_id, {
        expected_batch_revision: expected.revision,
      }), expected, true);
      publishBatchSnapshot(snapshot);
      setBatchAuthorityUncertain(false);
      setBatchCommandFeedback(batchResumeFeedback(snapshot));
      if (!canResumeCollectionImportBatch(snapshot)) statusRef.current?.focus();
      else trigger.focus({ preventScroll: true });
    } catch (error) {
      try {
        await reconcileBatchCommand("resume", expected, error);
      } catch (stableError) {
        setBatchCommandFeedback({ tone: "error", text: safeErrorMessage(stableError, "恢复没有完成，请按当前权威状态重试。") });
      }
    } finally {
      setBatchCommandBusy(null);
    }
  }, [batchAuthorityUncertain, batchCommandBusy, publishBatchSnapshot, reconcileBatchCommand, validateCommandSnapshot]);

  const cancelBatch = useCallback(async () => {
    const expected = latestBatchRef.current;
    if (batchCommandBusy !== null || batchAuthorityUncertain || !canCancelCollectionImportBatch(expected)) return;
    setBatchCommandBusy("cancel");
    commandAnnouncementIdentity.current = "";
    setBatchCommandFeedback({ tone: "info", text: "正在提交取消意图。已保存及正在提交的项不会被强制撤回。" });
    try {
      const result = await collectionImportApi.cancelBatch(expected.batch_id, {
        expected_batch_revision: expected.revision,
      });
      const snapshot = validateCommandSnapshot(result.snapshot, expected);
      if (result.status === 200 && snapshot.status !== "cancelled") {
        throw new CollectionImportApiError("取消完成响应与权威状态不一致。", "INVALID_RESPONSE", result.status);
      }
      if (result.status === 202 && snapshot.status !== "cancelling") {
        throw new CollectionImportApiError("取消受理响应与权威状态不一致。", "INVALID_RESPONSE", result.status);
      }
      publishBatchSnapshot(snapshot);
      setBatchAuthorityUncertain(false);
      setCancelOpen(false);
      setBatchCommandFeedback(result.status === 200
        ? { tone: "success", text: "已取消。已保存项仍会保留。" }
        : { tone: "info", text: "正在取消。已保存及正在提交的项可能保留，页面会继续读取权威状态。" });
      statusRef.current?.focus();
    } catch (error) {
      try {
        await reconcileBatchCommand("cancel", expected, error);
      } catch (stableError) {
        setBatchCommandFeedback({ tone: "error", text: safeErrorMessage(stableError, "取消没有完成，请核对后重试。") });
      }
    } finally {
      setBatchCommandBusy(null);
    }
  }, [batchAuthorityUncertain, batchCommandBusy, publishBatchSnapshot, reconcileBatchCommand, validateCommandSnapshot]);

  const refreshBatchAuthority = useCallback(async () => {
    if (batchCommandBusy !== null) return;
    const expected = latestBatchRef.current;
    setBatchCommandBusy("refresh");
    setBatchCommandFeedback({ tone: "info", text: "正在读取权威状态，不会自动执行恢复、取消或确认。" });
    try {
      const latest = validateCommandSnapshot(
        await collectionImportApi.getBatch(expected.batch_id),
        expected,
        true,
      );
      lifecycleFocusAfterCommand.current = "refresh";
      publishBatchSnapshot(latest);
      setBatchAuthorityUncertain(false);
      setCancelOpen(false);
      setBatchCommandFeedback({ tone: "success", text: "已读取最新权威状态。系统没有自动重放任何批次命令。" });
    } catch {
      setBatchAuthorityUncertain(true);
      setBatchCommandFeedback({ tone: "error", text: "权威状态仍无法读取。恢复、取消和再次确认继续阻断。" });
    } finally {
      setBatchCommandBusy(null);
    }
  }, [batchCommandBusy, publishBatchSnapshot, validateCommandSnapshot]);

  useEffect(() => {
    registerLeaveGuard(() => {
      const active = document.activeElement;
      const returnFocus = active instanceof HTMLElement && pageRef.current?.contains(active)
        ? active
        : pageRef.current?.querySelector<HTMLElement>("#batch-item-title") ?? titleRef.current;
      return itemGuard.current?.({
        run: () => window.history.back(),
        history: true,
        returnFocus,
      }) ?? false;
    });
    return () => registerLeaveGuard(null);
  }, [registerLeaveGuard]);

  useEffect(() => {
    setBatchCommandBusy(null);
    setBatchCommandFeedback(null);
    setBatchAuthorityUncertain(false);
    setCancelOpen(false);
    commandAnnouncementIdentity.current = "";
    lifecycleFocusAfterCommand.current = "";
  }, [batch.batch_id]);

  useEffect(() => {
    if (batchCommandBusy !== null) return;
    const focusToken = lifecycleFocusAfterCommand.current;
    if (!focusToken) return;
    lifecycleFocusAfterCommand.current = "";
    focusLifecycleTarget(focusToken, true);
  }, [batch, batchAuthorityUncertain, batchCommandBusy, focusLifecycleTarget]);

  useEffect(() => {
    if (!canCancelCollectionImportBatch(batch)) setCancelOpen(false);
  }, [batch]);

  useEffect(() => {
    const media = window.matchMedia?.("(max-width: 900px)");
    if (!media) return undefined;
    const change = (event: MediaQueryListEvent) => {
      const active = document.activeElement;
      const focusToken = active instanceof HTMLElement ? active.dataset.batchLifecycleFocus : undefined;
      lifecycleFocusAfterLayout.current = focusToken ?? "";
      setMobileLayout(event.matches);
    };
    setMobileLayout(media.matches);
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);

  useEffect(() => {
    const focusToken = lifecycleFocusAfterLayout.current;
    if (!focusToken) return;
    lifecycleFocusAfterLayout.current = "";
    window.requestAnimationFrame(() => {
      const replacement = pageRef.current?.querySelector<HTMLElement>(`[data-batch-lifecycle-focus="${focusToken}"]`);
      if (replacement) replacement.focus({ preventScroll: true });
      else (pageRef.current?.querySelector<HTMLElement>("#batch-item-title") ?? statusRef.current)?.focus({ preventScroll: true });
    });
  }, [mobileLayout]);

  useEffect(() => {
    const viewport = window.visualViewport;
    const node = pageRef.current;
    if (!viewport || !node) return undefined;
    const update = () => {
      const offset = Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop);
      node.style.setProperty("--batch-import-keyboard-offset", `${offset}px`);
    };
    update();
    viewport.addEventListener("resize", update);
    viewport.addEventListener("scroll", update);
    return () => {
      viewport.removeEventListener("resize", update);
      viewport.removeEventListener("scroll", update);
      node.style.removeProperty("--batch-import-keyboard-offset");
    };
  }, []);

  useEffect(() => {
    if (route.focus === "summary") titleRef.current?.focus();
    if (route.focus === "batch-status") statusRef.current?.focus();
  }, [batch.batch_id, route.focus]);
  useEffect(() => {
    if (route.view !== "item") setReviewDirty(false);
  }, [route.view]);
  useEffect(() => {
    if (route.view === "confirm" || route.focus !== "confirm-trigger") return undefined;
    return restoreBatchItemPosition(route.restoreScrollY, () => (
      route.view === "item" && window.matchMedia?.("(max-width: 900px)").matches
        ? pageRef.current?.querySelector<HTMLElement>("#batch-item-title") ?? null
        : pageRef.current?.querySelector<HTMLButtonElement>("[data-batch-confirm-trigger]:not(:disabled)")
          ?? pageRef.current?.querySelector<HTMLElement>("#batch-item-title")
          ?? statusRef.current
          ?? null
    ));
  }, [route.focus, route.restoreScrollY, route.restoreToken, route.view]);
  useEffect(() => {
    if (route.view !== "item") {
      restoredItemFocus.current = "";
      return undefined;
    }
    if (itemState !== "ready" || !itemDetail) return undefined;
    const existingItemId = route.focus?.startsWith("existing:") ? route.focus.slice("existing:".length) : null;
    if (route.focus !== "item-heading" && existingItemId !== itemDetail.batch_item_id) return undefined;
    const identity = `${batch.batch_id}|${route.view}|${route.itemId ?? ""}|${route.focus ?? ""}|${route.restoreToken ?? ""}`;
    if (restoredItemFocus.current === identity) return undefined;
    restoredItemFocus.current = identity;
    return restoreBatchItemPosition(route.restoreScrollY, () => (
      existingItemId
        ? [...(pageRef.current?.querySelectorAll<HTMLElement>("[data-batch-existing-trigger]") ?? [])]
          .find((node) => node.dataset.batchExistingTrigger === existingItemId) ?? null
        : pageRef.current?.querySelector<HTMLElement>("#batch-item-title") ?? null
    ));
  }, [batch.batch_id, itemDetail?.batch_item_id, itemState, route.focus, route.itemId, route.restoreScrollY, route.restoreToken, route.view]);
  useEffect(() => {
    const identity = `${batch.status}|${batch.revision}|${batch.items.map((item) => `${item.batch_item_id}:${item.state}`).join("|")}`;
    if (liveBatchId.current !== batch.batch_id) {
      liveBatchId.current = batch.batch_id;
      liveRef.current = identity;
      setAnnouncement("");
      return;
    }
    if (identity !== liveRef.current) {
      liveRef.current = identity;
      setAnnouncement(`批次状态已更新：${collectionImportBatchLabel(batch.status)}。`);
    }
  }, [batch]);

  useEffect(() => {
    if (!batchCommandFeedback || batchCommandFeedback.tone === "error") return;
    const identity = `${batch.batch_id}:${batchCommandFeedback.tone}:${batchCommandFeedback.text}`;
    if (commandAnnouncementIdentity.current === identity) return;
    commandAnnouncementIdentity.current = identity;
    setAnnouncement(batchCommandFeedback.text);
  }, [batch.batch_id, batchCommandFeedback]);

  const selectedItemId = route.view === "item" || route.view === "confirm" ? route.itemId : undefined;
  useEffect(() => {
    if (route.view !== "item") return undefined;
    const media = window.matchMedia?.("(max-width: 900px)");
    if (!media) return undefined;
    const moveFocusFromHiddenIndex = (event: MediaQueryListEvent) => {
      if (!event.matches) return;
      const page = pageRef.current;
      const active = document.activeElement;
      const index = page?.querySelector<HTMLElement>(".batch-import-index");
      if (!page || !active || !index?.contains(active)) return;
      window.requestAnimationFrame(() => {
        const destination = page.querySelector<HTMLElement>("#batch-item-title")
          ?? page.querySelector<HTMLElement>(".batch-import-review");
        destination?.focus({ preventScroll: true });
      });
    };
    media.addEventListener("change", moveFocusFromHiddenIndex);
    return () => media.removeEventListener("change", moveFocusFromHiddenIndex);
  }, [route.view]);

  const selectItem = useCallback((item: CollectionImportBatchItemSummary, trigger: HTMLButtonElement, triggerKind: "keyboard" | "pointer") => {
    const id = collectionImportItemId(item);
    if (selectedItemId === id) return;
    const mobile = window.matchMedia?.("(max-width: 900px)").matches;
    attempt(() => {
      setAnnouncement(`已打开第 ${collectionImportDisplayPosition(item.position)} 项，状态为${collectionImportStatusLabel(item.state)}。`);
      onRouteChange({
        batchId: batch.batch_id,
        view: "item",
        itemId: id,
        focus: mobile ? "item-heading" : `item:${id}`,
        anchorItemId: id,
        anchorOffset: trigger.getBoundingClientRect().top,
        trigger: triggerKind,
      });
      if (!mobile) window.requestAnimationFrame(() => trigger.focus({ preventScroll: true }));
    }, trigger);
  }, [attempt, batch.batch_id, onRouteChange, selectedItemId]);

  const openConfirm = useCallback((trigger: HTMLButtonElement) => {
    if (batchAuthorityUncertain || !canConfirmCollectionImportBatch(batch, reviewDirty)) return;
    attempt(() => {
      onRouteChange({
        batchId: batch.batch_id,
        view: "confirm",
        itemId: selectedItemId,
        focus: "confirm-heading",
      });
    }, trigger);
  }, [attempt, batch, batchAuthorityUncertain, onRouteChange, reviewDirty, selectedItemId]);

  const requestResume = useCallback((trigger: HTMLButtonElement) => {
    attempt(() => void resumeBatch(trigger), trigger);
  }, [attempt, resumeBatch]);

  const openCancel = useCallback((trigger: HTMLButtonElement) => {
    attempt(() => {
      setBatchCommandFeedback(null);
      setCancelOpen(true);
    }, trigger);
  }, [attempt]);

  const confirmCount = collectionImportConfirmationCounts(batch).save;
  const confirmEnabled = !batchAuthorityUncertain && canConfirmCollectionImportBatch(batch, reviewDirty);

  return (
    <div ref={pageRef} className="collection-page batch-import-page" data-view={route.view}>
      <BatchHeader onBack={(trigger) => attempt(onBackToLibrary, trigger)} />
      <main className="batch-import-main">
        <BatchSummaryBand batch={batch} titleRef={titleRef} statusRef={statusRef}>
          {!mobileLayout && route.view !== "confirm" && (
            <BatchLifecycleActions
              batch={batch}
              busy={batchCommandBusy}
              feedback={batchCommandFeedback}
              authorityUncertain={batchAuthorityUncertain}
              cancelOpen={cancelOpen}
              onResume={requestResume}
              onOpenCancel={openCancel}
              onCloseCancel={() => setCancelOpen(false)}
              onConfirmCancel={(trigger) => attempt(() => void cancelBatch(), trigger)}
              onRefresh={() => void refreshBatchAuthority()}
              showOutcomeRefresh={route.view === "list" || batch.items.find((item) => item.batch_item_id === selectedItemId)?.state !== "outcome_unknown"}
              placement="desktop"
            />
          )}
        </BatchSummaryBand>
        {batchError && (
          <div className="batch-import-batch-notice" role="alert">
            <WarningCircle size={21} weight="fill" aria-hidden />
            <div><strong>最新状态暂时没有读到</strong><p>{batchError}</p></div>
            <Button variant="outline" color="gray" onClick={onRefresh}>重新读取</Button>
          </div>
        )}
        <div className="batch-import-workspace">
          <OrderedBatchIndex batch={batch} selectedItemId={selectedItemId} focus={route.focus} anchorOffset={route.anchorOffset} onSelect={selectItem} />
          {mobileLayout && route.view === "list" && (
            <div className="batch-import-mobile-batch-dock">
              <BatchLifecycleActions
                batch={batch}
                busy={batchCommandBusy}
                feedback={batchCommandFeedback}
                authorityUncertain={batchAuthorityUncertain}
                cancelOpen={cancelOpen}
                onResume={requestResume}
                onOpenCancel={openCancel}
                onCloseCancel={() => setCancelOpen(false)}
                onConfirmCancel={(trigger) => attempt(() => void cancelBatch(), trigger)}
                onRefresh={() => void refreshBatchAuthority()}
                showOutcomeRefresh
                placement="mobile"
              />
              <BatchConfirmEntry
                batch={batch}
                dirty={reviewDirty}
                authorityBlocked={batchAuthorityUncertain}
                commandBlocked={cancelOpen || batchCommandBusy !== null}
                onOpen={openConfirm}
                compact
              />
            </div>
          )}
          <div className="batch-import-review-column">
            {route.view !== "confirm" || selectedItemId ? (
              <ItemReview
                batch={batch}
                itemId={selectedItemId}
                state={itemState}
                detail={itemDetail}
                error={itemError}
                onRetry={onRetryItem}
                onBackToList={(trigger) => selectedItemId && attempt(() => onBackToList(selectedItemId), trigger)}
                onBatchSnapshot={publishBatchSnapshot}
                onAuthoritativeDetail={onAuthoritativeDetail}
                onOpenExisting={onOpenExisting}
                registerGuard={(guard) => { itemGuard.current = guard; }}
                confirmEnabled={confirmEnabled}
                confirmCount={confirmCount}
                onOpenConfirm={openConfirm}
                onDirtyChange={setReviewDirty}
                onAnnouncement={setAnnouncement}
                confirmationOpen={route.view === "confirm"}
                authorityBlocked={batchAuthorityUncertain}
                batchCommandBlocked={cancelOpen || batchCommandBusy !== null}
              />
            ) : null}
            {route.view === "list" && !mobileLayout && <BatchConfirmEntry batch={batch} dirty={reviewDirty} authorityBlocked={batchAuthorityUncertain} commandBlocked={cancelOpen || batchCommandBusy !== null} onOpen={openConfirm} />}
            {route.view === "confirm" && (
              <BatchConfirmSurface
                batch={batch}
                state={confirmState}
                error={confirmError}
                busy={confirmBusy}
                onBack={onBackFromConfirm}
                onConfirm={onConfirm}
                onRefresh={onRefreshConfirm}
              />
            )}
          </div>
        </div>
      </main>
      <div className="sr-only" aria-live="polite" aria-atomic="true">{announcement}</div>
    </div>
  );
}

export function CollectionImportPage({ route, onRouteChange, onBackToLibrary, onBackToList, onBackFromConfirm, onOpenExisting, registerLeaveGuard }: CollectionImportPageProps) {
  const [pageState, setPageState] = useState<PageState>("loading");
  const [batch, setBatch] = useState<CollectionImportBatchSnapshot | null>(null);
  const [batchError, setBatchError] = useState("");
  const [refreshToken, setRefreshToken] = useState(0);
  const [batchRequestSettledToken, setBatchRequestSettledToken] = useState(0);
  const [draftItems, setDraftItems] = useState<CollectionImportDraftItem[]>(() => [newDraftItem(), newDraftItem()]);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [activeBatchId, setActiveBatchId] = useState<string | null>(null);
  const [itemState, setItemState] = useState<ItemState>("idle");
  const [itemDetail, setItemDetail] = useState<CollectionImportItemDetail | null>(null);
  const [itemError, setItemError] = useState("");
  const [itemRefreshToken, setItemRefreshToken] = useState(0);
  const [confirmState, setConfirmState] = useState<ConfirmState>("idle");
  const [confirmError, setConfirmError] = useState("");
  const [confirmBusy, setConfirmBusy] = useState(false);
  const requestIdentity = useRef<{ fingerprint: string; key: string } | null>(null);
  const confirmNeedsReactivation = useRef(false);
  const confirmExpected = useRef<CollectionImportBatchSnapshot | null>(null);
  const fatalHeadingRef = useRef<HTMLHeadingElement | null>(null);
  const selectedItemRevision = batch?.items.find((item) => item.batch_item_id === route.itemId)?.item_revision;

  const acceptBatchSnapshot = useCallback((snapshot: CollectionImportBatchSnapshot) => {
    const ordered = orderedBatchSnapshot(snapshot);
    setBatch(ordered);
    setItemDetail((current) => {
      if (!current) return current;
      const summary = ordered.items.find((item) => item.batch_item_id === current.batch_item_id);
      return summary ? { ...current, ...summary, batch_revision: ordered.revision } : current;
    });
    return ordered;
  }, []);

  useEffect(() => {
    if (pageState === "error") fatalHeadingRef.current?.focus();
  }, [pageState]);

  useEffect(() => {
    const controller = new AbortController();
    setBatchError("");
    if (route.view === "confirm") {
      setConfirmState("loading");
      setConfirmError("");
    } else {
      confirmNeedsReactivation.current = false;
      confirmExpected.current = null;
      setConfirmState("idle");
      setConfirmError("");
    }
    if (!route.batchId) {
      setPageState("loading");
      collectionImportApi.getActiveBatch(controller.signal)
        .then((active) => {
          if (controller.signal.aborted) return;
          if (!active) {
            setBatch(null);
            setPageState("create");
            return;
          }
          onRouteChange({ batchId: active.batch_id, view: "list", focus: "summary" }, true);
        })
        .catch((error) => {
          if (controller.signal.aborted) return;
          setBatchError(safeErrorMessage(error, "活动批次暂时无法读取。"));
          setPageState("error");
        });
    } else {
      setPageState((current) => batch?.batch_id === route.batchId && current === "batch" ? current : "loading");
      collectionImportApi.getBatch(route.batchId, controller.signal)
        .then((snapshot) => {
          if (controller.signal.aborted) return;
          if (snapshot.batch_id !== route.batchId) {
            setBatch(null);
            setBatchError("批次身份不一致，未显示其他批次内容。请返回素材库后重试。");
            setPageState("error");
            return;
          }
          const ordered = acceptBatchSnapshot(snapshot);
          setPageState("batch");
          if (route.itemId && !ordered.items.some((item) => collectionImportItemId(item) === route.itemId)) {
            if (route.view === "confirm") {
              onRouteChange({ batchId: route.batchId, view: "confirm", focus: "confirm-heading" }, true);
            } else {
              onRouteChange({ batchId: route.batchId, view: "list", focus: "summary" }, true);
              return;
            }
          }
          if (route.view === "confirm") {
            if (confirmExpected.current && collectionImportConfirmObserved(ordered, confirmExpected.current)) {
              onRouteChange({ batchId: route.batchId, view: "list", focus: "batch-status" }, true);
            } else if (confirmNeedsReactivation.current) {
              setConfirmState("blocked");
              setConfirmError("已重新读取权威状态，但系统不会自动重放确认。请返回审核并重新打开核对。 ");
            } else if (canConfirmCollectionImportBatch(ordered)) {
              setConfirmState("ready");
            } else {
              onRouteChange({
                batchId: route.batchId,
                view: "list",
                focus: collectionImportConfirmationFrozen(ordered) ? "batch-status" : "confirm-trigger",
              }, true);
            }
          }
        })
        .catch((error) => {
          if (controller.signal.aborted) return;
          const message = safeErrorMessage(error, "批次暂时无法读取。");
          if (route.view === "confirm" && batch) {
            setConfirmState("error");
            setConfirmError(`无法复核确认资格：${message}`);
            setPageState("batch");
          } else {
            setBatchError(message);
            setPageState((current) => batch ? current : "error");
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) setBatchRequestSettledToken((value) => value + 1);
        });
    }
    return () => controller.abort();
  }, [acceptBatchSnapshot, refreshToken, route.batchId, route.view === "confirm"]);

  useEffect(() => {
    if (!batch || !shouldPollCollectionImportBatch(batch)) return undefined;
    const timer = window.setTimeout(() => setRefreshToken((value) => value + 1), 1800);
    return () => window.clearTimeout(timer);
  }, [batch, batchRequestSettledToken]);

  useEffect(() => {
    if (!route.batchId || (route.view !== "item" && route.view !== "confirm") || !route.itemId) {
      setItemState("idle");
      setItemDetail(null);
      setItemError("");
      return undefined;
    }
    const controller = new AbortController();
    const batchId = route.batchId;
    const itemId = route.itemId;
    const retainCurrent = itemDetail?.batch_id === batchId && itemDetail.batch_item_id === itemId;
    if (!retainCurrent) {
      setItemState("loading");
      setItemDetail(null);
    }
    setItemError("");
    collectionImportApi.getItem(batchId, itemId, controller.signal)
      .then((detail) => {
        if (controller.signal.aborted) return;
        if (detail.batch_id !== batchId || detail.batch_item_id !== itemId) {
          setItemError("项目身份不一致，未显示其他项目内容。请返回批次列表重新打开。");
          setItemState("error");
          return;
        }
        setItemDetail(detail);
        setItemState("ready");
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        setItemError(safeErrorMessage(error, "这一项暂时无法读取。"));
        setItemState(retainCurrent ? "ready" : "error");
      });
    return () => controller.abort();
  }, [itemRefreshToken, route.batchId, route.itemId, route.view, selectedItemRevision]);

  const submit = async () => {
    const validation = validateCollectionImportDraft(draftItems);
    if (!validation.valid || submitting) return;
    const fingerprint = collectionImportRequestFingerprint(draftItems);
    const identity = requestIdentity.current?.fingerprint === fingerprint
      ? requestIdentity.current
      : { fingerprint, key: makeRequestKey() };
    requestIdentity.current = identity;
    const payload = { items: collectionImportCreateItems(draftItems) };
    setSubmitting(true);
    setSubmitError("");
    setActiveBatchId(null);
    try {
      const created = await collectionImportApi.createBatch(
        payload,
        identity.key,
      );
      setDraftItems([newDraftItem(), newDraftItem()]);
      requestIdentity.current = null;
      onRouteChange({ batchId: created.batch_id, view: "list", focus: "summary" }, true);
    } catch (error) {
      const apiError = error instanceof CollectionImportApiError ? error : null;
      if (apiError?.code === "BATCH_ACTIVE") setActiveBatchId(apiError.batchId);
      if (apiError?.code === "NETWORK_ERROR") {
        let observedActiveId: string | null = null;
        try {
          observedActiveId = (await collectionImportApi.getActiveBatch())?.batch_id ?? null;
          const reconciled = await collectionImportApi.createBatch(payload, identity.key);
          setDraftItems([newDraftItem(), newDraftItem()]);
          requestIdentity.current = null;
          onRouteChange({ batchId: reconciled.batch_id, view: "list", focus: "summary" }, true);
          return;
        } catch (reconciliationError) {
          const reconciliationApiError = reconciliationError instanceof CollectionImportApiError
            ? reconciliationError
            : null;
          if (reconciliationApiError?.code === "BATCH_ACTIVE") {
            setActiveBatchId(reconciliationApiError.batchId ?? observedActiveId);
            setSubmitError(reconciliationApiError.message);
            return;
          }
          // The original key and exact draft stay in memory for an explicit retry.
        }
      }
      setSubmitError(safeErrorMessage(error, "批次没有创建，请检查后重试。"));
    } finally {
      setSubmitting(false);
    }
  };

  const openActive = async () => {
    if (activeBatchId) {
      setDraftItems([newDraftItem(), newDraftItem()]);
      requestIdentity.current = null;
      onRouteChange({ batchId: activeBatchId, view: "list", focus: "summary" }, true);
      return;
    }
    setPageState("loading");
    try {
      const active = await collectionImportApi.getActiveBatch();
      if (active) {
        setDraftItems([newDraftItem(), newDraftItem()]);
        requestIdentity.current = null;
        onRouteChange({ batchId: active.batch_id, view: "list", focus: "summary" }, true);
      }
      else setPageState("create");
    } catch (error) {
      setBatchError(safeErrorMessage(error, "活动批次暂时无法读取。"));
      setPageState("error");
    }
  };

  const submitConfirm = async () => {
    if (!batch || route.view !== "confirm" || confirmState !== "ready" || confirmBusy) return;
    const expected = batch;
    confirmExpected.current = expected;
    setConfirmBusy(true);
    setConfirmError("");
    try {
      const snapshot = orderedBatchSnapshot(await collectionImportApi.confirmBatch(batch.batch_id, {
        expected_batch_revision: expected.revision,
      }));
      if (!collectionImportConfirmObserved(snapshot, expected)) {
        throw new CollectionImportApiError("确认响应无法与当前批次对账。", "INVALID_RESPONSE", 202);
      }
      acceptBatchSnapshot(snapshot);
      onRouteChange({ batchId: snapshot.batch_id, view: "list", focus: "batch-status" }, true);
    } catch (error) {
      confirmNeedsReactivation.current = true;
      try {
        const latest = orderedBatchSnapshot(await collectionImportApi.getBatch(batch.batch_id));
        if (latest.batch_id !== batch.batch_id) throw new Error("authority identity mismatch");
        acceptBatchSnapshot(latest);
        if (collectionImportConfirmObserved(latest, expected)) {
          onRouteChange({ batchId: latest.batch_id, view: "list", focus: "batch-status" }, true);
          return;
        }
        const apiError = error instanceof CollectionImportApiError ? error : null;
        const unknown = apiError?.code === "NETWORK_ERROR"
          || apiError?.code === "INVALID_RESPONSE"
          || apiError?.status === 0
          || !apiError;
        setConfirmState("blocked");
        setConfirmError(unknown
          ? "权威状态没有证明上次确认已生效。系统没有重放请求；请返回审核并重新打开核对。 "
          : "批次状态已经更新，本次确认没有继续。请返回审核并按最新权威状态重新打开核对。 ");
      } catch {
        setConfirmState("error");
        setConfirmError("无法确认上次请求是否生效。系统没有重放请求；请先刷新权威状态或返回审核。 ");
      }
    } finally {
      setConfirmBusy(false);
    }
  };

  if (pageState === "loading") return <LoadingSurface onBack={onBackToLibrary} />;
  if (pageState === "create") {
    return <CreateSurface draftItems={draftItems} setDraftItems={setDraftItems} submitting={submitting} submitError={submitError} activeBatchId={activeBatchId} onSubmit={submit} onOpenActive={openActive} onBack={onBackToLibrary} />;
  }
  if (pageState === "error" || !batch) {
    return (
      <div className="collection-page batch-import-page">
        <BatchHeader onBack={onBackToLibrary} />
        <main className="batch-import-fatal-error" role="alert">
          <WarningCircle size={30} weight="fill" aria-hidden />
          <h1 ref={fatalHeadingRef} tabIndex={-1}>批量导入暂时无法读取</h1>
          <p>{batchError}</p>
          <Button variant="outline" color="gray" onClick={() => setRefreshToken((value) => value + 1)}>重新读取</Button>
        </main>
      </div>
    );
  }
  return <BatchSurface route={route} batch={batch} batchError={batchError} itemState={itemState} itemDetail={itemDetail} itemError={itemError} onRefresh={() => setRefreshToken((value) => value + 1)} onRetryItem={() => setItemRefreshToken((value) => value + 1)} onRouteChange={onRouteChange} onBackToLibrary={onBackToLibrary} onBackToList={onBackToList} onOpenExisting={onOpenExisting} registerLeaveGuard={registerLeaveGuard} onBatchSnapshot={acceptBatchSnapshot} onAuthoritativeDetail={setItemDetail} confirmState={confirmState} confirmError={confirmError} confirmBusy={confirmBusy} onConfirm={() => void submitConfirm()} onRefreshConfirm={() => setRefreshToken((value) => value + 1)} onBackFromConfirm={onBackFromConfirm} />;
}
