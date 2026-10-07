import {
  ArrowLeft,
  ArrowsClockwise,
  CheckCircle,
  CircleNotch,
  LockSimple,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { Button, Dialog, IconButton } from "@radix-ui/themes";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  CollectionApiError,
  cachedItemCoverUrl,
  collectionApi,
  userItemCoverUrl,
  sourceItemCoverUrl,
  type CollectionItem,
  type CollectionItemUpdate,
  type InspirationDraft,
} from "./collection-api";
import {
  CollectionUserCoverInput,
  UserCoverPreview,
  discardUserCover,
  type UserCoverSelection,
} from "./collection-user-cover";
import {
  appendInspiration,
  clearEditMergeSession,
  defaultMergeSnapshot,
  differingFields,
  editableFieldLabels,
  normalizeEditableTag,
  normalizeSnapshot,
  readEditMergeSession,
  sameSnapshot,
  sameSnapshotField,
  setSnapshotField,
  snapshotFieldValue,
  snapshotFromItem,
  threeWayRebase,
  validateSnapshot,
  writeEditMergeSession,
  type CollectionUserSnapshot,
  type EditableField,
  type EditMergeMode,
  type EditMergeReturnKind,
  type EditMergeSession,
} from "./collection-edit-model";
import { TagEditor } from "./collection-tag-editor";
import { pythonTrim } from "./unicode-normalize";

type SurfaceStatus =
  | "item_loading"
  | "item_read_failed"
  | "ready"
  | "saving"
  | "failed_retryable"
  | "outcome_unknown"
  | "reconciling"
  | "conflict_loading_latest"
  | "conflict_review"
  | "conflict_reload_failed"
  | "success"
  | "merge_session_missing"
  | "item_missing";

type InspirationStrategy = "keep" | "append" | "replace";

const platformNames: Record<CollectionItem["platform"], string> = {
  bilibili: "哔哩哔哩",
  douyin: "抖音",
  xiaohongshu: "小红书",
  youtube: "YouTube",
  web: "网页",
  other: "其他来源",
  local_upload: "本地上传",
};

