import {
  ArrowLeft,
  ArrowSquareOut,
  BookOpenText,
  CaretDown,
  Check,
  CircleNotch,
  Clipboard,
  Funnel,
  FileAudio,
  Gear,
  Globe,
  Link as LinkIcon,
  ListPlus,
  MagnifyingGlass,
  Moon,
  Palette,
  SlidersHorizontal,
  Sparkle,
  ShieldCheck,
  Sun,
  Database,
  TelevisionSimple,
  TiktokLogo,
  UserCircle,
  X,
  YoutubeLogo,
} from "@phosphor-icons/react";
import {
  Button,
  Dialog,
  DropdownMenu,
  IconButton,
  Popover,
} from "@radix-ui/themes";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  CollectionApiError,
  cachedItemCoverUrl,
  collectionApi,
  itemCoverUrl,
  previewCoverUrl,
  type CollectionItem,
  type CollectionItemCreate,
  type CollectionListFacets,
  type CollectionListItem,
  type CollectionListQuery,
  type CollectionPlatform,
  type CollectionPreview,
} from "./collection-api";
import { CollectionEditMergeSurface } from "./collection-edit-merge";
import { useMobileInputViewport } from "./mobile-input-viewport";
import { isPhoneDevice } from "./phone-device";
import { VoiceCapture, type VoiceState } from "./voice-capture";
import {
  CollectionUserCoverInput,
  discardUserCover,
  forgetStoredUserCover,
  readStoredUserCover,
  type UserCoverSelection,
} from "./collection-user-cover";
import { CollectionImportPage, type CollectionImportRouteState } from "./collection-import-page";
import { ShareCapturePage } from "./share-capture-page";
import { displayedSourceTopics, reconcileSourceTopics, sourceTopicCandidates, withoutSourceTopicSuggestions } from "./collection-source-topics";
import { getShareSession } from "./share-bootstrap";
import {
  clearEditMergeSession,
  editableTagIdentity,
  normalizeEditableTag,
  normalizeSnapshot,
  snapshotFromCreate,
  snapshotFromItem,
  storedEditMergeEntryId,
  validateSnapshot,
  writeEditMergeSession,
  type EditMergeReturnKind,
  type EditMergeSession,
} from "./collection-edit-model";
import { TagEditor } from "./collection-tag-editor";
import { useThemePreferences } from "./theme";
import { pythonTrim } from "./unicode-normalize";

type Route = (
  | { name: "library" }
  | { name: "review" }
  | { name: "share" }
  | { name: "batch"; batchId?: string; view: "list" | "item" | "confirm"; itemId?: string; focus?: string; restoreScrollY?: number; restoreToken?: number; anchorItemId?: string; anchorOffset?: number; trigger?: "keyboard" | "pointer"; batchDepth?: number }
  | { name: "detail"; id: string }
  | { name: "edit-merge"; id: string; mode: "edit" | "merge" }
  | {
      name: "settings";
      from: "library" | "review" | "detail";
      returnToken?: number;
      materialId?: string;
      coreRevision?: string;
    }) & { entryId?: string };

interface MaterialCardData {
  id: string;
  title: string;
  author: string;
  authorIsSupplement: boolean;
  platform: CollectionPlatform;
  category: string;
  coverUrl: string;
  sourceUrl: string;
  sourceCopy: string;
  inspiration: string;
  organizationTags: string[];
  personalTags: string[];
  platformTags: string[];
  collection?: CollectionItem;
}

type TagSource = "platform" | "organization" | "personal";

interface LibraryQueryState {
  query: string;
  platform?: CollectionPlatform;
  primaryCategory: string;
  secondaryCategory: string;
  tag: string;
  tagSource?: TagSource;
}

const emptyFacets: CollectionListFacets = {
  platforms: [],
  categories: [],
  tags: [],
};

const platformNames: Record<CollectionPlatform, string> = {
  bilibili: "哔哩哔哩",
  douyin: "抖音",
  xiaohongshu: "小红书",
  youtube: "YouTube",
  web: "网页",
  other: "其他来源",
  local_upload: "本地上传",
};

function collectionCard(item: CollectionItem): MaterialCardData {
  return {
    id: item.id,
    title: item.display_title,
    author: item.user_author || item.metadata.author.value,
    authorIsSupplement: Boolean(item.user_author),
    platform: item.platform,
    category: item.organization_confirmation.primary_category || "暂未分类",
    coverUrl: item.user_cover_asset_id || item.metadata.cover_url.value ? itemCoverUrl(item.id) : "",
    sourceUrl: item.canonical_url || item.source_url,
    sourceCopy: item.metadata.source_copy.value,
    inspiration: item.inspiration?.content ?? "",
    organizationTags: item.organization_confirmation.organization_tags,
    personalTags: item.personal_tags,
    platformTags: displayedSourceTopics(item).map((tag) => tag.value),
    collection: item,
  };
}

function collectionListCard(item: CollectionListItem): MaterialCardData {
  return {
    id: item.id,
    title: item.display_title,
    author: item.user_author || item.source_author || "",
    authorIsSupplement: Boolean(item.user_author),
    platform: item.platform,
    category: item.primary_category || "暂未分类",
    coverUrl: item.has_user_cover || item.cover_url ? itemCoverUrl(item.id) : "",
    sourceUrl: "",
    sourceCopy: "",
    inspiration: "",
    organizationTags: [],
    personalTags: [],
    platformTags: [],
  };
}

function routePath(route: Route): string {
  if (route.name === "library") return "/";
  if (route.name === "review") return "/capture/review";
  if (route.name === "share") return "/capture/share";
  if (route.name === "batch") return route.batchId
    ? `/capture/batch/${encodeURIComponent(route.batchId)}`
    : "/capture/batch";
  if (route.name === "detail") return `/materials/${encodeURIComponent(route.id)}`;
  if (route.name === "edit-merge") return `/materials/${encodeURIComponent(route.id)}/${route.mode}`;
  return "/settings/privacy-storage";
}

// Retired entry points restore only the collection, never an analysis job/result.
function retiredAnalysisRoute(state: unknown): Route | null {
  const match = window.location.pathname.match(/^\/materials\/([^/]+)\/deep-analysis\/?$/);
  if (match) {
    try { return { name: "detail", id: decodeURIComponent(match[1]) }; }
    catch { return { name: "library" }; }
  }
  if (state && typeof state === "object" && "name" in state && state.name === "reader") {
    const id = "materialId" in state ? state.materialId : null;
    return typeof id === "string" && id.length > 0 && id.length <= 512
      ? { name: "detail", id }
      : { name: "library" };
  }
  return null;
}

function initialRoute(): Route {
  if (window.location.pathname === "/capture/share") {
    getShareSession();
    return { name: "share" };
  }
  const state = window.history.state?.collectionRoute as Route | undefined;
  const retired = retiredAnalysisRoute(window.history.state?.collectionRoute);
  if (retired) return retired;
  // Return credentials live only in this document. A reload must not revive them.
  if (window.location.pathname === "/settings/privacy-storage") return { name: "settings", from: "library" };
  const batch = window.location.pathname.match(/^\/capture\/batch(?:\/([^/]+))?\/?$/);
  if (batch) {
    try {
      const batchId = batch[1] ? decodeURIComponent(batch[1]) : undefined;
      const stored = state?.name === "batch" && routePath(state) === window.location.pathname ? state : null;
      const view = stored?.view === "item" || stored?.view === "confirm" ? stored.view : "list";
      return {
        name: "batch",
        batchId,
        view,
        itemId: (view === "item" || view === "confirm") && typeof stored?.itemId === "string" ? stored.itemId : undefined,
        focus: typeof stored?.focus === "string" ? stored.focus : batchId ? "summary" : undefined,
        restoreScrollY: typeof stored?.restoreScrollY === "number" && Number.isFinite(stored.restoreScrollY) && stored.restoreScrollY >= 0 ? stored.restoreScrollY : undefined,
        restoreToken: Number.isInteger(stored?.restoreToken) && (stored?.restoreToken ?? -1) >= 0 ? stored?.restoreToken : undefined,
        anchorItemId: typeof stored?.anchorItemId === "string" ? stored.anchorItemId : undefined,
        anchorOffset: typeof stored?.anchorOffset === "number" && Number.isFinite(stored.anchorOffset) ? stored.anchorOffset : undefined,
        trigger: stored?.trigger === "keyboard" || stored?.trigger === "pointer" ? stored.trigger : undefined,
        batchDepth: Number.isInteger(stored?.batchDepth) && (stored?.batchDepth ?? 0) >= 0 ? stored?.batchDepth : 0,
      };
    } catch { return { name: "batch", view: "list", batchDepth: 0 }; }
  }
  const editMerge = window.location.pathname.match(/^\/materials\/([^/]+)\/(edit|merge)\/?$/);
  if (editMerge) {
    try {
      const id = decodeURIComponent(editMerge[1]);
      const mode = editMerge[2] as "edit" | "merge";
      const storedEntryId = storedEditMergeEntryId(mode, id) ?? undefined;
      if (mode === "edit" && !storedEntryId) {
        return routeEntry({ name: "edit-merge", id, mode });
      }
      return { name: "edit-merge", id, mode, entryId: storedEntryId };
    } catch { return { name: "library" }; }
  }
  const detail = window.location.pathname.match(/^\/materials\/([^/]+)\/?$/);
  if (detail) {
    try { return { name: "detail", id: decodeURIComponent(detail[1]) }; } catch { return { name: "library" }; }
  }
  return { name: "library" };
}

function makeIdempotencyKey(): string {
  return globalThis.crypto?.randomUUID?.() ?? `collection-${Date.now()}-${Math.random()}`;
}

function routeEntry(route: Route): Route {
  return { ...route, entryId: makeIdempotencyKey() };
}

function sameRouteEntry(left: Route, right: Route): boolean {
  return Boolean(left.entryId && left.entryId === right.entryId && left.name === right.name && routePath(left) === routePath(right));
}

interface PageReturnContext {
  token: number;
  scrollY: number;
  focus: "preferences" | "title" | "merge" | "batch";
}

// Wait for the committed layout; user input always wins over deferred restoration.
function restorePagePosition(scrollY: number, focus: () => HTMLElement | null, consumed: () => void): () => void {
  let frame = 0;
  let finished = false;
  const stop = () => {
    window.cancelAnimationFrame(frame);
    for (const event of ["wheel", "touchstart", "pointerdown", "keydown"]) window.removeEventListener(event, interrupt);
  };
  const interrupt = () => {
    if (finished) return;
    finished = true;
    stop();
    consumed();
  };
  for (const event of ["wheel", "touchstart", "pointerdown", "keydown"]) window.addEventListener(event, interrupt, { once: true, passive: true });
  frame = window.requestAnimationFrame(() => {
    frame = window.requestAnimationFrame(() => {
      if (finished) return;
      finished = true;
      stop();
      const max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
      window.scrollTo({ top: Math.min(Math.max(0, scrollY), max), behavior: "auto" });
      focus()?.focus({ preventScroll: true });
      consumed();
    });
  });
  return () => { finished = true; stop(); };
}

function singleHttpLink(input: string): string | null {
  const matches = (input.match(/https?:\/\/[^\s<>{}\[\]"']+/gi) ?? [])
    .map((value) => value.replace(/[.,!?;:，。！？；：、)\]}）】》」』]+$/u, ""));
  if (matches.length !== 1) return null;
  try {
    const url = new URL(matches[0]);
    return url.protocol === "http:" || url.protocol === "https:" ? matches[0] : null;
  } catch {
    return null;
  }
}

function BrandWordmark() {
  return <span className="collection-wordmark" aria-label="瞬时录">瞬时录</span>;
}

function useMobileViewport(): boolean {
  const read = () => typeof window.matchMedia === "function"
    ? window.matchMedia("(max-width: 700px)").matches
    : false;
  const [mobile, setMobile] = useState(read);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return undefined;
    const query = window.matchMedia("(max-width: 700px)");
    const update = () => setMobile(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return mobile;
}

function moveRadioSelection<T extends string>(
  event: { key: string; preventDefault: () => void; currentTarget: HTMLButtonElement },
  values: readonly T[],
  currentIndex: number,
  apply: (value: T) => void,
) {
  let nextIndex = currentIndex;
  if (event.key === "ArrowRight" || event.key === "ArrowDown") nextIndex = (currentIndex + 1) % values.length;
  else if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextIndex = (currentIndex - 1 + values.length) % values.length;
  else if (event.key === "Home") nextIndex = 0;
  else if (event.key === "End") nextIndex = values.length - 1;
  else return;
  event.preventDefault();
  const radios = event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>("[role=\"radio\"]");
  radios?.[nextIndex]?.focus({ preventScroll: true });
  apply(values[nextIndex]);
}

function PreferencesMenu({ onSettings, detail = false, restoreSettingsTrigger = 0 }: { onSettings: (scrollY?: number) => void; detail?: boolean; restoreSettingsTrigger?: number }) {
  const { appearance, accent, mutation, setAppearance, setAccent } = useThemePreferences();
  const origin = useId();
  const mobile = useMobileViewport();
  const [open, setOpen] = useState(false);
  const error = mutation.origin === origin && mutation.phase === "failed"
    ? "外观偏好未能保存，已恢复上一次确认的组合。" : "";
  const dragStart = useRef<number | null>(null);
  const sourceScroll = useRef(0);
  const changeOpen = (next: boolean) => {
    if (next && !open) sourceScroll.current = window.scrollY;
    setOpen(next);
  };
  const restoredSettingsTrigger = useRef(0);
  useEffect(() => {
    if (!restoreSettingsTrigger || restoredSettingsTrigger.current === restoreSettingsTrigger) return;
    if (!open) { setOpen(true); return; }
    const frame = window.requestAnimationFrame(() => {
      const link = document.querySelector<HTMLElement>("[data-settings-link]");
      if (link) {
        link.focus({ preventScroll: true });
        restoredSettingsTrigger.current = restoreSettingsTrigger;
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [open, restoreSettingsTrigger]);
  const applyAppearance = (value: "light" | "dark") => {
    setAppearance(value, origin);
  };
  const applyAccent = (value: "indigo" | "teal" | "orange") => {
    setAccent(value, origin);
  };
  const trigger = detail ? (
    <button
      type="button"
      className="detail-preferences-trigger"
      aria-label="个人偏好"
      data-detail-focus="preferences"
      data-settings-focus="preferences"
    >
      <Gear size={23} aria-hidden /><span>偏好设置</span>
    </button>
  ) : (
    <IconButton
      className="collection-icon-button"
      variant="ghost"
      color="gray"
      aria-label="个人偏好"
      data-settings-focus="preferences"
    >
      <UserCircle size={25} aria-hidden />
    </IconButton>
  );
  if (!mobile) {
    return (
      <DropdownMenu.Root open={open} onOpenChange={changeOpen}>
        <DropdownMenu.Trigger>{trigger}</DropdownMenu.Trigger>
        <DropdownMenu.Content align="end" className="collection-preferences-menu">
          <DropdownMenu.Label>外观</DropdownMenu.Label>
          <DropdownMenu.RadioGroup value={appearance} onValueChange={(value) => applyAppearance(value as "light" | "dark")}>
            <DropdownMenu.RadioItem value="light">浅色</DropdownMenu.RadioItem>
            <DropdownMenu.RadioItem value="dark">深色</DropdownMenu.RadioItem>
          </DropdownMenu.RadioGroup>
          <DropdownMenu.Separator />
          <DropdownMenu.Label>强调色</DropdownMenu.Label>
          <DropdownMenu.RadioGroup value={accent} onValueChange={(value) => applyAccent(value as "indigo" | "teal" | "orange")}>
            <DropdownMenu.RadioItem value="indigo">靛蓝</DropdownMenu.RadioItem>
            <DropdownMenu.RadioItem value="teal">青绿</DropdownMenu.RadioItem>
            <DropdownMenu.RadioItem value="orange">暖橙</DropdownMenu.RadioItem>
          </DropdownMenu.RadioGroup>
          {error && <DropdownMenu.Label className="preference-error">{error}</DropdownMenu.Label>}
          <DropdownMenu.Separator />
          <DropdownMenu.Item data-settings-link onSelect={() => onSettings(sourceScroll.current)}>隐私与存储设置</DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Root>
    );
  }
  return (
    <Dialog.Root open={open} onOpenChange={changeOpen}>
      <Dialog.Trigger>{trigger}</Dialog.Trigger>
      <Dialog.Content className="mobile-preferences-sheet" aria-describedby="mobile-preferences-description" onOpenAutoFocus={(event) => { event.preventDefault(); document.querySelector<HTMLElement>(".mobile-preferences-sheet .sheet-drag-handle")?.focus({ preventScroll: true }); }}>
        <button
          type="button"
          className="sheet-drag-handle"
          aria-label="向下滑动或按关闭按钮收起个人偏好"
          onPointerDown={(event) => { dragStart.current = event.clientY; event.currentTarget.setPointerCapture(event.pointerId); }}
          onPointerUp={(event) => {
            if (dragStart.current !== null && event.clientY - dragStart.current >= 72) setOpen(false);
            dragStart.current = null;
          }}
          onPointerCancel={() => { dragStart.current = null; }}
        ><span /></button>
        <div className="sheet-title-row">
          <div><Dialog.Title>个人偏好</Dialog.Title><Dialog.Description id="mobile-preferences-description">主题与强调色会作为一套完整组合即时应用。</Dialog.Description></div>
          <Dialog.Close><IconButton variant="ghost" color="gray" aria-label="关闭个人偏好"><X size={22} aria-hidden /></IconButton></Dialog.Close>
        </div>
        <fieldset className="preference-sheet-group">
          <legend>主题</legend>
          <div role="radiogroup" aria-label="主题">
            {(["light", "dark"] as const).map((value, index, values) => <button key={value} type="button" role="radio" aria-checked={appearance === value} tabIndex={appearance === value ? 0 : -1} onKeyDown={(event) => moveRadioSelection(event, values, index, applyAppearance)} onClick={() => applyAppearance(value)}>{appearance === value && <Check size={17} aria-hidden />}{value === "light" ? "浅色" : "深色"}</button>)}
          </div>
        </fieldset>
        <fieldset className="preference-sheet-group">
          <legend>强调色</legend>
          <div role="radiogroup" aria-label="强调色">
            {(["indigo", "teal", "orange"] as const).map((value, index, values) => <button key={value} type="button" role="radio" aria-checked={accent === value} tabIndex={accent === value ? 0 : -1} onKeyDown={(event) => moveRadioSelection(event, values, index, applyAccent)} onClick={() => applyAccent(value)}><span className={`accent-dot dot-${value}`} />{value === "indigo" ? "靛蓝" : value === "teal" ? "青绿" : "暖橙"}</button>)}
          </div>
        </fieldset>
        {error && <p className="preference-error" role="alert">{error}</p>}
        <Button data-settings-link className="preference-settings-link" variant="outline" color="gray" onClick={() => { setOpen(false); onSettings(sourceScroll.current); }}>隐私与存储设置</Button>
      </Dialog.Content>
    </Dialog.Root>
  );
}

function GlobalHeader({ onSettings, restoreSettingsTrigger = 0 }: { onSettings: (scrollY?: number) => void; restoreSettingsTrigger?: number }) {
  return (
    <header className="collection-global-header">
      <div className="collection-global-identity">
        <BrandWordmark />
        <span className="collection-current-page">素材库</span>
      </div>
      <PreferencesMenu onSettings={onSettings} restoreSettingsTrigger={restoreSettingsTrigger} />
    </header>
  );
}

function Cover({ src, title, missingLabel }: { src: string; title: string; missingLabel?: string }) {
  const fallbackSrc = "/assets/material-cover-fallback.webp";
  const controlledSrc = /^\/api\/v1\/collection-(?:previews|items)\/[^/?#\\]+\/cover(?:\?(?:cache|source)=only)?$/.test(src)
    ? src
    : "";
  const [failedSrc, setFailedSrc] = useState("");
  const displayedSrc = controlledSrc && failedSrc !== controlledSrc
    ? controlledSrc
    : fallbackSrc;
  const cover = (
    <div className="material-cover">
      <img
        src={displayedSrc}
        alt=""
        onError={displayedSrc === fallbackSrc ? undefined : () => setFailedSrc(controlledSrc)}
      />
      <span className="sr-only">{title}的{displayedSrc === fallbackSrc ? "封面占位图" : "素材封面"}</span>
    </div>
  );
  return missingLabel ? <div className="source-cover-block">{cover}{displayedSrc === fallbackSrc && <p className="source-cover-note">{missingLabel}</p>}</div> : cover;
}

function MaterialCard({ item, featured, onOpen }: { item: MaterialCardData; featured: boolean; onOpen: () => void }) {
  return (
    <article className={`material-card ${featured ? "is-featured" : ""}`}>
      <button type="button" className="material-card-button" onClick={onOpen} aria-label={`打开素材：${item.title}`}>
        <Cover src={item.coverUrl} title={item.title} />
        <span className="material-card-copy">
          <strong>{item.title}</strong>
          {item.author && <span className="material-card-author">{item.author}{item.authorIsSupplement && <small>用户补充</small>}</span>}
          <span>{platformNames[item.platform]} · {item.category}</span>
        </span>
      </button>
    </article>
  );
}

function LibraryPage({
  items,
  facets,
  total,
  status,
  error,
  loadMoreError,
  nextCursor,
  query,
  searchText,
  captureText,
  captureError,
  setCaptureError,
  setCaptureText,
  setSearchText,
  setSearchComposing,
  setQuery,
  onRetry,
  onRetrySavedRefresh,
  onLoadMore,
  onCapture,
  onBatchImport,
  onOpen,
  onSettings,
  focusId,
  captureFocusToken,
  savedItem,
  restoreContext,
  onRestoreConsumed,
}: {
  items: MaterialCardData[];
  facets: CollectionListFacets;
  total: number;
  status: "loading" | "ready" | "error" | "loading-more";
  error: string;
  loadMoreError: string;
  nextCursor: string | null;
  query: LibraryQueryState;
  searchText: string;
  captureText: string;
  captureError: string;
  setCaptureError: (value: string) => void;
  setCaptureText: (value: string) => void;
  setSearchText: (value: string) => void;
  setSearchComposing: (value: boolean) => void;
  setQuery: (value: LibraryQueryState) => void;
  onRetry: () => void;
  onRetrySavedRefresh: () => void;
  onLoadMore: () => void;
  onCapture: () => void;
  onBatchImport: () => void;
  onOpen: (id: string) => void;
  onSettings: (scrollY?: number) => void;
  focusId: string | null;
  captureFocusToken: number;
  savedItem: CollectionItem | null;
  restoreContext: PageReturnContext | null;
  onRestoreConsumed: () => void;
}) {
  const [filterOpen, setFilterOpen] = useState(false);
  const [filterDraft, setFilterDraft] = useState<Pick<LibraryQueryState, "platform" | "secondaryCategory" | "tag" | "tagSource">>({
    platform: query.platform,
    secondaryCategory: query.secondaryCategory,
    tag: query.tag,
    tagSource: query.tagSource,
  });
  const [mobileSurface, setMobileSurface] = useState(() => typeof window.matchMedia === "function" && window.matchMedia("(max-width: 760px)").matches);
  const mobileDock = useRef<HTMLDivElement | null>(null);
  useMobileInputViewport(mobileDock);
  const captureInput = useRef<HTMLTextAreaElement | null>(null);
  const mobileCaptureInput = useRef<HTMLInputElement | null>(null);
  const titleRef = useRef<HTMLHeadingElement | null>(null);
  const focusedIdentity = useRef("");
  const composing = useRef(false);
  const loadMoreRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return undefined;
    const media = window.matchMedia("(max-width: 760px)");
    const update = () => setMobileSurface(media.matches);
    media.addEventListener?.("change", update);
    return () => media.removeEventListener?.("change", update);
  }, []);

  useEffect(() => {
    const identity = `${focusId ?? ""}|${captureFocusToken}|${savedItem?.id ?? ""}`;
    if (restoreContext) {
      if (status === "loading" || status === "loading-more") return;
      focusedIdentity.current = identity;
      const settingsTrigger = document.querySelector<HTMLElement>(".library-page [data-settings-focus=preferences]");
      const batchTrigger = document.querySelector<HTMLElement>(".library-page [data-batch-import-focus]");
      const trigger = restoreContext.focus === "preferences" ? settingsTrigger : restoreContext.focus === "batch" ? batchTrigger : null;
      return restorePagePosition(trigger ? restoreContext.scrollY : 0,
        () => trigger ?? titleRef.current, onRestoreConsumed);
    }
    if (focusedIdentity.current === identity) return;
    if (focusId) {
      const target = document.querySelector<HTMLElement>(`[data-material-id="${CSS.escape(focusId)}"] button`);
      if (!target && status !== "error" && !error) return;
      (target ?? titleRef.current)?.focus({
        preventScroll: savedItem?.id !== focusId,
      });
    } else if (captureFocusToken > 0) {
      const mobile = typeof window.matchMedia === "function" && window.matchMedia("(max-width: 900px)").matches;
      (mobile ? mobileCaptureInput.current : captureInput.current)?.focus({ preventScroll: mobile });
    } else {
      titleRef.current?.focus();
    }
    focusedIdentity.current = identity;
  }, [captureFocusToken, error, focusId, items, onRestoreConsumed, restoreContext, savedItem, status]);

  useEffect(() => {
    if (!nextCursor || loadMoreError || status === "loading-more" || typeof IntersectionObserver === "undefined") return undefined;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) onLoadMore();
    }, { rootMargin: "240px 0px" });
    if (loadMoreRef.current) observer.observe(loadMoreRef.current);
    return () => observer.disconnect();
  }, [loadMoreError, nextCursor, onLoadMore, status]);

  const openFilter = () => {
    setFilterDraft({
      platform: query.platform,
      secondaryCategory: query.secondaryCategory,
      tag: query.tag,
      tagSource: query.tagSource,
    });
    setFilterOpen(true);
  };

  const discardFilter = () => {
    setFilterDraft({
      platform: query.platform,
      secondaryCategory: query.secondaryCategory,
      tag: query.tag,
      tagSource: query.tagSource,
    });
    setFilterOpen(false);
  };

  const applyFilter = () => {
    setQuery({ ...query, ...filterDraft });
    setFilterOpen(false);
  };

  const clearConditions = () => {
    setSearchText("");
    setQuery({ query: "", primaryCategory: "", secondaryCategory: "", tag: "" });
  };

  const secondaryFacets = query.primaryCategory
    ? facets.categories.find((entry) => entry.primary_category === query.primaryCategory)?.children ?? []
    : Array.from(new Map(
      facets.categories
        .flatMap((entry) => entry.children)
        .map((entry) => [entry.secondary_category.normalize("NFKC").toLocaleLowerCase("und"), entry]),
    ).values());
  const hasConditions = Boolean(searchText.trim() || query.platform || query.primaryCategory || query.secondaryCategory || query.tag);

  const filterPanel = (
    <div className="filter-panel">
      <section>
        <h3>来源平台</h3>
        <div className="filter-options" role="radiogroup" aria-label="来源平台">
          <button type="button" role="radio" aria-checked={!filterDraft.platform} onClick={() => setFilterDraft((value) => ({ ...value, platform: undefined }))}>全部来源</button>
          {facets.platforms.map((entry) => (
            <button key={entry.platform} type="button" role="radio" aria-checked={filterDraft.platform === entry.platform} onClick={() => setFilterDraft((value) => ({ ...value, platform: entry.platform }))}>
              {filterDraft.platform === entry.platform && <Check size={17} aria-hidden />}{platformNames[entry.platform]}
            </button>
          ))}
        </div>
      </section>
      <section>
        <h3>二级分类</h3>
        <div className="filter-options" role="radiogroup" aria-label="二级分类">
          <button type="button" role="radio" aria-checked={!filterDraft.secondaryCategory} onClick={() => setFilterDraft((value) => ({ ...value, secondaryCategory: "" }))}>全部二级分类</button>
          {secondaryFacets.map((entry) => (
            <button key={entry.secondary_category} type="button" role="radio" aria-checked={filterDraft.secondaryCategory === entry.secondary_category} onClick={() => setFilterDraft((value) => ({ ...value, secondaryCategory: entry.secondary_category }))}>
              {filterDraft.secondaryCategory === entry.secondary_category && <Check size={17} aria-hidden />}{entry.secondary_category}
            </button>
          ))}
        </div>
      </section>
      <section>
        <h3>标签</h3>
        <div className="filter-options filter-tag-options" role="radiogroup" aria-label="标签">
          <button type="button" role="radio" aria-checked={!filterDraft.tag} onClick={() => setFilterDraft((value) => ({ ...value, tag: "", tagSource: undefined }))}>全部标签</button>
          {facets.tags.map((entry) => (
            <button key={`${entry.source}:${entry.name}`} type="button" role="radio" aria-checked={filterDraft.tag === entry.name && filterDraft.tagSource === entry.source} onClick={() => setFilterDraft((value) => ({ ...value, tag: entry.name, tagSource: entry.source }))}>
              {filterDraft.tag === entry.name && filterDraft.tagSource === entry.source && <Check size={17} aria-hidden />}
              <span>{entry.name}<small>{entry.source === "platform" ? "平台" : entry.source === "organization" ? "整理" : "个人"}</small></span>
            </button>
          ))}
        </div>
      </section>
      <div className="dialog-actions">
        <Button variant="ghost" color="gray" onClick={() => setFilterDraft({ platform: undefined, secondaryCategory: "", tag: "", tagSource: undefined })}>清除筛选</Button>
        <Button variant="outline" color="gray" onClick={discardFilter}>取消</Button>
        <Button onClick={applyFilter}>应用筛选</Button>
      </div>
    </div>
  );

  const submitCapture = (surface: "desktop" | "mobile") => {
    const input = surface === "mobile" ? mobileCaptureInput.current : captureInput.current;
    const focusError = () => window.requestAnimationFrame(() => {
      const error = document.getElementById(surface === "mobile" ? "mobile-capture-error" : "capture-error");
      (error ?? input)?.focus();
    });
    if (!captureText.trim()) {
      setCaptureError("请粘贴一条公开链接或完整分享文本。");
      focusError();
      return;
    }
    if (!singleHttpLink(captureText)) {
      setCaptureError("一次只能收一条有效的 HTTP/HTTPS 链接，请保留一个链接后重试。");
      focusError();
      return;
    }
    setCaptureError("");
    onCapture();
  };

  return (
    <div className="collection-page library-page">
      <GlobalHeader onSettings={onSettings} />
      <main className="library-main">
        <section className="library-hero" aria-labelledby="library-title">
          <h1 ref={titleRef} tabIndex={-1} id="library-title">把看到的，<br />变成以后用得上的</h1>
          <div className="capture-column">
            <div className="capture-box">
              <LinkIcon size={25} aria-hidden />
              <textarea
                ref={captureInput}
                value={captureText}
                onChange={(event) => setCaptureText(event.target.value)}
                placeholder="粘贴链接或分享文本"
                aria-label="粘贴链接或分享文本"
                aria-describedby={captureError ? "capture-error" : undefined}
                rows={2}
              />
              <Button size="4" onClick={() => submitCapture("desktop")}>收进来</Button>
              {captureError && <p id="capture-error" className="inline-error" role="alert" tabIndex={-1}>{captureError}</p>}
            </div>
            <Button data-batch-import-focus className="batch-import-library-entry" variant="ghost" color="gray" onClick={onBatchImport}>
              <ListPlus size={18} aria-hidden />需要一次整理多条？进入批量导入
            </Button>
          </div>
        </section>

        <section className="library-toolbar" aria-label="搜索与筛选素材">
          <label className="library-search">
            <MagnifyingGlass size={21} aria-hidden />
            <span className="sr-only">搜索素材和我的灵感</span>
            <input
              value={searchText}
              onChange={(event) => setSearchText(event.target.value)}
              onCompositionStart={() => { composing.current = true; setSearchComposing(true); }}
              onCompositionEnd={(event) => { composing.current = false; setSearchComposing(false); setSearchText(event.currentTarget.value); }}
              data-composing={composing.current || undefined}
              placeholder="搜索素材和我的灵感"
            />
          </label>
          <div className="category-rail" aria-label="一级分类">
            {["", ...facets.categories.map((entry) => entry.primary_category)].map((name) => (
              <button key={name || "all"} type="button" className={query.primaryCategory === name ? "is-active" : ""} onClick={() => setQuery({ ...query, primaryCategory: name, secondaryCategory: "" })} aria-pressed={query.primaryCategory === name}>
                {name || "全部"}
              </button>
            ))}
          </div>
          {mobileSurface ? (
            <Dialog.Root open={filterOpen} onOpenChange={(open) => { if (open) openFilter(); else discardFilter(); }}>
              <Dialog.Trigger><Button variant="outline" color="gray" className="filter-trigger"><SlidersHorizontal size={20} aria-hidden />筛选<CaretDown size={15} aria-hidden /></Button></Dialog.Trigger>
              <Dialog.Content className="filter-dialog mobile-filter-dialog" maxWidth="430px">
                <Dialog.Title>筛选素材</Dialog.Title>
                <Dialog.Description>筛选会与当前关键词和一级分类组合。</Dialog.Description>
                {filterPanel}
              </Dialog.Content>
            </Dialog.Root>
          ) : (
            <Popover.Root open={filterOpen} onOpenChange={(open) => { if (open) openFilter(); else discardFilter(); }}>
              <Popover.Trigger><Button variant="outline" color="gray" className="filter-trigger"><SlidersHorizontal size={20} aria-hidden />筛选<CaretDown size={15} aria-hidden /></Button></Popover.Trigger>
              <Popover.Content className="filter-popover" width="430px" align="end">
                <h2>筛选素材</h2>
                <p>筛选会与当前关键词和一级分类组合。</p>
                {filterPanel}
              </Popover.Content>
            </Popover.Root>
          )}
        </section>

        {savedItem && !items.some((item) => item.id === savedItem.id) && status === "ready" && (
          <div className="library-save-notice" role="status">
            <span>{error ? `“${savedItem.display_title}”已保存；素材库刷新暂未完成。` : `“${savedItem.display_title}”已保存，但不符合当前筛选条件。`}</span>
            <div>
              {error && <Button variant="outline" color="gray" onClick={onRetrySavedRefresh}>按原条件重试</Button>}
              <Button variant="outline" color="gray" onClick={() => onOpen(savedItem.id)}>打开已保存素材</Button>
            </div>
          </div>
        )}

        <section className="editorial-grid" aria-label="素材库">
          <span className="sr-only" role="status">当前条件共有 {total} 条素材</span>
          {status === "loading" ? Array.from({ length: 6 }, (_, index) => (
            <div key={index} className={index === 0 ? "grid-featured" : ""} aria-hidden>
              <div className={`material-card material-skeleton ${index === 0 ? "is-featured" : ""}`}><span /><span /></div>
            </div>
          )) : status === "error" ? (
            <div className="library-local-state" role="alert">
              <Funnel size={30} aria-hidden />
              <h2>素材库暂时没有读出来</h2>
              <p>{error}</p>
              <Button onClick={onRetry}>按原条件重试</Button>
            </div>
          ) : items.length ? items.map((item, index) => (
            <div key={item.id} data-material-id={item.id} className={index === 0 ? "grid-featured" : ""}>
              <MaterialCard item={item} featured={index === 0} onOpen={() => onOpen(item.id)} />
            </div>
          )) : (
            <div className="library-local-state">
              <Funnel size={30} aria-hidden />
              <h2>{hasConditions ? "没有符合当前条件的素材" : "从第一条素材开始"}</h2>
              <p>{hasConditions ? "清除搜索和筛选，或换一个关键词。" : "粘贴公开链接或分享文本，先把它安全收下。"}</p>
              <Button onClick={() => { if (hasConditions) clearConditions(); else captureInput.current?.focus(); }}>
                {hasConditions ? "清除条件" : "收第一条"}
              </Button>
            </div>
          )}
        </section>
        <div ref={loadMoreRef} className="library-load-sentinel" aria-live="polite">
          {loadMoreError ? (
            <div role="alert">
              <span>{loadMoreError}</span>
              <Button variant="outline" color="gray" onClick={onLoadMore}>重试加载更多</Button>
            </div>
          ) : status === "loading-more" ? "正在加载更多素材" : nextCursor ? "继续向下浏览会加载更多素材" : items.length ? "已经看到当前条件下的全部素材" : ""}
        </div>
      </main>
      <div ref={mobileDock} className="mobile-capture-dock" aria-label="快速收进来">
        <LinkIcon size={24} aria-hidden />
        <input
          ref={mobileCaptureInput}
          value={captureText}
          onChange={(event) => setCaptureText(event.target.value)}
          placeholder="粘贴链接或分享文本"
          aria-label="粘贴链接或分享文本（手机）"
          aria-describedby={captureError ? "mobile-capture-error" : undefined}
        />
        <Button onClick={() => submitCapture("mobile")}>收进来</Button>
        {captureError && <p id="mobile-capture-error" className="mobile-capture-error" role="alert" tabIndex={-1}>{captureError}</p>}
      </div>
    </div>
  );
}

function SourceStatus({ preview }: { preview: CollectionPreview }) {
  const label = preview.metadata_status === "recognized"
    ? "已识别公开来源信息"
    : preview.metadata_status === "generic"
      ? "已获取普通网页信息"
      : "公开元信息暂不可用，仍可保存";
  return <p className={`metadata-status status-${preview.metadata_status}`}><Check size={18} aria-hidden />{label}</p>;
}

const provenanceNames: Record<string, string> = {
  open_graph: "公开页面 Open Graph",
  page_metadata: "公开页面元信息",
  platform_public: "平台公开信息",
  page_description: "公开页面描述",
  platform_description: "平台公开简介",
  share_text: "用户粘贴的分享文本",
  user: "用户补充",
  legacy_import: "历史导入",
  none: "来源未提供",
};

function Provenance({ source }: { source: string }) {
  return <span className="field-provenance">来源：{provenanceNames[source] ?? "公开来源"}</span>;
}


function ReviewPage({ inputText, active, reviewStorageIdentity, restoreContext, onRestoreConsumed, onCancel, onSaved, onOpenExisting, onMergeExisting, onSettings, onUnknownSaveDiscard, registerLeaveGuard }: {
  inputText: string;
  active: boolean;
  reviewStorageIdentity: string;
  restoreContext: PageReturnContext | null;
  onRestoreConsumed: () => void;
  onCancel: (discarded?: boolean) => void;
  onSaved: (item: CollectionItem) => void;
  onOpenExisting: (id: string) => void;
  onMergeExisting: (id: string, payload: CollectionItemCreate, userCoverClaimToken: string | null) => boolean;
  onSettings: (discardedReview?: boolean, scrollY?: number) => void;
  onUnknownSaveDiscard: () => void;
  registerLeaveGuard: (guard: (() => boolean) | null) => void;
}) {
  const [preview, setPreview] = useState<CollectionPreview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [previewLoading, setPreviewLoading] = useState(true);
  const [previewGeneration, setPreviewGeneration] = useState(0);
  const saveDock = useRef<HTMLElement | null>(null);
  useMobileInputViewport(saveDock, active);
  const [userTitle, setUserTitle] = useState("");
  const [userAuthor, setUserAuthor] = useState("");
  const [userCover, setUserCover] = useState<UserCoverSelection>(() => readStoredUserCover(reviewStorageIdentity));
  const [userCoverBusy, setUserCoverBusy] = useState(false);
  const [untitledConfirmed, setUntitledConfirmed] = useState(false);
  const [primary, setPrimary] = useState("");
  const [secondary, setSecondary] = useState("");
  const [organizationTags, setOrganizationTags] = useState<string[]>([]);
  const [selectedSourceTopics, setSelectedSourceTopics] = useState<number[]>([]);
  const [sourceTopicMessage, setSourceTopicMessage] = useState("");
  const acceptedPreviewRef = useRef<CollectionPreview | null>(null);
  const selectedSourceTopicsRef = useRef<number[]>([]);
  const automaticOrganizationTags = useRef(new Set<string>());
  const refreshSourceRef = useRef<HTMLButtonElement | null>(null);
  const focusedSourceTopic = useRef<string | null>(null);
  const restoreSourceFocus = useRef(false);
  const [personalTags, setPersonalTags] = useState<string[]>([]);
  const [organizationTagDraft, setOrganizationTagDraft] = useState("");
  const [personalTagDraft, setPersonalTagDraft] = useState("");
  const [inspiration, setInspiration] = useState("");
  const [phoneVoice] = useState(isPhoneDevice);
  const [inspirationMode, setInspirationMode] = useState<"text" | "voice">("text");
  const [transcriptionDraft, setTranscriptionDraft] = useState<string | null>(null);
  const [voiceState, setVoiceState] = useState<VoiceState>("idle");
  const [saveState, setSaveState] = useState<"idle" | "saving" | "failed" | "duplicate">("idle");
  const [saveMessage, setSaveMessage] = useState("");
  const [existingId, setExistingId] = useState<string | null>(null);
  const [saveAttempt, setSaveAttempt] = useState<{ payload: CollectionItemCreate; key: string; fingerprint: string; identityUrl: string; userCoverClaimToken: string | null } | null>(null);
  const [outcomeUnknown, setOutcomeUnknown] = useState(false);
  const [initializedPreviewId, setInitializedPreviewId] = useState("");
  const [suggestionCandidates, setSuggestionCandidates] = useState<{ primary: string; secondary: string; tags: string[] }>({ primary: "", secondary: "", tags: [] });
  const [sourceCopyExpanded, setSourceCopyExpanded] = useState(false);
  const [sourceCopyOverflows, setSourceCopyOverflows] = useState(false);
  const [sourceActionMessage, setSourceActionMessage] = useState("");
  const [copyPending, setCopyPending] = useState(false);
  const [leaveOpen, setLeaveOpen] = useState(false);
  const [leaveTarget, setLeaveTarget] = useState<"library" | "settings" | "existing">("library");
  const [restoreSettingsTrigger, setRestoreSettingsTrigger] = useState(0);
  const leaveOrigin = useRef<HTMLElement | null>(null);
  const settingsSourceScroll = useRef<number | undefined>(undefined);
  const titleRef = useRef<HTMLHeadingElement | null>(null);
  const cancelVoiceRef = useRef<() => void>(() => undefined);
  const previousSourceCopyIdentity = useRef("");
  const previousSourceTitleAvailability = useRef<boolean | null>(null);
  const sourceCopyRef = useRef<HTMLParagraphElement | null>(null);
  const previewRequestRef = useRef<AbortController | null>(null);
  const pageActiveRef = useRef(true);
  const submitSequence = useRef(0);
  const duplicateIdentityRef = useRef<string | null>(null);
  const exactSourceUrl = singleHttpLink(inputText) ?? "";

  useEffect(() => { titleRef.current?.focus(); }, []);
  useEffect(() => {
    if (!active || !restoreContext || previewLoading) return;
    return restorePagePosition(restoreContext.scrollY,
      () => restoreContext.focus === "merge"
        ? document.querySelector<HTMLElement>(".review-page [data-review-focus=merge]") ?? titleRef.current
        : document.querySelector<HTMLElement>(".review-page [data-settings-focus=preferences]") ?? titleRef.current,
      onRestoreConsumed);
  }, [active, onRestoreConsumed, previewLoading, restoreContext]);
  useEffect(() => {
    pageActiveRef.current = true;
    return () => { pageActiveRef.current = false; };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    previewRequestRef.current = controller;
    setPreviewError("");
    setPreviewLoading(true);
    // StrictMode may replay setup/cleanup before the first microtask. Only the
    // surviving setup starts a request; aborting a sent request is not deduping it.
    void Promise.resolve().then(() => {
      if (controller.signal.aborted) return null;
      return collectionApi.createPreview(inputText, previewGeneration > 0, controller.signal);
    })
      .then((next) => {
        if (controller.signal.aborted || !pageActiveRef.current || !next) return;
        const previous = acceptedPreviewRef.current;
        // HTTP success may still describe a failed metadata read. Keep the whole
        // accepted snapshot: the selected indices belong to that exact preview.
        if (previous && next.metadata_status === "metadata_unavailable") {
          setPreviewError("重新获取公开信息失败；已保留上次来源信息和话题选择，仍可保存书签。");
          return;
        }
        if (previous) {
          const selected = reconcileSourceTopics(previous.metadata.platform_tags, selectedSourceTopicsRef.current, next.metadata.platform_tags);
          const removed = selectedSourceTopicsRef.current.length - selected.length;
          selectedSourceTopicsRef.current = selected;
          setSelectedSourceTopics(selected);
          setSourceTopicMessage(removed > 0 ? `来源话题已更新，取消了 ${removed} 项已消失的话题。` : "来源话题已更新，保留仍存在的选择；新增话题未选择。");
          restoreSourceFocus.current = Boolean(focusedSourceTopic.current && !sourceTopicCandidates(next.metadata.platform_tags).some((topic) => topic.identity === focusedSourceTopic.current));
        }
        const duplicateIdentity = duplicateIdentityRef.current;
        if (duplicateIdentity && next.identity_url !== duplicateIdentity) {
          duplicateIdentityRef.current = null;
          setExistingId(null);
          setSaveAttempt(null);
          setSaveState("idle");
          setSaveMessage("公开来源身份已变化，请重新保存以确认是否重复。");
        }
        acceptedPreviewRef.current = next;
        setPreview(next);
        setPreviewError("");
      })
      .catch((error) => {
        if (controller.signal.aborted || !pageActiveRef.current) return;
        setPreviewError(error instanceof Error ? error.message : "公开来源识别失败，请重试。");
      })
      .finally(() => {
        if (previewRequestRef.current === controller) {
          previewRequestRef.current = null;
          setPreviewLoading(false);
        }
      });
    return () => {
      controller.abort();
      if (previewRequestRef.current === controller) previewRequestRef.current = null;
    };
  }, [inputText, previewGeneration]);

  useEffect(() => {
    if (!previewLoading && restoreSourceFocus.current) {
      restoreSourceFocus.current = false;
      refreshSourceRef.current?.focus({ preventScroll: true });
    }
  }, [previewLoading]);

  useEffect(() => {
    if (!preview) return;
    const suggestion = preview.organization_suggestion;
    const safeTags = withoutSourceTopicSuggestions(suggestion.tags, preview.metadata.platform_tags);
    if (!initializedPreviewId) {
      setPrimary(suggestion.primary_category);
      setSecondary(suggestion.secondary_category);
      automaticOrganizationTags.current = new Set(safeTags.map(editableTagIdentity));
      setOrganizationTags(safeTags);
      setInitializedPreviewId(preview.preview_id);
      return;
    }
    // Newly discovered topics can match earlier automatic suggestions. Remove
    // only those automatic entries; explicit manual input remains intact.
    const topicIdentities = new Set(preview.metadata.platform_tags.map((topic) => editableTagIdentity(topic.value)));
    setOrganizationTags((current) => current.filter((tag) => !automaticOrganizationTags.current.has(editableTagIdentity(tag)) || !topicIdentities.has(editableTagIdentity(tag))));
    setSuggestionCandidates({
      primary: suggestion.primary_category && suggestion.primary_category !== primary ? suggestion.primary_category : "",
      secondary: suggestion.secondary_category && suggestion.secondary_category !== secondary ? suggestion.secondary_category : "",
      tags: safeTags.join("\0") !== organizationTags.join("\0") ? safeTags : [],
    });
  }, [initializedPreviewId, preview]);

  useEffect(() => {
    if (!preview) return;
    const identity = `${preview.metadata.source_copy.source}\0${preview.metadata.source_copy.value}`;
    if (previousSourceCopyIdentity.current && previousSourceCopyIdentity.current !== identity) setSourceCopyExpanded(false);
    previousSourceCopyIdentity.current = identity;
  }, [preview]);

  useEffect(() => {
    setSourceActionMessage("");
  }, [preview?.preview_id, preview?.source_url]);

  const sourceTitle = preview?.metadata.title.value ?? "";
  const normalizedSourceTitle = pythonTrim(sourceTitle);
  const sourceAuthor = preview?.metadata.author.value ?? "";
  const sourceCopy = preview?.metadata.source_copy.value ?? "";
  const visitTarget = preview ? (preview.canonical_url || preview.source_url) : "";
  let visitDomain = "来源";
  try { visitDomain = new URL(visitTarget).hostname; } catch { visitDomain = platformNames[preview?.platform ?? "other"]; }

  useEffect(() => {
    if (!preview) return;
    const available = Boolean(normalizedSourceTitle);
    if (previousSourceTitleAvailability.current !== null && previousSourceTitleAvailability.current !== available) {
      setUntitledConfirmed(false);
    }
    previousSourceTitleAvailability.current = available;
  }, [normalizedSourceTitle, preview]);

  useEffect(() => {
    if (!active) return undefined;
    const measure = () => {
      const node = sourceCopyRef.current;
      if (!node || sourceCopyExpanded) return;
      setSourceCopyOverflows(node.scrollHeight > node.clientHeight + 1);
    };
    const frame = window.requestAnimationFrame(measure);
    window.addEventListener("resize", measure);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", measure);
    };
  }, [active, sourceCopy, sourceCopyExpanded]);

  const isBusy = saveState === "saving";
  const voiceActive = ["permission", "recording", "stopping", "transcribing"].includes(voiceState);
  const normalizedOrganizationTagDraft = normalizeEditableTag(organizationTagDraft);
  const normalizedPersonalTagDraft = normalizeEditableTag(personalTagDraft);
  const normalizedTranscriptionDraft = pythonTrim(transcriptionDraft ?? "");
  const reviewSnapshot = useMemo(() => normalizeSnapshot({
    user_title: userTitle || null,
    user_author: userAuthor || null,
    user_cover_asset_id: userCover.assetId,
    organization_confirmation: {
      primary_category: primary,
      secondary_category: secondary,
      organization_tags: organizationTags,
    },
    personal_tags: personalTags,
    inspiration: pythonTrim(inspiration)
      ? {
        content: inspiration,
        input_mode: inspirationMode,
        transcription_status: inspirationMode === "voice" ? "completed" : "not_applicable",
      }
      : null,
  }), [inspiration, inspirationMode, organizationTags, personalTags, primary, secondary, userAuthor, userCover.assetId, userTitle]);
  const reviewValidation = useMemo(() => validateSnapshot(reviewSnapshot), [reviewSnapshot]);
  const validationBlockReason = reviewValidation.errors.user_title
    || reviewValidation.errors.user_author
    || reviewValidation.errors.primary_category
    || reviewValidation.errors.secondary_category
    || reviewValidation.errors.organization_tags
    || reviewValidation.errors.personal_tags
    || reviewValidation.errors.inspiration
    || "";
  const suggestionSnapshot = useMemo(() => preview ? normalizeSnapshot({
    user_title: null,
    user_author: null,
    user_cover_asset_id: null,
    organization_confirmation: {
      primary_category: preview.organization_suggestion.primary_category,
      secondary_category: preview.organization_suggestion.secondary_category,
      organization_tags: withoutSourceTopicSuggestions(preview.organization_suggestion.tags, preview.metadata.platform_tags),
    },
    personal_tags: [],
    inspiration: null,
  }) : null, [preview]);
  const titleEligible = Boolean(normalizedSourceTitle || reviewSnapshot.user_title || untitledConfirmed);
  const dirty = Boolean(
    reviewSnapshot.user_title
    || reviewSnapshot.user_author
    || reviewSnapshot.user_cover_asset_id
    || untitledConfirmed
    || reviewSnapshot.personal_tags.length
    || selectedSourceTopics.length
    || normalizedOrganizationTagDraft
    || normalizedPersonalTagDraft
    || reviewSnapshot.inspiration
    || transcriptionDraft !== null
  ) || Boolean(suggestionSnapshot && (
    reviewSnapshot.organization_confirmation.primary_category !== suggestionSnapshot.organization_confirmation.primary_category
    || reviewSnapshot.organization_confirmation.secondary_category !== suggestionSnapshot.organization_confirmation.secondary_category
    || reviewSnapshot.organization_confirmation.organization_tags.join("\0") !== suggestionSnapshot.organization_confirmation.organization_tags.join("\0")
  ));

  const payload = useMemo<CollectionItemCreate | null>(() => preview ? ({
    preview_id: preview.preview_id,
    selected_source_topic_indices: [...selectedSourceTopics],
    user_title: reviewSnapshot.user_title,
    user_author: reviewSnapshot.user_author,
    user_cover_asset_id: reviewSnapshot.user_cover_asset_id,
    untitled_confirmed: !normalizedSourceTitle && !reviewSnapshot.user_title && untitledConfirmed,
    organization_confirmation: reviewSnapshot.organization_confirmation,
    personal_tags: reviewSnapshot.personal_tags,
    inspiration: reviewSnapshot.inspiration,
  }) : null, [normalizedSourceTitle, preview, reviewSnapshot, selectedSourceTopics, untitledConfirmed]);
  const payloadFingerprint = payload ? JSON.stringify(payload) : "";
  const saveBlockReason = previewLoading || previewRequestRef.current !== null
    ? "来源信息仍在更新，请等待当前安全读取完成。"
    : !preview
    ? "来源安全校验尚未完成，完成后即可保存。"
    : !titleEligible
      ? "请填写标题，或明确选择以“未命名收藏”保存。"
      : validationBlockReason
        ? validationBlockReason
        : normalizedOrganizationTagDraft
          ? "请先添加或清空尚未提交的整理标签。"
          : normalizedPersonalTagDraft
            ? "请先添加或清空尚未提交的个人标签。"
            : transcriptionDraft !== null
              ? "请先追加、替换或丢弃独立转写草稿。"
              : userCoverBusy
                ? "请等待当前图片处理完成。"
              : voiceActive
                ? "请先结束或取消当前录音处理。"
                : "";
  const mergeBlockReason = saveBlockReason || (isBusy ? "请等待当前保存完成。" : "");
  const hasCurrentRetry = saveState === "failed" && Boolean(saveAttempt && saveAttempt.fingerprint === payloadFingerprint);
  const canSave = Boolean(payload && !saveBlockReason && !isBusy && !hasCurrentRetry && saveState !== "duplicate");

  useEffect(() => {
    if (saveState === "failed" && saveAttempt && saveAttempt.fingerprint !== payloadFingerprint) {
      setSaveAttempt(null);
      setSaveState("idle");
      setSaveMessage("草稿已更改；下次保存会创建新的安全提交。");
    }
  }, [payloadFingerprint, saveAttempt, saveState]);

  const leaveNow = (target: "library" | "settings" | "existing", discarded = false) => {
    cancelVoiceRef.current();
    if (discarded) {
      void discardUserCover(userCover);
      forgetStoredUserCover(reviewStorageIdentity);
      if (isBusy || outcomeUnknown) onUnknownSaveDiscard();
      // Tombstone all in-flight callbacks before unmount/history consumption.
      submitSequence.current += 1;
      pageActiveRef.current = false;
      setSaveAttempt(null);
      setOutcomeUnknown(false);
    } else if (target === "settings" && previewRequestRef.current) {
      // Keep only the accepted clean snapshot. A request started before settings
      // cannot mutate that snapshot while hidden or after the one-time return.
      previewRequestRef.current.abort();
      previewRequestRef.current = null;
      setPreviewLoading(false);
      if (!preview) setPreviewError("来源读取已暂停，请重试元信息继续整理。");
    }
    if (target === "settings") onSettings(discarded, settingsSourceScroll.current);
    else if (target === "existing" && existingId) onOpenExisting(existingId);
    else onCancel(discarded);
  };

  const requestLeave = (target: "library" | "settings" | "existing" = "library") => {
    leaveOrigin.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setLeaveTarget(target);
    if (dirty || voiceActive || isBusy || outcomeUnknown) setLeaveOpen(true);
    else leaveNow(target);
  };

  const continueReview = () => {
    setLeaveOpen(false);
    if (leaveTarget === "settings") setRestoreSettingsTrigger((value) => value + 1);
  };

  useEffect(() => {
    if (!active) return;
    registerLeaveGuard(() => {
      if (dirty || voiceActive || isBusy || outcomeUnknown) {
        setLeaveTarget("library");
        setLeaveOpen(true);
        return true;
      }
      cancelVoiceRef.current();
      return false;
    });
    return () => registerLeaveGuard(null);
  }, [active, dirty, isBusy, outcomeUnknown, registerLeaveGuard, voiceActive]);

  useEffect(() => {
    if (!active || (!dirty && !voiceActive && !isBusy && !outcomeUnknown)) return undefined;
    const protect = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [active, dirty, isBusy, outcomeUnknown, voiceActive]);

  const submitAttempt = async (attempt: { payload: CollectionItemCreate; key: string; fingerprint: string; identityUrl: string; userCoverClaimToken: string | null }) => {
    const sequence = ++submitSequence.current;
    setSaveState("saving");
    setSaveMessage("正在保存这份冻结草稿…");
    try {
      const item = await collectionApi.createItem(attempt.payload, attempt.key, undefined, attempt.userCoverClaimToken ?? undefined);
      if (!pageActiveRef.current || sequence !== submitSequence.current) return;
      setOutcomeUnknown(false);
      setSaveMessage("已保存到素材库。");
      forgetStoredUserCover(reviewStorageIdentity);
      onSaved(item);
    } catch (error) {
      if (!pageActiveRef.current || sequence !== submitSequence.current) return;
      if (error instanceof CollectionApiError && error.code === "COLLECTION_EXISTS") {
        duplicateIdentityRef.current = attempt.identityUrl;
        setExistingId(error.collectionItemId);
        setOutcomeUnknown(false);
        setSaveState("duplicate");
        setSaveMessage("这条素材已经在素材库中。当前草稿没有被覆盖。已有收藏的来源话题保持不变。");
      } else {
        setSaveState("failed");
        setSaveMessage(error instanceof Error ? error.message : "保存失败，草稿已保留。");
        const definitive = error instanceof CollectionApiError && error.status > 0 && error.status < 500;
        setOutcomeUnknown(!definitive);
        if (definitive) setSaveAttempt(null);
      }
    }
  };

  const save = () => {
    if (!payload || !preview || isBusy) return;
    if (saveBlockReason) {
      setSaveMessage(saveBlockReason);
      const validationTarget = reviewValidation.errors.user_title
        ? document.getElementById("review-user-title")
        : reviewValidation.errors.user_author
          ? document.getElementById("review-user-author")
        : reviewValidation.errors.primary_category
          ? document.getElementById("review-primary-category")
          : reviewValidation.errors.secondary_category
            ? document.getElementById("review-secondary-category")
            : reviewValidation.errors.organization_tags
              ? document.getElementById("review-organization-tags-input")
              : reviewValidation.errors.personal_tags
                ? document.getElementById("review-personal-tags-input")
                : reviewValidation.errors.inspiration
                  ? document.getElementById("review-inspiration")
                  : null;
      const target = !titleEligible
        ? document.getElementById("review-user-title")
        : validationTarget ?? (normalizedOrganizationTagDraft
            ? document.getElementById("review-organization-tags-input")
            : normalizedPersonalTagDraft
              ? document.getElementById("review-personal-tags-input")
              : transcriptionDraft !== null
                ? document.getElementById("transcription-draft")
                : document.querySelector<HTMLElement>(".voice-capture button"));
      target?.focus();
      return;
    }
    const attempt = { payload, key: makeIdempotencyKey(), fingerprint: payloadFingerprint, identityUrl: preview.identity_url, userCoverClaimToken: userCover.claimToken };
    setSaveAttempt(attempt);
    void submitAttempt(attempt);
  };

  const retrySave = () => {
    if (!saveAttempt || saveAttempt.fingerprint !== payloadFingerprint || isBusy || saveBlockReason) return;
    void submitAttempt(saveAttempt);
  };
  const mergeExisting = () => {
    if (!existingId || !payload) return;
    if (mergeBlockReason) {
      setSaveMessage(mergeBlockReason);
      return;
    }
    cancelVoiceRef.current();
    if (!onMergeExisting(existingId, payload, userCover.claimToken)) {
      setSaveMessage("当前标签页无法建立安全合并会话，草稿已保留。");
    }
  };
  const copyOriginalLink = async () => {
    if (!preview?.source_url || copyPending) return;
    setCopyPending(true);
    setSourceActionMessage("");
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(preview.source_url);
      setSourceActionMessage("已复制当前预览的原始链接。");
    } catch {
      setSourceActionMessage("复制失败；你仍可选择上方原始链接手动复制。");
    } finally {
      setCopyPending(false);
    }
  };
  const registerVoiceCancel = useCallback((cancel: () => void) => {
    cancelVoiceRef.current = cancel;
  }, []);
  const changeSourceTopics = (indices: number[]) => {
    if (isBusy || previewLoading || previewRequestRef.current) return;
    const selected = [...indices].sort((left, right) => left - right);
    selectedSourceTopicsRef.current = selected;
    setSelectedSourceTopics(selected);
    setSourceTopicMessage("");
  };
  const sourceTopics = sourceTopicCandidates(preview?.metadata.platform_tags ?? []);
  const changeManualOrganizationTags = (tags: string[]) => {
    const unchanged = new Set(tags.filter((tag) => organizationTags.includes(tag)).map(editableTagIdentity));
    automaticOrganizationTags.current = new Set([...automaticOrganizationTags.current].filter((identity) => unchanged.has(identity)));
    setOrganizationTags(tags);
  };

  return (
    <div className="collection-page review-page">
      <GlobalHeader onSettings={(scrollY) => { settingsSourceScroll.current = scrollY; requestLeave("settings"); }} restoreSettingsTrigger={restoreSettingsTrigger} />
      <main className="review-main">
        <div className="review-heading-row">
          <div>
            <h1 ref={titleRef} tabIndex={-1}>保存前整理</h1>
            <p>核对来源、留下自己的念头，再把它保存到素材库。</p>
          </div>
          <Button className="mobile-review-cancel" variant="ghost" color="gray" onClick={() => requestLeave("library")}>取消整理</Button>
        </div>

        <div className="review-columns">
          <section className="review-source" aria-labelledby="source-review-title">
            <h2 id="source-review-title">来源预览</h2>
            {!preview && (
              <div className="progressive-source">
                <p className="progressive-source-label">原始链接</p>
                <a href={exactSourceUrl} target="_blank" rel="noreferrer noopener">{exactSourceUrl}</a>
                {previewLoading && <div className="review-loading" role="status"><CircleNotch className="spin" size={24} aria-hidden />来源信息正在安全识别…</div>}
              </div>
            )}
            {previewError && (
              <div className="review-error" role="alert"><p>{previewError}</p><Button disabled={isBusy || previewLoading} variant="outline" color="gray" onClick={() => setPreviewGeneration((value) => value + 1)}>重试元信息</Button></div>
            )}
            {preview && (
              <>
                <CollectionUserCoverInput
                  idPrefix="review-user-cover"
                  value={userCover}
                  fallbackSrc={preview.metadata.cover_url.value ? previewCoverUrl(preview.preview_id) : "/assets/material-cover-fallback.webp"}
                  fallbackKind={preview.metadata.cover_url.value ? "source" : "placeholder"}
                  storageIdentity={reviewStorageIdentity}
                  disabled={isBusy}
                  onChange={setUserCover}
                  onBusyChange={setUserCoverBusy}
                />
                <h3>{sourceTitle || userTitle || "未命名收藏"}</h3>
                <Provenance source={preview.metadata.title.source} />
                <p className="source-byline">{platformNames[preview.platform]} · {preview.source_kind === "video" ? "视频作者" : "作者"}：{sourceAuthor || "未获取到作者"}</p>
                <Provenance source={preview.metadata.author.source} />
                <div className="user-author-field">
                  <label htmlFor="review-user-author">我补充的作者 <span>用户补充</span></label>
                  <input id="review-user-author" disabled={isBusy} value={userAuthor} onChange={(event) => setUserAuthor(event.target.value)} aria-invalid={Boolean(reviewValidation.errors.user_author)} aria-describedby={reviewValidation.errors.user_author ? "review-user-author-help review-user-author-error" : "review-user-author-help"} placeholder="可选，保存后优先显示" />
                  <p id="review-user-author-help">留空或清除后继续显示来源作者。</p>
                  {reviewValidation.errors.user_author && <p id="review-user-author-error" className="inline-error" role="alert">{reviewValidation.errors.user_author}</p>}
                </div>
                <div className="source-link-row">
                  <LinkIcon size={18} aria-hidden />
                  <span className="source-url-text">{preview.source_url}</span>
                  <a href={visitTarget} target="_blank" rel="noreferrer noopener">访问来源（{visitDomain}）<ArrowSquareOut size={16} aria-hidden /></a>
                  <button type="button" disabled={copyPending} onClick={() => void copyOriginalLink()}><Clipboard size={17} aria-hidden />{copyPending ? "正在复制" : "复制原链接"}</button>
                </div>
                {sourceActionMessage && <p className="source-action-feedback" role="status">{sourceActionMessage}</p>}
                <SourceStatus preview={preview} />
                {preview.metadata.warnings.map((warning) => <p key={warning} className="inline-warning">{warning}</p>)}
                {(!normalizedSourceTitle || Boolean(userTitle) || Boolean(reviewValidation.errors.user_title)) && (
                  <div className="untitled-path">
                    <label htmlFor="review-user-title">收藏标题</label>
                    <input id="review-user-title" disabled={isBusy} value={userTitle} onChange={(event) => { setUserTitle(event.target.value); if (event.target.value) setUntitledConfirmed(false); }} aria-invalid={Boolean(reviewValidation.errors.user_title)} aria-describedby={reviewValidation.errors.user_title ? "review-user-title-error" : undefined} placeholder="补充一个便于查找的标题" />
                    {reviewValidation.errors.user_title && <p id="review-user-title-error" className="inline-error" role="alert">{reviewValidation.errors.user_title}</p>}
                    {!normalizedSourceTitle && <label className="checkbox-row"><input disabled={isBusy} type="checkbox" checked={untitledConfirmed} onChange={(event) => { setUntitledConfirmed(event.target.checked); if (event.target.checked) setUserTitle(""); }} />仍以“未命名收藏”保存</label>}
                  </div>
                )}
                {sourceCopy && <div className="source-copy">
                  <div><h3>来源文案</h3><span>{preview.metadata.source_copy.source === "share_text" ? "来自分享文本" : "来自公开页面描述"}</span></div>
                  <p ref={sourceCopyRef} id="source-copy-content" className={sourceCopyExpanded ? "is-expanded" : ""}>{sourceCopy}</p>
                  {(sourceCopyOverflows || sourceCopyExpanded) && <Button className="source-copy-toggle" type="button" variant="ghost" color="gray" aria-expanded={sourceCopyExpanded} aria-controls="source-copy-content" onClick={() => setSourceCopyExpanded((value) => !value)}>{sourceCopyExpanded ? "收起" : "展开全文"}</Button>}
                </div>}
                <section className="source-topic-selection" aria-labelledby="source-topics-title" aria-describedby="source-topics-help">
                  <div className="source-topics-heading"><h3 id="source-topics-title">来源话题</h3><p id="source-topics-help">点选你想加入整理结果的话题，可不选。</p></div>
                  {sourceTopics.length ? <div className="source-topic-options">
                    {sourceTopics.map((topic) => <label key={topic.identity} className={"source-topic-chip" + (selectedSourceTopics.includes(topic.index) ? " is-selected" : "")}>
                      <input type="checkbox" checked={selectedSourceTopics.includes(topic.index)} disabled={isBusy || previewLoading}
                        onFocus={() => { focusedSourceTopic.current = topic.identity; }}
                        onBlur={() => { if (!previewRequestRef.current) focusedSourceTopic.current = null; }}
                        onChange={(event) => changeSourceTopics(event.target.checked ? [...selectedSourceTopics, topic.index] : selectedSourceTopics.filter((index) => index !== topic.index))} />
                      <span>{topic.value}</span>
                    </label>)}
                  </div> : <p className="source-topics-empty">暂无可选来源话题，仍可保存普通书签。</p>}
                  <div className="source-topics-footer"><p role="status">已选 {selectedSourceTopics.length} 项 · 仅选中的话题加入整理结果</p><Button type="button" variant="ghost" disabled={isBusy || previewLoading || !selectedSourceTopics.length} onClick={() => changeSourceTopics([])}>清空选择</Button></div>
                  {sourceTopicMessage && <p className="source-topics-feedback" role="status">{sourceTopicMessage}</p>}
                </section>
                <Button ref={refreshSourceRef} disabled={isBusy || previewLoading} variant="ghost" color="gray" onClick={() => setPreviewGeneration((value) => value + 1)}>{previewLoading ? "正在重新获取" : "重新获取公开信息"}</Button>
              </>
            )}
          </section>

          <div className="review-editing">
            <section className="review-organization" aria-labelledby="organization-title">
              <h2 id="organization-title">整理建议</h2>
              <p className="section-help">建议只基于已标明的公开元信息，全部可以修改或清空。</p>
              <p className="suggestion-status" role="status">
                {!preview
                  ? "正在生成整理建议；不会阻挡你先写灵感。"
                  : previewLoading
                    ? "正在生成新的建议；当前确认值不会被覆盖。"
                    : preview.organization_suggestion.status === "generated"
                      ? "整理建议已初始化为可编辑确认值；全部可以修改或清空。"
                      : preview.organization_suggestion.status === "insufficient_metadata"
                        ? "公开信息不足，整理字段仍可编辑或留空。"
                        : "整理建议暂不可用，仍可编辑或留空保存。"}
              </p>
              <div className="category-fields">
                <div className="category-field">
                  <label htmlFor="review-primary-category">一级分类{suggestionCandidates.primary && <Button type="button" variant="ghost" color="gray" disabled={isBusy} onClick={() => { setPrimary(suggestionCandidates.primary); setSuggestionCandidates((value) => ({ ...value, primary: "" })); }}>采用建议</Button>}</label>
                  <input id="review-primary-category" disabled={isBusy || !preview} value={primary} onChange={(event) => setPrimary(event.target.value)} aria-invalid={Boolean(reviewValidation.errors.primary_category)} aria-describedby={reviewValidation.errors.primary_category ? "review-primary-category-error" : undefined} placeholder="可留空" />
                  {reviewValidation.errors.primary_category && <p id="review-primary-category-error" className="inline-error" role="alert">{reviewValidation.errors.primary_category}</p>}
                </div>
                <div className="category-field">
                  <label htmlFor="review-secondary-category">二级分类{suggestionCandidates.secondary && <Button type="button" variant="ghost" color="gray" disabled={isBusy} onClick={() => { setSecondary(suggestionCandidates.secondary); setSuggestionCandidates((value) => ({ ...value, secondary: "" })); }}>采用建议</Button>}</label>
                  <input id="review-secondary-category" disabled={isBusy || !preview} value={secondary} onChange={(event) => setSecondary(event.target.value)} aria-invalid={Boolean(reviewValidation.errors.secondary_category)} aria-describedby={reviewValidation.errors.secondary_category ? "review-secondary-category-error" : undefined} placeholder="可留空" />
                  {reviewValidation.errors.secondary_category && <p id="review-secondary-category-error" className="inline-error" role="alert">{reviewValidation.errors.secondary_category}</p>}
                </div>
              </div>
              {suggestionCandidates.tags.length > 0 && <Button className="adopt-tag-suggestion" type="button" variant="ghost" color="gray" disabled={isBusy} onClick={() => {
                const safeTags = withoutSourceTopicSuggestions(suggestionCandidates.tags, preview?.metadata.platform_tags ?? []);
                const manualTopics = organizationTags.filter((tag) => !automaticOrganizationTags.current.has(editableTagIdentity(tag)) && sourceTopics.some((topic) => topic.identity === editableTagIdentity(tag)));
                automaticOrganizationTags.current = new Set(safeTags.map(editableTagIdentity));
                setOrganizationTags([...safeTags, ...manualTopics]); setSuggestionCandidates((value) => ({ ...value, tags: [] }));
              }}>采用整理标签建议</Button>}
              <TagEditor idPrefix="review-organization-tags" label="整理标签" values={organizationTags} setValues={changeManualOrganizationTags} disabled={isBusy || !preview} onDraftChange={setOrganizationTagDraft} validationError={reviewValidation.errors.organization_tags} />
              <TagEditor idPrefix="review-personal-tags" label="个人标签" personal values={personalTags} setValues={setPersonalTags} disabled={isBusy || !preview} onDraftChange={setPersonalTagDraft} validationError={reviewValidation.errors.personal_tags} />
            </section>

            <section className="review-inspiration" aria-labelledby="inspiration-title">
              <h2 id="inspiration-title">我的灵感</h2>
              <p className="section-help">{phoneVoice ? "记录你的想法，可留空。" : "记录你的想法、感受或可参考的点（可留空）"}</p>
              <textarea id="review-inspiration" disabled={isBusy} value={inspiration} onChange={(event) => setInspiration(event.target.value)} aria-label="我的灵感" aria-invalid={Boolean(reviewValidation.errors.inspiration)} aria-describedby={reviewValidation.errors.inspiration ? "review-inspiration-error" : undefined} placeholder={phoneVoice ? "写下此刻的想法…" : "例如：我喜欢这种自然光的层次，想在自己的书房里试试百叶帘的光影效果…"} rows={7} />
              {reviewValidation.errors.inspiration && <p id="review-inspiration-error" className="inline-error" role="alert">{reviewValidation.errors.inspiration}</p>}
              {transcriptionDraft !== null && (
                <div className="transcription-draft">
                  <label htmlFor="transcription-draft">独立转写草稿</label>
                  <textarea disabled={isBusy} id="transcription-draft" value={transcriptionDraft} onChange={(event) => setTranscriptionDraft(event.target.value)} rows={4} />
                  <div>
                    <Button disabled={isBusy || !normalizedTranscriptionDraft} variant="outline" color="gray" onClick={() => { setInspiration((value) => [pythonTrim(value), normalizedTranscriptionDraft].filter(Boolean).join("\n")); setInspirationMode("voice"); setTranscriptionDraft(null); document.getElementById("review-inspiration")?.focus({ preventScroll: true }); }}>追加到灵感</Button>
                    <Button disabled={isBusy || !normalizedTranscriptionDraft} variant="outline" color="gray" onClick={() => { setInspiration(normalizedTranscriptionDraft); setInspirationMode("voice"); setTranscriptionDraft(null); document.getElementById("review-inspiration")?.focus({ preventScroll: true }); }}>替换现有内容</Button>
                    <Button disabled={isBusy} variant="ghost" color="gray" onClick={() => { setTranscriptionDraft(null); document.getElementById("review-inspiration")?.focus({ preventScroll: true }); }}>丢弃草稿</Button>
                  </div>
                </div>
              )}
              {phoneVoice && <VoiceCapture identity={reviewStorageIdentity} hasDraft={transcriptionDraft !== null} disabled={isBusy} onDraft={setTranscriptionDraft} onStateChange={setVoiceState} registerCancel={registerVoiceCancel} />}
            </section>
          </div>
        </div>
      </main>
      <footer ref={saveDock} className="save-review-bar">
        <p role={!validationBlockReason && (saveState === "failed" || saveState === "duplicate" || Boolean(saveBlockReason)) ? "alert" : "status"}>{saveState === "duplicate" && mergeBlockReason ? mergeBlockReason : saveMessage || saveBlockReason || "你也可以暂时留空灵感，保存后再补充。"}</p>
        <div>
          {saveState === "duplicate" && existingId && <Button variant="outline" color="gray" onClick={() => requestLeave("existing")}>打开已有项</Button>}
          {saveState === "duplicate" && existingId && payload && <Button data-review-focus="merge" variant="outline" color="gray" disabled={Boolean(mergeBlockReason)} onClick={mergeExisting}>比较并合并</Button>}
          {saveState === "failed" && saveAttempt && saveAttempt.fingerprint === payloadFingerprint && <Button variant="outline" color="gray" disabled={Boolean(saveBlockReason) || isBusy} onClick={retrySave}>重试保存</Button>}
          <Button className="desktop-cancel" variant="outline" color="gray" onClick={() => requestLeave("library")}>取消</Button>
          <Button disabled={!canSave} onClick={save}>{isBusy && <CircleNotch className="spin" size={18} aria-hidden />}{isBusy ? "正在保存" : "保存到素材库"}</Button>
        </div>
      </footer>
      <Dialog.Root open={leaveOpen} onOpenChange={(open) => { if (!open) continueReview(); else setLeaveOpen(true); }}>
        <Dialog.Content maxWidth="430px" onCloseAutoFocus={(event) => { event.preventDefault(); if (pageActiveRef.current && leaveTarget !== "settings") leaveOrigin.current?.focus({ preventScroll: true }); }}>
          <Dialog.Title>放弃当前整理吗？</Dialog.Title>
          <Dialog.Description>{isBusy || outcomeUnknown ? "保存请求可能已经成功；离开只会停止本页继续处理，不会撤销服务端写入。" : "已编辑的标题、分类、标签、灵感或转写草稿不会保存，活动录音会立即清理。"}</Dialog.Description>
          <div className="dialog-actions"><Button variant="outline" color="gray" onClick={continueReview}>继续整理</Button><Button color="red" onClick={() => leaveNow(leaveTarget, true)}>{leaveTarget === "settings" ? "放弃并前往设置" : leaveTarget === "existing" ? "放弃并打开已有项" : "放弃并返回素材库"}</Button></div>
        </Dialog.Content>
      </Dialog.Root>
    </div>
  );
}