function useDesktopSidecar(): boolean {
  const [desktop, setDesktop] = useState(() => window.matchMedia("(min-width: 901px)").matches);
  useEffect(() => {
    const media = window.matchMedia("(min-width: 901px)");
    const update = () => setDesktop(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return desktop;
}

function snapshotValueText(snapshot: CollectionUserSnapshot, field: EditableField): string {
  const value = snapshotFieldValue(snapshot, field);
  if (field === "user_cover_asset_id") return value ? "用户补充封面" : "来源封面或占位图";
  if (field === "inspiration") return (value as InspirationDraft | null)?.content || "留空";
  if (Array.isArray(value)) return value.length ? value.join("、") : "留空";
  return typeof value === "string" && value ? value : "留空";
}

function initialInspirationStrategy(
  base: CollectionUserSnapshot,
  incoming: CollectionUserSnapshot,
  desired: CollectionUserSnapshot,
): InspirationStrategy | null {
  if (sameSnapshotField(desired, base, "inspiration")) return "keep";
  if (sameSnapshotField(desired, incoming, "inspiration")) return "replace";
  const appended = { ...desired, inspiration: appendInspiration(base.inspiration, incoming.inspiration) };
  return sameSnapshotField(desired, appended, "inspiration") ? "append" : null;
}

function isDefinitiveClientFailure(error: unknown): boolean {
  return error instanceof CollectionApiError && [400, 401, 403, 404, 422].includes(error.status)
    && error.code !== "COLLECTION_REVISION_CONFLICT";
}

function isMatchingAuthoritativeItem(value: CollectionItem, itemId: string): boolean {
  return value.id === itemId && Number.isInteger(value.revision) && value.revision >= 1;
}

function ImmutableIdentityCover({ item }: { item: CollectionItem }) {
  const fallbackSrc = "/assets/material-cover-fallback.webp";
  const controlledSrc = item.metadata.cover_url.value ? cachedItemCoverUrl(item.id) : "";
  const [failedSrc, setFailedSrc] = useState("");
  const displayedSrc = controlledSrc && failedSrc !== controlledSrc ? controlledSrc : fallbackSrc;
  return (
    <img
      src={displayedSrc}
      alt=""
      onError={displayedSrc === fallbackSrc ? undefined : () => setFailedSrc(controlledSrc)}
    />
  );
}

export function CollectionEditMergeSurface({
  itemId,
  mode,
  routeEntryId,
  onAuthoritativeItem,
  onSuccessCommitted,
  onCompleted,
  onExit,
  onItemMissingExit,
  onUnknownDiscard,
  registerLeaveGuard,
}: {
  itemId: string;
  mode: EditMergeMode;
  routeEntryId: string;
  onAuthoritativeItem: (item: CollectionItem) => void;
  onSuccessCommitted: (item: CollectionItem, returnKind: EditMergeReturnKind) => void;
  onCompleted: (item: CollectionItem, returnKind: EditMergeReturnKind) => void;
  onExit: (returnKind: EditMergeReturnKind) => void;
  onItemMissingExit: () => void;
  onUnknownDiscard: () => void;
  registerLeaveGuard: (guard: (() => boolean) | null) => void;
}) {
  const desktop = useDesktopSidecar();
  const initialSession = useMemo(
    () => readEditMergeSession(mode, itemId, routeEntryId),
    [itemId, mode, routeEntryId],
  );
  const [session, setSession] = useState<EditMergeSession | null>(initialSession);
  const sessionRef = useRef(session);
  const [item, setItem] = useState<CollectionItem | null>(null);
  const [desired, setDesired] = useState<CollectionUserSnapshot | null>(initialSession?.desired ?? null);
  const [status, setStatus] = useState<SurfaceStatus>(
    mode === "merge" && !initialSession ? "merge_session_missing" : "item_loading",
  );
  const [message, setMessage] = useState("");
  const [storageFailed, setStorageFailed] = useState(false);
  const [safeRetry, setSafeRetry] = useState(false);
  const [conflictFields, setConflictFields] = useState<EditableField[]>([]);
  const [conflictChoices, setConflictChoices] = useState<Partial<Record<EditableField, "latest" | "mine">>>({});
  const [latestSnapshot, setLatestSnapshot] = useState<CollectionUserSnapshot | null>(null);
  const [mineSnapshot, setMineSnapshot] = useState<CollectionUserSnapshot | null>(null);
  const [inspirationStrategy, setInspirationStrategy] = useState<InspirationStrategy | null>("keep");
  const [organizationTagDraft, setOrganizationTagDraft] = useState("");
  const [personalTagDraft, setPersonalTagDraft] = useState("");
  const [userCoverBusy, setUserCoverBusy] = useState(false);
  const [leaveOpen, setLeaveOpen] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const leaveOrigin = useRef<HTMLElement | null>(null);
  const surfaceRef = useRef<HTMLDivElement | null>(null);
  const actionsRef = useRef<HTMLElement | null>(null);
  const titleRef = useRef<HTMLHeadingElement | null>(null);
  const errorSummaryRef = useRef<HTMLDivElement | null>(null);
  const successStatusRef = useRef<HTMLDivElement | null>(null);
  const authoritativeItemCallback = useRef(onAuthoritativeItem);
  const successCommittedCallback = useRef(onSuccessCommitted);
  const completedCallback = useRef(onCompleted);
  const successCommitted = useRef(false);
  const generation = useRef(0);
  const mounted = useRef(true);
  const completeTimer = useRef<number | null>(null);

  useEffect(() => {
    sessionRef.current = session;
  }, [session]);

  useEffect(() => {
    authoritativeItemCallback.current = onAuthoritativeItem;
    successCommittedCallback.current = onSuccessCommitted;
    completedCallback.current = onCompleted;
  }, [onAuthoritativeItem, onCompleted, onSuccessCommitted]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (completeTimer.current !== null) window.clearTimeout(completeTimer.current);
      completeTimer.current = null;
    };
  }, []);

  const persistSession = useCallback((next: EditMergeSession): boolean => {
    sessionRef.current = next;
    setSession(next);
    if (!writeEditMergeSession(next)) {
      setStorageFailed(true);
      setMessage("当前标签页无法保存安全恢复草稿。请先保留本页，再重试。");
      return false;
    }
    setStorageFailed(false);
    return true;
  }, []);

  const finishSuccess = useCallback((nextItem: CollectionItem) => {
    if (successCommitted.current) return;
    successCommitted.current = true;
    const current = sessionRef.current;
    const returnKind = current?.returnKind ?? "detail";
    const authoritative = snapshotFromItem(nextItem);
    clearEditMergeSession(mode, itemId);
    if (
      current?.userCoverClaimToken
      && current.incoming?.user_cover_asset_id
      && current.incoming.user_cover_asset_id !== nextItem.user_cover_asset_id
    ) {
      void discardUserCover({
        assetId: current.incoming.user_cover_asset_id,
        claimToken: current.userCoverClaimToken,
      });
    }
    if (current) {
      const completed = {
        ...current,
        base: authoritative,
        desired: authoritative,
        expectedRevision: nextItem.revision,
        requestState: "draft" as const,
        conflictRecovery: null,
        updatedAt: Date.now(),
      };
      sessionRef.current = completed;
      setSession(completed);
    }
    setDesired(authoritative);
    setStorageFailed(false);
    setLeaveOpen(false);
    setItem(nextItem);
    authoritativeItemCallback.current(nextItem);
    setStatus("success");
    setMessage(mode === "merge" ? "已按最终预览合并到原收藏。" : "收藏内容已更新。");
    successCommittedCallback.current(nextItem, returnKind);
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    completeTimer.current = window.setTimeout(() => {
      if (mounted.current) completedCallback.current(nextItem, returnKind);
    }, reduced ? 0 : 160);
  }, [itemId, mode]);

  const applyLatest = useCallback((
    latestItem: CollectionItem,
    current: EditMergeSession,
    uncertain: boolean,
  ) => {
    const latest = snapshotFromItem(latestItem);
    if (current.expectedRevision !== null && latestItem.revision < current.expectedRevision) {
      setSafeRetry(false);
      setStatus(uncertain ? "outcome_unknown" : "conflict_reload_failed");
      setMessage(uncertain
        ? "读到的权威版本早于待核验提交，尚不能判断结果。不会盲目重发，请再次核验。"
        : "读到的权威版本早于当前草稿基线，未采用这份内容。请重新比较。");
      return;
    }
    setItem(latestItem);
    authoritativeItemCallback.current(latestItem);
    if (!current.base || !current.desired) return;

    const hadUserIntent = !sameSnapshot(current.base, current.desired);
    if (hadUserIntent && sameSnapshot(latest, current.desired)) {
      finishSuccess(latestItem);
      return;
    }
    const storedConflict = current.conflictRecovery;
    if (storedConflict
      && latestItem.revision === storedConflict.latestRevision
      && sameSnapshot(latest, storedConflict.latest)) {
      setDesired(current.desired);
      setLatestSnapshot(storedConflict.latest);
      setMineSnapshot(storedConflict.mine);
      setConflictFields(storedConflict.conflictFields);
      setConflictChoices(storedConflict.choices);
      setSafeRetry(false);
      setStatus("conflict_review");
      const remaining = storedConflict.conflictFields.filter((field) => !storedConflict.choices[field]).length;
      setMessage(remaining
        ? `已恢复冲突选择，还有 ${remaining} 个字段需要确认。`
        : "已恢复全部冲突选择，请核对最终快照后保存。");
      return;
    }
    if (latestItem.revision === current.expectedRevision && sameSnapshot(latest, current.base)) {
      const resumed = { ...current, requestState: "draft" as const, conflictRecovery: null, updatedAt: Date.now() };
      persistSession(resumed);
      setDesired(current.desired);
      setLatestSnapshot(null);
      setMineSnapshot(null);
      setConflictFields([]);
      setConflictChoices({});
      setStatus("ready");
      setSafeRetry(uncertain);
      setMessage(uncertain ? "服务端仍是提交前版本，可以安全重试同一快照。" : "已恢复当前标签页的草稿。");
      if (uncertain) setStatus("failed_retryable");
      return;
    }

    let rebased = threeWayRebase(current.base, current.desired, latest);
    if (storedConflict) {
      let rebasedDesired = rebased.desired;
      const conflicts = [...rebased.conflicts];
      for (const field of storedConflict.conflictFields) {
        const choice = storedConflict.choices[field];
        if (choice === "latest") {
          rebasedDesired = setSnapshotField(rebasedDesired, field, latest);
          const index = conflicts.indexOf(field);
          if (index >= 0) conflicts.splice(index, 1);
        } else if (!choice) {
          if (sameSnapshotField(latest, storedConflict.mine, field)) {
            rebasedDesired = setSnapshotField(rebasedDesired, field, latest);
            const index = conflicts.indexOf(field);
            if (index >= 0) conflicts.splice(index, 1);
          } else {
            rebasedDesired = setSnapshotField(rebasedDesired, field, storedConflict.mine);
            if (!conflicts.includes(field)) conflicts.push(field);
          }
        }
      }
      rebased = { desired: normalizeSnapshot(rebasedDesired), conflicts };
    }
    if (hadUserIntent && rebased.conflicts.length === 0 && sameSnapshot(rebased.desired, latest)) {
      finishSuccess(latestItem);
      return;
    }
    const conflictRecovery = rebased.conflicts.length ? {
      latest,
      mine: rebased.desired,
      conflictFields: rebased.conflicts,
      choices: {},
      latestRevision: latestItem.revision,
    } : null;
    const nextSession: EditMergeSession = {
      ...current,
      base: latest,
      desired: rebased.desired,
      expectedRevision: latestItem.revision,
      requestState: "draft",
      conflictRecovery,
      updatedAt: Date.now(),
    };
    persistSession(nextSession);
    setDesired(rebased.desired);
    if (current.incoming) {
      setInspirationStrategy(initialInspirationStrategy(latest, current.incoming, rebased.desired));
    }
    setLatestSnapshot(conflictRecovery?.latest ?? null);
    setMineSnapshot(conflictRecovery?.mine ?? null);
    setConflictFields(conflictRecovery?.conflictFields ?? []);
    setConflictChoices(conflictRecovery?.choices ?? {});
    setSafeRetry(false);
    if (rebased.conflicts.length) {
      setStatus("conflict_review");
      setMessage("收藏已在其他位置更新。只需重新选择真正冲突的字段。");
    } else {
      setStatus("ready");
      setMessage("已吸收服务端的非冲突更新，你的修改仍已保留。");
    }
  }, [finishSuccess, persistSession]);

  useEffect(() => {
    if (mode === "merge" && !initialSession) return undefined;
    const currentGeneration = ++generation.current;
    const controller = new AbortController();
    const preserveUnknownRecovery = (nextMessage: string): boolean => {
      const current = sessionRef.current;
      if (!current || current.requestState === "draft") return false;
      const unknownSession = {
        ...current,
        requestState: "outcome_unknown" as const,
        updatedAt: Date.now(),
      };
      persistSession(unknownSession);
      setDesired(current.desired);
      setSafeRetry(false);
      setStatus("outcome_unknown");
      setMessage(nextMessage);
      return true;
    };
    setStatus("item_loading");
    setMessage("正在读取最新收藏，再校验当前标签页草稿。");
    collectionApi.getItem(itemId, controller.signal)
      .then((latestItem) => {
        if (controller.signal.aborted || currentGeneration !== generation.current) return;
        if (!isMatchingAuthoritativeItem(latestItem, itemId)) {
          if (preserveUnknownRecovery("服务端响应无法绑定到待核验素材。仍不会重发，请再次核验权威结果。")) return;
          setStatus("item_read_failed");
          setMessage("服务端返回的素材身份或 revision 不匹配，未采用这份内容。");
          return;
        }
        const latest = snapshotFromItem(latestItem);
        const stored = readEditMergeSession(mode, itemId, routeEntryId);
        if (mode === "merge" && (!stored || !stored.incoming)) {
          clearEditMergeSession(mode, itemId);
          setStatus("merge_session_missing");
          setMessage("合并草稿已失效，未根据 URL 推测或重建候选内容。");
          return;
        }
        if (!stored) {
          setItem(latestItem);
          authoritativeItemCallback.current(latestItem);
          const created: EditMergeSession = {
            version: 1,
            mode,
            itemId,
            routeEntryId,
            returnKind: "detail",
            reviewEntryId: null,
            base: latest,
            desired: latest,
            incoming: null,
            expectedRevision: latestItem.revision,
            requestState: "draft",
            conflictRecovery: null,
            updatedAt: Date.now(),
            userCoverClaimToken: null,
          };
          const persisted = persistSession(created);
          setDesired(latest);
          setStatus("ready");
          if (!persisted) return;
          setMessage("已读取最新收藏。");
          return;
        }
        if (!stored.base || !stored.desired) {
          setItem(latestItem);
          authoritativeItemCallback.current(latestItem);
          const incoming = stored.incoming;
          if (!incoming) {
            setStatus("merge_session_missing");
            setMessage("合并草稿不完整，未发生任何写入。");
            return;
          }
          const merged = defaultMergeSnapshot(latest, incoming);
          const initialized: EditMergeSession = {
            ...stored,
            base: latest,
            desired: merged,
            expectedRevision: latestItem.revision,
            requestState: "draft",
            updatedAt: Date.now(),
          };
          const persisted = persistSession(initialized);
          setDesired(merged);
          setInspirationStrategy(initialInspirationStrategy(latest, incoming, merged));
          setStatus("ready");
          if (!persisted) return;
          setMessage(differingFields(latest, incoming).length
            ? "默认保留已有标量和灵感，标签已按规范身份去重合并。"
            : "两个版本的可编辑内容相同，无需写入。");
          return;
        }
        setDesired(stored.desired);
        if (stored.incoming) setInspirationStrategy(initialInspirationStrategy(stored.base, stored.incoming, stored.desired));
        applyLatest(latestItem, stored, stored.requestState !== "draft");
      })
      .catch((error) => {
        if (controller.signal.aborted || currentGeneration !== generation.current) return;
        if (error instanceof CollectionApiError && (error.status === 404 || error.code === "NOT_FOUND")) {
          setStatus("item_missing");
          setMessage("这条素材已不存在，不会将草稿重建为新收藏。");
        } else {
          if (preserveUnknownRecovery("首次核验暂时失败，实际提交快照仍已冻结。不会盲目重发，请再次核验。")) return;
          setStatus("item_read_failed");
          setMessage(error instanceof Error ? error.message : "收藏暂时无法读取，草稿仍在当前标签页中。");
        }
      });
    return () => controller.abort();
  }, [applyLatest, initialSession, itemId, loadAttempt, mode, persistSession, routeEntryId]);

  const normalizedDesired = useMemo(() => desired ? normalizeSnapshot(desired) : null, [desired]);
  const validation = useMemo(() => normalizedDesired ? validateSnapshot(normalizedDesired) : { valid: false, errors: {} }, [normalizedDesired]);
  const hasPendingTagDraft = Boolean(
    normalizeEditableTag(organizationTagDraft) || normalizeEditableTag(personalTagDraft),
  );
  const dirty = hasPendingTagDraft
    || Boolean(session?.base && normalizedDesired && !sameSnapshot(session.base, normalizedDesired))
    || Boolean(status === "item_missing" && mode === "merge" && session?.incoming);
  const unresolvedConflicts = conflictFields.filter((field) => !conflictChoices[field]);
  const busy = status === "saving" || status === "reconciling" || status === "conflict_loading_latest" || status === "success";
  const controlsLocked = busy || userCoverBusy || status === "outcome_unknown";
  const noChanges = Boolean(session?.base && normalizedDesired && sameSnapshot(session.base, normalizedDesired));
  const canSubmit = Boolean(
    normalizedDesired
    && session?.base
    && session.expectedRevision
    && validation.valid
    && !noChanges
    && !hasPendingTagDraft
    && !busy
    && !userCoverBusy
    && !storageFailed
    && (status === "ready" || (status === "conflict_review" && unresolvedConflicts.length === 0)),
  );

  const updateDesired = useCallback((
    next: CollectionUserSnapshot,
    changedField?: EditableField,
    selectedConflict?: { field: EditableField; choice: "latest" | "mine" },
  ) => {
    const current = sessionRef.current;
    if (!current || busy || status === "outcome_unknown") return;
    const normalized = normalizeSnapshot(next);
    const recoveryGated = status === "conflict_reload_failed";
    let conflictRecovery = current.conflictRecovery;
    if (status === "conflict_review" && conflictRecovery) {
      if (selectedConflict) {
        conflictRecovery = {
          ...conflictRecovery,
          choices: { ...conflictRecovery.choices, [selectedConflict.field]: selectedConflict.choice },
        };
      } else if (changedField && conflictRecovery.conflictFields.includes(changedField)) {
        const choices = { ...conflictRecovery.choices };
        delete choices[changedField];
        conflictRecovery = {
          ...conflictRecovery,
          mine: setSnapshotField(conflictRecovery.mine, changedField, normalized),
          choices,
        };
      }
    }
    const updated = {
      ...current,
      desired: normalized,
      requestState: "draft" as const,
      conflictRecovery,
      updatedAt: Date.now(),
    };
    const persisted = persistSession(updated);
    setDesired(next);
    if (conflictRecovery) {
      setMineSnapshot(conflictRecovery.mine);
      setConflictChoices(conflictRecovery.choices);
    }
    if (!persisted) return;
    setSafeRetry(false);
    if (status !== "conflict_review" && !recoveryGated) {
      setStatus("ready");
      setMessage("");
    }
  }, [busy, persistSession, status]);

  const updateTextField = (field: "user_title" | "user_author" | "primary_category" | "secondary_category", value: string) => {
    if (!desired) return;
    const next = structuredClone(desired);
    if (field === "user_title") next.user_title = value;
    else if (field === "user_author") next.user_author = value;
    else next.organization_confirmation[field] = value;
    updateDesired(next, field);
  };

  const updateInspiration = (content: string) => {
    if (!desired) return;
    const next = structuredClone(desired);
    const baseInspiration = sessionRef.current?.base?.inspiration ?? null;
    if (!content) next.inspiration = null;
    else if (baseInspiration && content === baseInspiration.content) next.inspiration = { ...baseInspiration };
    else next.inspiration = { content, input_mode: "text", transcription_status: "not_applicable" };
    updateDesired(next, "inspiration");
  };

  const updateUserCover = (selection: UserCoverSelection) => {
    if (!desired) return;
    const next = structuredClone(desired);
    next.user_cover_asset_id = selection.assetId;
    updateDesired(next, "user_cover_asset_id");
    const current = sessionRef.current;
    if (current) persistSession({
      ...current,
      userCoverClaimToken: selection.claimToken,
      updatedAt: Date.now(),
    });
  };

  const chooseMergeScalar = (field: "user_title" | "user_author" | "user_cover_asset_id" | "primary_category" | "secondary_category", source: "base" | "incoming") => {
    const current = sessionRef.current;
    if (!desired || !current?.base || !current.incoming) return;
    updateDesired(setSnapshotField(desired, field, source === "base" ? current.base : current.incoming), field);
  };

  const chooseInspirationStrategy = (strategy: InspirationStrategy) => {
    const current = sessionRef.current;
    if (!desired || !current?.base || !current.incoming) return;
    const next = structuredClone(desired);
    next.inspiration = strategy === "keep"
      ? current.base.inspiration
      : strategy === "replace"
        ? current.incoming.inspiration
        : appendInspiration(current.base.inspiration, current.incoming.inspiration);
    setInspirationStrategy(strategy);
    updateDesired(next, "inspiration");
  };

  const chooseConflict = (field: EditableField, choice: "latest" | "mine") => {
    const recovery = sessionRef.current?.conflictRecovery;
    if (!desired || !recovery) return;
    const source = choice === "latest" ? recovery.latest : recovery.mine;
    const next = setSnapshotField(desired, field, source);
    updateDesired(next, undefined, { field, choice });
    const current = sessionRef.current;
    if (field === "inspiration" && current?.base && current.incoming) {
      setInspirationStrategy(initialInspirationStrategy(current.base, current.incoming, next));
    }
  };

  const reconcile = useCallback(async (current: EditMergeSession, fromConflict: boolean) => {
    const currentGeneration = ++generation.current;
    setStatus(fromConflict ? "conflict_loading_latest" : "reconciling");
    setMessage(fromConflict ? "正在读取最新版本并重新比较。" : "正在核验服务端的权威结果。");
    try {
      const latest = await collectionApi.getItem(itemId);
      if (!mounted.current || currentGeneration !== generation.current) return;
      if (!isMatchingAuthoritativeItem(latest, itemId)) {
        throw new CollectionApiError("服务端返回的素材身份或 revision 不匹配。", "INVALID_RESPONSE", 200);
      }
      applyLatest(latest, current, !fromConflict);
    } catch (error) {
      if (!mounted.current || currentGeneration !== generation.current) return;
      if (error instanceof CollectionApiError && (error.status === 404 || error.code === "NOT_FOUND")) {
        setStatus("item_missing");
        setMessage("这条素材已不存在，未执行重试或新建。");
      } else {
        setStatus(fromConflict ? "conflict_reload_failed" : "outcome_unknown");
        setMessage(fromConflict
          ? "最新版本暂时没有读出来。你的草稿已保留，可以再次重新比较。"
          : "暂时无法判断上次提交是否成功。不会盲目重发，请再次核验。");
      }
    }
  }, [applyLatest, itemId]);

  const submit = useCallback(async (retry = false) => {
    const current = sessionRef.current;
    const retryAllowed = retry && status === "failed_retryable" && validation.valid && !noChanges && !hasPendingTagDraft && !busy && !userCoverBusy && !storageFailed;
    if (!current?.base || !current.desired || !current.expectedRevision || (!canSubmit && !retryAllowed)) return;
    const finalSnapshot = normalizeSnapshot(current.desired);
    const finalValidation = validateSnapshot(finalSnapshot);
    if (!finalValidation.valid) {
      setMessage("请先修正标出的字段。");
      window.requestAnimationFrame(() => errorSummaryRef.current?.focus());
      return;
    }
    const savingSession: EditMergeSession = {
      ...current,
      desired: finalSnapshot,
      requestState: "saving",
      conflictRecovery: null,
      updatedAt: Date.now(),
    };
    const attemptGeneration = ++generation.current;
    if (!persistSession(savingSession)) return;
    setDesired(finalSnapshot);
    setStatus("saving");
    setMessage(mode === "merge" ? "正在原子写入最终合并快照。" : "正在原子保存完整用户内容。");
    const payload: CollectionItemUpdate = {
      expected_revision: current.expectedRevision,
      ...finalSnapshot,
    };
    try {
      const updated = await collectionApi.updateItem(
        itemId,
        payload,
        undefined,
        current.userCoverClaimToken ?? undefined,
      );
      if (!mounted.current || attemptGeneration !== generation.current) return;
      if (!isMatchingAuthoritativeItem(updated, itemId)
        || updated.revision <= current.expectedRevision
        || !sameSnapshot(snapshotFromItem(updated), finalSnapshot)) {
        const unknownSession = { ...savingSession, requestState: "outcome_unknown" as const, updatedAt: Date.now() };
        persistSession(unknownSession);
        setStatus("outcome_unknown");
        setMessage("提交响应无法绑定到当前素材，正在读取权威收藏对账。");
        void reconcile(unknownSession, false);
        return;
      }
      finishSuccess(updated);
    } catch (error) {
      if (!mounted.current || attemptGeneration !== generation.current) return;
      if (error instanceof CollectionApiError && error.code === "COLLECTION_REVISION_CONFLICT") {
        const minimumRevision = Math.max(
          current.expectedRevision + 1,
          Number.isInteger(error.currentRevision) ? Number(error.currentRevision) : 0,
        );
        const draftSession = {
          ...savingSession,
          expectedRevision: minimumRevision,
          requestState: "draft" as const,
          updatedAt: Date.now(),
        };
        persistSession(draftSession);
        void reconcile(draftSession, true);
      } else if (error instanceof CollectionApiError && (error.status === 404 || error.code === "NOT_FOUND")) {
        setStatus("item_missing");
        setMessage("这条素材已不存在，未执行任何后续写入。");
      } else if (isDefinitiveClientFailure(error)) {
        const draftSession = { ...savingSession, requestState: "draft" as const, updatedAt: Date.now() };
        persistSession(draftSession);
        setStatus("failed_retryable");
        setSafeRetry(false);
        setMessage(error instanceof Error ? error.message : "内容未通过服务端校验，草稿已保留。");
      } else {
        const unknownSession = { ...savingSession, requestState: "outcome_unknown" as const, updatedAt: Date.now() };
        persistSession(unknownSession);
        setStatus("outcome_unknown");
        setMessage("未收到可判断的提交结果，正在先读取权威收藏对账。");
        void reconcile(unknownSession, false);
      }
    }
  }, [busy, canSubmit, finishSuccess, hasPendingTagDraft, itemId, mode, noChanges, persistSession, reconcile, status, storageFailed, userCoverBusy, validation.valid]);

  const requestLeave = useCallback((): boolean => {
    leaveOrigin.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (dirty || status === "outcome_unknown" || status === "reconciling" || status === "saving") {
      setLeaveOpen(true);
      return true;
    }
    const returnKind = sessionRef.current?.returnKind ?? "detail";
    clearEditMergeSession(mode, itemId);
    onExit(returnKind);
    return false;
  }, [dirty, itemId, mode, onExit, status]);

  useEffect(() => {
    registerLeaveGuard(() => {
      if (dirty || status === "outcome_unknown" || status === "reconciling" || status === "saving") {
        leaveOrigin.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        setLeaveOpen(true);
        return true;
      }
      clearEditMergeSession(mode, itemId);
      return false;
    });
    return () => registerLeaveGuard(null);
  }, [dirty, itemId, mode, registerLeaveGuard, status]);

  useEffect(() => {
    if (!dirty && status !== "outcome_unknown" && status !== "reconciling" && status !== "saving") return undefined;
    const protect = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [dirty, status]);

  useEffect(() => {
    if (!desktop) return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = previousOverflow; };
  }, [desktop]);

  useEffect(() => {
    const surface = surfaceRef.current;
    const actions = actionsRef.current;
    if (!surface || desktop || !actions) {
      surface?.style.removeProperty("--edit-merge-footer-height");
      return undefined;
    }
    const measure = () => {
      const height = Math.ceil(actions.getBoundingClientRect().height);
      if (height > 0) surface.style.setProperty("--edit-merge-footer-height", `${height}px`);
    };
    measure();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(actions);
    return () => observer.disconnect();
  }, [desktop, status]);

  useEffect(() => {
    titleRef.current?.focus({ preventScroll: true });
  }, []);

  useLayoutEffect(() => {
    if (status === "success") successStatusRef.current?.focus({ preventScroll: true });
  }, [status]);

  useEffect(() => {
    if (!desktop || leaveOpen) return undefined;
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        requestLeave();
        return;
      }
      if (event.key !== "Tab" || !surfaceRef.current) return;
      const focusable = [...surfaceRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )].filter((node) => !node.hidden && node.getAttribute("aria-hidden") !== "true");
      if (!focusable.length) {
        event.preventDefault();
        (successStatusRef.current ?? titleRef.current)?.focus({ preventScroll: true });
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (!(active instanceof HTMLElement) || !surfaceRef.current.contains(active) || !focusable.includes(active)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && active === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && active === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [desktop, leaveOpen, requestLeave]);

  useEffect(() => {
    const viewport = window.visualViewport;
    const node = surfaceRef.current;
    if (!viewport || !node) return undefined;
    const update = () => {
      const offset = Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop);
      node.style.setProperty("--edit-merge-keyboard-offset", `${offset}px`);
    };
    update();
    viewport.addEventListener("resize", update);
    viewport.addEventListener("scroll", update);
    return () => {
      viewport.removeEventListener("resize", update);
      viewport.removeEventListener("scroll", update);
    };
  }, []);

  const discardAndExit = () => {
    const returnKind = sessionRef.current?.returnKind ?? "detail";
    if (status === "saving" || status === "outcome_unknown" || status === "reconciling") {
      onUnknownDiscard();
    }
    generation.current += 1;
    if (mode === "edit" && sessionRef.current?.userCoverClaimToken) {
      void discardUserCover({
        assetId: sessionRef.current.desired?.user_cover_asset_id ?? null,
        claimToken: sessionRef.current.userCoverClaimToken,
      });
    }
    clearEditMergeSession(mode, itemId);
    setLeaveOpen(false);
    if (status === "item_missing") onItemMissingExit();
    else onExit(returnKind);
  };

  const requestItemMissingExit = () => {
    leaveOrigin.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (dirty) {
      setLeaveOpen(true);
      return;
    }
    generation.current += 1;
    clearEditMergeSession(mode, itemId);
    onItemMissingExit();
  };

  const retryRead = () => {
    const current = sessionRef.current;
    if (!current || status === "item_read_failed") {
      setStatus("item_loading");
      setLoadAttempt((attempt) => attempt + 1);
      return;
    }
    void reconcile(current, status === "conflict_reload_failed");
  };

  const base = session?.base ?? null;
  const incoming = session?.incoming ?? null;
  const mergeFields = base && incoming ? differingFields(base, incoming) : [];
  const statusIsError = ["item_read_failed", "failed_retryable", "outcome_unknown", "conflict_reload_failed", "item_missing", "merge_session_missing"].includes(status);
  const primaryActionLabel = mode === "merge" ? "确认合并" : "保存修改";
  const closeLabel = desktop
    ? mode === "merge" ? "关闭合并" : "关闭编辑"
    : session?.returnKind === "review" || mode === "merge" ? "返回保存前整理" : "返回收藏详情";

  return (
    <div className={`edit-merge-overlay ${desktop ? "is-desktop" : "is-mobile"}`}>
      <div
        ref={surfaceRef}
        className="edit-merge-surface"
        role={desktop ? "dialog" : undefined}
        aria-modal={desktop ? "true" : undefined}
        aria-labelledby="edit-merge-title"
      >
        <header className="edit-merge-header">
          <div>
            <h1 id="edit-merge-title" ref={titleRef} tabIndex={-1}>{mode === "merge" ? "合并重复收藏" : "编辑收藏"}</h1>
            <p>{mode === "merge" ? "检查另一个版本，只在确认后更新原收藏。" : "修改你的标题、整理结果和个人灵感。"}</p>
          </div>
          <IconButton disabled={status === "success"} variant="ghost" color="gray" aria-label={closeLabel} onClick={requestLeave}>
            {desktop ? <X size={22} aria-hidden /> : <ArrowLeft size={22} aria-hidden />}
          </IconButton>
        </header>

        <div className="edit-merge-scroll">
          {status === "item_loading" && (
            <div className="edit-merge-loading" role="status" aria-live="polite">
              <CircleNotch className="spin" size={25} aria-hidden />
              <h2>正在读取最新收藏</h2>
              <p>先校验素材身份、revision 和当前标签页草稿。</p>
            </div>
          )}

          {status !== "item_loading" && item && (
            <section className="immutable-identity" aria-labelledby="immutable-identity-title">
              <ImmutableIdentityCover item={item} />
              <div>
                <h2 id="immutable-identity-title">正在修改这条收藏</h2>
                <p><span>来源平台</span>{platformNames[item.platform]}</p>
                <p><span>规范标题</span>{item.metadata.title.value || "来源未提供标题"}</p>
                <p><span>来源账号</span>{item.metadata.author.value || "作者未提供"}</p>
                <p>已有收藏的来源话题保持不变。</p>
              </div>
              <LockSimple size={20} aria-label="来源身份只读" />
            </section>
          )}

          {status !== "item_loading" && message && (
            <div ref={successStatusRef} className={`edit-merge-status ${statusIsError ? "is-error" : ""}`} role={statusIsError ? "alert" : "status"} aria-live={statusIsError ? "assertive" : "polite"} tabIndex={status === "success" ? -1 : undefined}>
              {status === "success" ? <CheckCircle size={20} aria-hidden /> : statusIsError ? <WarningCircle size={20} aria-hidden /> : <ArrowsClockwise size={20} aria-hidden />}
              <span>{message}</span>
            </div>
          )}

          {storageFailed && (
            <div className="edit-merge-error-summary" role="alert">
              <strong>恢复草稿未能写入当前标签页</strong>
              <p>为避免刷新或断网后丢失内容，已暂停提交。可在保留本页的前提下继续尝试。</p>
            </div>
          )}

          {(status === "item_read_failed" || status === "conflict_reload_failed" || status === "outcome_unknown") && (
            <div className="edit-merge-recovery">
              <Button variant="outline" color="gray" onClick={retryRead}>
                <ArrowsClockwise size={18} aria-hidden />{status === "outcome_unknown" ? "再次核验" : status === "conflict_reload_failed" ? "重新比较" : "重试读取"}
              </Button>
            </div>
          )}

          {(status === "merge_session_missing" || status === "item_missing") && (
            <div className="edit-merge-terminal">
              <h2>{status === "merge_session_missing" ? "合并会话已失效" : "素材不存在"}</h2>
              <p>{status === "merge_session_missing" ? "当前地址不包含个人草稿，系统也不会根据链接推测要合并的内容。" : "草稿仍不会被发送或改存为新收藏。"}</p>
              <Button onClick={() => {
                if (status === "item_missing") requestItemMissingExit();
                else {
                  clearEditMergeSession(mode, itemId);
                  onExit("detail");
                }
              }}>{status === "item_missing" ? "返回素材库" : "进入收藏详情"}</Button>
            </div>
          )}

          {desired && base && !["item_loading", "item_read_failed", "merge_session_missing", "item_missing"].includes(status) && (
            <>
              {status === "conflict_review" && latestSnapshot && mineSnapshot && (
                <section className="conflict-recovery-panel" aria-labelledby="conflict-title">
                  <h2 id="conflict-title">{unresolvedConflicts.length ? "重新选择冲突字段" : "冲突字段已确认"}</h2>
                  <p>其他位置的非冲突更新已自动保留。</p>
                  {conflictFields.map((field) => (
                    <fieldset key={field}>
                      <legend>{editableFieldLabels[field]}</legend>
                      <label><input type="radio" name={`conflict-${field}`} checked={conflictChoices[field] === "latest"} onChange={() => chooseConflict(field, "latest")} />
                        <span><strong>使用最新内容</strong><small>{snapshotValueText(latestSnapshot, field)}</small></span>
                      </label>
                      <label><input type="radio" name={`conflict-${field}`} checked={conflictChoices[field] === "mine"} onChange={() => chooseConflict(field, "mine")} />
                        <span><strong>保留我的内容</strong><small>{snapshotValueText(mineSnapshot, field)}</small></span>
                      </label>
                    </fieldset>
                  ))}
                </section>
              )}

              <form className="edit-merge-form" onSubmit={(event) => { event.preventDefault(); void submit(); }} noValidate>
                {mode === "edit" ? (
                  <>
                    <section className="edit-merge-field-group">
                      <label htmlFor="edit-user-title">自定义标题</label>
                      <input id="edit-user-title" value={desired.user_title ?? ""} disabled={controlsLocked} aria-invalid={Boolean(validation.errors.user_title)} onChange={(event) => updateTextField("user_title", event.target.value)} aria-describedby={validation.errors.user_title ? "edit-user-title-help edit-user-title-error" : "edit-user-title-help"} />
                      <p id="edit-user-title-help" className="field-help">留空时继续显示来源标题，来源事实不会被改写。</p>
                      {validation.errors.user_title && <p id="edit-user-title-error" className="inline-error" role="alert">{validation.errors.user_title}</p>}
                    </section>
                    <section className="edit-merge-field-group user-author-field">
                      <label htmlFor="edit-user-author">我补充的作者 <span>用户补充</span></label>
                      <input id="edit-user-author" value={desired.user_author ?? ""} disabled={controlsLocked} aria-invalid={Boolean(validation.errors.user_author)} onChange={(event) => updateTextField("user_author", event.target.value)} aria-describedby={validation.errors.user_author ? "edit-user-author-help edit-user-author-error" : "edit-user-author-help"} />
                      <p id="edit-user-author-help" className="field-help">留空或清除后继续显示来源作者。</p>
                      {validation.errors.user_author && <p id="edit-user-author-error" className="inline-error" role="alert">{validation.errors.user_author}</p>}
                    </section>
                    <CollectionUserCoverInput
                      idPrefix="edit-user-cover"
                      value={{
                        assetId: desired.user_cover_asset_id ?? null,
                        claimToken: session?.userCoverClaimToken ?? null,
                      }}
                      fallbackSrc={desired.user_cover_asset_id && desired.user_cover_asset_id === item?.user_cover_asset_id
                        ? userItemCoverUrl(item.id)
                        : item?.metadata.cover_url.value
                          ? sourceItemCoverUrl(item.id)
                          : "/assets/material-cover-fallback.webp"}
                      fallbackKind={desired.user_cover_asset_id && desired.user_cover_asset_id === item?.user_cover_asset_id
                        ? "user" : item?.metadata.cover_url.value ? "source" : "placeholder"}
                      disabled={controlsLocked}
                      onChange={updateUserCover}
                      onBusyChange={setUserCoverBusy}
                    />
                    <section className="edit-merge-field-group category-fields">
                      <label htmlFor="edit-primary-category">一级分类<input id="edit-primary-category" value={desired.organization_confirmation.primary_category} disabled={controlsLocked} aria-invalid={Boolean(validation.errors.primary_category)} aria-describedby={validation.errors.primary_category ? "edit-primary-category-error" : undefined} onChange={(event) => updateTextField("primary_category", event.target.value)} />
                        {validation.errors.primary_category && <span id="edit-primary-category-error" className="inline-error" role="alert">{validation.errors.primary_category}</span>}
                      </label>
                      <label htmlFor="edit-secondary-category">二级分类<input id="edit-secondary-category" value={desired.organization_confirmation.secondary_category} disabled={controlsLocked} aria-invalid={Boolean(validation.errors.secondary_category)} aria-describedby={validation.errors.secondary_category ? "edit-secondary-category-error" : undefined} onChange={(event) => updateTextField("secondary_category", event.target.value)} />
                        {validation.errors.secondary_category && <span id="edit-secondary-category-error" className="inline-error" role="alert">{validation.errors.secondary_category}</span>}
                      </label>
                    </section>
                  </>
                ) : incoming ? (
                  <>
                    {(["user_title", "user_author", "primary_category", "secondary_category"] as const).filter((field) => mergeFields.includes(field)).map((field) => (
                      <fieldset className="scalar-choice" key={field}>
                        <legend>{editableFieldLabels[field]} <span>不同</span></legend>
                        <div className="scalar-choice-options">
                          <label><input type="radio" name={`merge-${field}`} checked={sameSnapshotField(desired, base, field)} onChange={() => chooseMergeScalar(field, "base")} disabled={controlsLocked} /><span><small>现有内容</small>{snapshotValueText(base, field)}</span></label>
                          <label><input type="radio" name={`merge-${field}`} checked={sameSnapshotField(desired, incoming, field)} onChange={() => chooseMergeScalar(field, "incoming")} disabled={controlsLocked} /><span><small>使用此版本</small>{snapshotValueText(incoming, field)}</span></label>
                        </div>
                      </fieldset>
                    ))}
                    {mergeFields.includes("user_cover_asset_id") && (
                      <fieldset className="scalar-choice cover-choice">
                        <legend>个人封面 <span>不同</span></legend>
                        <div className="scalar-choice-options">
                          <label>
                            <input type="radio" name="merge-user_cover_asset_id" checked={sameSnapshotField(desired, base, "user_cover_asset_id")} onChange={() => chooseMergeScalar("user_cover_asset_id", "base")} disabled={controlsLocked} />
                              <span><small>现有内容</small><span className="cover-choice-preview"><UserCoverPreview assetId={base.user_cover_asset_id ?? null} claimToken={null} fallbackSrc={base.user_cover_asset_id ? userItemCoverUrl(item!.id) : item?.metadata.cover_url.value ? sourceItemCoverUrl(item.id) : "/assets/material-cover-fallback.webp"} alt="现有封面预览" /></span>{base.user_cover_asset_id ? "用户补充封面" : item?.metadata.cover_url.value ? "来源封面" : "占位图"}</span>
                          </label>
                          <label>
                            <input type="radio" name="merge-user_cover_asset_id" checked={sameSnapshotField(desired, incoming, "user_cover_asset_id")} onChange={() => chooseMergeScalar("user_cover_asset_id", "incoming")} disabled={controlsLocked} />
                            <span><small>使用此版本</small><span className="cover-choice-preview"><UserCoverPreview assetId={incoming.user_cover_asset_id ?? null} claimToken={session?.userCoverClaimToken ?? null} fallbackSrc={incoming.user_cover_asset_id ? "" : item?.metadata.cover_url.value ? sourceItemCoverUrl(item.id) : "/assets/material-cover-fallback.webp"} alt="此版本封面预览" /></span>{incoming.user_cover_asset_id ? "用户补充封面" : item?.metadata.cover_url.value ? "来源封面" : "占位图"}</span>
                          </label>
                        </div>
                      </fieldset>
                    )}
                  </>
                ) : null}

                {(mode === "edit" || mergeFields.includes("organization_tags")) && (
                  <section className="edit-merge-field-group">
                    {mode === "merge" && <p className="merge-field-context"><strong>整理标签</strong><span>默认去重合并，可继续增删</span></p>}
                    <TagEditor idPrefix="edit-organization-tags" label="整理标签" values={desired.organization_confirmation.organization_tags} setValues={(values) => updateDesired({ ...desired, organization_confirmation: { ...desired.organization_confirmation, organization_tags: values } }, "organization_tags")} disabled={controlsLocked} onDraftChange={setOrganizationTagDraft} />
                    {validation.errors.organization_tags && <p className="inline-error" role="alert">{validation.errors.organization_tags}</p>}
                  </section>
                )}

                {(mode === "edit" || mergeFields.includes("personal_tags")) && (
                  <section className="edit-merge-field-group">
                    {mode === "merge" && <p className="merge-field-context"><strong>个人标签</strong><span>默认去重合并，可继续增删</span></p>}
                    <TagEditor idPrefix="edit-personal-tags" label="个人标签" personal values={desired.personal_tags} setValues={(values) => updateDesired({ ...desired, personal_tags: values }, "personal_tags")} disabled={controlsLocked} onDraftChange={setPersonalTagDraft} />
                    {validation.errors.personal_tags && <p className="inline-error" role="alert">{validation.errors.personal_tags}</p>}
                  </section>
                )}

                {(mode === "edit" || mergeFields.includes("inspiration")) && (
                  <section className="edit-merge-field-group inspiration-editor">
                    <h2>我的灵感</h2>
                    {mode === "merge" && incoming ? (
                      <>
                        <div className="inspiration-compare">
                          <div><small>现有内容</small><p>{base.inspiration?.content || "留空"}</p></div>
                          <div><small>此版本内容</small><p>{incoming.inspiration?.content || "留空"}</p></div>
                        </div>
                        <fieldset className="inspiration-strategy">
                          <legend>合并方式</legend>
                          <label><input type="radio" name="inspiration-strategy" checked={inspirationStrategy === "keep"} onChange={() => chooseInspirationStrategy("keep")} disabled={controlsLocked} />保留现有内容</label>
                          <label><input type="radio" name="inspiration-strategy" checked={inspirationStrategy === "append"} onChange={() => chooseInspirationStrategy("append")} disabled={controlsLocked} />追加到现有内容</label>
                          <label><input type="radio" name="inspiration-strategy" checked={inspirationStrategy === "replace"} onChange={() => chooseInspirationStrategy("replace")} disabled={controlsLocked} />替换为此版本内容</label>
                        </fieldset>
                      </>
                    ) : (
                      <>
                        <label className="sr-only" htmlFor="edit-inspiration">我的灵感</label>
                        <textarea id="edit-inspiration" rows={7} value={desired.inspiration?.content ?? ""} disabled={controlsLocked} aria-invalid={Boolean(validation.errors.inspiration)} onChange={(event) => updateInspiration(event.target.value)} aria-describedby={validation.errors.inspiration ? "edit-inspiration-count edit-inspiration-error" : "edit-inspiration-count"} />
                        <p id="edit-inspiration-count" className="field-help">{[...(desired.inspiration?.content ?? "")].length} / 4000</p>
                      </>
                    )}
                    {validation.errors.inspiration && <p id="edit-inspiration-error" className="inline-error" role="alert">{validation.errors.inspiration}</p>}
                  </section>
                )}

                <section className="final-snapshot-preview" aria-labelledby="final-preview-title">
                  <h2 id="final-preview-title">最终结果预览</h2>
                  <dl>
                    <div><dt>标题</dt><dd>{normalizedDesired?.user_title || item?.metadata.title.value || "未命名收藏"}</dd></div>
                    <div><dt>作者</dt><dd>{normalizedDesired?.user_author || item?.metadata.author.value || "未获取到作者"}{normalizedDesired?.user_author && <small>用户补充</small>}</dd></div>
                    <div><dt>封面</dt><dd>{normalizedDesired?.user_cover_asset_id ? "用户补充封面" : item?.metadata.cover_url.value ? "来源封面" : "占位图"}</dd></div>
                    <div><dt>分类</dt><dd>{[normalizedDesired?.organization_confirmation.primary_category, normalizedDesired?.organization_confirmation.secondary_category].filter(Boolean).join(" / ") || "留空"}</dd></div>
                    <div><dt>整理标签</dt><dd>{normalizedDesired?.organization_confirmation.organization_tags.join("、") || "留空"}</dd></div>
                    <div><dt>个人标签</dt><dd>{normalizedDesired?.personal_tags.join("、") || "留空"}</dd></div>
                    <div><dt>我的灵感</dt><dd>{normalizedDesired?.inspiration?.content || "留空"}</dd></div>
                  </dl>
                </section>

                {!validation.valid && (
                  <div ref={errorSummaryRef} className="edit-merge-error-summary" tabIndex={-1}>
                    <strong>请先修正以下内容</strong>
                    <ul>{Object.values(validation.errors).map((error) => <li key={error}>{error}</li>)}</ul>
                  </div>
                )}
              </form>
            </>
          )}
        </div>

        {!(["merge_session_missing", "item_missing", "success"].includes(status)) && (
          <footer ref={actionsRef} className="edit-merge-actions">
            <p role="status">{hasPendingTagDraft ? "先添加或清空尚未提交标签。" : noChanges ? "当前没有需要保存的变更。" : unresolvedConflicts.length ? `还有 ${unresolvedConflicts.length} 个冲突字段待选择。` : busy ? "草稿已冻结，请等待当前操作完成。" : "只会写入上方最终用户内容。"}</p>
            <div>
              {(status === "failed_retryable") && <Button variant="outline" color="gray" disabled={busy || storageFailed || !validation.valid || noChanges || hasPendingTagDraft} onClick={() => void submit(true)}>{safeRetry ? "安全重试" : "重试保存"}</Button>}
              <Button variant="outline" color="gray" disabled={busy} onClick={requestLeave}>取消</Button>
              <Button disabled={!canSubmit} onClick={() => void submit()}>
                {busy && <CircleNotch className="spin" size={18} aria-hidden />}{status === "saving" ? "正在保存" : status === "reconciling" || status === "conflict_loading_latest" ? "正在核验" : primaryActionLabel}
              </Button>
            </div>
          </footer>
        )}
      </div>

      <Dialog.Root open={leaveOpen} onOpenChange={(open) => {
        if (!open) {
          setLeaveOpen(false);
          window.requestAnimationFrame(() => leaveOrigin.current?.focus({ preventScroll: true }));
        }
      }}>
        <Dialog.Content className="edit-merge-leave-dialog" maxWidth="440px" onEscapeKeyDown={(event) => {
          event.preventDefault();
          setLeaveOpen(false);
          window.requestAnimationFrame(() => leaveOrigin.current?.focus({ preventScroll: true }));
        }}>
          <Dialog.Title>放弃未保存的内容吗？</Dialog.Title>
          <Dialog.Description>{status === "outcome_unknown" || status === "reconciling" || status === "saving" ? "服务端可能已经收到提交。放弃只会停止本页对账，不会发送危险的撤销请求。" : "当前标签页草稿会被清除，服务端收藏不会发生变化。"}</Dialog.Description>
          <div className="dialog-actions">
            <Button autoFocus variant="outline" color="gray" onClick={() => {
              setLeaveOpen(false);
              window.requestAnimationFrame(() => leaveOrigin.current?.focus({ preventScroll: true }));
            }}>继续编辑</Button>
            <Button color="red" onClick={discardAndExit}>放弃并退出</Button>
          </div>
        </Dialog.Content>
      </Dialog.Root>
    </div>
  );
}