function PlatformIdentity({ item }: { item: CollectionItem }) {
  const platform = item.platform;
  const icon = platform === "douyin"
    ? <TiktokLogo size={19} weight="fill" aria-hidden />
    : platform === "youtube"
      ? <YoutubeLogo size={20} weight="fill" aria-hidden />
      : platform === "bilibili"
        ? <TelevisionSimple size={19} weight="fill" aria-hidden />
        : platform === "xiaohongshu"
          ? <BookOpenText size={19} weight="fill" aria-hidden />
          : platform === "local_upload"
            ? <FileAudio size={19} weight="fill" aria-hidden />
            : <Globe size={19} aria-hidden />;
  let label = platformNames[platform];
  if ((platform === "web" || platform === "other") && item.source_url) {
    try { label = new URL(item.canonical_url || item.source_url).hostname; } catch { /* use neutral label */ }
  }
  return <span className={`platform-identity collection-platform-${platform}`}>{icon}{label}</span>;
}

const metadataStatusLabels = {
  recognized: "平台公开信息",
  generic: "公开网页信息",
  metadata_unavailable: "普通书签",
} as const;

interface DetailReturnContext {
  token: number;
  scrollY: number;
  focus: "preferences" | "edit";
}

interface SettingsReturnContext extends PageReturnContext {
  source: Route;
  settingsEntryId: string;
  materialId?: string;
  coreRevision?: string;
}

function verifiedSettingsContext(route: Route, contexts: Map<number, SettingsReturnContext>): SettingsReturnContext | null {
  if (route.name !== "settings" || typeof route.returnToken !== "number") return null;
  const context = contexts.get(route.returnToken);
  return context && route.entryId === context.settingsEntryId && route.from === context.source.name
    && route.materialId === context.materialId && route.coreRevision === context.coreRevision
    ? context : null;
}

function validSourceTarget(item: CollectionItem): { target: string; label: string } | null {
  const raw = item.canonical_url || item.source_url;
  if (!raw) return null;
  try {
    const parsed = new URL(raw);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    return {
      target: raw,
      label: item.platform === "web" || item.platform === "other"
        ? parsed.hostname
        : platformNames[item.platform],
    };
  } catch {
    return null;
  }
}

function DetailHeader({ onBack, onSettings }: { onBack: () => void; onSettings: (scrollY?: number) => void }) {
  return <header className="collection-detail-header"><Button variant="ghost" color="gray" onClick={onBack}><ArrowLeft size={20} aria-hidden />返回素材库</Button><BrandWordmark /><PreferencesMenu detail onSettings={onSettings} /></header>;
}

function DetailPage({
  item,
  state,
  error,
  restoreContext,
  onRetry,
  onBack,
  onSettings,
  onRestoreConsumed,
  onEdit,
  background = false,
}: {
  item: CollectionItem | null;
  state: "loading" | "ready" | "error";
  error: string;
  restoreContext: DetailReturnContext | null;
  onRetry: () => void;
  onBack: () => void;
  onSettings: (scrollY?: number) => void;
  onRestoreConsumed: () => void;
  onEdit: (item: CollectionItem) => boolean;
  background?: boolean;
}) {
  const titleRef = useRef<HTMLHeadingElement | null>(null);
  const restoredToken = useRef<number | null>(null);
  const focusedMaterial = useRef<string | null>(null);
  const [sourceFeedback, setSourceFeedback] = useState("");
  const [editLaunchError, setEditLaunchError] = useState("");
  const editLaunchErrorRef = useRef<HTMLParagraphElement | null>(null);
  useEffect(() => { setSourceFeedback(""); setEditLaunchError(""); }, [item?.id]);
  useEffect(() => {
    if (editLaunchError) editLaunchErrorRef.current?.focus({ preventScroll: true });
  }, [editLaunchError]);
  useEffect(() => {
    if (background) return;
    if (state === "error") {
      titleRef.current?.focus({ preventScroll: true });
      if (restoreContext) onRestoreConsumed();
      return;
    }
    if (state !== "ready" || !item) return;
    if (restoreContext && restoredToken.current !== restoreContext.token) {
      focusedMaterial.current = item.id;
      return restorePagePosition(restoreContext.scrollY,
        () => document.querySelector<HTMLElement>(`[data-detail-focus="${restoreContext.focus}"]`)
          ?? titleRef.current,
        () => { restoredToken.current = restoreContext.token; onRestoreConsumed(); });
    }
    if (!restoreContext && focusedMaterial.current !== item.id) {
      focusedMaterial.current = item.id;
      titleRef.current?.focus({ preventScroll: true });
    }
  }, [background, item, onRestoreConsumed, restoreContext, state]);
  if (state === "loading") {
    return <div className="collection-page detail-page"><DetailHeader onBack={onBack} onSettings={onSettings} /><main className="detail-main detail-loading" aria-live="polite"><section className="detail-prelude"><div className="detail-skeleton detail-skeleton-cover" /><div><div className="detail-skeleton detail-skeleton-title" /><p>正在从统一后端读取当前素材的完整核心快照。</p></div></section><section className="detail-organization"><h2>整理结果</h2><div className="detail-skeleton detail-skeleton-line" /></section><section className="detail-inspiration"><h2>我的灵感</h2><div className="detail-skeleton detail-skeleton-line" /></section></main></div>;
  }
  if (state === "error" || !item) {
    return <div className="collection-page detail-page"><DetailHeader onBack={onBack} onSettings={onSettings} /><main className="detail-error" role="alert"><h1 ref={titleRef} tabIndex={-1}>这条素材暂时没有读出来</h1><p>{error || "它可能已被移除，或当前页面链接已经失效。"}</p><div className="dialog-actions"><Button variant="outline" color="gray" onClick={onBack}>返回素材库</Button><Button onClick={onRetry}>重试恢复</Button></div></main></div>;
  }
  const source = validSourceTarget(item);
  const visitSource = () => {
    setSourceFeedback("");
    if (!source) { setSourceFeedback("保存的来源目标无效，未尝试打开其他链接。"); return; }
    try {
      const opened = window.open("", "_blank");
      if (!opened) {
        setSourceFeedback("浏览器阻止了来源页面，请允许此站点打开新标签页后重试。");
        return;
      }
      opened.opener = null;
      opened.location.href = source.target;
      setSourceFeedback("已请求在隔离的新标签页打开来源。");
    } catch {
      setSourceFeedback("未能请求打开来源；详情内容和当前位置没有改变。");
    }
  };
  const confirmation = item.organization_confirmation;
  const visibleSourceTopics = displayedSourceTopics(item);
  const hasOrganization = Boolean(confirmation.primary_category || confirmation.secondary_category || confirmation.organization_tags.length || item.personal_tags.length || visibleSourceTopics.length);
  const launchEdit = () => {
    setEditLaunchError("");
    if (!onEdit(item)) {
      setEditLaunchError("当前标签页无法建立安全编辑会话，请检查浏览器存储后重试。");
    }
  };
  return (
    <div className={`collection-page detail-page ${background ? "is-edit-merge-background" : ""}`} inert={background ? true : undefined} aria-hidden={background || undefined}>
      <DetailHeader onBack={onBack} onSettings={onSettings} />
      <main className="detail-main">
        <section className="detail-prelude">
          <Cover src={item.user_cover_asset_id || item.metadata.cover_url.value ? (background ? cachedItemCoverUrl(item.id) : itemCoverUrl(item.id)) : ""} title={item.display_title} missingLabel={`未获取到${item.source_kind === "video" ? "视频" : "素材"}封面，当前显示占位图。`} />
          <div className="detail-source-body">
            <p className="detail-source-status">{metadataStatusLabels[item.metadata_status]}<span>{provenanceNames[item.metadata.title.source]}</span></p>
            <h1 ref={titleRef} tabIndex={-1}>{item.display_title}</h1>
            <p className="detail-byline"><PlatformIdentity item={item} /><span aria-hidden>·</span><span>{item.source_kind === "video" ? "视频作者" : "作者"}：{item.user_author || item.metadata.author.value || "未获取到作者"}</span>{item.user_author && <small className="user-supplement-badge">用户补充</small>}</p>
            {item.user_author && <p className="detail-source-author">来源作者：{item.metadata.author.value || "未获取到作者"}</p>}
            <button type="button" className="source-visit" disabled={!source} onClick={visitSource}><LinkIcon size={18} aria-hidden />访问{source?.label ?? "来源"}<ArrowSquareOut size={16} aria-hidden /></button>
            {!source && <p className="source-feedback" role="alert">保存的来源目标无效，未尝试打开其他链接。</p>}
            {sourceFeedback && <p className="source-feedback" role={sourceFeedback.startsWith("已请求") ? "status" : "alert"}>{sourceFeedback}</p>}
            <div className="detail-source-copy"><h2>来源文案 <span>{provenanceNames[item.metadata.source_copy.source]}</span></h2>{item.metadata.source_copy.value ? item.metadata.source_copy.value.split(/\n+/).map((paragraph, index) => <p key={`${paragraph}-${index}`}>{paragraph}</p>) : <p className="detail-empty-copy">未获取到来源文案；仍可保存原链接，不会自动编写简介。</p>}</div>
            {item.metadata.warnings.length > 0 && <ul className="detail-source-warnings" aria-label="来源限制">{item.metadata.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>}
          </div>
        </section>
        <section className="detail-collection-content">
          <div>
            <h2>我的收藏内容</h2>
            <p>标题、分类、标签和个人灵感可以随时修改，来源信息始终保持只读。</p>
            {editLaunchError && <p ref={editLaunchErrorRef} className="detail-edit-error" role="alert" tabIndex={-1}>{editLaunchError}</p>}
          </div>
          <Button data-detail-focus="edit" variant="outline" color="gray" onClick={launchEdit}>{editLaunchError ? "重试编辑" : "编辑收藏"}</Button>
        </section>
        <section className="detail-organization">
          <h2>整理结果</h2>
          {hasOrganization ? <div className="detail-tags">
            {confirmation.primary_category && <span className="category-tag"><Funnel size={18} aria-hidden />{confirmation.primary_category}<small>一级</small></span>}
            {confirmation.secondary_category && <span className="category-tag">{confirmation.secondary_category}<small>二级</small></span>}
            {confirmation.organization_tags.map((tag) => <span key={`organization-${tag}`}>{tag}<small>整理</small></span>)}
            {item.personal_tags.map((tag) => <span className="personal-tag" key={`personal-${tag}`}>{tag}<small>个人</small></span>)}
            {visibleSourceTopics.map((tag, index) => <span className="platform-tag" key={"platform-" + index}>{tag.value}<small>{item.selected_source_topic_indices == null ? tag.source === "share_text" ? "分享话题" : "来源标签" : "来源话题"}</small></span>)}
          </div> : <p className="detail-empty-organization">还没有已确认的整理结果。</p>}
        </section>
        <section className="detail-inspiration">
          <h2>我的灵感</h2>
          {item.inspiration ? item.inspiration.content.split(/\n+/).map((paragraph, index) => <p key={`${paragraph}-${index}`}>{paragraph}</p>) : <p className="empty-inspiration">还没有留下个人灵感。来源文案不会代替你的想法。</p>}
        </section>
      </main>
    </div>
  );
}

function SettingsPage({ from, onBack }: { from: "library" | "review" | "detail"; onBack: () => void }) {
  const { appearance, accent, restoreState, mutation, setAppearance, setAccent } = useThemePreferences();
  const origin = useId();
  const mobile = useMobileViewport();
  const titleRef = useRef<HTMLHeadingElement | null>(null);
  const [announcement, setAnnouncement] = useState({ identity: "", polite: "", assertive: "" });
  const notice = restoreState === "missing_default"
    ? "未找到已保存的完整外观组合，当前使用浅色与靛蓝安全默认。"
    : restoreState === "invalid_default"
      ? "当前浏览器的外观偏好损坏、不完整、版本不兼容或无法读取，已整套使用浅色与靛蓝安全默认。收藏、个人灵感与后端数据不受影响；原偏好未被覆盖。"
      : "";
  const ownedMutation = mutation.origin === origin;
  const failed = ownedMutation && mutation.phase === "failed";
  const status = ownedMutation && mutation.phase === "applying" ? "正在保存当前外观组合…"
    : ownedMutation && mutation.phase === "applied" ? "外观偏好已保存在当前浏览器。" : "";
  const failure = "外观偏好未能保存，已整套恢复上一次确认的组合。你可以重新选择；收藏与个人灵感不受影响。";
  const announcementIdentity = `${ownedMutation ? mutation.token : 0}:${ownedMutation ? mutation.phase : "ready"}`;
  useEffect(() => { titleRef.current?.focus({ preventScroll: true }); }, []);
  useEffect(() => {
    // One route-scoped channel coalesces ready/default/applying/success. Failures
    // have their own latest-token identity, and never take focus from an option.
    if (failed) {
      setAnnouncement({ identity: announcementIdentity, polite: "", assertive: failure });
    } else {
      setAnnouncement({ identity: announcementIdentity, polite: status || notice || "当前外观偏好已就绪。", assertive: "" });
    }
  }, [announcementIdentity, failed, notice, status]);
  const returnLabel = from === "review" ? (mobile ? "返回整理" : "返回保存整理")
    : from === "detail" ? (mobile ? "返回详情" : "返回素材详情") : "返回素材库";
  const applyAppearance = (value: "light" | "dark") => { setAppearance(value, origin); };
  const applyAccent = (value: "indigo" | "teal" | "orange") => { setAccent(value, origin); };
  return (
    <div className="collection-page settings-page">
      <header className="settings-header"><BrandWordmark /><Button variant="ghost" color="gray" onClick={onBack}><ArrowLeft size={20} aria-hidden />{returnLabel}</Button></header>
      <main className="settings-main">
        <aside className="settings-intro">
          <h1 ref={titleRef} tabIndex={-1}>隐私与存储设置</h1>
          <p>外观随你，保存边界清楚可见。<br />收藏与个人灵感保存在统一后端；这里的外观选择仅保存在当前浏览器。</p>
        </aside>
        <div className="settings-content">
          <section className="appearance-settings" aria-labelledby="settings-appearance-title">
            <div className="appearance-row">
              <div className="settings-section-heading"><Palette size={25} aria-hidden /><div><h2 id="settings-appearance-title">外观偏好</h2><p>选择你偏好的界面主题。</p></div></div>
              <div className="choice-grid two-choices" role="radiogroup" aria-label="主题">{(["light", "dark"] as const).map((value, index, values) => <button key={value} type="button" role="radio" aria-checked={appearance === value} tabIndex={appearance === value ? 0 : -1} onKeyDown={(event) => moveRadioSelection(event, values, index, applyAppearance)} onClick={() => applyAppearance(value)}><span className="choice-check" aria-hidden>{appearance === value && <Check size={15} weight="bold" />}</span>{value === "light" ? <Sun size={21} aria-hidden /> : <Moon size={21} aria-hidden />}{value === "light" ? "浅色" : "深色"}</button>)}</div>
            </div>
            <div className="appearance-row accent-row">
              <div className="settings-section-heading"><Palette size={25} aria-hidden /><div><h3>强调色</h3><p>选择界面中使用的强调色。</p></div></div>
              <div className="choice-grid three-choices" role="radiogroup" aria-label="强调色">{(["indigo", "teal", "orange"] as const).map((value, index, values) => <button key={value} type="button" role="radio" aria-checked={accent === value} tabIndex={accent === value ? 0 : -1} onKeyDown={(event) => moveRadioSelection(event, values, index, applyAccent)} onClick={() => applyAccent(value)}><span className="choice-check" aria-hidden>{accent === value && <Check size={15} weight="bold" />}</span><span className={`accent-dot dot-${value}`} aria-hidden />{value === "indigo" ? "靛蓝" : value === "teal" ? "青绿" : "暖橙"}</button>)}</div>
            </div>
            <div className="preference-preview" aria-label={`当前外观：${appearance === "light" ? "浅色" : "深色"}，${accent === "indigo" ? "靛蓝" : accent === "teal" ? "青绿" : "暖橙"}`}><Check size={18} aria-hidden /><span>当前组合</span><strong>{appearance === "light" ? "浅色" : "深色"} · {accent === "indigo" ? "靛蓝" : accent === "teal" ? "青绿" : "暖橙"}</strong></div>
            {notice && <p className="preference-storage-note">{notice}</p>}
            {status && <p className="preference-status">{status}</p>}
            {failed && <p className="preference-error">{failure}</p>}
            <p className="sr-only" role="status" aria-atomic="true">{announcement.polite && <span key={announcement.identity}>{announcement.polite}</span>}</p>
            <p className="sr-only" role="alert" aria-atomic="true">{announcement.assertive && <span key={announcement.identity}>{announcement.assertive}</span>}</p>
          </section>
          <section className="policy-section" aria-labelledby="settings-privacy-title"><h2 id="settings-privacy-title"><ShieldCheck size={25} aria-hidden />隐私保护</h2><ul><li><strong>只访问公开来源</strong><span>不绕过登录、验证码、付费墙、DRM 或平台风控。</span></li><li><strong>个人灵感独立保存</strong><span>不会把来源文案或自动建议伪装成你的想法。</span></li><li><strong>语音临时处理</strong><span>只转写你主动录制的灵感，不读取来源素材音轨；完成、失败、取消或超时后清理临时音频。</span></li><li><strong>日志最小化</strong><span>日志不记录令牌、Cookie、完整私密路径或用户敏感内容。</span></li></ul></section>
          <section className="policy-section" aria-labelledby="settings-storage-title"><h2 id="settings-storage-title"><Database size={25} aria-hidden />存储边界</h2><ul><li><strong>统一后端</strong><span>保存收藏、来源信息、整理结果与个人灵感。</span></li><li><strong>当前浏览器</strong><span>只保存主题和强调色等外观偏好，不替代后端素材库。</span></li><li><strong>用户补充封面</strong><span>你主动上传并保存到收藏的封面由后端保留；临时录音处理后清理，不随收藏保留。</span></li></ul></section>
        </div>
      </main>
    </div>
  );
}

export function CollectionApp() {
  const [route, setRoute] = useState<Route>(() => {
    const initial = initialRoute();
    return initial.entryId ? initial : routeEntry(initial);
  });
  const [captureText, setCaptureText] = useState("");
  const [captureError, setCaptureError] = useState("");
  const [reviewSession, setReviewSession] = useState<{ entryId: string; inputText: string } | null>(null);
  const reviewSessionRef = useRef(reviewSession);
  const expiredReviewEntries = useRef(new Set<string>());
  const [sourceRestore, setSourceRestore] = useState<PageReturnContext | null>(null);
  const consumeSourceRestore = useCallback(() => setSourceRestore(null), []);
  const consumeDetailRestore = useCallback(() => setDetailRestore(null), []);
  const [libraryItems, setLibraryItems] = useState<CollectionListItem[]>([]);
  const [libraryFacets, setLibraryFacets] = useState<CollectionListFacets>(emptyFacets);
  const [libraryTotal, setLibraryTotal] = useState(0);
  const [libraryStatus, setLibraryStatus] = useState<"loading" | "ready" | "error" | "loading-more">("loading");
  const [libraryError, setLibraryError] = useState("");
  const [loadMoreError, setLoadMoreError] = useState("");
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [libraryQuery, setLibraryQuery] = useState<LibraryQueryState>({
    query: "",
    primaryCategory: "",
    secondaryCategory: "",
    tag: "",
  });
  const [searchText, setSearchText] = useState("");
  const [searchComposing, setSearchComposing] = useState(false);
  const [libraryRefresh, setLibraryRefresh] = useState(0);
  const refreshAfterUnknownDiscard = useRef(false);
  const markUnknownSaveDiscard = useCallback(() => { refreshAfterUnknownDiscard.current = true; }, []);
  const [savedItem, setSavedItem] = useState<CollectionItem | null>(null);
  const [detailItems, setDetailItems] = useState<Record<string, CollectionItem>>({});
  const [detailStatus, setDetailStatus] = useState<"loading" | "ready" | "error">("loading");
  const [detailError, setDetailError] = useState("");
  const [detailRefresh, setDetailRefresh] = useState(0);
  const [detailRestore, setDetailRestore] = useState<DetailReturnContext | null>(null);
  const committedDetail = useRef<CollectionItem | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);
  const [captureFocusToken, setCaptureFocusToken] = useState(0);
  const detailGeneration = useRef(0);
  const settingsReturnContexts = useRef(new Map<number, SettingsReturnContext>());
  const batchReturnContext = useRef<{ source: Route; context: PageReturnContext } | null>(null);
  const batchListFocusRestore = useRef<string | null>(null);
  const batchExistingReturnContext = useRef<{
    detailEntryId: string;
    source: Extract<Route, { name: "batch" }>;
    scrollY: number;
    restoreToken: number;
  } | null>(null);
  const detailReturnToken = useRef(0);
  const currentRoute = useRef<Route>(route);
  const reviewLeaveGuard = useRef<(() => boolean) | null>(null);
  const editMergeLeaveGuard = useRef<(() => boolean) | null>(null);
  const batchLeaveGuard = useRef<(() => boolean) | null>(null);
  const shareLeaveGuard = useRef<((leave: () => void) => void) | null>(null);
  const ownedHistory = useRef(new Map([[route.entryId!, 0]]));
  const historyPosition = useRef(0);
  const shareHistoryReturn = useRef<{ target: Route; delta: number } | null>(null);
  const approvedShareHistory = useRef<string | null>(null);
  const editMergeReturnInFlight = useRef<string | null>(null);
  const editMergeReturnContext = useRef<{
    editEntryId: string;
    source: Route;
    scrollY: number;
    detailContext: DetailReturnContext | null;
  } | null>(null);
  const editMergeSuccessContext = useRef<{
    editEntryId: string;
    itemId: string;
    canConsumeEditEntry: boolean;
  } | null>(null);
  const libraryScroll = useRef(0);
  const listGeneration = useRef(0);
  const preserveLibraryOnRefresh = useRef(false);
  const returnInFlight = useRef<string | null>(null);
  const clearReviewSession = useCallback(() => {
    if (reviewSessionRef.current) expiredReviewEntries.current.add(reviewSessionRef.current.entryId);
    reviewSessionRef.current = null;
    setReviewSession(null);
    reviewLeaveGuard.current = null;
  }, []);
  const safeLibraryRoute = useCallback((): Route => {
    clearReviewSession();
    setSourceRestore(null);
    setDetailRestore(null);
    setFocusId(null);
    setCaptureFocusToken(0);
    setCaptureText("");
    setCaptureError("");
    setSearchText("");
    setSearchComposing(false);
    setLibraryQuery({ query: "", primaryCategory: "", secondaryCategory: "", tag: "" });
    libraryScroll.current = 0;
    return routeEntry({ name: "library" });
  }, [clearReviewSession]);
  const restoreLibraryPosition = useCallback(() => {
    const entryId = currentRoute.current.entryId;
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        if (currentRoute.current.name !== "library" || currentRoute.current.entryId !== entryId) return;
        window.scrollTo({ top: libraryScroll.current, behavior: "auto" });
      });
    });
  }, []);
  const registerReviewLeaveGuard = useCallback((guard: (() => boolean) | null) => {
    reviewLeaveGuard.current = guard;
  }, []);
  const registerEditMergeLeaveGuard = useCallback((guard: (() => boolean) | null) => {
    editMergeLeaveGuard.current = guard;
  }, []);
  const registerBatchLeaveGuard = useCallback((guard: (() => boolean) | null) => {
    batchLeaveGuard.current = guard;
  }, []);
  const registerShareLeaveGuard = useCallback((guard: ((leave: () => void) => void) | null) => {
    shareLeaveGuard.current = guard;
  }, []);
  useEffect(() => { currentRoute.current = route; }, [route]);

  const materials = useMemo(
    () => libraryItems.map(collectionListCard),
    [libraryItems],
  );

  const apiQuery = useMemo<CollectionListQuery>(() => ({
    query: libraryQuery.query || undefined,
    platform: libraryQuery.platform,
    primaryCategory: libraryQuery.primaryCategory || undefined,
    secondaryCategory: libraryQuery.secondaryCategory || undefined,
    tag: libraryQuery.tag || undefined,
    tagSource: libraryQuery.tagSource,
    limit: 24,
  }), [libraryQuery]);
  const apiQueryKey = JSON.stringify(apiQuery);

  useEffect(() => {
    if (route.name !== "library" || searchComposing) return undefined;
    if (!searchText && libraryQuery.query) {
      setLibraryQuery((value) => ({ ...value, query: "" }));
      return undefined;
    }
    const timer = window.setTimeout(() => {
      setLibraryQuery((value) => value.query === searchText ? value : { ...value, query: searchText });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [libraryQuery.query, route.name, searchComposing, searchText]);

  useEffect(() => {
    if (route.name !== "library" || !refreshAfterUnknownDiscard.current) return;
    refreshAfterUnknownDiscard.current = false;
    // Discarding local response ownership is not cancellation of the backend save.
    // Read with a new query generation only when the library actually returns.
    preserveLibraryOnRefresh.current = false;
    setLibraryRefresh((value) => value + 1);
  }, [route.entryId, route.name]);

  const shareActive = route.name === "share";
  useEffect(() => {
    if (shareActive) return undefined;
    if (refreshAfterUnknownDiscard.current && currentRoute.current.name !== "library") return undefined;
    const controller = new AbortController();
    const generation = listGeneration.current + 1;
    listGeneration.current = generation;
    const preserveSnapshot = preserveLibraryOnRefresh.current;
    preserveLibraryOnRefresh.current = false;
    if (!preserveSnapshot) setLibraryStatus("loading");
    setLibraryError("");
    setLoadMoreError("");
    collectionApi.listItems(apiQuery, controller.signal)
      .then((page) => {
        if (controller.signal.aborted || listGeneration.current !== generation) return;
        setLibraryItems(page.items);
        setLibraryFacets(page.facets);
        setLibraryTotal(page.total);
        setNextCursor(page.next_cursor);
        setLibraryStatus("ready");
      })
      .catch((error) => {
        if (controller.signal.aborted || listGeneration.current !== generation) return;
        setLibraryError(error instanceof Error ? error.message : "素材库暂时无法读取，请重试。");
        if (preserveSnapshot) {
          setLibraryStatus("ready");
        } else {
          setLibraryItems([]);
          setLibraryStatus("error");
        }
      });
    return () => controller.abort();
  }, [apiQueryKey, libraryRefresh, shareActive]);

  const loadMore = useCallback(() => {
    if (!nextCursor || libraryStatus === "loading-more") return;
    const generation = listGeneration.current;
    setLibraryStatus("loading-more");
    setLoadMoreError("");
    collectionApi.listItems({ ...apiQuery, cursor: nextCursor })
      .then((page) => {
        if (listGeneration.current !== generation) return;
        setLibraryItems((items) => {
          const known = new Set(items.map((item) => item.id));
          return [...items, ...page.items.filter((item) => !known.has(item.id))];
        });
        setLibraryFacets(page.facets);
        setLibraryTotal(page.total);
        setNextCursor(page.next_cursor);
        setLibraryStatus("ready");
      })
      .catch((error) => {
        if (listGeneration.current !== generation) return;
        setLoadMoreError(error instanceof Error ? error.message : "更多素材暂时没有加载出来。");
        setLibraryStatus("ready");
      });
  }, [apiQueryKey, libraryStatus, nextCursor]);

  const detailRouteId = route.name === "detail" ? route.id : "";
  useEffect(() => {
    if (!detailRouteId) return undefined;
    if (committedDetail.current?.id === detailRouteId) {
      const authoritative = committedDetail.current;
      committedDetail.current = null;
      setDetailItems((items) => ({ ...items, [authoritative.id]: authoritative }));
      setDetailStatus("ready");
      setDetailError("");
      return undefined;
    }
    const currentGeneration = detailGeneration.current + 1;
    detailGeneration.current = currentGeneration;
    const controller = new AbortController();
    setDetailStatus("loading");
    setDetailError("");
    collectionApi.getItem(detailRouteId, controller.signal)
      .then((item) => {
        if (controller.signal.aborted || detailGeneration.current !== currentGeneration) return;
        if (item.id !== detailRouteId) {
          setDetailError("素材身份已经变化，未恢复旧内容；请返回素材库重新打开。");
          setDetailStatus("error");
          return;
        }
        setDetailItems((items) => ({ ...items, [item.id]: item }));
        setDetailStatus("ready");
      })
      .catch((error) => {
        if (controller.signal.aborted || detailGeneration.current !== currentGeneration) return;
        setDetailError(error instanceof Error ? error.message : "素材暂时无法恢复，请重试。");
        setDetailStatus("error");
      });
    return () => controller.abort();
  }, [detailRefresh, detailRouteId]);

  const navigate = useCallback((target: Route, replace = false) => {
    const next = target.entryId ? target : routeEntry(target);
    // History commit can fail. Do not grant a review request owner beforehand.
    window.history[replace ? "replaceState" : "pushState"]({ collectionRoute: next }, "", routePath(next));
    const nextPosition = historyPosition.current + (replace ? 0 : 1);
    if (replace) ownedHistory.current.delete(currentRoute.current.entryId!);
    if (!replace) {
      for (const [entryId, position] of ownedHistory.current) {
        if (position > historyPosition.current) ownedHistory.current.delete(entryId);
      }
    }
    ownedHistory.current.set(next.entryId!, nextPosition);
    historyPosition.current = nextPosition;
    currentRoute.current = next;
    returnInFlight.current = null;
    setRoute(next);
    try { window.scrollTo({ top: 0, behavior: "auto" }); } catch { /* Navigation is already committed. */ }
  }, []);

  useEffect(() => {
    window.history.replaceState({ collectionRoute: route }, "", routePath(route));
    const onPopState = (event: PopStateEvent) => {
      const retired = retiredAnalysisRoute(event.state?.collectionRoute);
      const next = retired ? routeEntry(retired) : event.state?.collectionRoute as Route | undefined;
      const leaving = currentRoute.current;
      const pendingShareReturn = shareHistoryReturn.current;
      if (pendingShareReturn && next?.entryId === leaving.entryId) {
        shareHistoryReturn.current = null;
        let executed = false;
        const leave = () => {
          if (executed || currentRoute.current.entryId !== leaving.entryId) return;
          executed = true;
          approvedShareHistory.current = pendingShareReturn.target.entryId!;
          getShareSession().end();
          window.history.go(pendingShareReturn.delta);
        };
        if (shareLeaveGuard.current) shareLeaveGuard.current(leave);
        else leave();
        return;
      }
      if (leaving.name === "share" && next?.entryId !== leaving.entryId) {
        const targetPosition = next?.entryId ? ownedHistory.current.get(next.entryId) : undefined;
        const approved = Boolean(next?.entryId && approvedShareHistory.current === next.entryId);
        approvedShareHistory.current = null;
        if (!approved && targetPosition !== undefined && shareLeaveGuard.current) {
          const delta = targetPosition - historyPosition.current;
          if (delta !== 0) {
            shareHistoryReturn.current = { target: next!, delta };
            window.history.go(-delta);
            return;
          }
        }
        getShareSession().end();
      }
      if (next?.entryId && ownedHistory.current.has(next.entryId)) {
        historyPosition.current = ownedHistory.current.get(next.entryId)!;
      }
      const committedSuccess = leaving.name === "edit-merge"
        && editMergeSuccessContext.current?.editEntryId === (leaving.entryId ?? "completed-edit-merge")
        && editMergeSuccessContext.current.itemId === leaving.id;
      if (committedSuccess) {
        const resolved = routeEntry({ name: "detail", id: leaving.id });
        editMergeSuccessContext.current = null;
        editMergeReturnContext.current = null;
        editMergeReturnInFlight.current = null;
        editMergeLeaveGuard.current = null;
        setDetailRestore(null);
        setDetailStatus("ready");
        currentRoute.current = resolved;
        setRoute(resolved);
        window.history.replaceState({ collectionRoute: resolved }, "", routePath(resolved));
        window.scrollTo({ top: 0, behavior: "auto" });
        return;
      }
      const guard = leaving.name === "review"
        ? reviewLeaveGuard.current
        : leaving.name === "edit-merge" && editMergeReturnInFlight.current !== leaving.entryId
          ? editMergeLeaveGuard.current
          : leaving.name === "batch"
            ? batchLeaveGuard.current
            : null;
      if (guard?.()) {
        window.history.pushState({ collectionRoute: leaving }, "", routePath(leaving));
        return;
      }
      if (leaving.name === "review" && guard) setCaptureFocusToken((value) => value + 1);
      let resolved: Route = next ?? routeEntry({ name: "library" });
      let safeFallback = false;
      let restoredSettingsSource = false;
      if (retired) {
        setDetailRestore(null);
        if (leaving.name === "settings" && typeof leaving.returnToken === "number") {
          settingsReturnContexts.current.delete(leaving.returnToken);
        }
      }
      if (leaving.name === "edit-merge") {
        const context = editMergeReturnContext.current;
        const valid = Boolean(context && context.editEntryId === leaving.entryId && sameRouteEntry(resolved, context.source));
        editMergeReturnContext.current = null;
        if (valid && context) {
          restoredSettingsSource = true;
          if (resolved.name === "detail") {
            setDetailRestore(context.detailContext);
            setDetailStatus(detailItems[resolved.id]?.id === resolved.id ? "ready" : "loading");
          } else if (resolved.name === "review") {
            setSourceRestore({ token: ++detailReturnToken.current, scrollY: context.scrollY, focus: "merge" });
          }
        }
        editMergeSuccessContext.current = null;
      } else if (leaving.name === "batch") {
        if (resolved.name === "batch") {
          const focusItemId = batchListFocusRestore.current ?? resolved.anchorItemId;
          if (focusItemId && resolved.view === "list") {
            resolved = { ...resolved, focus: `item:${focusItemId}` };
            window.history.replaceState({ collectionRoute: resolved }, "", routePath(resolved));
          }
          batchListFocusRestore.current = null;
        } else if (resolved.name === "library") {
          const context = batchReturnContext.current;
          batchReturnContext.current = null;
          batchListFocusRestore.current = null;
          if (context && sameRouteEntry(resolved, context.source)) {
            setSourceRestore(context.context);
            restoredSettingsSource = true;
          }
        }
      } else if (leaving.name === "detail" && resolved.name === "batch") {
        const context = batchExistingReturnContext.current;
        batchExistingReturnContext.current = null;
        if (context
          && context.detailEntryId === leaving.entryId
          && sameRouteEntry(resolved, context.source)) {
          resolved = {
            ...resolved,
            focus: context.source.itemId ? `existing:${context.source.itemId}` : "item-heading",
            restoreScrollY: context.scrollY,
            restoreToken: context.restoreToken,
          };
          restoredSettingsSource = true;
        }
      } else if (leaving.name === "settings" && !retired) {
        const context = verifiedSettingsContext(leaving, settingsReturnContexts.current);
        const valid = context && sameRouteEntry(resolved, context.source)
          && (resolved.name !== "review" || (reviewSessionRef.current?.entryId === resolved.entryId && !expiredReviewEntries.current.has(resolved.entryId!)));
        if (typeof leaving.returnToken === "number") settingsReturnContexts.current.delete(leaving.returnToken);
        if (valid && context) {
          restoredSettingsSource = true;
          if (resolved.name === "detail") {
            setDetailRestore({ token: context.token, scrollY: context.scrollY, focus: "preferences" });
            setDetailStatus("loading");
          } else {
            setSourceRestore({ token: context.token, scrollY: context.scrollY, focus: context.focus });
          }
        } else {
          resolved = safeLibraryRoute();
          safeFallback = true;
        }
      }
      if ((resolved.name === "review" && (reviewSessionRef.current?.entryId !== resolved.entryId || expiredReviewEntries.current.has(resolved.entryId!)))
        || (resolved.name === "settings" && !verifiedSettingsContext(resolved, settingsReturnContexts.current))) {
        resolved = safeLibraryRoute();
        safeFallback = true;
      }
      if (leaving.name === "review" && resolved.name !== "review" && resolved.name !== "settings") clearReviewSession();
      if (safeFallback || retired) window.history.replaceState({ collectionRoute: resolved }, "", routePath(resolved));
      if (safeFallback && next?.entryId && ownedHistory.current.has(next.entryId)) {
        ownedHistory.current.delete(next.entryId);
        ownedHistory.current.set(resolved.entryId!, historyPosition.current);
      }
      currentRoute.current = resolved;
      editMergeReturnInFlight.current = null;
      returnInFlight.current = null;
      setRoute(resolved);
      if (resolved.name === "library" && !safeFallback && !restoredSettingsSource) {
        restoreLibraryPosition();
      } else if (!restoredSettingsSource) {
        window.scrollTo({ top: 0, behavior: "auto" });
      }
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [clearReviewSession, restoreLibraryPosition, safeLibraryRoute]);

  const createDetailReturnContext = (focus: DetailReturnContext["focus"]): DetailReturnContext | null => {
    if (route.name !== "detail") return null;
    detailReturnToken.current += 1;
    return {
      token: detailReturnToken.current,
      scrollY: window.scrollY,
      focus,
    };
  };

  const openSettings = (discardedReview = false, sourceScrollY?: number) => {
    const sourceRoute = currentRoute.current;
    if (sourceRoute.name === "settings") return;
    let source: Route = sourceRoute;
    let fallback = false;
    if (discardedReview || (source.name === "detail" && (detailStatus !== "ready" || detailItems[source.id]?.id !== source.id))) {
      source = safeLibraryRoute();
      fallback = true;
      window.history.replaceState({ collectionRoute: source }, "", routePath(source));
    }
    if (source.name !== "library" && source.name !== "review" && source.name !== "detail") return;
    const context: SettingsReturnContext = {
      token: ++detailReturnToken.current,
      source,
      settingsEntryId: makeIdempotencyKey(),
      scrollY: fallback ? 0 : sourceScrollY ?? window.scrollY,
      focus: fallback ? "title" : "preferences",
      ...(source.name === "detail" ? {
        materialId: source.id,
        coreRevision: detailItems[source.id].updated_at,
      } : {}),
    };
    settingsReturnContexts.current.clear();
    settingsReturnContexts.current.set(context.token, context);
    setSourceRestore(null);
    navigate({ name: "settings", entryId: context.settingsEntryId, from: source.name, returnToken: context.token,
      materialId: context.materialId, coreRevision: context.coreRevision });
  };

  const openPageSettings = (scrollY?: number) => openSettings(false, scrollY);

  const returnFromSettings = () => {
    const leaving = currentRoute.current;
    if (leaving.name !== "settings" || returnInFlight.current === leaving.entryId) return;
    returnInFlight.current = leaving.entryId ?? "direct-settings";
    if (verifiedSettingsContext(leaving, settingsReturnContexts.current)) {
      window.history.back();
    } else {
      settingsReturnContexts.current.clear();
      navigate(safeLibraryRoute(), true);
    }
  };

  const leaveReview = (discarded = false) => {
    clearReviewSession();
    setFocusId(null);
    setCaptureFocusToken((value) => value + 1);
    if (!discarded) {
      navigate({ name: "library" });
      return;
    }
    setCaptureText("");
    navigate({ name: "library" }, true);
  };

  const openDetail = (id: string) => {
    if (currentRoute.current.name === "review") clearReviewSession();
    libraryScroll.current = window.scrollY;
    setFocusId(id);
    setDetailRestore(null);
    setDetailStatus("loading");
    navigate({ name: "detail", id });
  };

  const openBatchExisting = (id: string) => {
    const source = currentRoute.current;
    if (source.name !== "batch" || source.view !== "item") return;
    const next = routeEntry({ name: "detail", id });
    const scrollY = window.scrollY;
    const restoreToken = ++detailReturnToken.current;
    const sourceForReturn: Extract<Route, { name: "batch" }> = {
      ...source,
      focus: source.itemId ? `existing:${source.itemId}` : "item-heading",
      restoreScrollY: scrollY,
      restoreToken,
    };
    window.history.replaceState({ collectionRoute: sourceForReturn }, "", routePath(sourceForReturn));
    batchExistingReturnContext.current = {
      detailEntryId: next.entryId!,
      source: sourceForReturn,
      scrollY,
      restoreToken,
    };
    setDetailRestore(null);
    setFocusId(id);
    setDetailStatus("loading");
    navigate(next);
  };

  const openEdit = (item: CollectionItem): boolean => {
    const source = currentRoute.current;
    if (source.name !== "detail" || source.id !== item.id) return false;
    const next = routeEntry({ name: "edit-merge", id: item.id, mode: "edit" });
    const base = snapshotFromItem(item);
    const session: EditMergeSession = {
      version: 1,
      mode: "edit",
      itemId: item.id,
      routeEntryId: next.entryId!,
      returnKind: "detail",
      reviewEntryId: null,
      base,
      desired: base,
      incoming: null,
      expectedRevision: item.revision,
      requestState: "draft",
      conflictRecovery: null,
      updatedAt: Date.now(),
    };
    if (!writeEditMergeSession(session)) return false;
    editMergeReturnContext.current = {
      editEntryId: next.entryId!,
      source,
      scrollY: window.scrollY,
      detailContext: createDetailReturnContext("edit"),
    };
    navigate(next);
    return true;
  };

  const openMergeExisting = (id: string, payload: CollectionItemCreate, userCoverClaimToken: string | null): boolean => {
    const source = currentRoute.current;
    if (source.name !== "review" || !reviewSessionRef.current || source.entryId !== reviewSessionRef.current.entryId) return false;
    const next = routeEntry({ name: "edit-merge", id, mode: "merge" });
    const session: EditMergeSession = {
      version: 1,
      mode: "merge",
      itemId: id,
      routeEntryId: next.entryId!,
      returnKind: "review",
      reviewEntryId: source.entryId ?? null,
      base: null,
      desired: null,
      incoming: snapshotFromCreate(payload),
      expectedRevision: null,
      requestState: "draft",
      conflictRecovery: null,
      updatedAt: Date.now(),
      userCoverClaimToken,
    };
    if (!writeEditMergeSession(session)) return false;
    editMergeReturnContext.current = {
      editEntryId: next.entryId!,
      source,
      scrollY: window.scrollY,
      detailContext: null,
    };
    navigate(next);
    return true;
  };

  const returnFromEditMerge = (returnKind: EditMergeReturnKind) => {
    const leaving = currentRoute.current;
    if (leaving.name !== "edit-merge" || editMergeReturnInFlight.current === leaving.entryId) return;
    const context = editMergeReturnContext.current;
    const valid = Boolean(
      context
      && context.editEntryId === leaving.entryId
      && ((returnKind === "detail" && context.source.name === "detail")
        || (returnKind === "review" && context.source.name === "review" && reviewSessionRef.current?.entryId === context.source.entryId)),
    );
    if (valid) {
      editMergeReturnInFlight.current = leaving.entryId ?? "direct-edit-merge";
      window.history.back();
      return;
    }
    editMergeReturnContext.current = null;
    setDetailRestore(null);
    setDetailStatus(detailItems[leaving.id]?.id === leaving.id ? "ready" : "loading");
    navigate({ name: "detail", id: leaving.id }, true);
  };

  const returnMissingEditMergeToLibrary = () => {
    const leaving = currentRoute.current;
    if (leaving.name !== "edit-merge") return;
    clearEditMergeSession(leaving.mode, leaving.id);
    if (leaving.mode === "merge") clearReviewSession();
    editMergeReturnContext.current = null;
    editMergeSuccessContext.current = null;
    editMergeLeaveGuard.current = null;
    setDetailRestore(null);
    preserveLibraryOnRefresh.current = false;
    setLibraryRefresh((value) => value + 1);
    navigate({ name: "library" }, true);
    restoreLibraryPosition();
  };

  const commitEditMergeSuccess = (item: CollectionItem) => {
    const leaving = currentRoute.current;
    if (leaving.name !== "edit-merge" || leaving.id !== item.id) return;
    if (editMergeSuccessContext.current?.editEntryId === leaving.entryId) return;
    const returnContext = editMergeReturnContext.current;
    const canConsumeEditEntry = Boolean(
      leaving.mode === "edit"
      && returnContext
      && returnContext.editEntryId === leaving.entryId
      && returnContext.source.name === "detail"
      && returnContext.source.id === item.id,
    );
    const editEntryId = leaving.entryId ?? "completed-edit-merge";
    editMergeSuccessContext.current = { editEntryId, itemId: item.id, canConsumeEditEntry };
    editMergeReturnInFlight.current = editEntryId;
    clearEditMergeSession(leaving.mode, leaving.id);
    if (leaving.mode === "merge") clearReviewSession();
    editMergeLeaveGuard.current = null;
    setDetailItems((items) => ({ ...items, [item.id]: item }));
    setDetailStatus("ready");
    setDetailError("");
    setDetailRestore(null);
    setSavedItem(item);
    committedDetail.current = item;
    setFocusId(item.id);
    preserveLibraryOnRefresh.current = false;
    setLibraryRefresh((value) => value + 1);
  };

  const completeEditMerge = (item: CollectionItem) => {
    const completion = editMergeSuccessContext.current;
    const leaving = currentRoute.current;
    if (!completion || completion.itemId !== item.id) return;
    if (leaving.name !== "edit-merge" || leaving.id !== item.id || (leaving.entryId ?? "completed-edit-merge") !== completion.editEntryId) {
      editMergeSuccessContext.current = null;
      return;
    }
    editMergeSuccessContext.current = null;
    if (completion.canConsumeEditEntry) {
      window.history.back();
      return;
    }
    editMergeReturnContext.current = null;
    editMergeReturnInFlight.current = null;
    navigate({ name: "detail", id: item.id }, true);
  };

  const returnToLibrary = () => {
    batchExistingReturnContext.current = null;
    setDetailRestore(null);
    navigate({ name: "library" }, true);
    restoreLibraryPosition();
  };

  const openReview = (inputText = captureText) => {
    libraryScroll.current = window.scrollY;
    const next = routeEntry({ name: "review" });
    const session = { entryId: next.entryId!, inputText };
    navigate(next);
    reviewSessionRef.current = session;
    setReviewSession(session);
    setSourceRestore(null);
  };

  const acceptShare = (inputText: string) => {
    if (currentRoute.current.name !== "share" || getShareSession().getSnapshot().status !== "ready") {
      throw new Error("分享接收会话已失效，请重新分享。");
    }
    openReview(inputText);
    getShareSession().end();
  };

  const leaveShare = () => {
    getShareSession().end();
    setCaptureText("");
    setCaptureError("");
    setCaptureFocusToken((value) => value + 1);
    navigate({ name: "library" }, true);
  };

  const openBatchImport = () => {
    if (route.name !== "library") return;
    libraryScroll.current = window.scrollY;
    batchReturnContext.current = {
      source: route,
      context: { token: ++detailReturnToken.current, scrollY: window.scrollY, focus: "batch" },
    };
    setSourceRestore(null);
    navigate({ name: "batch", view: "list", batchDepth: 0 });
  };

  const changeBatchRoute = (next: CollectionImportRouteState, replace = false) => {
    const current = currentRoute.current;
    if (current.name === "batch" && next.view === "confirm" && !replace) {
      const returnSnapshot: Route = {
        ...current,
        focus: "confirm-trigger",
        restoreScrollY: window.scrollY,
        restoreToken: (current.restoreToken ?? 0) + 1,
      };
      window.history.replaceState({ collectionRoute: returnSnapshot }, "", routePath(returnSnapshot));
    }
    if (current.name === "batch" && current.view === "list" && next.view === "item" && !replace) {
      const listSnapshot: Route = {
        ...current,
        focus: next.itemId ? `item:${next.itemId}` : current.focus,
        anchorItemId: next.anchorItemId,
        anchorOffset: next.anchorOffset,
        trigger: next.trigger,
      };
      window.history.replaceState({ collectionRoute: listSnapshot }, "", routePath(listSnapshot));
    }
    const depth = current.name === "batch"
      ? replace ? current.batchDepth ?? 0 : (current.batchDepth ?? 0) + 1
      : 0;
    navigate({ name: "batch", ...next, batchDepth: depth }, replace);
  };

  const returnFromBatchToLibrary = () => {
    const context = batchReturnContext.current;
    const current = currentRoute.current;
    if (context && current.name === "batch") {
      window.history.go(-((current.batchDepth ?? 0) + 1));
      return;
    }
    batchReturnContext.current = null;
    batchListFocusRestore.current = null;
    setSourceRestore(null);
    navigate({ name: "library" }, true);
  };

  const returnToBatchList = (batchItemId: string) => {
    const current = currentRoute.current;
    if (current.name !== "batch") return;
    batchListFocusRestore.current = batchItemId;
    if ((current.batchDepth ?? 0) > 0) {
      window.history.back();
      return;
    }
    changeBatchRoute({
      batchId: current.batchId,
      view: "list",
      focus: `item:${batchItemId}`,
      anchorItemId: current.anchorItemId ?? batchItemId,
      anchorOffset: current.anchorOffset,
      trigger: current.trigger,
    }, true);
  };

  const returnFromBatchConfirm = () => {
    const current = currentRoute.current;
    if (current.name !== "batch") return;
    if ((current.batchDepth ?? 0) > 0) {
      window.history.back();
      return;
    }
    changeBatchRoute({
      batchId: current.batchId,
      view: "list",
      focus: "confirm-trigger",
      restoreScrollY: current.restoreScrollY,
      restoreToken: (current.restoreToken ?? 0) + 1,
    }, true);
  };

  const retrySavedRefresh = () => {
    preserveLibraryOnRefresh.current = true;
    setLibraryRefresh((value) => value + 1);
  };

  const settingsContext = verifiedSettingsContext(route, settingsReturnContexts.current);
  return <>
    {route.name === "share" && <ShareCapturePage session={getShareSession()} onSubmit={acceptShare} onLeave={leaveShare} registerLeaveGuard={registerShareLeaveGuard} />}
    {reviewSession && <div hidden={route.name !== "review"}>
      <ReviewPage key={reviewSession.entryId} inputText={reviewSession.inputText} active={route.name === "review"} reviewStorageIdentity={reviewSession.entryId} restoreContext={sourceRestore} onRestoreConsumed={consumeSourceRestore}
        onCancel={leaveReview} onSaved={(item) => { clearReviewSession(); setDetailItems((items) => ({ ...items, [item.id]: item })); setSavedItem(item); setCaptureText(""); setFocusId(item.id); preserveLibraryOnRefresh.current = true; setLibraryRefresh((value) => value + 1); navigate({ name: "library" }, true); restoreLibraryPosition(); }}
        onOpenExisting={openDetail} onMergeExisting={openMergeExisting} onSettings={openSettings} onUnknownSaveDiscard={markUnknownSaveDiscard} registerLeaveGuard={registerReviewLeaveGuard} />
    </div>}
    {route.name === "detail" && <DetailPage key={route.entryId} item={detailItems[route.id] ?? null} state={detailStatus} error={detailError} restoreContext={detailRestore} onRetry={() => setDetailRefresh((value) => value + 1)} onBack={returnToLibrary} onSettings={openPageSettings} onRestoreConsumed={consumeDetailRestore} onEdit={openEdit} />}
    {route.name === "edit-merge" && <>
      <div className="edit-merge-background-shell" inert aria-hidden="true">
        <DetailPage key={`background-${route.id}`} background item={detailItems[route.id] ?? null} state={detailItems[route.id] ? "ready" : "loading"} error="" restoreContext={null} onRetry={() => undefined} onBack={() => undefined} onSettings={() => undefined} onRestoreConsumed={() => undefined} onEdit={() => false} />
      </div>
      <CollectionEditMergeSurface key={route.entryId} itemId={route.id} mode={route.mode} routeEntryId={route.entryId!} onAuthoritativeItem={(item) => { setDetailItems((items) => ({ ...items, [item.id]: item })); setDetailStatus("ready"); }} onSuccessCommitted={(item) => commitEditMergeSuccess(item)} onCompleted={(item) => completeEditMerge(item)} onExit={returnFromEditMerge} onItemMissingExit={returnMissingEditMergeToLibrary} onUnknownDiscard={markUnknownSaveDiscard} registerLeaveGuard={registerEditMergeLeaveGuard} />
    </>}
    {route.name === "settings" && <SettingsPage key={route.entryId} from={settingsContext?.source.name === "review" ? "review" : settingsContext?.source.name === "detail" ? "detail" : "library"} onBack={returnFromSettings} />}
    {route.name === "batch" && <CollectionImportPage route={{ batchId: route.batchId, view: route.view, itemId: route.itemId, focus: route.focus, restoreScrollY: route.restoreScrollY, restoreToken: route.restoreToken, anchorItemId: route.anchorItemId, anchorOffset: route.anchorOffset, trigger: route.trigger }} onRouteChange={changeBatchRoute} onBackToLibrary={returnFromBatchToLibrary} onBackToList={returnToBatchList} onBackFromConfirm={returnFromBatchConfirm} onOpenExisting={openBatchExisting} registerLeaveGuard={registerBatchLeaveGuard} />}
    {route.name === "library" && <LibraryPage key={route.entryId} items={materials} facets={libraryFacets} total={libraryTotal} status={libraryStatus} error={libraryError} loadMoreError={loadMoreError} nextCursor={nextCursor} query={libraryQuery} searchText={searchText} captureText={captureText} captureError={captureError} setCaptureError={setCaptureError} setCaptureText={setCaptureText} setSearchText={setSearchText} setSearchComposing={setSearchComposing} setQuery={setLibraryQuery} onRetry={() => setLibraryRefresh((value) => value + 1)} onRetrySavedRefresh={retrySavedRefresh} onLoadMore={loadMore} onCapture={openReview} onBatchImport={openBatchImport} onOpen={openDetail} onSettings={openPageSettings} focusId={focusId} captureFocusToken={captureFocusToken} savedItem={savedItem} restoreContext={sourceRestore} onRestoreConsumed={consumeSourceRestore} />}
  </>;
}
