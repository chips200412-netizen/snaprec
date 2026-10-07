import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type MutableRefObject,
} from "react";
import {
  AlertDialog,
  Badge,
  Button,
  Checkbox,
  DropdownMenu,
  IconButton,
  Tabs,
  TextField,
  Tooltip,
} from "@radix-ui/themes";
import {
  ArrowLeft,
  ArrowSquareOut,
  ArrowsOutSimple,
  BookmarkSimple,
  BookOpenText,
  CaretDown,
  Check,
  CheckSquare,
  ChatCircleText,
  ClipboardText,
  Clock,
  DownloadSimple,
  FileAudio,
  FileVideo,
  GridFour,
  LinkSimple,
  ListBullets,
  Lightbulb,
  MagnifyingGlass,
  NotePencil,
  PaperPlaneTilt,
  PencilSimple,
  Play,
  PlayCircle,
  Plus,
  Quotes,
  Sparkle,
  SpinnerGap,
  SquaresFour,
  Subtitles,
  Tag,
  Trash,
  UploadSimple,
  Warning,
  X,
} from "@phosphor-icons/react";
import { demoVideos } from "./data";
import {
  ApiError,
  apiClient,
  mapJobStatus,
  mapApiVideoQuestion,
  mergePersonalNotesIntoRecord,
  mapVideoDetailToRecord,
  mapVideoFacets,
  mapVideoSearchPageToRecords,
  mapPlaybackCapability,
  type ApiJob,
  type ApiJobBatch,
  type ApiPersonalNotes,
  type ApiPlatform,
  type ApiVideoDetail,
} from "./api";
import { formatDuration, formatTimestamp } from "./format";
import { StatusIcon } from "./status";
import { ThemeProvider, ThemeSwitcher } from "./theme";
import type {
  Evidence,
  ParseStatus,
  ReadingMode,
  PlaybackCapability,
  VideoFacets,
  VideoRecord,
} from "./types";

const activeBatchJobStatuses = new Set<ApiJob["status"]>([
  "queued",
  "resolving",
  "fetching_metadata",
  "fetching_subtitles",
  "transcribing",
  "cleaning",
  "extracting",
  "saving",
]);

function isBatchEligible(job?: ApiJob): boolean {
  if (!job || job.cancel_requested) return false;
  if (activeBatchJobStatuses.has(job.status)) return true;
  return (
    job.status === "failed" &&
    job.retryable &&
    job.retry_count < job.max_retries
  );
}

function batchEligibilityReason(job?: ApiJob): string {
  if (!job) return "没有可操作的解析任务";
  if (job.cancel_requested || job.status === "cancelled") return "任务已取消";
  if (job.status === "completed" || job.status === "completed_with_warnings") {
    return "任务已经完成";
  }
  if (job.status === "failed" && !isBatchEligible(job)) return "任务不可安全重试";
  return "当前任务不可加入批次";
}

function batchPlatformLabel(platform: ApiPlatform): string {
  if (platform === "bilibili") return "B站";
  if (platform === "douyin") return "抖音";
  return "本地上传";
}

function batchStatusLabel(batch: ApiJobBatch): string {
  if (batch.status === "queued") return "等待任务开始";
  if (batch.status === "running") return "批次处理中";
  if (batch.status === "completed") return "批次已完成";
  return "批次已结束，部分任务需要处理";
}

const statusFilters: Array<{ value: ParseStatus | "all"; label: string }> = [
  { value: "all", label: "全部" },
  { value: "complete", label: "已完成" },
  { value: "processing", label: "解析中" },
  { value: "warning", label: "有警告" },
  { value: "missing", label: "字幕缺失" },
  { value: "failed", label: "失败或取消" },
];

const modeLabels: Record<ReadingMode, string> = {
  overview: "概览",
  steps: "操作步骤",
  transcript: "原始字幕",
};

type TagSourceFilter = "platform" | "automatic" | "personal";

const emptyFacets: VideoFacets = { tags: [], categories: [] };

const tagSourceLabels: Record<TagSourceFilter, string> = {
  platform: "平台",
  automatic: "自动",
  personal: "个人",
};

interface SaveDraft {
  title: string;
  author: string;
  titleOverride: string | null;
  authorOverride: string | null;
  primaryCategory: string;
  secondaryCategory: string;
  tags: string[];
  sparkNote: string;
  sourceUrl: string;
  focusQuery: string;
  file: File | null;
  subtitleFile: File | null;
  retainMedia: boolean;
  coverUrl: string;
}

const categoryOptions = [
  "创作参考",
  "AI 与工具",
  "知识与观点",
  "旅行资料",
  "灵感与体验",
  "暂未分类",
];

interface SupportedLink {
  url: string;
  platform: Exclude<ApiPlatform, "local_upload">;
}

const sharedUrlPattern =
  /https?:\/\/[^\s<>'"“”‘’，。！？；：、）】》」』]+/gi;

function supportedLinks(input: string): SupportedLink[] {
  const links: string[] = Array.from(input.match(sharedUrlPattern) ?? []);
  const supported: SupportedLink[] = [];
  links.forEach((raw) => {
    const url = raw.replace(/[.,!?;:，。！？；：、)\]}）】》」』]+$/, "");
    try {
      const parsed = new URL(url);
      const host = parsed.hostname.toLocaleLowerCase("en-US");
      const bilibili =
        ((host === "b23.tv" || host.endsWith(".b23.tv")) &&
          parsed.pathname !== "/") ||
        ((host === "bilibili.com" || host.endsWith(".bilibili.com")) &&
          /^\/video\/(?:BV[\w]+|av\d+)/i.test(parsed.pathname));
      if (bilibili) {
        supported.push({ url, platform: "bilibili" });
        return;
      }
      const douyin =
        (host === "v.douyin.com" && parsed.pathname !== "/") ||
        (host === "www.douyin.com" &&
          /^\/video\/\d+/i.test(parsed.pathname));
      if (douyin) supported.push({ url, platform: "douyin" });
    } catch {
      // Invalid URLs are reported by linkInputError.
    }
  });
  return supported;
}

function linkInputError(input: string): string {
  const allLinks = input.match(sharedUrlPattern) ?? [];
  const supported = supportedLinks(input);
  if (!input.trim()) return "";
  if (supported.length > 1) {
    return "一次只能解析一个受支持的视频链接，请只保留要收藏的那一条。";
  }
  if (supported.length === 0) {
    return allLinks.length
      ? "暂不支持这个平台，请改用抖音、哔哩哔哩链接或上传本地文件。"
      : "没有识别到视频链接，请粘贴完整分享文本或链接。";
  }
  return "";
}

function formatPersonalNoteTime(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function markdownFilename(video: VideoRecord): string {
  const title = video.title
    .normalize("NFKC")
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "-")
    .replace(/[. ]+$/g, "")
    .trim()
    .slice(0, 80) || "视频笔记";
  const date = new Date().toISOString().slice(0, 10);
  const stableId = video.id.replace(/[^a-zA-Z0-9]/g, "").slice(-8) || "video";
  return `${title}-${date}-${stableId}.md`;
}

function PlatformMark({ platform }: { platform: VideoRecord["platform"] }) {
  const label =
    platform === "bilibili" ? "B站" : platform === "douyin" ? "抖音" : "本地";
  return (
    <span className={`platform-mark platform-${platform}`} aria-label={label}>
      {platform === "local" ? (
        <FileAudio size={18} weight="duotone" aria-hidden />
      ) : (
        label
      )}
    </span>
  );
}

function sourcedTags(video: VideoRecord) {
  return [
    ...video.tags.map((name) => ({
      name,
      source: "platform" as const,
      label: `平台：${name}`,
    })),
    ...(video.automaticTags ?? []).map((tag) => ({
      name: tag.name,
      source: "automatic" as const,
      label: `自动：${tag.name}`,
    })),
    ...video.userTags.map((name) => ({
      name,
      source: "personal" as const,
      label: `个人：${name}`,
    })),
  ];
}

function AutomaticTagState({ video }: { video: VideoRecord }) {
  const tagging = video.automaticTagging;
  const status = tagging?.status ?? "not_generated";
  const copy = {
    generated: {
      title: "自动标签已生成",
      detail:
        tagging?.tags.length === 0
          ? "生成器已完成，未返回可用标签"
          : tagging?.tags.some((tag) => tag.generationMethod === "llm")
          ? "模型基于字幕生成"
          : "离线规则基于字幕提取",
    },
    skipped_no_transcript: {
      title: "自动标签已跳过",
      detail: "当前没有可用字幕，系统没有根据标题或简介猜测标签。",
    },
    failed: {
      title: "自动标签生成失败",
      detail: tagging?.warning || "平台标签和个人标签仍可正常使用。",
    },
    not_generated: {
      title: "自动标签尚未生成",
      detail: "取得完整字幕后可生成，不影响当前内容阅读。",
    },
  }[status];
  return (
    <div className={`automatic-tag-state automatic-tag-${status}`} role="status">
      <Sparkle size={17} aria-hidden />
      <span>
        <strong>{copy.title}</strong>
        <small>{copy.detail}</small>
      </span>
    </div>
  );
}

function TagSourceGroups({ video }: { video: VideoRecord }) {
  const groups = [
    {
      source: "platform" as const,
      title: "平台标签",
      description: "平台公开元信息提供",
      tags: video.tags,
    },
    {
      source: "automatic" as const,
      title: "自动标签",
      description: "仅依据字幕和已验证提取结果",
      tags: (video.automaticTags ?? []).map((tag) => tag.name),
    },
    {
      source: "personal" as const,
      title: "个人标签",
      description: "由你添加和维护",
      tags: video.userTags,
    },
  ];
  return (
    <section className="tag-source-groups" aria-label="标签及来源">
      {groups.map((group) => (
        <div className={`tag-source-group source-${group.source}`} key={group.source}>
          <span>
            <strong>{group.title}</strong>
            <small>{group.description}</small>
          </span>
          <div>
            {group.tags.length ? (
              group.tags.map((tag) => (
                <Badge key={tag} color={group.source === "automatic" ? "iris" : "gray"} variant={group.source === "personal" ? "outline" : "soft"}>
                  {tag}
                </Badge>
              ))
            ) : (
              <small>暂无</small>
            )}
          </div>
        </div>
      ))}
      <AutomaticTagState video={video} />
    </section>
  );
}

function StatusLabel({
  status,
  text,
}: {
  status: ParseStatus;
  text: string;
}) {
  return (
    <span
      className={`status-label status-${status}`}
      role={status === "processing" ? "status" : undefined}
      aria-live={status === "processing" ? "polite" : undefined}
    >
      <StatusIcon status={status} />
      {text}
    </span>
  );
}

function TimestampButton({
  evidence,
  onActivate,
}: {
  evidence?: Evidence;
  onActivate: (evidence: Evidence) => void;
}) {
  if (!evidence || evidence.start === null) {
    return <span className="timestamp-unavailable">无时间戳</span>;
  }
  return (
    <button
      className="timestamp-button"
      type="button"
      onClick={() => onActivate(evidence)}
      aria-label={`查看 ${formatTimestamp(evidence.start)} 的字幕依据`}
    >
      <Play size={13} weight="fill" aria-hidden />
      {formatTimestamp(evidence.start)}
    </button>
  );
}

interface SeekRequest {
  time: number;
  token: number;
}

export function resolveSeekTarget(requested: number, duration: number) {
  if (!Number.isFinite(requested) || requested < 0) return null;
  if (Number.isFinite(duration) && duration > 0 && requested >= duration) {
    return null;
  }
  return requested;
}

function useMediaQuery(query: string) {
  const [matches, setMatches] = useState(() =>
    typeof window.matchMedia === "function" ? window.matchMedia(query).matches : false,
  );
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return undefined;
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);
  return matches;
}

function playbackReason(reason: PlaybackCapability["reason"]) {
  if (reason === "unsupported_source") return "平台没有提供可合规播放的媒体。";
  if (reason === "unsupported_browser_format") return "当前浏览器不支持该媒体格式。";
  if (reason === "missing_file") return "保留的媒体文件已经缺失。";
  return "媒体未保留，时间戳仅定位字幕。";
}

function PlaybackExperience({
  video,
  enabled,
  activeEvidenceId,
  seekRequest,
  failed,
  onSelectEvidence,
  onError,
  onNotify,
  mediaRef,
}: {
  video: VideoRecord;
  enabled: boolean;
  activeEvidenceId: string | null;
  seekRequest: SeekRequest | null;
  failed: boolean;
  onSelectEvidence: (evidence: Evidence) => void;
  onError: () => void;
  onNotify: (message: string) => void;
  mediaRef: MutableRefObject<HTMLMediaElement | null>;
}) {
  const [miniOpen, setMiniOpen] = useState(false);
  const playback = video.playback;
  const available =
    enabled &&
    !failed &&
    playback?.availability === "available" &&
    playback.kind === "retained_local" &&
    Boolean(playback.streamUrl);

  useEffect(() => {
    const media = mediaRef.current;
    if (!media || !seekRequest || !available) return undefined;
    const seek = () => {
      const duration = Number.isFinite(media.duration) ? media.duration : video.duration;
      const target = resolveSeekTarget(seekRequest.time, duration);
      if (target === null) {
        onNotify("该时间戳超过媒体时长，已改为定位字幕依据。");
        return;
      }
      const wasPlaying = !media.paused;
      try {
        setMiniOpen(true);
        media.currentTime = target;
        if (wasPlaying) void media.play().catch(() => undefined);
      } catch {
        onError();
        onNotify("媒体无法跳到该时间，已改为定位字幕依据。");
      }
    };
    if (media.readyState >= 1) {
      seek();
      return undefined;
    }
    media.addEventListener("loadedmetadata", seek, { once: true });
    return () => media.removeEventListener("loadedmetadata", seek);
  }, [available, seekRequest?.token]);

  if (!enabled || !playback) return null;
  if (!available) {
    return (
      <div className="playback-fallback-shell">
        <div className="playback-capability-note" role="status">
          <Subtitles size={18} aria-hidden />
          <span>
            <strong>{failed ? "媒体暂时无法播放" : "当前仅支持字幕定位"}</strong>
            <small>
              {failed
                ? "播放器加载失败，时间戳仍会打开并定位字幕依据。"
                : playbackReason(playback.reason)}
            </small>
          </span>
        </div>
      </div>
    );
  }

  const setMediaRef = (element: HTMLMediaElement | null) => {
    mediaRef.current = element;
  };
  const enterFullscreen = async () => {
    const media = mediaRef.current;
    if (!media?.requestFullscreen) {
      onNotify("当前浏览器不支持从这里进入全屏。");
      return;
    }
    try {
      await media.requestFullscreen();
    } catch {
      onNotify("浏览器未允许进入全屏，请使用播放器自带的全屏按钮。");
    }
  };
  return (
    <section
      className={`playback-shell ${miniOpen ? "is-mini-open" : ""}`}
      aria-label="视频播放器与证据时间线"
    >
      <div className="playback-main">
        <div className="playback-heading">
          <div>
            <PlayCircle size={20} weight="duotone" aria-hidden />
            <span>
              <strong>时间戳播放器</strong>
              <small>{video.title}</small>
            </span>
          </div>
          <div className="playback-actions">
            <IconButton
              size="2"
              variant="ghost"
              color="gray"
              onClick={() => void enterFullscreen()}
              aria-label="全屏播放"
            >
              <ArrowsOutSimple size={17} aria-hidden />
            </IconButton>
            <IconButton
              size="2"
              variant="ghost"
              color="gray"
              onClick={() => setMiniOpen(false)}
              aria-label="关闭迷你播放器"
              className="mini-player-close"
            >
              <X size={17} aria-hidden />
            </IconButton>
          </div>
        </div>
        <div className="playback-compact-summary" role="status">
          <span>
            <PlayCircle size={20} weight="duotone" aria-hidden />
            <span>
              <strong>已保留可播放媒体</strong>
              <small>点击有效时间戳后打开播放器；媒体可在下方单独删除。</small>
            </span>
          </span>
        </div>
        {playback.mimeType.startsWith("audio/") || video.mediaType === "音频" ? (
          <div className="audio-player-surface">
            <FileAudio size={34} weight="duotone" aria-hidden />
            <audio
              ref={setMediaRef}
              controls
              preload="metadata"
              src={playback.streamUrl}
              onError={() => {
                onError();
                onNotify("媒体加载失败，时间戳将继续定位字幕依据。");
              }}
            />
          </div>
        ) : (
          <video
            ref={setMediaRef}
            controls
            preload="metadata"
            src={playback.streamUrl}
            onError={() => {
              onError();
              onNotify("媒体加载失败，时间戳将继续定位字幕依据。");
            }}
          />
        )}
      </div>
      <aside className="playback-timeline" aria-label="证据时间线">
        <div>
          <strong>证据时间线</strong>
          <small>点击即可跳转并核对原话</small>
        </div>
        <ol>
          {video.evidence.filter((item) => item.start !== null).map((item) => (
            <li key={item.id}>
              <button
                type="button"
                className={activeEvidenceId === item.id ? "is-active" : ""}
                aria-current={activeEvidenceId === item.id ? "true" : undefined}
                onClick={() => onSelectEvidence(item)}
              >
                <time>{formatTimestamp(item.start)}</time>
                <span>{item.text}</span>
              </button>
            </li>
          ))}
        </ol>
      </aside>
    </section>
  );
}

function NewParsePanel({
  onClose,
  onSave,
  initialSource = "",
  initialKind = "link",
}: {
  onClose: () => void;
  onSave: (draft: SaveDraft) => void;
  initialSource?: string;
  initialKind?: "link" | "upload";
}) {
  const panelRef = useRef<HTMLElement>(null);
  const [sourceKind, setSourceKind] = useState<"link" | "upload">(initialKind);
  const [focused, setFocused] = useState(false);
  const [focusQuery, setFocusQuery] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [subtitleFile, setSubtitleFile] = useState<File | null>(null);
  const [retainMedia, setRetainMedia] = useState(false);
  const [step, setStep] = useState<"source" | "review">("source");
  const [sourceUrl, setSourceUrl] = useState(initialSource);
  const [title, setTitle] = useState("待确认的视频标题");
  const [author, setAuthor] = useState("等待识别作者");
  const [titleEdited, setTitleEdited] = useState(false);
  const [authorEdited, setAuthorEdited] = useState(false);
  const [primaryCategory, setPrimaryCategory] = useState("创作参考");
  const [secondaryCategory, setSecondaryCategory] = useState("");
  const [tags, setTags] = useState("待整理, 视频收藏");
  const [sparkNote, setSparkNote] = useState("");
  const [coverUrl, setCoverUrl] = useState("");
  const [previewWarning, setPreviewWarning] = useState("");
  const [previewPending, setPreviewPending] = useState(false);
  const sourceError =
    sourceKind === "link" ? linkInputError(sourceUrl) : "";

  useEffect(() => {
    panelRef.current?.focus();
  }, [step]);

  const handlePanelKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const controls = Array.from(
      event.currentTarget.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    );
    if (!controls.length) return;
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const continueToReview = async () => {
    if (sourceKind === "upload") {
      setPreviewWarning("");
      setStep("review");
      return;
    }
    if (sourceError || supportedLinks(sourceUrl).length !== 1) return;
    setPreviewPending(true);
    setPreviewWarning("");
    try {
      const preview = await apiClient.previewResolution(sourceUrl);
      setTitle(preview.title || "未命名视频");
      setAuthor(preview.author || "作者未知");
      setTitleEdited(false);
      setAuthorEdited(false);
      setTags(preview.tags.join(", "));
      setCoverUrl(preview.cover_url);
      if (preview.warnings.length) {
        setPreviewWarning(`公开元信息带有提示：${preview.warnings.join("；")}`);
      }
    } catch (error) {
      setTitle("公开元信息未识别");
      setAuthor("作者待后台解析");
      setTitleEdited(false);
      setAuthorEdited(false);
      setCoverUrl("");
      const message = (
        error instanceof Error ? error.message : "公开元信息暂时无法读取"
      ).replace(/[。！？!?；;：:\s]+$/u, "");
      setPreviewWarning(
        `${message}。仍可保存；保存后会创建解析任务。若仍失败，请上传本地媒体。`,
      );
    } finally {
      setPreviewPending(false);
      setStep("review");
    }
  };

  if (step === "review") {
    return (
      <section
        ref={panelRef}
        tabIndex={-1}
        onKeyDown={handlePanelKeyDown}
        className="new-parse-panel save-review-panel"
        aria-labelledby="save-review-title"
      >
        <div className="panel-heading-row">
          <button type="button" className="text-action" onClick={() => setStep("source")}>
            返回
          </button>
          <div className="review-heading">
            <h2 id="save-review-title">保存前整理</h2>
            <p>分类和闪念会帮助未来的你重新找到它</p>
          </div>
          <Button
            size="2"
            onClick={() =>
              onSave({
                title,
                author,
                titleOverride:
                  titleEdited || Boolean(file) ? title.trim() || null : null,
                authorOverride:
                  authorEdited || Boolean(file) ? author.trim() || null : null,
                primaryCategory,
                secondaryCategory,
                tags: tags
                  .split(/[,，]/)
                  .map((tag) => tag.trim())
                  .filter(Boolean),
                sparkNote,
                sourceUrl,
                focusQuery: focused ? focusQuery.trim() : "",
                file,
                subtitleFile,
                retainMedia,
                coverUrl,
              })
            }
          >
            保存
          </Button>
        </div>
        <div className="review-cover" aria-label="媒体预览">
          {coverUrl ? (
            <img src={coverUrl} alt="" />
          ) : (
            <div>
              <Play size={34} weight="fill" aria-hidden />
            </div>
          )}
          <span>{file ? "本地文件" : "链接识别预览"}</span>
        </div>
        {previewWarning && (
          <p className="preview-warning" role="status">
            <Warning size={16} aria-hidden />
            {previewWarning}
          </p>
        )}
        <div className="review-grid">
          <div className="field-label compact-field">
            媒体类型
            <div className="readonly-value">
              {file?.type.startsWith("audio/") ? "音频" : "视频"}
            </div>
          </div>
          <label className="field-label compact-field field-span-2">
            标题
            <input
              value={title}
              onChange={(event) => {
                setTitle(event.target.value);
                setTitleEdited(true);
              }}
            />
          </label>
          <label className="field-label compact-field field-span-2">
            作者
            <input
              value={author}
              onChange={(event) => {
                setAuthor(event.target.value);
                setAuthorEdited(true);
              }}
            />
          </label>
          <label className="field-label compact-field">
            一级分类
            <select
              value={primaryCategory}
              onChange={(event) => setPrimaryCategory(event.target.value)}
            >
              {categoryOptions.map((category) => (
                <option key={category}>{category}</option>
              ))}
            </select>
          </label>
          <label className="field-label compact-field">
            二级分类
            <input
              value={secondaryCategory}
              onChange={(event) => setSecondaryCategory(event.target.value)}
              placeholder="例如：AI 编程"
            />
          </label>
          <label className="field-label compact-field field-span-2">
            个人标签
            <input value={tags} onChange={(event) => setTags(event.target.value)} />
          </label>
        </div>
        <div className="limited-summary">
          <span>AI 摘要预览</span>
          <strong>{subtitleFile ? "用户字幕" : "有限来源"}</strong>
          <p>
            {subtitleFile
              ? "已附加用户字幕。保存后会以字幕内容生成摘要、要点和时间戳证据。"
              : "字幕尚未获得。这里只能根据公开标题和简介显示有限预览，保存后将以字幕重新整理。"}
          </p>
          {subtitleFile && <small>字幕文件：{subtitleFile.name}</small>}
        </div>
        {file && retainMedia && (
          <div className="media-retention-review" role="status">
            <FileVideo size={18} aria-hidden />
            <span>
              <strong>已授权保留媒体</strong>
              <small>用于以后按时间戳播放，可在详情中删除，不影响字幕和笔记。</small>
            </span>
          </div>
        )}
        <label className="spark-input">
          <span>
            <Lightbulb size={17} weight="duotone" aria-hidden />
            <strong>闪念</strong>
            <small>只属于你的收藏当下记录</small>
          </span>
          <textarea
            rows={3}
            value={sparkNote}
            onChange={(event) => setSparkNote(event.target.value)}
            placeholder="例如：以后做同类项目时，回来看看这里的步骤"
          />
        </label>
      </section>
    );
  }

  return (
    <section
      ref={panelRef}
      tabIndex={-1}
      onKeyDown={handlePanelKeyDown}
      className="new-parse-panel"
      aria-labelledby="new-parse-title"
    >
      <div className="panel-heading-row">
        <div>
          <h2 id="new-parse-title">新建解析</h2>
          <p>粘贴一条公开链接，或上传你拥有的视频和音频。</p>
        </div>
        <IconButton
          size="3"
          variant="ghost"
          color="gray"
          onClick={onClose}
          aria-label="关闭新建解析"
        >
          <X size={18} aria-hidden />
        </IconButton>
      </div>
      <div className="source-kind-switch" aria-label="解析来源">
        <button
          type="button"
          className={sourceKind === "link" ? "is-active" : ""}
          onClick={() => {
            setSourceKind("link");
            setFile(null);
            setSubtitleFile(null);
            setRetainMedia(false);
          }}
        >
          <ArrowSquareOut size={16} aria-hidden />
          视频链接
        </button>
        <button
          type="button"
          className={sourceKind === "upload" ? "is-active" : ""}
          onClick={() => {
            setSourceKind("upload");
            setSourceUrl("");
          }}
        >
          <UploadSimple size={16} aria-hidden />
          本地文件
        </button>
      </div>
      {sourceKind === "link" ? (
        <label className="field-label">
          链接或分享文本
          <textarea
            rows={3}
            placeholder="粘贴抖音、哔哩哔哩视频链接或完整分享文本"
            value={sourceUrl}
            onChange={(event) => {
              setSourceUrl(event.target.value);
              setPreviewWarning("");
            }}
          />
        </label>
      ) : (
        <div className="upload-fields">
          <label className="upload-dropzone">
            <FileVideo size={24} weight="duotone" aria-hidden />
            <span>{file ? file.name : "选择视频或音频文件"}</span>
            <small>文件只用于本次解析，临时媒体处理后清理</small>
            <input
              className="sr-only"
              type="file"
              accept="video/*,audio/*"
              onChange={(event) => {
                const nextFile = event.target.files?.[0] ?? null;
                setFile(nextFile);
                if (nextFile) {
                  setTitle(nextFile.name.replace(/\.[^.]+$/, ""));
                  setAuthor("本地上传");
                  setTitleEdited(false);
                  setAuthorEdited(false);
                }
              }}
            />
          </label>
          <label className="subtitle-upload">
            <Subtitles size={18} aria-hidden />
            <span>
              <strong>{subtitleFile ? subtitleFile.name : "可选：附加字幕"}</strong>
              <small>SRT、VTT 或 TXT；提供后优先使用，不触发 ASR</small>
            </span>
            <input
              className="sr-only"
              type="file"
              accept=".srt,.vtt,.txt,text/plain,text/vtt"
              onChange={(event) =>
                setSubtitleFile(event.target.files?.[0] ?? null)
              }
            />
          </label>
          <label className="retain-media-field">
            <Checkbox
              checked={retainMedia}
              onCheckedChange={(checked) => setRetainMedia(checked === true)}
            />
            <span>
              <strong>保留媒体用于时间戳播放</strong>
              <small>默认不保留。开启后媒体存入本工具受控存储，可随时删除。</small>
            </span>
          </label>
        </div>
      )}
      {sourceError && (
        <p className="form-error" role="alert">
          {sourceError}
        </p>
      )}
      <label className="focus-switch">
        <Checkbox
          checked={focused}
          onCheckedChange={(checked) => setFocused(checked === true)}
        />
        只提取我关心的问题
      </label>
      {focused && (
        <label className="field-label">
          你关心什么
          <input
            value={focusQuery}
            onChange={(event) => setFocusQuery(event.target.value)}
            placeholder="例如：只整理视频里可以直接执行的获客方法"
          />
        </label>
      )}
      <div className="form-actions">
        <span>先确认分类与闪念，再保存并开始解析</span>
        <Button
          size="3"
          onClick={() => void continueToReview()}
          disabled={
            previewPending ||
            (sourceKind === "link" && !sourceUrl.trim()) ||
            (sourceKind === "upload" && !file) ||
            (sourceKind === "link" && Boolean(sourceError)) ||
            (focused && !focusQuery.trim())
          }
        >
          {previewPending ? (
            <SpinnerGap className="spin" size={17} aria-hidden />
          ) : (
            <BookmarkSimple size={17} aria-hidden />
          )}
          {previewPending ? "正在识别" : "识别并整理"}
        </Button>
      </div>
    </section>
  );
}

function LibraryItem({
  video,
  active,
  onOpen,
}: {
  video: VideoRecord;
  active: boolean;
  onOpen: () => void;
}) {
  return (
    <article className={`library-item ${active ? "is-active" : ""}`}>
      <button type="button" className="library-open" onClick={onOpen}>
        <PlatformMark platform={video.platform} />
        <span className="library-copy">
          <span className="library-title">{video.title}</span>
          <span className="library-summary">{video.summary}</span>
          <span className="library-meta">
            <StatusLabel status={video.status} text={video.statusText} />
            <span>{video.updatedAt}</span>
          </span>
        </span>
      </button>
    </article>
  );
}

function LibraryPane({
  videos,
  activeId,
  onOpen,
  search,
  onSearch,
  status,
  onStatus,
}: {
  videos: VideoRecord[];
  activeId: string;
  onOpen: (id: string) => void;
  search: string;
  onSearch: (value: string) => void;
  status: ParseStatus | "all";
  onStatus: (value: ParseStatus | "all") => void;
}) {
  const commonTags = Array.from(
    new Set(videos.flatMap((video) => [...video.tags, ...video.userTags])),
  ).slice(0, 5);
  return (
    <aside className="library-pane" aria-label="视频资源列表">
      <div className="library-brand">
        <span className="brand-mark">
          <BookOpenText size={20} weight="duotone" aria-hidden />
        </span>
        <span>
          <strong>视频知识库</strong>
          <small>证据优先阅读器</small>
        </span>
      </div>
      <div className="library-controls">
        <TextField.Root
          size="3"
          value={search}
          onChange={(event) => onSearch(event.target.value)}
          placeholder="搜索标题、作者、字幕"
          aria-label="搜索视频"
        >
          <TextField.Slot>
            <MagnifyingGlass size={17} aria-hidden />
          </TextField.Slot>
        </TextField.Root>
        <div className="filter-strip" aria-label="按状态筛选">
          {statusFilters.map((item) => (
            <button
              type="button"
              key={item.value}
              className={status === item.value ? "is-active" : ""}
              onClick={() => onStatus(item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>
      <div className="tag-groups">
        <div className="tag-group-heading">
          <span>
            <Tag size={15} aria-hidden />
            常用标签
          </span>
        </div>
        <div className="tag-cloud">
          {commonTags.length ? (
            commonTags.map((tag) => (
              <button type="button" key={tag} onClick={() => onSearch(tag)}>
                {tag}
              </button>
            ))
          ) : (
            <span>解析完成后可按标签快速回访</span>
          )}
        </div>
      </div>
      <div className="library-section-heading">
        <span>最近解析</span>
        <span>{videos.length} 条</span>
      </div>
      <div className="library-list">
        {videos.length > 0 ? (
          videos.map((video) => (
            <LibraryItem
              key={video.id}
              video={video}
              active={video.id === activeId}
              onOpen={() => onOpen(video.id)}
            />
          ))
        ) : (
          <div className="empty-list">
            <MagnifyingGlass size={24} aria-hidden />
            <strong>没有匹配的视频</strong>
            <span>换一个关键词或清除筛选条件。</span>
          </div>
        )}
      </div>
    </aside>
  );
}

function QuickCapture({
  onContinue,
}: {
  onContinue: (source: string) => void;
}) {
  const [source, setSource] = useState("");
  const error = linkInputError(source);
  return (
    <section className="quick-capture" aria-labelledby="quick-capture-title">
      <div>
        <span className="quick-capture-mark">
          <BookmarkSimple size={19} weight="duotone" aria-hidden />
          你的灵感收件箱
        </span>
        <h1 id="quick-capture-title">
          先收下，
          <br />
          <strong>需要时再找到。</strong>
        </h1>
        <p>把视频和当下的想法一起保存，再由字幕和证据慢慢整理。</p>
      </div>
      <div className="capture-composer">
        <label>
          <span className="sr-only">视频链接或分享文本</span>
          <textarea
            rows={3}
            value={source}
            onChange={(event) => setSource(event.target.value)}
            placeholder="粘贴抖音、哔哩哔哩链接或完整分享文本"
          />
        </label>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div>
          <span>一次保存一条，避免意外批量采集</span>
          <Button
            size="3"
            disabled={
              !source.trim() ||
              Boolean(error) ||
              supportedLinks(source).length !== 1
            }
            onClick={() => onContinue(source.trim())}
          >
            收藏并整理
            <ArrowSquareOut size={17} aria-hidden />
          </Button>
        </div>
      </div>
    </section>
  );
}

function JobActions({
  job,
  onCancel,
  onRetry,
}: {
  job?: ApiJob;
  onCancel: () => void;
  onRetry: () => void;
}) {
  if (!job) return null;
  const terminal = job.status === "failed" || job.status === "cancelled";
  if (terminal) {
    if (!job.retryable) {
      return (
        <span className="job-terminal-hint">
          {job.status === "cancelled" ? "已取消" : "请重新提交"}
        </span>
      );
    }
    return (
      <Button
        size="1"
        variant="soft"
        color="gray"
        onClick={onRetry}
      >
        重试
      </Button>
    );
  }
  return (
    <Button size="1" variant="ghost" color="gray" onClick={onCancel}>
      取消
    </Button>
  );
}

function BatchQueuePanel({
  batch,
  loading,
  error,
  cancellingJobId,
  onClose,
  onCancelItem,
}: {
  batch: ApiJobBatch | null;
  loading: boolean;
  error: string;
  cancellingJobId: string | null;
  onClose: () => void;
  onCancelItem: (jobId: string) => void;
}) {
  const createdAt = batch
    ? new Intl.DateTimeFormat("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }).format(new Date(batch.created_at))
    : "";

  return (
    <aside className="batch-queue-panel" aria-label="批量解析队列">
      <header className="batch-panel-header">
        <div>
          <strong>任务队列</strong>
          {batch && <span>创建于 {createdAt}</span>}
        </div>
        <IconButton
          size="3"
          variant="ghost"
          color="gray"
          onClick={onClose}
          aria-label="收起任务队列"
        >
          <X size={18} aria-hidden />
        </IconButton>
      </header>

      {loading && !batch ? (
        <div className="batch-panel-state" role="status">
          <SpinnerGap className="status-spinner" size={18} aria-hidden />
          <span>正在恢复最近批次</span>
        </div>
      ) : error && !batch ? (
        <div className="batch-panel-state is-error" role="alert">
          <Warning size={18} aria-hidden />
          <span>{error}</span>
        </div>
      ) : batch ? (
        <>
          <div className="batch-panel-summary" aria-live="polite">
            <span>{batchStatusLabel(batch)}</span>
            <strong>
              已完成 {batch.completed}/{batch.total}
            </strong>
          </div>
          {error && (
            <div className="batch-inline-error" role="alert">
              <Warning size={16} aria-hidden />
              <span>{error}</span>
            </div>
          )}
          <ol className="batch-item-list">
            {batch.items.map((item) => {
              const mapped = mapJobStatus(item.job);
              const canCancel = activeBatchJobStatuses.has(item.job.status);
              const cancelling = cancellingJobId === item.job.id;
              return (
                <li key={item.job.id}>
                  <div className="batch-item-copy">
                    <span className={`batch-item-status status-${mapped.status}`}>
                      <StatusIcon status={mapped.status} />
                      {mapped.statusText}
                    </span>
                    <strong>{item.title}</strong>
                    <small>{batchPlatformLabel(item.platform)}</small>
                  </div>
                  {canCancel ? (
                    <Button
                      size="2"
                      variant="ghost"
                      color="gray"
                      disabled={cancelling}
                      onClick={() => onCancelItem(item.job.id)}
                    >
                      {cancelling ? "取消中" : "取消任务"}
                    </Button>
                  ) : (
                    <span className="batch-item-terminal">已结束</span>
                  )}
                </li>
              );
            })}
          </ol>
        </>
      ) : (
        <div className="batch-panel-state">
          <ListBullets size={18} aria-hidden />
          <span>还没有批次。请先选择 2-10 个任务。</span>
        </div>
      )}
    </aside>
  );
}

function LibraryGrid({
  videos,
  facets,
  loading,
  search,
  onSearch,
  activeCategory,
  onCategory,
  onOpen,
  onNew,
  onImport,
  platform,
  onPlatform,
  media,
  onMedia,
  status,
  onStatus,
  secondaryCategory,
  onSecondaryCategory,
  tag,
  onTag,
  tagSource,
  onTagSource,
  hasFilters,
  onClearFilters,
  jobsByRecord,
  onCancel,
  onRetry,
  onFavorite,
  selectionMode,
  selectedIds,
  onToggleSelectionMode,
  onToggleBatchItem,
  onCreateBatch,
  batchCreating,
  batchPanelOpen,
  activeBatch,
  batchLoading,
  batchError,
  cancellingBatchJobId,
  onOpenBatchPanel,
  onCloseBatchPanel,
  onCancelBatchItem,
}: {
  videos: VideoRecord[];
  facets: VideoFacets;
  loading: boolean;
  search: string;
  onSearch: (value: string) => void;
  activeCategory: string;
  onCategory: (value: string) => void;
  onOpen: (id: string) => void;
  onNew: () => void;
  onImport: () => void;
  platform: VideoRecord["platform"] | "all";
  onPlatform: (value: VideoRecord["platform"] | "all") => void;
  media: VideoRecord["mediaType"] | "all";
  onMedia: (value: VideoRecord["mediaType"] | "all") => void;
  status: ParseStatus | "all";
  onStatus: (value: ParseStatus | "all") => void;
  secondaryCategory: string;
  onSecondaryCategory: (value: string) => void;
  tag: string;
  onTag: (value: string) => void;
  tagSource: TagSourceFilter;
  onTagSource: (value: TagSourceFilter) => void;
  hasFilters: boolean;
  onClearFilters: () => void;
  jobsByRecord: Map<string, ApiJob>;
  onCancel: (recordId: string) => void;
  onRetry: (recordId: string) => void;
  onFavorite: (recordId: string) => void;
  selectionMode: boolean;
  selectedIds: Set<string>;
  onToggleSelectionMode: () => void;
  onToggleBatchItem: (recordId: string) => void;
  onCreateBatch: () => void;
  batchCreating: boolean;
  batchPanelOpen: boolean;
  activeBatch: ApiJobBatch | null;
  batchLoading: boolean;
  batchError: string;
  cancellingBatchJobId: string | null;
  onOpenBatchPanel: () => void;
  onCloseBatchPanel: () => void;
  onCancelBatchItem: (jobId: string) => void;
}) {
  const facetCategories = facets.categories.length
    ? facets.categories.map((item) => item.primaryCategory)
    : categoryOptions;
  const categories = ["全部", ...facetCategories];
  const primaryCategoryVideos =
    activeCategory === "全部"
      ? videos
      : videos.filter((video) => video.primaryCategory === activeCategory);
  const activeCategoryFacet = facets.categories.find(
    (item) => item.primaryCategory === activeCategory,
  );
  const secondaryCategories = activeCategoryFacet
    ? activeCategoryFacet.children.map((item) => item.secondaryCategory)
    : Array.from(
        new Set(
          primaryCategoryVideos
            .map((video) => video.secondaryCategory)
            .filter((value): value is string => Boolean(value)),
        ),
      );
  const fallbackTags = Array.from(
    new Set(
      videos.flatMap((video) =>
        sourcedTags(video)
          .filter((tag) => tag.source === tagSource)
          .map((tag) => tag.name),
      ),
    ),
  ).sort((left, right) => left.localeCompare(right, "zh-CN"));
  const availableTags = facets.tags.length
    ? facets.tags
        .filter((item) => item.source === tagSource)
        .map((item) => item.name)
    : fallbackTags;
  const categoryVideos =
    secondaryCategory === "全部"
      ? primaryCategoryVideos
      : primaryCategoryVideos.filter(
          (video) => video.secondaryCategory === secondaryCategory,
        );
  return (
    <div className="library-grid-page">
      <header className="library-page-header">
        <div className="library-page-brand">
          <span className="brand-mark">
            <BookOpenText size={20} weight="duotone" aria-hidden />
          </span>
          <span>
            <strong>视频知识库</strong>
            <small>收藏、想法与证据在同一个地方</small>
          </span>
        </div>
        <div className="library-page-actions">
          <ThemeSwitcher />
          {activeBatch && !batchPanelOpen && (
            <Button variant="soft" color="gray" onClick={onOpenBatchPanel}>
              <ListBullets size={17} aria-hidden />
              任务队列 {activeBatch.completed}/{activeBatch.total}
            </Button>
          )}
          <Button
            variant="soft"
            color={selectionMode ? "indigo" : "gray"}
            onClick={onToggleSelectionMode}
          >
            <CheckSquare size={17} aria-hidden />
            {selectionMode ? "退出选择" : "批量选择"}
          </Button>
          <Button variant="soft" color="gray" onClick={onImport}>
            <UploadSimple size={17} aria-hidden />
            导入
          </Button>
          <Button onClick={onNew}>
            <Plus size={17} aria-hidden />
            新建收藏
          </Button>
        </div>
      </header>
      <section className="library-toolbar" aria-label="收藏库搜索与筛选">
        <TextField.Root
          size="3"
          value={search}
          onChange={(event) => onSearch(event.target.value)}
          placeholder="搜地点、标签、用途、作者或字幕"
          aria-label="搜索收藏库"
        >
          <TextField.Slot>
            <MagnifyingGlass size={18} aria-hidden />
          </TextField.Slot>
        </TextField.Root>
        <div className="toolbar-filters">
          <label>
            <span className="sr-only">按平台筛选</span>
            <select
              value={platform}
              onChange={(event) =>
                onPlatform(
                  event.target.value as VideoRecord["platform"] | "all",
                )
              }
            >
              <option value="all">全部平台</option>
              <option value="bilibili">B站</option>
              <option value="douyin">抖音</option>
              <option value="local">本地</option>
            </select>
          </label>
          <label>
            <span className="sr-only">按二级分类筛选</span>
            <select
              value={secondaryCategory}
              onChange={(event) => onSecondaryCategory(event.target.value)}
            >
              <option value="全部">全部子类</option>
              {secondaryCategories.map((category) => (
                <option value={category} key={category}>
                  {category}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="sr-only">按标签来源筛选</span>
            <select
              value={tagSource}
              onChange={(event) => {
                onTagSource(event.target.value as TagSourceFilter);
                onTag("all");
              }}
            >
              <option value="platform">平台标签</option>
              <option value="automatic">自动标签</option>
              <option value="personal">个人标签</option>
            </select>
          </label>
          <label>
            <span className="sr-only">按标签值筛选</span>
            <select
              aria-label={`按${tagSourceLabels[tagSource]}标签筛选`}
              value={tag}
              onChange={(event) => onTag(event.target.value)}
            >
              <option value="all">全部{tagSourceLabels[tagSource]}标签</option>
              {availableTags.map((item) => (
                <option value={item} key={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="sr-only">按媒体类型筛选</span>
            <select
              value={media}
              onChange={(event) =>
                onMedia(event.target.value as VideoRecord["mediaType"] | "all")
              }
            >
              <option value="all">全部媒体</option>
              <option value="视频">视频</option>
              <option value="音频">音频</option>
            </select>
          </label>
          <label>
            <span className="sr-only">按解析状态筛选</span>
            <select
              value={status}
              onChange={(event) =>
                onStatus(event.target.value as ParseStatus | "all")
              }
            >
              {statusFilters.map((item) => (
                <option value={item.value} key={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      </section>
      <div className="filter-summary" role="status">
        <span>{loading ? "正在加载收藏库" : `找到 ${videos.length} 条`}</span>
        {hasFilters && (
          <Button size="2" variant="ghost" color="gray" onClick={onClearFilters}>
            <X size={15} aria-hidden />
            清除筛选
          </Button>
        )}
      </div>
      {selectionMode && (
        <div className="batch-selection-strip" role="status">
          <span>
            已选择 {selectedIds.size}/10 项。至少选择 2 项才能创建批次。
          </span>
          <Button
            size="2"
            disabled={selectedIds.size < 2 || batchCreating}
            onClick={onCreateBatch}
          >
            {batchCreating ? "创建中" : "创建批次"}
          </Button>
        </div>
      )}
      <nav className="category-tabs" aria-label="一级分类">
        {categories.map((category) => (
          <button
            type="button"
            key={category}
            className={activeCategory === category ? "is-active" : ""}
            onClick={() => onCategory(category)}
          >
            <span>{category}</span>
            <small>
              {category === "全部"
                  ? facets.categories.reduce((sum, item) => sum + item.count, 0) ||
                  videos.length
                : facets.categories.find(
                    (item) => item.primaryCategory === category,
                  )?.count ??
                  videos.filter(
                    (video) => video.primaryCategory === category,
                  ).length}
            </small>
          </button>
        ))}
      </nav>
      <div
        className={`library-grid-layout ${batchPanelOpen ? "has-batch-panel" : ""}`}
      >
        <aside className="category-sidebar" aria-label="分类目录">
          <strong>{activeCategory}</strong>
          <button
            type="button"
            className={secondaryCategory === "全部" ? "is-active" : ""}
            onClick={() => onSecondaryCategory("全部")}
          >
            <span>全部</span>
            <span>{activeCategoryFacet?.count ?? primaryCategoryVideos.length}</span>
          </button>
          {secondaryCategories.map((category) => (
            <button
              type="button"
              key={category}
              className={secondaryCategory === category ? "is-active" : ""}
              onClick={() => onSecondaryCategory(category)}
            >
              <span>{category}</span>
              <span>
                {activeCategoryFacet?.children.find(
                  (item) => item.secondaryCategory === category,
                )?.count ??
                  primaryCategoryVideos.filter(
                    (video) => video.secondaryCategory === category,
                  ).length}
              </span>
            </button>
          ))}
        </aside>
        <section className="video-grid" aria-label="收藏视频网格">
          {categoryVideos.map((video) => {
            const job = jobsByRecord.get(video.id);
            const eligible = isBatchEligible(job);
            const selected = selectedIds.has(video.id);
            return (
            <article
              className={`grid-video-card ${selected ? "is-selected" : ""} ${selectionMode && !eligible ? "is-batch-disabled" : ""}`}
              key={video.id}
            >
              {selectionMode && (
                <button
                    type="button"
                    className="grid-batch-selector"
                    disabled={!eligible}
                    onClick={() => onToggleBatchItem(video.id)}
                    aria-pressed={eligible ? selected : undefined}
                    aria-label={eligible ? `勾选任务：${video.title}` : `${video.title}：${batchEligibilityReason(job)}`}
                  >
                  {selected ? (
                    <Check size={17} weight="bold" aria-hidden />
                  ) : (
                    <CheckSquare size={17} aria-hidden />
                  )}
                  <span>{eligible ? (selected ? "已选择" : "选择任务") : batchEligibilityReason(job)}</span>
                </button>
              )}
              <button
                type="button"
                className="grid-card-main"
                onClick={() =>
                  selectionMode
                    ? eligible && onToggleBatchItem(video.id)
                    : onOpen(video.id)
                }
                aria-label={
                  selectionMode
                    ? eligible
                      ? `${selected ? "取消选择" : "选择任务"}：${video.title}`
                      : `${video.title}：${batchEligibilityReason(job)}`
                    : `打开解析详情：${video.title}`
                }
                aria-pressed={selectionMode && eligible ? selected : undefined}
                aria-disabled={selectionMode && !eligible ? true : undefined}
              >
                <div className={`grid-card-cover cover-${video.platform}`}>
                  {video.coverUrl && <img src={video.coverUrl} alt="" />}
                  <PlatformMark platform={video.platform} />
                  <span>{video.primaryCategory}</span>
                  <Play size={28} weight="fill" aria-hidden />
                </div>
                <div className="grid-card-copy">
                  <h2>{video.title}</h2>
                  <p>{video.summary}</p>
                  {video.sparkNote && (
                    <div className="grid-spark">
                      <Lightbulb size={14} weight="duotone" aria-hidden />
                      <span>{video.sparkNote}</span>
                    </div>
                  )}
                  <div className="grid-tag-row">
                    {sourcedTags(video).slice(0, 3).map((tag) => (
                      <span className={`source-${tag.source}`} key={`${tag.source}:${tag.name}`}>
                        {tag.label}
                      </span>
                    ))}
                    {sourcedTags(video).length > 3 && (
                      <span>另有 {sourcedTags(video).length - 3} 个</span>
                    )}
                  </div>
                </div>
              </button>
              <footer className="grid-card-footer">
                <span>
                  <StatusLabel status={video.status} text={video.statusText} />
                  {video.updatedAt}
                </span>
                <div className="card-footer-actions">
                  <JobActions
                    job={job}
                    onCancel={() => onCancel(video.id)}
                    onRetry={() => onRetry(video.id)}
                  />
                  {apiIdentity(video) && (
                    <Tooltip content={video.favorite ? "取消收藏" : "收藏"}>
                      <IconButton
                        size="1"
                        variant="ghost"
                        color="gray"
                        onClick={() => onFavorite(video.id)}
                        aria-label={video.favorite ? "取消收藏" : "收藏"}
                      >
                        <BookmarkSimple
                          size={15}
                          weight={video.favorite ? "fill" : "regular"}
                          aria-hidden
                        />
                      </IconButton>
                    </Tooltip>
                  )}
                  {video.sourceUrl ? (
                    <a
                      href={video.sourceUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      onClick={(event) => event.stopPropagation()}
                    >
                      <LinkSimple size={14} aria-hidden />
                      访问原链接
                    </a>
                  ) : (
                    <span className="source-link-disabled">原链接不可用</span>
                  )}
                </div>
              </footer>
            </article>
          )})}
          {categoryVideos.length === 0 && (
            <div className="empty-content grid-empty">
              <GridFour size={28} aria-hidden />
              <h2>{hasFilters ? "没有匹配的视频" : "收藏库还是空的"}</h2>
              <p>
                {hasFilters
                  ? "调整条件或清除筛选，即可查看其他收藏。"
                  : "新建收藏后，视频摘要、标签和分类会出现在这里。"}
              </p>
              {hasFilters && (
                <Button variant="soft" color="gray" onClick={onClearFilters}>
                  清除筛选
                </Button>
              )}
            </div>
          )}
        </section>
        {batchPanelOpen && (
          <BatchQueuePanel
            batch={activeBatch}
            loading={batchLoading}
            error={batchError}
            cancellingJobId={cancellingBatchJobId}
            onClose={onCloseBatchPanel}
            onCancelItem={onCancelBatchItem}
          />
        )}
      </div>
    </div>
  );
}

function ResultSkeleton() {
  return (
    <div className="result-skeleton" aria-label="AI 解析中" aria-busy="true">
      <div className="skeleton-line skeleton-title" />
      <div className="skeleton-line" />
      <div className="skeleton-line skeleton-short" />
      <div className="skeleton-block" />
      <p>
        <SpinnerGap className="status-spinner" size={17} aria-hidden />
        正在生成字幕并整理内容，页面刷新后会从服务端恢复任务。
      </p>
    </div>
  );
}

function IntegrityNotice({ video }: { video: VideoRecord }) {
  const icon =
    video.status === "missing" ||
    video.status === "warning" ||
    video.status === "failed" ? (
      <Warning size={18} weight="fill" aria-hidden />
    ) : (
      <Check size={18} weight="bold" aria-hidden />
    );
  return (
    <div className={`integrity-notice integrity-${video.status}`}>
      {icon}
      <div>
        <strong>{video.integrity}</strong>
        <span>
          字幕来源：{video.subtitleSource}。所有结论仅基于当前可用字幕。
        </span>
      </div>
    </div>
  );
}

function Outline({
  video,
  onNavigate,
}: {
  video: VideoRecord;
  onNavigate: (outline: VideoRecord["outline"][number]) => void;
}) {
  if (video.outline.length === 0) return null;
  return (
    <nav className="content-outline" aria-label="本视频目录">
      <div className="outline-title-row">
        <div>
          <h3>本视频目录</h3>
          <p>按主题快速跳到你需要的部分</p>
        </div>
        <ListBullets size={20} aria-hidden />
      </div>
      <ol>
        {video.outline.map((item, index) => (
          <li key={item.id}>
            <button type="button" onClick={() => onNavigate(item)}>
              <span className="outline-index">{String(index + 1).padStart(2, "0")}</span>
              <span>{item.title}</span>
              <span className="outline-time">
                {item.start === null
                  ? "无时间戳"
                  : `${formatTimestamp(item.start)} - ${formatTimestamp(item.end)}`}
              </span>
            </button>
          </li>
        ))}
      </ol>
    </nav>
  );
}

function EvidencePanel({
  evidence,
  activeEvidenceId,
  open,
  onOpenChange,
  scopeId,
}: {
  evidence: Evidence[];
  activeEvidenceId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  scopeId: string;
}) {
  if (evidence.length === 0) return null;
  return (
    <details
      className="evidence-panel"
      open={open}
      onToggle={(event) => onOpenChange(event.currentTarget.open)}
    >
      <summary>
        <span>
          <Subtitles size={18} aria-hidden />
          字幕依据
        </span>
        <span className="evidence-summary-action">
          默认折叠
          <CaretDown size={16} aria-hidden />
        </span>
      </summary>
      <div className="evidence-list">
        {evidence.map((item) => (
          <blockquote
            id={`${scopeId}-evidence-${item.id}`}
            key={item.id}
            className={activeEvidenceId === item.id ? "is-active" : ""}
          >
            <div className="quote-time">
              <Clock size={15} aria-hidden />
              {item.start === null
                ? "无时间戳"
                : `${formatTimestamp(item.start)} - ${formatTimestamp(item.end)}`}
            </div>
            <p>{item.text}</p>
          </blockquote>
        ))}
      </div>
    </details>
  );
}

interface PersonalNoteActions {
  saveSpark: (content: string) => Promise<void>;
  deleteSpark: () => Promise<void>;
  createAnnotation: (targetKey: string, content: string) => Promise<void>;
  updateAnnotation: (noteId: string, content: string) => Promise<void>;
  deleteAnnotation: (noteId: string) => Promise<void>;
}

function SparkNoteEditor({
  video,
  actions,
}: {
  video: VideoRecord;
  actions?: PersonalNoteActions;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(video.sparkNote ?? "");
  const [pending, setPending] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [error, setError] = useState("");
  const actionRef = useRef<HTMLButtonElement>(null);

  const restoreActionFocus = () => {
    window.requestAnimationFrame(() => actionRef.current?.focus());
  };

  useEffect(() => {
    if (!editing) setDraft(video.sparkNote ?? "");
  }, [editing, video.sparkNote]);

  const save = async () => {
    if (!draft.trim() || !actions || pending) return;
    setPending(true);
    setError("");
    try {
      await actions.saveSpark(draft);
      setEditing(false);
      setConfirmDelete(false);
      restoreActionFocus();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "闪念保存失败。");
    } finally {
      setPending(false);
    }
  };

  const remove = async () => {
    if (!actions || pending) return;
    setPending(true);
    setError("");
    try {
      await actions.deleteSpark();
      setEditing(false);
      setConfirmDelete(false);
      restoreActionFocus();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "闪念删除失败。");
    } finally {
      setPending(false);
    }
  };

  if (!video.sparkNote && !editing) {
    return actions ? (
      <button
        type="button"
        className="spark-empty-action"
        ref={actionRef}
        onClick={() => setEditing(true)}
      >
        <Lightbulb size={18} weight="duotone" aria-hidden />
        <span>
          <strong>添加闪念</strong>
          <small>记录你收藏这个视频时想到的事</small>
        </span>
      </button>
    ) : null;
  }

  return (
    <section
      className="spark-note-banner"
      aria-label="收藏时的闪念"
      aria-busy={pending}
    >
      <Lightbulb size={19} weight="duotone" aria-hidden />
      <div>
        <span>
          闪念
          <small>
            {video.sparkAuthor ?? "我"}
            {video.sparkCreatedAt
              ? ` · ${formatPersonalNoteTime(video.sparkCreatedAt)}`
              : ""}
          </small>
        </span>
        {editing ? (
          <div className="personal-note-editor">
            <textarea
              rows={3}
              maxLength={4000}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape" && !pending) {
                  setEditing(false);
                  setDraft(video.sparkNote ?? "");
                  setError("");
                  restoreActionFocus();
                }
              }}
              aria-label="编辑闪念"
              disabled={pending}
              autoFocus
            />
            <div className="personal-note-editor-actions">
              <span>{draft.length}/4000</span>
              <Button
                size="2"
                variant="ghost"
                color="gray"
                disabled={pending}
                onClick={() => {
                  setEditing(false);
                  setDraft(video.sparkNote ?? "");
                  setError("");
                  restoreActionFocus();
                }}
              >
                取消
              </Button>
              <Button
                size="2"
                disabled={!draft.trim() || pending}
                onClick={() => void save()}
              >
                {pending ? "保存中" : "保存闪念"}
              </Button>
            </div>
          </div>
        ) : (
          <p>{video.sparkNote}</p>
        )}
        {error && (
          <small className="personal-note-error" role="alert">
            {error}
          </small>
        )}
      </div>
      {actions && !editing && (
        <div className="personal-note-tools">
          <button
            type="button"
            ref={actionRef}
            onClick={() => {
              setDraft(video.sparkNote ?? "");
              setEditing(true);
              setConfirmDelete(false);
            }}
            aria-label="编辑闪念"
          >
            <PencilSimple size={16} aria-hidden />
            编辑
          </button>
          {confirmDelete ? (
            <>
              <button
                type="button"
                className="danger-action"
                onClick={() => void remove()}
                disabled={pending}
              >
                {pending ? "删除中" : "确认删除"}
              </button>
              <button
                type="button"
                onClick={() => setConfirmDelete(false)}
                disabled={pending}
              >
                取消
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmDelete(true)}
              aria-label="删除闪念"
            >
              <Trash size={16} aria-hidden />
              删除
            </button>
          )}
        </div>
      )}
    </section>
  );
}

interface AnnotationDraft {
  pointId: string;
  noteId?: string;
  text: string;
}

function PointAnnotations({
  video,
  pointId,
  targetKey,
  actions,
  draft,
  onDraft,
  pending,
  setPending,
  error,
  setError,
  confirmDeleteId,
  setConfirmDeleteId,
}: {
  video: VideoRecord;
  pointId: string;
  targetKey?: string;
  actions?: PersonalNoteActions;
  draft: AnnotationDraft | null;
  onDraft: (draft: AnnotationDraft | null) => void;
  pending: boolean;
  setPending: (pending: boolean) => void;
  error: string;
  setError: (error: string) => void;
  confirmDeleteId: string | null;
  setConfirmDeleteId: (id: string | null) => void;
}) {
  const notes = video.notes.filter((note) =>
    targetKey ? note.targetKey === targetKey : note.pointId === pointId,
  );
  const activeDraft = draft?.pointId === pointId ? draft : null;
  const anotherEditorOpen = Boolean(draft && draft.pointId !== pointId);
  const addActionRef = useRef<HTMLButtonElement>(null);
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const editActionRefs = useRef(new Map<string, HTMLButtonElement>());
  const returnFocusRef = useRef<
    { kind: "add" } | { kind: "edit"; noteId: string }
  >({ kind: "add" });
  const restoreActionFocus = () => {
    window.requestAnimationFrame(() => {
      const returnTarget = returnFocusRef.current;
      const target =
        returnTarget.kind === "edit"
          ? editActionRefs.current.get(returnTarget.noteId)
          : addActionRef.current;
      (target ?? addActionRef.current)?.focus();
    });
  };

  const save = async () => {
    const currentDraft = activeDraft;
    if (!currentDraft?.text.trim() || !actions || pending) return;
    setPending(true);
    setError("");
    try {
      if (currentDraft.noteId) {
        await actions.updateAnnotation(currentDraft.noteId, currentDraft.text);
      } else {
        if (!targetKey) return;
        await actions.createAnnotation(targetKey, currentDraft.text);
      }
      onDraft(null);
      restoreActionFocus();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "个人备注保存失败。");
      window.requestAnimationFrame(() => editorRef.current?.focus());
    } finally {
      setPending(false);
    }
  };

  const remove = async (noteId: string) => {
    if (!actions || pending) return;
    setPending(true);
    setError("");
    try {
      await actions.deleteAnnotation(noteId);
      returnFocusRef.current = { kind: "add" };
      setConfirmDeleteId(null);
      if (draft?.noteId === noteId) onDraft(null);
      restoreActionFocus();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "个人备注删除失败。");
    } finally {
      setPending(false);
    }
  };

  if (!notes.length && !actions && !activeDraft) return null;
  return (
    <aside
      className="point-annotations"
      aria-label="个人备注"
      aria-busy={pending}
    >
      {notes.map((note) => (
        <article className="personal-note" key={note.id}>
          <div className="personal-note-meta">
            <span>
              <NotePencil size={15} aria-hidden />
              个人备注
            </span>
            <small>
              {note.author} · {formatPersonalNoteTime(note.createdAt)}
            </small>
          </div>
          <p>{note.text}</p>
          {actions && (
            <div className="personal-note-tools">
              <button
                type="button"
                ref={(element) => {
                  if (element) editActionRefs.current.set(note.id, element);
                  else editActionRefs.current.delete(note.id);
                }}
                onClick={() => {
                  returnFocusRef.current = { kind: "edit", noteId: note.id };
                  onDraft({
                    pointId,
                    noteId: note.id,
                    text: note.text,
                  });
                  setConfirmDeleteId(null);
                  setError("");
                }}
                disabled={pending || anotherEditorOpen}
              >
                <PencilSimple size={15} aria-hidden />
                编辑
              </button>
              {confirmDeleteId === note.id ? (
                <>
                  <button
                    type="button"
                    className="danger-action"
                    onClick={() => void remove(note.id)}
                    disabled={pending}
                  >
                    {pending ? "删除中" : "确认删除"}
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmDeleteId(null)}
                    disabled={pending}
                  >
                    取消
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  onClick={() => setConfirmDeleteId(note.id)}
                  disabled={pending || anotherEditorOpen}
                >
                  <Trash size={15} aria-hidden />
                  删除
                </button>
              )}
            </div>
          )}
        </article>
      ))}
      {activeDraft && (
        <div className="personal-note-editor">
          <textarea
            ref={editorRef}
            rows={3}
            maxLength={4000}
            value={activeDraft.text}
            onChange={(event) =>
              onDraft({ ...activeDraft, text: event.target.value })
            }
            onKeyDown={(event) => {
              if (event.key === "Escape" && !pending) {
                onDraft(null);
                setError("");
                restoreActionFocus();
              }
            }}
            aria-label={activeDraft.noteId ? "编辑个人备注" : "添加个人备注"}
            disabled={pending}
            autoFocus
          />
          <div className="personal-note-editor-actions">
            <span>{activeDraft.text.length}/4000</span>
            <Button
              size="2"
              variant="ghost"
              color="gray"
              disabled={pending}
              onClick={() => {
                onDraft(null);
                setError("");
                restoreActionFocus();
              }}
            >
              取消
            </Button>
            <Button
              size="2"
              disabled={!activeDraft.text.trim() || pending}
              onClick={() => void save()}
            >
              {pending ? "保存中" : "保存备注"}
            </Button>
          </div>
        </div>
      )}
      {error &&
        (activeDraft ||
          (confirmDeleteId &&
            notes.some((note) => note.id === confirmDeleteId))) && (
        <span className="personal-note-error" role="alert">
          {error}
        </span>
      )}
      {actions && !activeDraft && (
        <button
          type="button"
          className="add-personal-note"
          ref={addActionRef}
          onClick={() => {
            returnFocusRef.current = { kind: "add" };
            onDraft({ pointId, text: "" });
            setConfirmDeleteId(null);
            setError("");
          }}
          disabled={pending || anotherEditorOpen}
        >
          <NotePencil size={16} aria-hidden />
          添加个人备注
        </button>
      )}
    </aside>
  );
}

function HighlightedTranscriptText({
  text,
  query,
}: {
  text: string;
  query: string;
}) {
  const needle = query.trim();
  if (!needle) return <>{text}</>;
  const lowerText = text.toLocaleLowerCase("zh-CN");
  const lowerNeedle = needle.toLocaleLowerCase("zh-CN");
  const parts: Array<{ text: string; match: boolean }> = [];
  let cursor = 0;
  while (cursor < text.length) {
    const index = lowerText.indexOf(lowerNeedle, cursor);
    if (index < 0) {
      parts.push({ text: text.slice(cursor), match: false });
      break;
    }
    if (index > cursor) {
      parts.push({ text: text.slice(cursor, index), match: false });
    }
    parts.push({
      text: text.slice(index, index + needle.length),
      match: true,
    });
    cursor = index + needle.length;
  }
  return (
    <>
      {parts.map((part, index) =>
        part.match ? (
          <mark key={`${index}-${part.text}`}>{part.text}</mark>
        ) : (
          <span key={`${index}-${part.text}`}>{part.text}</span>
        ),
      )}
    </>
  );
}

function TranscriptContent({
  video,
  activeEvidenceId,
  scopeId,
}: {
  video: VideoRecord;
  activeEvidenceId: string | null;
  scopeId: string;
}) {
  const transcript = video.transcript ?? video.evidence;
  const [query, setQuery] = useState("");
  const [matchIndex, setMatchIndex] = useState(0);
  const normalizedQuery = query.trim().toLocaleLowerCase("zh-CN");
  const matches = useMemo(
    () =>
      normalizedQuery
        ? transcript.filter((item) =>
            item.text.toLocaleLowerCase("zh-CN").includes(normalizedQuery),
          )
        : [],
    [normalizedQuery, transcript],
  );
  const currentIndex = matches.length
    ? Math.min(matchIndex, matches.length - 1)
    : 0;
  const currentMatchId = matches[currentIndex]?.id ?? null;

  const navigateMatch = (nextIndex: number) => {
    if (!matches.length) return;
    const boundedIndex = (nextIndex + matches.length) % matches.length;
    setMatchIndex(boundedIndex);
    window.setTimeout(() => {
      document
        .getElementById(`${scopeId}-transcript-${matches[boundedIndex].id}`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 20);
  };

  return (
    <section className="transcript-view" aria-label="原始字幕">
      <IntegrityNotice video={video} />
      {transcript.length > 0 && (
        <div className="transcript-search">
          <label>
            <MagnifyingGlass size={17} aria-hidden />
            <input
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                setMatchIndex(0);
              }}
              placeholder="在当前字幕中搜索"
              aria-label="搜索当前字幕"
            />
          </label>
          <span aria-live="polite">
            {normalizedQuery
              ? matches.length
                ? `${currentIndex + 1} / ${matches.length} 条匹配`
                : "0 条匹配"
              : "输入关键词定位字幕"}
          </span>
          <div>
            <button
              type="button"
              onClick={() => navigateMatch(currentIndex - 1)}
              disabled={!matches.length}
            >
              上一条
            </button>
            <button
              type="button"
              onClick={() => navigateMatch(currentIndex + 1)}
              disabled={!matches.length}
            >
              下一条
            </button>
          </div>
        </div>
      )}
      {transcript.length === 0 ? (
        <div className="empty-content">
          <Subtitles size={28} aria-hidden />
          <h3>当前没有可用字幕</h3>
          <p>可以上传本地媒体或字幕文件后重新解析。</p>
        </div>
      ) : (
        transcript.map((evidence) => (
          <article
            id={`${scopeId}-transcript-${evidence.id}`}
            className={`transcript-row ${
              activeEvidenceId === evidence.id ||
              currentMatchId === evidence.id
                ? "is-active"
                : ""
            }`}
            key={evidence.id}
          >
            <time>{formatTimestamp(evidence.start)}</time>
            <p>
              <HighlightedTranscriptText text={evidence.text} query={query} />
            </p>
          </article>
        ))
      )}
    </section>
  );
}

function KnowledgeContent({
  video,
  mode,
  activeEvidenceId,
  evidenceOpen,
  setEvidenceOpen,
  onTimestamp,
  scopeId,
  noteActions,
}: {
  video: VideoRecord;
  mode: ReadingMode;
  activeEvidenceId: string | null;
  evidenceOpen: boolean;
  setEvidenceOpen: (open: boolean) => void;
  onTimestamp: (evidence: Evidence) => void;
  scopeId: string;
  noteActions?: PersonalNoteActions;
}) {
  const [annotationDraft, setAnnotationDraft] =
    useState<AnnotationDraft | null>(null);
  const [annotationPending, setAnnotationPending] = useState(false);
  const [annotationError, setAnnotationError] = useState("");
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  if (mode === "transcript") {
    return (
      <TranscriptContent
        video={video}
        activeEvidenceId={activeEvidenceId}
        scopeId={scopeId}
      />
    );
  }

  const visiblePoints =
    mode === "steps"
      ? video.points.filter(
          (point) => point.kind === "step" || point.kind === "risk",
        )
      : video.points;
  const visibleEvidenceIds = new Set(
    visiblePoints.flatMap((point) => point.evidenceIds),
  );
  const visibleEvidence =
    mode === "steps"
      ? video.evidence.filter((item) => visibleEvidenceIds.has(item.id))
      : video.evidence;

  if (visiblePoints.length === 0) {
    return (
      <div className="empty-content">
        <SquaresFour size={28} aria-hidden />
        <h3>
          {mode === "steps"
            ? "该视频没有提取到可执行步骤"
            : "当前没有可展示的提炼内容"}
        </h3>
        <p>系统不会用通用知识补全视频没有明确提供的内容。</p>
      </div>
    );
  }

  const grouped = video.outline.length
    ? video.outline.map((outline, index) => ({
        outline,
        points: visiblePoints.filter(
          (point) =>
            point.sectionId === outline.id ||
            (!point.sectionId && index === 0),
        ),
      }))
    : [{ outline: null, points: visiblePoints }];

  return (
    <>
      <div className="knowledge-sections">
        {grouped.map(({ outline, points }, groupIndex) => {
          if (!points.length) return null;
          return (
            <section
              id={
                outline
                  ? `${scopeId}-section-${outline.id}`
                  : `${scopeId}-section-${groupIndex}`
              }
              className="knowledge-section"
              key={outline?.id ?? groupIndex}
              tabIndex={-1}
            >
              {outline && (
                <div className="section-title">
                  <h3>{outline.title}</h3>
                  <span>
                    {formatTimestamp(outline.start)} -{" "}
                    {formatTimestamp(outline.end)}
                  </span>
                </div>
              )}
              {points.map((point) => {
                const evidence = video.evidence.find((item) =>
                  point.evidenceIds.includes(item.id),
                );
                return (
                  <article className={`knowledge-point point-${point.kind}`} key={point.id}>
                    <div className="point-heading">
                      <span className="point-kind">
                        {point.kind === "step"
                          ? "操作步骤"
                          : point.kind === "risk"
                            ? "注意事项"
                            : "核心要点"}
                      </span>
                      <TimestampButton evidence={evidence} onActivate={onTimestamp} />
                    </div>
                    <h4>{point.title}</h4>
                    <p>{point.body}</p>
                    <PointAnnotations
                      video={video}
                      pointId={point.id}
                      targetKey={point.annotationTargetKey}
                      actions={
                        point.annotationTargetKey ? noteActions : undefined
                      }
                      draft={annotationDraft}
                      onDraft={setAnnotationDraft}
                      pending={annotationPending}
                      setPending={setAnnotationPending}
                      error={annotationError}
                      setError={setAnnotationError}
                      confirmDeleteId={confirmDeleteId}
                      setConfirmDeleteId={setConfirmDeleteId}
                    />
                  </article>
                );
              })}
            </section>
          );
        })}
      </div>
      <EvidencePanel
        evidence={visibleEvidence}
        activeEvidenceId={activeEvidenceId}
        open={evidenceOpen}
        onOpenChange={setEvidenceOpen}
        scopeId={scopeId}
      />
    </>
  );
}

const questionStatusCopy = {
  explicit: "视频明确提到",
  inferred: "根据上下文可以合理归纳",
  not_mentioned: "视频没有提到",
  unknown_incomplete_transcript: "字幕残缺，无法判断",
} as const;

function QuestionPanel({
  video,
  scopeId,
  onAsk,
  onEvidence,
}: {
  video: VideoRecord;
  scopeId: string;
  onAsk?: (question: string) => Promise<void>;
  onEvidence: (evidence: Evidence) => void;
}) {
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const hasTranscript = Boolean(video.transcript?.length);
  const available = Boolean(onAsk) && hasTranscript;
  const fieldId = `${scopeId}-question-input`;
  const hintId = `${scopeId}-question-hint`;

  const submit = async () => {
    const question = draft.trim();
    if (!question || !onAsk || pending || !available) return;
    setPending(true);
    setError("");
    try {
      await onAsk(question);
      setDraft("");
      window.requestAnimationFrame(() => inputRef.current?.focus());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "继续提问失败，请重试。");
      window.requestAnimationFrame(() => inputRef.current?.focus());
    } finally {
      setPending(false);
    }
  };

  return (
    <section className="question-panel" aria-labelledby={`${scopeId}-question-title`}>
      <div className="question-panel-heading">
        <div>
          <ChatCircleText size={20} weight="duotone" aria-hidden />
          <div>
            <h2 id={`${scopeId}-question-title`}>继续提问</h2>
            <small>只查找这条视频已经保存的字幕，不会重新下载或转写</small>
          </div>
        </div>
        {video.questions.length > 0 && (
          <span>{video.questions.length} 条回答</span>
        )}
      </div>

      <form
        className="question-composer"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
        aria-busy={pending}
      >
        <label htmlFor={fieldId} className="sr-only">
          围绕当前视频字幕继续提问
        </label>
        <textarea
          id={fieldId}
          ref={inputRef}
          value={draft}
          maxLength={500}
          rows={2}
          disabled={!available || pending}
          aria-describedby={hintId}
          placeholder={
            !hasTranscript
              ? "当前没有可供追问的字幕"
              : !onAsk
                ? "连接统一后端后可继续提问"
                : "例如：视频提到的限制条件是什么？"
          }
          onChange={(event) => setDraft(event.target.value)}
        />
        <div>
          <small id={hintId}>
            {!hasTranscript
              ? "上传字幕或重新解析后才能继续提问"
              : !onAsk
                ? "示例预览不会生成或保存回答"
                : `${draft.length}/500 · 回答会附上原始字幕依据`}
          </small>
          <Button
            type="submit"
            disabled={!available || pending || !draft.trim()}
          >
            {pending ? (
              <SpinnerGap className="spin" size={17} aria-hidden />
            ) : (
              <PaperPlaneTilt size={17} aria-hidden />
            )}
            {pending ? "正在查找字幕依据" : "提问"}
          </Button>
        </div>
      </form>
      {error && (
        <p className="question-error" role="alert">
          {error}
        </p>
      )}

      {video.questions.length > 0 && (
        <ol className="question-history" aria-label="继续提问历史">
          {video.questions.map((item) => {
            const uncertain =
              item.mentionStatus === "not_mentioned" ||
              item.mentionStatus === "unknown_incomplete_transcript";
            return (
              <li key={item.id}>
                <article>
                  <header>
                    <span className="question-mark">问</span>
                    <h3>{item.question}</h3>
                    <time>{formatPersonalNoteTime(item.createdAt)}</time>
                  </header>
                  <div className={`question-integrity is-${item.mentionStatus}`}>
                    {uncertain ? (
                      <Warning size={16} weight="fill" aria-hidden />
                    ) : (
                      <Check size={16} weight="bold" aria-hidden />
                    )}
                    {questionStatusCopy[item.mentionStatus]}
                  </div>
                  <p>{item.directAnswer}</p>
                  {item.missingInformation.length > 0 && (
                    <ul className="question-missing">
                      {item.missingInformation.map((missing) => (
                        <li key={missing}>{missing}</li>
                      ))}
                    </ul>
                  )}
                  {item.evidence.length > 0 && (
                    <div className="question-evidence-list">
                      <strong>字幕依据</strong>
                      {item.evidence.map((evidence) => (
                        <button
                          type="button"
                          key={evidence.id}
                          onClick={() => onEvidence(evidence)}
                        >
                          <time>
                            {evidence.start === null
                              ? "无时间戳"
                              : formatTimestamp(evidence.start)}
                          </time>
                          <span>{evidence.text}</span>
                        </button>
                      ))}
                    </div>
                  )}
                </article>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

function ResultDetail({
  video,
  scopeId,
  onNotify,
  job,
  onCancel,
  onRetry,
  onFavorite,
  noteActions,
  onAskQuestion,
  onPlaybackDeleteRequest,
}: {
  video: VideoRecord;
  scopeId: string;
  onNotify: (message: string) => void;
  job?: ApiJob;
  onCancel?: () => void;
  onRetry?: () => void;
  onFavorite?: () => void;
  noteActions?: PersonalNoteActions;
  onAskQuestion?: (question: string) => Promise<void>;
  onPlaybackDeleteRequest?: () => void;
}) {
  const [mode, setMode] = useState<ReadingMode>("overview");
  const [activeEvidenceId, setActiveEvidenceId] = useState<string | null>(null);
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [seekRequest, setSeekRequest] = useState<SeekRequest | null>(null);
  const [playbackFailed, setPlaybackFailed] = useState(false);
  const [includePersonal, setIncludePersonal] = useState(true);
  const mobileViewport = useMediaQuery("(max-width: 1023px)");
  const playbackEnabled = scopeId.startsWith("mobile-")
    ? mobileViewport
    : !mobileViewport;
  const detailRef = useRef<HTMLDivElement>(null);
  const mediaRef = useRef<HTMLMediaElement | null>(null);
  const exportUnavailable =
    video.status === "processing" || video.status === "failed";

  const navigateOutline = (outline: VideoRecord["outline"][number]) => {
    const target = document.getElementById(`${scopeId}-section-${outline.id}`);
    target?.scrollIntoView({ behavior: "smooth", block: "start" });
    target?.focus({ preventScroll: true });
    if (
      outline.start !== null &&
      playbackEnabled &&
      !playbackFailed &&
      video.playback?.availability === "available"
    ) {
      setSeekRequest({ time: outline.start, token: Date.now() });
      onNotify(`已定位 ${formatTimestamp(outline.start)}，播放器已同步到本段开头。`);
    }
  };

  const activateEvidence = (evidence: Evidence) => {
    setEvidenceOpen(true);
    setActiveEvidenceId(evidence.id);
    window.setTimeout(() => {
      document
        .getElementById(`${scopeId}-evidence-${evidence.id}`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 40);
    const playbackAvailable =
      playbackEnabled &&
      !playbackFailed &&
      video.playback?.availability === "available" &&
      Boolean(video.playback.streamUrl);
    if (playbackAvailable && evidence.start !== null) {
      const media = mediaRef.current;
      if (media?.readyState && Number.isFinite(media.duration)) {
        const target = resolveSeekTarget(evidence.start, media.duration);
        if (target === null) {
          onNotify("该时间戳超过媒体时长，已改为定位字幕依据。");
          return;
        }
        media.currentTime = target;
      }
      setSeekRequest({ time: evidence.start, token: Date.now() });
      onNotify(`已定位 ${formatTimestamp(evidence.start)}，播放器与字幕依据已同步。`);
    } else {
      onNotify(
        `已定位 ${formatTimestamp(evidence.start)} 的字幕。${
          playbackFailed
            ? "媒体加载失败，因此只定位字幕依据。"
            : video.playback
              ? playbackReason(video.playback.reason)
              : "当前没有可播放媒体，因此只定位字幕依据。"
        }`,
      );
    }
  };

  const activateQuestionEvidence = (evidence: Evidence) => {
    const transcriptId = evidence.segmentIds?.[0] ?? null;
    setMode("transcript");
    setActiveEvidenceId(transcriptId);
    window.setTimeout(() => {
      if (transcriptId) {
        document
          .getElementById(`${scopeId}-transcript-${transcriptId}`)
          ?.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    }, 50);
    if (
      playbackEnabled &&
      !playbackFailed &&
      video.playback?.availability === "available" &&
      evidence.start !== null
    ) {
      setSeekRequest({ time: evidence.start, token: Date.now() });
      onNotify(`已定位 ${formatTimestamp(evidence.start)}，播放器与原始字幕已同步。`);
    } else {
      onNotify("已切换到原始字幕并定位这条回答的依据。");
    }
  };

  const markdown = useMemo(() => {
    const exportedPoints =
      mode === "steps"
        ? video.points.filter(
            (point) => point.kind === "step" || point.kind === "risk",
          )
        : video.points;
    const pointLines = exportedPoints.flatMap((point) => {
      const evidence = video.evidence.find((item) =>
        point.evidenceIds.includes(item.id),
      );
      return [
        `- ${point.title}：${point.body}`,
        evidence
          ? `  - 字幕依据${
              evidence.start === null
                ? ""
                : `（${formatTimestamp(evidence.start)}–${formatTimestamp(
                    evidence.end,
                  )}）`
            }：${evidence.text}`
          : "  - 字幕依据：当前结论没有可关联的字幕片段",
      ];
    });
    const transcript = video.transcript ?? [];
    const platformLabel =
      video.platform === "bilibili"
        ? "哔哩哔哩"
        : video.platform === "douyin"
          ? "抖音"
          : "本地上传";
    const generatedAt = new Intl.DateTimeFormat("zh-CN", {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date());
    const evidenceLines = video.evidence.map(
      (item) =>
        `- ${
          item.start === null ? "无时间戳" : formatTimestamp(item.start)
        }–${
          item.end === null ? "无时间戳" : formatTimestamp(item.end)
        }：${item.text}`,
    );
    const personalContentLines =
      includePersonal && (video.sparkNote || video.notes.length)
        ? [
            "",
            "## 个人内容",
            ...(video.sparkNote
              ? [
                  "",
                  "### 闪念",
                  `- ${video.sparkNote}（${video.sparkAuthor ?? "我"}${
                    video.sparkCreatedAt
                      ? `，${formatPersonalNoteTime(video.sparkCreatedAt)}`
                      : ""
                  }）`,
                ]
              : []),
            ...(video.notes.length
              ? [
                  "",
                  "### 个人备注",
                  ...video.notes.map((note) => {
                    const target = video.points.find(
                      (point) => point.id === note.pointId,
                    );
                    return `- ${target?.title ?? "历史要点"}：${note.text}（${note.author}，${formatPersonalNoteTime(note.createdAt)}）`;
                  }),
                ]
              : []),
          ]
        : [];
    const metadataLines = [
      `# ${video.title}`,
      "",
      `平台：${platformLabel}`,
      `作者：${video.author}`,
      `来源：${video.sourceUrl ?? "用户本地上传"}`,
      `字幕来源：${video.subtitleSource}`,
      `信息完整性：${video.integrity}`,
      `生成时间：${generatedAt}`,
    ];
    if (mode === "transcript") {
      return [
        ...metadataLines,
        ...(video.warnings?.length
          ? ["", "## 警告", ...video.warnings.map((item) => `- ${item}`)]
          : []),
        ...personalContentLines,
        "",
        "## 完整字幕",
        ...(transcript.length
          ? transcript.map(
              (item) =>
                `- ${
                  item.start === null
                    ? "无时间戳"
                    : formatTimestamp(item.start)
                } ${item.text}`,
            )
          : ["当前没有可用字幕。"]),
      ].join("\n");
    }
    return [
      ...metadataLines,
      ...(video.focusQuery
        ? ["", "## 用户关注的问题", video.focusQuery]
        : []),
      ...(mode === "overview"
        ? [
            "",
            video.focusQuery ? "## 直接结论" : "## 一句话摘要",
            video.summary,
          ]
        : []),
      "",
      mode === "steps"
        ? "## 仅操作步骤与限制"
        : video.focusQuery
          ? "## 相关内容整理"
          : "## 要点",
      ...(pointLines.length
        ? pointLines
        : ["- 该视频没有提取到可执行步骤。"]),
      ...(video.missingInformation?.length
        ? [
            "",
            "## 信息缺口",
            ...video.missingInformation.map((item) => `- ${item}`),
          ]
        : []),
      ...(video.warnings?.length
        ? ["", "## 警告", ...video.warnings.map((item) => `- ${item}`)]
        : []),
      ...personalContentLines,
      ...(mode === "overview" && video.focusQuery
        ? [
            "",
            "## 字幕依据",
            ...(evidenceLines.length
              ? evidenceLines
              : ["当前没有可用的相关字幕依据。"]),
          ]
        : []),
    ].join("\n");
  }, [includePersonal, mode, video]);

  const copyMarkdown = async () => {
    if (exportUnavailable) {
      onNotify("任务完成后才能导出可靠内容。");
      return;
    }
    try {
      const identity = apiIdentity(video);
      const content = identity
        ? await apiClient.exportMarkdown(identity.videoId, identity.platform, {
            view: mode,
            includePersonal,
            focusQueryHash: video.focusQueryHash,
          })
        : markdown;
      await navigator.clipboard.writeText(content);
      onNotify("Markdown 已复制。");
    } catch {
      onNotify("复制失败，请使用导出文件。");
    }
  };

  const downloadMarkdown = async (exportMode: ReadingMode = mode) => {
    if (exportUnavailable) {
      onNotify("任务完成后才能导出可靠内容。");
      return;
    }
    let content = markdown;
    const identity = apiIdentity(video);
    if (identity) {
      try {
        content = await apiClient.exportMarkdown(
          identity.videoId,
          identity.platform,
          {
            view: exportMode,
            includePersonal,
            focusQueryHash: video.focusQueryHash,
          },
        );
      } catch (caught) {
        onNotify(caught instanceof Error ? caught.message : "后端导出失败，请重试。");
        return;
      }
    }
    const url = URL.createObjectURL(
      new Blob([content], { type: "text/markdown;charset=utf-8" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = markdownFilename(video);
    link.click();
    URL.revokeObjectURL(url);
    onNotify("Markdown 已导出。");
  };

  return (
    <div className="result-detail" ref={detailRef}>
      <header className="detail-header">
        <div className="video-identity">
          <PlatformMark platform={video.platform} />
          <div>
            <div className="detail-meta">
              <span>{video.author}</span>
              <span>{formatDuration(video.duration)}</span>
              <StatusLabel status={video.status} text={video.statusText} />
            </div>
            <h1>{video.title}</h1>
            <p className="classification-path">
              {video.primaryCategory}
              {video.secondaryCategory ? ` / ${video.secondaryCategory}` : ""}
            </p>
          </div>
        </div>
        <div className="detail-actions">
          {job && onCancel && onRetry && (
            <JobActions job={job} onCancel={onCancel} onRetry={onRetry} />
          )}
          {onFavorite && (
            <Tooltip content={video.favorite ? "取消收藏" : "收藏"}>
              <IconButton
                size="3"
                variant="soft"
                color="gray"
                onClick={onFavorite}
                aria-label={video.favorite ? "取消收藏" : "收藏"}
              >
                <BookmarkSimple
                  size={18}
                  weight={video.favorite ? "fill" : "regular"}
                  aria-hidden
                />
              </IconButton>
            </Tooltip>
          )}
          {video.sourceUrl && (
            <Button asChild size="3" variant="soft" color="gray">
              <a
                href={video.sourceUrl}
                target="_blank"
                rel="noopener noreferrer"
              >
                <LinkSimple size={17} aria-hidden />
                原视频
              </a>
            </Button>
          )}
          <Tooltip content={`复制当前${modeLabels[mode]} Markdown`}>
            <IconButton
              size="3"
              variant="soft"
              color="gray"
              onClick={copyMarkdown}
              disabled={exportUnavailable}
              aria-label="复制 Markdown"
            >
              <ClipboardText size={18} aria-hidden />
            </IconButton>
          </Tooltip>
          <DropdownMenu.Root>
            <DropdownMenu.Trigger>
              <Button size="3" disabled={exportUnavailable}>
                <DownloadSimple size={18} aria-hidden />
                导出
                <CaretDown size={14} aria-hidden />
              </Button>
            </DropdownMenu.Trigger>
            <DropdownMenu.Content align="end" className="export-menu">
              <DropdownMenu.Label>Markdown 范围</DropdownMenu.Label>
              <DropdownMenu.Item onSelect={() => void downloadMarkdown(mode)}>
                当前模式：{modeLabels[mode]}
              </DropdownMenu.Item>
              <DropdownMenu.Item
                disabled={!apiIdentity(video)}
                onSelect={() => void downloadMarkdown("steps")}
              >
                仅操作步骤
              </DropdownMenu.Item>
              <DropdownMenu.Separator />
              <DropdownMenu.CheckboxItem
                checked={includePersonal}
                onCheckedChange={(checked) => setIncludePersonal(checked === true)}
              >
                包含个人内容
              </DropdownMenu.CheckboxItem>
            </DropdownMenu.Content>
          </DropdownMenu.Root>
        </div>
      </header>

      {video.status === "processing" ? (
        <ResultSkeleton />
      ) : (
        <>
          <TagSourceGroups video={video} />
          <IntegrityNotice video={video} />
          <PlaybackExperience
            video={video}
            enabled={playbackEnabled}
            activeEvidenceId={activeEvidenceId}
            seekRequest={seekRequest}
            failed={playbackFailed}
            onSelectEvidence={activateEvidence}
            onError={() => setPlaybackFailed(true)}
            onNotify={onNotify}
            mediaRef={mediaRef}
          />
          {video.playback?.availability === "available" &&
            video.platform === "local" &&
            onPlaybackDeleteRequest && (
              <div className="playback-delete-row playback-management-row">
                <button
                  type="button"
                  onClick={onPlaybackDeleteRequest}
                >
                  <Trash size={15} aria-hidden />
                  删除可播放媒体
                </button>
              </div>
            )}
          {video.focusQuery && (
            <section className="focused-query-panel" aria-label="定向提取目标">
              <span>用户关注的问题</span>
              <h2>{video.focusQuery}</h2>
              {video.missingInformation?.length ? (
                <div>
                  <strong>信息缺口</strong>
                  <ul>
                    {video.missingInformation.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </section>
          )}
          <section className="summary-highlight" aria-labelledby={`${scopeId}-summary`}>
            <div className="summary-icon">
              <Quotes size={21} weight="duotone" aria-hidden />
            </div>
            <div>
              <h2 id={`${scopeId}-summary`}>一句话看懂</h2>
              <p>{video.summary}</p>
            </div>
          </section>

          <SparkNoteEditor video={video} actions={noteActions} />

          <QuestionPanel
            video={video}
            scopeId={scopeId}
            onAsk={onAskQuestion}
            onEvidence={activateQuestionEvidence}
          />

          <Tabs.Root
            value={mode}
            onValueChange={(value) => setMode(value as ReadingMode)}
            className="reading-tabs"
          >
            <Tabs.List aria-label="阅读模式">
              <Tabs.Trigger value="overview">
                <BookOpenText size={16} aria-hidden />
                概览
              </Tabs.Trigger>
              <Tabs.Trigger value="steps">
                <CheckSquare size={16} aria-hidden />
                操作步骤
              </Tabs.Trigger>
              <Tabs.Trigger value="transcript">
                <Subtitles size={16} aria-hidden />
                原始字幕
              </Tabs.Trigger>
            </Tabs.List>

            {mode === "overview" && (
              <Outline video={video} onNavigate={navigateOutline} />
            )}

            {(["overview", "steps", "transcript"] as ReadingMode[]).map(
              (tabMode) => (
                <Tabs.Content value={tabMode} key={tabMode}>
                  <KnowledgeContent
                    video={video}
                    mode={tabMode}
                    activeEvidenceId={activeEvidenceId}
                    evidenceOpen={evidenceOpen}
                    setEvidenceOpen={setEvidenceOpen}
                    onTimestamp={activateEvidence}
                    scopeId={scopeId}
                    noteActions={noteActions}
                  />
                </Tabs.Content>
              ),
            )}
          </Tabs.Root>
        </>
      )}
    </div>
  );
}

function MobileVideoCard({
  video,
  expanded,
  onToggle,
  onNotify,
  job,
  onCancel,
  onRetry,
  onFavorite,
  noteActions,
  onAskQuestion,
  selectionMode,
  selected,
  batchEligible,
  onToggleBatchItem,
  onPlaybackDeleteRequest,
}: {
  video: VideoRecord;
  expanded: boolean;
  onToggle: () => void;
  onNotify: (message: string) => void;
  job?: ApiJob;
  onCancel: () => void;
  onRetry: () => void;
  onFavorite?: () => void;
  noteActions?: PersonalNoteActions;
  onAskQuestion?: (question: string) => Promise<void>;
  selectionMode: boolean;
  selected: boolean;
  batchEligible: boolean;
  onToggleBatchItem: () => void;
  onPlaybackDeleteRequest?: () => void;
}) {
  const panelId = `mobile-panel-${video.id}`;
  return (
    <article
      className={`mobile-video-card ${expanded ? "is-expanded" : ""} ${selected ? "is-selected" : ""} ${selectionMode && !batchEligible ? "is-batch-disabled" : ""}`}
    >
      <button
        type="button"
        className="mobile-card-trigger"
        onClick={selectionMode ? onToggleBatchItem : onToggle}
        aria-expanded={selectionMode ? undefined : expanded}
        aria-controls={selectionMode ? undefined : panelId}
        aria-pressed={selectionMode && batchEligible ? selected : undefined}
        aria-disabled={selectionMode && !batchEligible ? true : undefined}
        aria-label={
          selectionMode
            ? batchEligible
              ? `${selected ? "取消选择" : "选择任务"}：${video.title}`
              : `${video.title}：${batchEligibilityReason(job)}`
            : undefined
        }
      >
        <span className={`mobile-card-cover cover-${video.platform}`}>
          {video.coverUrl ? (
            <img src={video.coverUrl} alt="" />
          ) : (
            <PlatformMark platform={video.platform} />
          )}
        </span>
        <span className="mobile-card-copy">
          <span className="mobile-card-topline">
            <StatusLabel status={video.status} text={video.statusText} />
            <span>{video.updatedAt}</span>
          </span>
          <strong>{video.title}</strong>
          <span className="mobile-summary">{video.summary}</span>
          <span className="mobile-tag-row">
            {sourcedTags(video).slice(0, 3).map((tag) => (
              <span className={`source-${tag.source}`} key={`${tag.source}:${tag.name}`}>
                {tag.label}
              </span>
            ))}
            {sourcedTags(video).length > 3 && (
              <span>另有 {sourcedTags(video).length - 3} 个</span>
            )}
          </span>
        </span>
        {selectionMode ? (
          <span className="mobile-batch-marker" aria-hidden>
            {selected ? <Check size={18} weight="bold" /> : <CheckSquare size={18} />}
          </span>
        ) : (
          <CaretDown className="mobile-caret" size={18} aria-hidden />
        )}
      </button>
      {selectionMode && !batchEligible && (
        <div className="mobile-batch-reason">{batchEligibilityReason(job)}</div>
      )}
      {expanded && !selectionMode && (
        <div id={panelId} className="mobile-card-detail">
          <ResultDetail
            video={video}
            scopeId={`mobile-${video.id}`}
            onNotify={onNotify}
            job={job}
            onCancel={onCancel}
            onRetry={onRetry}
            onFavorite={onFavorite}
            noteActions={noteActions}
            onAskQuestion={onAskQuestion}
            onPlaybackDeleteRequest={onPlaybackDeleteRequest}
          />
        </div>
      )}
    </article>
  );
}

type DataMode = "loading" | "live" | "demo";

interface SavedMeta {
  primaryCategory: string;
  secondaryCategory: string | null;
  userTags: string[];
  sparkNote: string | null;
  sparkCreatedAt: string | null;
  sparkSyncPending?: boolean;
  classificationSyncPending?: boolean;
  focusQuery?: string;
  titleOverride?: string | null;
  authorOverride?: string | null;
  mediaType?: VideoRecord["mediaType"];
  coverUrl?: string | null;
}

interface PendingDescriptor {
  job: ApiJob;
  platform: ApiPlatform;
  record: VideoRecord;
  meta: SavedMeta;
  focusQuery?: string;
}

const pendingStorageKey = "video-knowledge.pending-jobs.v1";
const metaStorageKey = "video-knowledge.personal-meta.v1";

function readJsonStorage<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function readSavedMeta(): Record<string, SavedMeta> {
  return readJsonStorage<Record<string, SavedMeta>>(metaStorageKey, {});
}

function enrichWithMeta(record: VideoRecord, meta?: SavedMeta): VideoRecord {
  if (!meta) return record;
  const preferLocalSpark =
    meta.sparkSyncPending === true ||
    (meta.sparkSyncPending === undefined && Boolean(meta.sparkNote));
  return {
    ...record,
    title: meta.titleOverride || record.title,
    author: meta.authorOverride || record.author,
    mediaType: meta.mediaType ?? record.mediaType,
    coverUrl: meta.coverUrl ?? record.coverUrl,
    focusQuery: meta.focusQuery || record.focusQuery,
    primaryCategory: meta.primaryCategory,
    secondaryCategory: meta.secondaryCategory,
    userTags: meta.userTags,
    sparkNote: preferLocalSpark ? meta.sparkNote : record.sparkNote,
    sparkCreatedAt: preferLocalSpark
      ? meta.sparkCreatedAt
      : record.sparkCreatedAt,
  };
}

function preserveLoadedDetail(
  listRecord: VideoRecord,
  current?: VideoRecord,
): VideoRecord {
  if (!current?.detailLoaded) return listRecord;
  return {
    ...listRecord,
    duration: current.duration,
    integrity: current.integrity,
    subtitleSource: current.subtitleSource,
    warnings: current.warnings,
    focusQuery: current.focusQuery,
    missingInformation: current.missingInformation,
    outline: current.outline,
    points: current.points,
    evidence: current.evidence,
    transcript: current.transcript,
    notes: current.notes,
    questions: current.questions,
    sparkNote: current.sparkNote,
    sparkCreatedAt: current.sparkCreatedAt,
    sparkId: current.sparkId,
    sparkAuthor: current.sparkAuthor,
    sparkUpdatedAt: current.sparkUpdatedAt,
    detailLoaded: true,
  };
}

function detailForSavedFocus(
  detail: ApiVideoDetail,
  meta?: SavedMeta,
): ApiVideoDetail {
  const focusQuery = meta?.focusQuery?.trim();
  if (!focusQuery) return detail;
  const history = [...detail.focused_history]
    .reverse()
    .find((item) => item.focus_query.trim() === focusQuery);
  if (!history) return detail;
  return {
    ...detail,
    result: {
      ...detail.result,
      focus_query: history.focus_query,
      extraction_mode: "focused",
      focused_answer: history.focused_answer,
      summary: history.focused_answer.direct_answer.slice(0, 100),
    },
  };
}

function apiIdentity(record: VideoRecord): {
  videoId: string;
  platform: ApiPlatform;
} | null {
  return apiIdentityFromResourceKey(record.id);
}

function apiIdentityFromResourceKey(resourceKey: string): {
  videoId: string;
  platform: ApiPlatform;
} | null {
  const separator = resourceKey.indexOf(":");
  if (separator < 1) return null;
  const prefix = resourceKey.slice(0, separator);
  const platform =
    prefix === "local_upload" ||
    prefix === "bilibili" ||
    prefix === "douyin"
      ? prefix
      : null;
  if (!platform) return null;
  return { platform, videoId: resourceKey.slice(separator + 1) };
}

function AppContent({ initialResourceKey, onCollectionReturn }: { initialResourceKey?: string; onCollectionReturn?: () => void }) {
  const [pendingJobs, setPendingJobs] = useState<PendingDescriptor[]>(() =>
    readJsonStorage<PendingDescriptor[]>(pendingStorageKey, []),
  );
  const [videos, setVideos] = useState<VideoRecord[]>(() => {
    const pending = readJsonStorage<PendingDescriptor[]>(pendingStorageKey, []);
    return pending.length
      ? [
          ...pending.map((item) => ({
            ...item.record,
            ...mapJobStatus(item.job),
            summary: item.job.message || item.record.summary,
          })),
          ...demoVideos,
        ]
      : demoVideos;
  });
  const [dataMode, setDataMode] = useState<DataMode>("loading");
  const [facets, setFacets] = useState<VideoFacets>(emptyFacets);
  const [desktopView, setDesktopView] = useState<"library" | "detail">(initialResourceKey ? "detail" : "library");
  const [activeCategory, setActiveCategory] = useState("全部");
  const [secondaryCategoryFilter, setSecondaryCategoryFilter] =
    useState("全部");
  const [activeId, setActiveId] = useState(initialResourceKey ?? demoVideos[0].id);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<ParseStatus | "all">("all");
  const [platformFilter, setPlatformFilter] = useState<
    VideoRecord["platform"] | "all"
  >("all");
  const [mediaFilter, setMediaFilter] = useState<
    VideoRecord["mediaType"] | "all"
  >("all");
  const [tagFilter, setTagFilter] = useState("all");
  const [tagSourceFilter, setTagSourceFilter] =
    useState<TagSourceFilter>("personal");
  const [showNewParse, setShowNewParse] = useState(false);
  const [initialSource, setInitialSource] = useState("");
  const [initialKind, setInitialKind] = useState<"link" | "upload">("link");
  const [notice, setNotice] = useState("");
  const [playbackDeleteTargetId, setPlaybackDeleteTargetId] = useState<
    string | null
  >(null);
  const [playbackDeleting, setPlaybackDeleting] = useState(false);
  const [batchSelectionMode, setBatchSelectionMode] = useState(false);
  const [selectedBatchIds, setSelectedBatchIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [activeBatch, setActiveBatch] = useState<ApiJobBatch | null>(null);
  const [batchPanelOpen, setBatchPanelOpen] = useState(false);
  const [batchLoading, setBatchLoading] = useState(true);
  const [batchCreating, setBatchCreating] = useState(false);
  const [batchError, setBatchError] = useState("");
  const [cancellingBatchJobId, setCancellingBatchJobId] = useState<
    string | null
  >(null);
  const newParseTriggerRef = useRef<HTMLElement | null>(null);
  const playbackDeleteTriggerRef = useRef<HTMLElement | null>(null);
  const loadedDetailCacheRef = useRef(new Map<string, VideoRecord>());
  const initialReaderOpenedRef = useRef(false);

  useEffect(() => {
    for (const record of videos) {
      if (record.detailLoaded) {
        loadedDetailCacheRef.current.set(record.id, record);
      }
    }
  }, [videos]);

  useEffect(() => {
    if (!showNewParse) return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [showNewParse]);

  const openNewParse = (
    kind: "link" | "upload" = "link",
    source = "",
  ) => {
    newParseTriggerRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    setInitialSource(source);
    setInitialKind(kind);
    setShowNewParse(true);
  };

  const updateVideoPlayback = (
    recordId: string,
    playback: PlaybackCapability,
  ) => {
    setVideos((items) =>
      items.map((item) =>
        item.id === recordId ? { ...item, playback } : item,
      ),
    );
    const cached = loadedDetailCacheRef.current.get(recordId);
    if (cached) {
      loadedDetailCacheRef.current.set(recordId, { ...cached, playback });
    }
  };

  const requestPlaybackDelete = (recordId: string) => {
    playbackDeleteTriggerRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    setPlaybackDeleteTargetId(recordId);
  };

  const confirmPlaybackDelete = async () => {
    if (!playbackDeleteTargetId || playbackDeleting) return;
    const record = videos.find((item) => item.id === playbackDeleteTargetId);
    const identity = record ? apiIdentity(record) : null;
    if (!record || !identity || identity.platform !== "local_upload") {
      setPlaybackDeleteTargetId(null);
      notify("当前媒体不能从这里删除。");
      return;
    }
    setPlaybackDeleting(true);
    try {
      const deleted = await apiClient.deleteRetainedMedia(
        identity.videoId,
        identity.platform,
      );
      updateVideoPlayback(record.id, mapPlaybackCapability(deleted));
      setPlaybackDeleteTargetId(null);
      notify("可播放媒体已删除，字幕、摘要和个人笔记仍然保留。");
    } catch (error) {
      notify(error instanceof Error ? error.message : "媒体删除失败，请稍后重试。");
    } finally {
      setPlaybackDeleting(false);
    }
  };

  const closeNewParse = () => {
    setShowNewParse(false);
    window.requestAnimationFrame(() => {
      newParseTriggerRef.current?.focus();
    });
  };

  useEffect(() => {
    const controller = new AbortController();
    apiClient
      .listVideos({ limit: 100, signal: controller.signal })
      .then((page) => {
        setFacets(mapVideoFacets(page.facets));
        const personalMeta = readSavedMeta();
        const records = mapVideoSearchPageToRecords(page).map((record) =>
          enrichWithMeta(record, personalMeta[record.id]),
        );
        setVideos((current) => {
          const pendingIds = new Set(pendingJobs.map((item) => item.record.id));
          const livePending = current.filter((item) => pendingIds.has(item.id));
          return [...livePending, ...records];
        });
        setDataMode("live");
      })
      .catch(() => {
        if (!controller.signal.aborted) setDataMode("demo");
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    apiClient
      .listJobBatches(1, { signal: controller.signal })
      .then((batches) => {
        const latest = batches[0] ?? null;
        setActiveBatch(latest);
        setBatchPanelOpen(
          latest?.status === "queued" || latest?.status === "running",
        );
      })
      .catch((error) => {
        if (!controller.signal.aborted && error instanceof ApiError) {
          setBatchError(error.message);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setBatchLoading(false);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!activeBatch || activeBatch.active === 0) return undefined;
    const controller = new AbortController();
    const poll = () => {
      apiClient
        .getJobBatch(activeBatch.id, { signal: controller.signal })
        .then((batch) => {
          setActiveBatch(batch);
          setBatchError("");
        })
        .catch((error) => {
          if (!controller.signal.aborted && error instanceof ApiError) {
            setBatchError(error.message);
          }
        });
    };
    const timer = window.setInterval(poll, 1600);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [activeBatch?.active, activeBatch?.id]);

  useEffect(() => {
    if (dataMode !== "live") return undefined;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      apiClient
        .listVideos({
          query: search.trim(),
          tag: tagFilter === "all" ? undefined : tagFilter,
          tagSource: tagFilter === "all" ? undefined : tagSourceFilter,
          platform:
            platformFilter === "all"
              ? undefined
              : platformFilter === "local"
                ? "local_upload"
                : platformFilter,
          primaryCategory:
            activeCategory === "全部" ? undefined : activeCategory,
          secondaryCategory:
            secondaryCategoryFilter === "全部"
              ? undefined
              : secondaryCategoryFilter,
          limit: 100,
          signal: controller.signal,
        })
        .then((page) => {
          setFacets(mapVideoFacets(page.facets));
          const personalMeta = readSavedMeta();
          const records = mapVideoSearchPageToRecords(page).map((record) =>
            enrichWithMeta(record, personalMeta[record.id]),
          );
          setVideos((current) => {
            const currentById = new Map(
              current.map((record) => [record.id, record]),
            );
            const mergedRecords = records.map((record) => {
              const visibleRecord = currentById.get(record.id);
              const loadedRecord = visibleRecord?.detailLoaded
                ? visibleRecord
                : loadedDetailCacheRef.current.get(record.id);
              return preserveLoadedDetail(record, loadedRecord);
            });
            const recordIds = new Set(records.map((record) => record.id));
            const query = search.trim().toLocaleLowerCase("zh-CN");
            const locallyMatched = current.filter((item) => {
              if (recordIds.has(item.id)) return false;
              if (item.id.startsWith("saved-")) return true;
              const meta = personalMeta[item.id];
              if (!meta) return false;
              const matchesTag =
                tagFilter === "all" ||
                (tagSourceFilter === "personal" &&
                  meta.userTags.includes(tagFilter));
              const matchesCategory =
                (activeCategory === "全部" ||
                  meta.primaryCategory === activeCategory) &&
                (secondaryCategoryFilter === "全部" ||
                  meta.secondaryCategory === secondaryCategoryFilter);
              const localSearchable = [
                ...meta.userTags,
                meta.sparkNote,
                meta.titleOverride,
                meta.authorOverride,
                meta.primaryCategory,
                meta.secondaryCategory,
              ]
                .filter((value): value is string => Boolean(value))
                .join(" ")
                .toLocaleLowerCase("zh-CN");
              return (
                matchesTag &&
                matchesCategory &&
                (!query || localSearchable.includes(query))
              );
            });
            return [...locallyMatched, ...mergedRecords];
          });
        })
        .catch(() => {
          // Keep the current list when a search request is interrupted.
        });
    }, 280);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [
    activeCategory,
    dataMode,
    platformFilter,
    search,
    secondaryCategoryFilter,
    tagFilter,
    tagSourceFilter,
  ]);

  useEffect(() => {
    try {
      window.localStorage.setItem(pendingStorageKey, JSON.stringify(pendingJobs));
    } catch {
      // The app remains usable when storage is unavailable.
    }
  }, [pendingJobs]);

  useEffect(() => {
    if (!pendingJobs.some((item) => !["failed", "cancelled"].includes(item.job.status))) {
      return undefined;
    }
    let disposed = false;
    const poll = async () => {
      const current = [...pendingJobs];
      for (const pending of current) {
        if (["failed", "cancelled"].includes(pending.job.status)) continue;
        try {
          const job = await apiClient.getJob(pending.job.id);
          if (disposed) return;
          const mappedStatus = mapJobStatus(job);
          setVideos((items) =>
            items.map((item) =>
              item.id === pending.record.id
                ? { ...item, ...mappedStatus, summary: job.message || item.summary }
                : item,
            ),
          );
          if (
            (job.status === "completed" ||
              job.status === "completed_with_warnings")
          ) {
            if (!job.video_id) {
              setVideos((items) =>
                items.map((item) =>
                  item.id === pending.record.id
                    ? {
                        ...item,
                        status: "failed",
                        statusText: "任务结果不完整",
                        summary:
                          "任务已经结束，但服务没有返回视频标识，无法读取解析详情。",
                      }
                    : item,
                ),
              );
              setPendingJobs((items) =>
                items.filter((item) => item.job.id !== job.id),
              );
              continue;
            }
            const finalId = `${pending.platform}:${job.video_id}`;
            try {
              let detail = await apiClient.getVideoDetail(
                job.video_id,
                pending.platform,
              );
              let resolvedMeta = { ...pending.meta };
              if (pending.focusQuery?.trim()) {
                const focusedResult =
                  await apiClient.createFocusedExtraction(
                    job.video_id,
                    pending.focusQuery,
                    pending.platform,
                  );
                detail = { ...detail, result: focusedResult };
              }
              try {
                const favoriteDetail = await apiClient.setFavorite(
                  job.video_id,
                  true,
                  pending.platform,
                );
                detail = { ...detail, favorite: favoriteDetail.favorite };
              } catch {
                // The result remains readable if favorite persistence fails.
              }
              if (pending.meta.userTags.length) {
                try {
                  const taggedDetail = await apiClient.updateTags(
                    job.video_id,
                    pending.meta.userTags,
                    "add",
                    pending.platform,
                  );
                  detail = {
                    ...detail,
                    favorite: taggedDetail.favorite,
                    personal_tags: taggedDetail.personal_tags,
                  };
                } catch {
                  // Local personal metadata remains available if tag sync fails.
                }
              }
              try {
                const classifiedDetail = await apiClient.updateClassification(
                  job.video_id,
                  pending.meta.primaryCategory,
                  pending.meta.secondaryCategory ?? undefined,
                  pending.platform,
                );
                detail = {
                  ...detail,
                  classification: classifiedDetail.classification,
                };
                resolvedMeta = {
                  ...resolvedMeta,
                  classificationSyncPending: false,
                };
              } catch {
                resolvedMeta = {
                  ...resolvedMeta,
                  classificationSyncPending: true,
                };
              }
              if (
                pending.meta.sparkNote &&
                pending.meta.sparkSyncPending !== false
              ) {
                try {
                  const personalNotes = await apiClient.upsertSpark(
                    job.video_id,
                    pending.meta.sparkNote,
                    "我",
                    pending.platform,
                  );
                  detail = { ...detail, personal_notes: personalNotes };
                  resolvedMeta = {
                    ...resolvedMeta,
                    sparkSyncPending: false,
                  };
                } catch {
                  resolvedMeta = {
                    ...resolvedMeta,
                    sparkSyncPending: true,
                  };
                }
              }
              if (disposed) return;
              const completed = enrichWithMeta(
                mapVideoDetailToRecord(detail),
                resolvedMeta,
              );
              const savedMeta = readSavedMeta();
              savedMeta[completed.id] = resolvedMeta;
              window.localStorage.setItem(
                metaStorageKey,
                JSON.stringify(savedMeta),
              );
              setVideos((items) => [
                completed,
                ...items.filter(
                  (item) =>
                    item.id !== pending.record.id && item.id !== completed.id,
                ),
              ]);
              setActiveId(completed.id);
            } catch {
              const savedMeta = readSavedMeta();
              savedMeta[finalId] = pending.meta;
              window.localStorage.setItem(
                metaStorageKey,
                JSON.stringify(savedMeta),
              );
              setVideos((items) => [
                {
                  ...pending.record,
                  id: finalId,
                  status: "warning",
                  statusText: "详情待刷新",
                  summary:
                    "解析已经完成，但详情暂时无法读取。点击卡片可重新加载。",
                },
                ...items.filter(
                  (item) =>
                    item.id !== pending.record.id && item.id !== finalId,
                ),
              ]);
              setActiveId(finalId);
            }
            setPendingJobs((items) =>
              items.filter((item) => item.job.id !== job.id),
            );
          } else {
            setPendingJobs((items) => {
              let changed = false;
              const nextItems = items.map((item) => {
                if (item.job.id !== job.id) return item;
                const sameSnapshot =
                  item.job.revision === job.revision &&
                  item.job.status === job.status &&
                  item.job.progress === job.progress &&
                  item.job.message === job.message;
                if (sameSnapshot) return item;
                changed = true;
                return { ...item, job };
              });
              return changed ? nextItems : items;
            });
          }
        } catch (error) {
          if (error instanceof ApiError && error.code === "NOT_FOUND") {
            if (disposed) return;
            setVideos((items) =>
              items.map((item) =>
                item.id === pending.record.id
                  ? {
                      ...item,
                      status: "failed",
                      statusText: "任务已失效",
                      summary:
                        "后端已找不到这个任务，可能更换了数据实例。请重新提交解析。",
                    }
                  : item,
              ),
            );
            setPendingJobs((items) =>
              items.filter((item) => item.job.id !== pending.job.id),
            );
          }
          // Transient network failures keep the descriptor for later recovery.
        }
      }
    };
    const timer = window.setInterval(poll, 1600);
    void poll();
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [pendingJobs]);

  const filteredVideos = useMemo(() => {
    const query = search.trim().toLocaleLowerCase("zh-CN");
    return videos.filter((video) => {
      const matchesStatus = status === "all" || video.status === status;
      const matchesPlatform =
        platformFilter === "all" || video.platform === platformFilter;
      const matchesMedia =
        mediaFilter === "all" || video.mediaType === mediaFilter;
      const matchesTag =
        tagFilter === "all" ||
        (tagSourceFilter === "platform"
          ? video.tags.includes(tagFilter)
          : tagSourceFilter === "automatic"
            ? (video.automaticTags ?? []).some(
                (item) => item.name === tagFilter,
              )
            : video.userTags.includes(tagFilter));
      const matchesCategory =
        (activeCategory === "全部" ||
          video.primaryCategory === activeCategory) &&
        (secondaryCategoryFilter === "全部" ||
          video.secondaryCategory === secondaryCategoryFilter);
      const searchable = [
        video.title,
        video.author,
        video.summary,
        ...video.tags,
        ...(video.automaticTags ?? []).map((item) => item.name),
        ...video.userTags,
      ]
        .join(" ")
        .toLocaleLowerCase("zh-CN");
      return (
        matchesStatus &&
        matchesPlatform &&
        matchesMedia &&
        matchesTag &&
        matchesCategory &&
        ((dataMode === "live" && !video.id.startsWith("saved-")) ||
          !query ||
          searchable.includes(query))
      );
    });
  }, [
    dataMode,
    activeCategory,
    mediaFilter,
    platformFilter,
    search,
    status,
    secondaryCategoryFilter,
    tagFilter,
    tagSourceFilter,
    videos,
  ]);

  const jobsByRecord = useMemo(
    () =>
      new Map(
        pendingJobs.map((pending) => [pending.record.id, pending.job] as const),
      ),
    [pendingJobs],
  );

  const activeVideo =
    videos.find((video) => video.id === activeId) ?? filteredVideos[0] ?? videos[0];
  const hasLibraryFilters =
    Boolean(search.trim()) ||
    status !== "all" ||
    platformFilter !== "all" ||
    mediaFilter !== "all" ||
    activeCategory !== "全部" ||
    secondaryCategoryFilter !== "全部" ||
    tagFilter !== "all";

  const clearLibraryFilters = () => {
    setSearch("");
    setStatus("all");
    setPlatformFilter("all");
    setMediaFilter("all");
    setActiveCategory("全部");
    setSecondaryCategoryFilter("全部");
    setTagSourceFilter("personal");
    setTagFilter("all");
  };
  const mobileTagOptions = facets.tags.length
    ? facets.tags.filter((item) => item.source === tagSourceFilter)
    : Array.from(
        new Set(
          videos.flatMap((video) =>
            sourcedTags(video)
              .filter((item) => item.source === tagSourceFilter)
              .map((item) => item.name),
          ),
        ),
      ).map((name) => ({
        name,
        source: tagSourceFilter,
        count: videos.filter((video) =>
          sourcedTags(video).some(
            (item) => item.source === tagSourceFilter && item.name === name,
          ),
        ).length,
      }));

  const notify = (message: string) => {
    setNotice(message);
    window.setTimeout(() => setNotice(""), 2800);
  };

  const toggleBatchSelectionMode = () => {
    setBatchSelectionMode((current) => {
      if (current) setSelectedBatchIds(new Set());
      else setBatchPanelOpen(false);
      return !current;
    });
    setBatchError("");
  };

  const toggleBatchItem = (recordId: string) => {
    const pending = pendingJobs.find((item) => item.record.id === recordId);
    if (!isBatchEligible(pending?.job)) {
      notify(batchEligibilityReason(pending?.job));
      return;
    }
    setSelectedBatchIds((current) => {
      const next = new Set(current);
      if (next.has(recordId)) {
        next.delete(recordId);
        return next;
      }
      if (next.size >= 10) {
        notify("每个批次最多选择 10 个任务。");
        return current;
      }
      next.add(recordId);
      return next;
    });
  };

  const syncBatchJobs = (batch: ApiJobBatch) => {
    const jobs = new Map(batch.items.map((item) => [item.job.id, item.job]));
    setPendingJobs((items) =>
      items.map((item) => {
        const job = jobs.get(item.job.id);
        return job ? { ...item, job } : item;
      }),
    );
    setVideos((items) =>
      items.map((video) => {
        const pending = pendingJobs.find((item) => item.record.id === video.id);
        const job = pending ? jobs.get(pending.job.id) : undefined;
        return job
          ? { ...video, ...mapJobStatus(job), summary: job.message || video.summary }
          : video;
      }),
    );
  };

  const createSelectedBatch = async () => {
    const selected = pendingJobs.filter((item) =>
      selectedBatchIds.has(item.record.id),
    );
    if (selected.length < 2) {
      notify("请至少选择 2 个可操作任务。");
      return;
    }
    if (selected.length > 10 || selected.some((item) => !isBatchEligible(item.job))) {
      notify("选择中包含不可加入批次的任务，请刷新后重新选择。");
      return;
    }
    setBatchCreating(true);
    setBatchError("");
    try {
      const batch = await apiClient.createJobBatch(
        selected.map((item) => ({
          job_id: item.job.id,
          title: item.record.title,
          platform: item.platform,
        })),
      );
      setActiveBatch(batch);
      setBatchPanelOpen(true);
      setBatchSelectionMode(false);
      setSelectedBatchIds(new Set());
      syncBatchJobs(batch);
      notify(`已创建包含 ${batch.total} 个任务的批次。`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "批次创建失败。";
      setBatchError(message);
      notify(message);
    } finally {
      setBatchCreating(false);
    }
  };

  const cancelBatchItem = async (jobId: string) => {
    if (!activeBatch || cancellingBatchJobId) return;
    setCancellingBatchJobId(jobId);
    setBatchError("");
    try {
      const batch = await apiClient.cancelJobBatchItem(activeBatch.id, jobId);
      setActiveBatch(batch);
      syncBatchJobs(batch);
      notify("已取消所选任务，其他任务继续处理。");
    } catch (error) {
      const message = error instanceof Error ? error.message : "任务取消失败。";
      setBatchError(message);
      notify(message);
    } finally {
      setCancellingBatchJobId(null);
    }
  };

  const updatePendingJob = (recordId: string, job: ApiJob) => {
    const mapped = mapJobStatus(job);
    setPendingJobs((items) =>
      items.map((item) =>
        item.record.id === recordId ? { ...item, job } : item,
      ),
    );
    setVideos((items) =>
      items.map((item) =>
        item.id === recordId ? { ...item, ...mapped } : item,
      ),
    );
  };

  const cancelPendingJob = async (recordId: string) => {
    const pending = pendingJobs.find((item) => item.record.id === recordId);
    if (!pending) return;
    try {
      const cancelled = await apiClient.cancelJob(pending.job.id);
      updatePendingJob(recordId, cancelled);
      notify(
        cancelled.status === "completed" ||
          cancelled.status === "completed_with_warnings"
          ? "任务已经完成，结果已保留。"
          : "已请求取消任务，已完成的字幕与元信息会保留。",
      );
    } catch (error) {
      notify(error instanceof Error ? error.message : "取消任务失败。");
    }
  };

  const retryPendingJob = async (recordId: string) => {
    const pending = pendingJobs.find((item) => item.record.id === recordId);
    if (!pending) return;
    try {
      updatePendingJob(recordId, await apiClient.retryJob(pending.job.id));
      notify("任务已从最近检查点继续。");
    } catch (error) {
      notify(error instanceof Error ? error.message : "重试任务失败。");
    }
  };

  const toggleFavorite = async (recordId: string) => {
    const record = videos.find((item) => item.id === recordId);
    const identity = record ? apiIdentity(record) : null;
    if (!record || !identity) return;
    try {
      const detail = await apiClient.setFavorite(
        identity.videoId,
        !record.favorite,
        identity.platform,
      );
      const updated = enrichWithMeta(
        mapVideoDetailToRecord(
          detailForSavedFocus(detail, readSavedMeta()[recordId]),
        ),
        readSavedMeta()[recordId],
      );
      setVideos((items) =>
        items.map((item) => (item.id === recordId ? updated : item)),
      );
      notify(updated.favorite ? "已加入收藏。" : "已取消收藏，解析数据仍然保留。");
    } catch (error) {
      notify(error instanceof Error ? error.message : "收藏状态更新失败。");
    }
  };

  const applyPersonalNotes = (
    recordId: string,
    personalNotes: ApiPersonalNotes,
  ) => {
    const record = videos.find((item) => item.id === recordId);
    if (!record) return;
    setVideos((items) =>
      items.map((item) =>
        item.id === recordId
          ? mergePersonalNotesIntoRecord(item, personalNotes)
          : item,
      ),
    );
    const savedMeta = readSavedMeta();
    const currentMeta = savedMeta[recordId] ?? {
      primaryCategory: record.primaryCategory,
      secondaryCategory: record.secondaryCategory,
      userTags: record.userTags,
      sparkNote: record.sparkNote,
      sparkCreatedAt: record.sparkCreatedAt,
    };
    savedMeta[recordId] = {
      ...currentMeta,
      sparkNote: personalNotes.spark?.content ?? null,
      sparkCreatedAt: personalNotes.spark?.created_at ?? null,
      sparkSyncPending: false,
    };
    try {
      window.localStorage.setItem(
        metaStorageKey,
        JSON.stringify(savedMeta),
      );
    } catch {
      // The server remains authoritative when local metadata cannot be updated.
    }
  };

  const personalNoteActionsFor = (
    record: VideoRecord,
  ): PersonalNoteActions | undefined => {
    const identity = apiIdentity(record);
    if (
      !identity ||
      record.status === "processing" ||
      record.status === "failed"
    ) {
      return undefined;
    }
    return {
      saveSpark: async (content) => {
        const notes = await apiClient.upsertSpark(
          identity.videoId,
          content,
          "我",
          identity.platform,
        );
        applyPersonalNotes(record.id, notes);
      },
      deleteSpark: async () => {
        const notes = await apiClient.deleteSpark(
          identity.videoId,
          identity.platform,
        );
        applyPersonalNotes(record.id, notes);
      },
      createAnnotation: async (targetKey, content) => {
        const notes = await apiClient.createAnnotation(
          identity.videoId,
          targetKey,
          content,
          "我",
          identity.platform,
        );
        applyPersonalNotes(record.id, notes);
      },
      updateAnnotation: async (noteId, content) => {
        const notes = await apiClient.updateAnnotation(
          identity.videoId,
          noteId,
          content,
          "我",
          identity.platform,
        );
        applyPersonalNotes(record.id, notes);
      },
      deleteAnnotation: async (noteId) => {
        const notes = await apiClient.deleteAnnotation(
          identity.videoId,
          noteId,
          identity.platform,
        );
        applyPersonalNotes(record.id, notes);
      },
    };
  };

  const askQuestion = async (recordId: string, question: string) => {
    const record = videos.find((item) => item.id === recordId);
    const identity = record ? apiIdentity(record) : null;
    if (!record || !identity) {
      throw new Error("示例预览不能保存追问，请先连接统一后端。");
    }
    const answer = mapApiVideoQuestion(
      await apiClient.askQuestion(
        identity.videoId,
        question,
        identity.platform,
      ),
    );
    setVideos((items) =>
      items.map((item) => {
        if (item.id !== recordId) return item;
        const existing = item.questions.findIndex(
          (entry) => entry.id === answer.id,
        );
        const questions = [...item.questions];
        if (existing >= 0) questions[existing] = answer;
        else questions.push(answer);
        return { ...item, questions };
      }),
    );
    notify("回答已保存，并附上当前字幕依据。");
  };

  const openVideo = async (id: string, target: "desktop" | "mobile") => {
    setActiveId(id);
    if (target === "desktop") setDesktopView("detail");
    const record = videos.find((item) => item.id === id);
    const identity = record ? apiIdentity(record) : apiIdentityFromResourceKey(id);
    const storedMeta = readSavedMeta()[id];
    if (
      !identity ||
      (record?.points.length &&
        !storedMeta?.sparkSyncPending &&
        !storedMeta?.classificationSyncPending)
    ) {
      return;
    }
    try {
      let detail = await apiClient.getVideoDetail(
        identity.videoId,
        identity.platform,
      );
      const savedMeta = readSavedMeta();
      let meta = savedMeta[id];
      if (
        meta?.sparkNote &&
        meta.sparkSyncPending !== false
      ) {
        try {
          const personalNotes = await apiClient.upsertSpark(
            identity.videoId,
            meta.sparkNote,
            "我",
            identity.platform,
          );
          detail = { ...detail, personal_notes: personalNotes };
          meta = { ...meta, sparkSyncPending: false };
          savedMeta[id] = meta;
          window.localStorage.setItem(
            metaStorageKey,
            JSON.stringify(savedMeta),
          );
        } catch {
          meta = { ...meta, sparkSyncPending: true };
          notify("闪念仍保存在本机，连接恢复后可再次打开视频同步。");
        }
      }
      if (meta?.classificationSyncPending) {
        try {
          const classifiedDetail = await apiClient.updateClassification(
            identity.videoId,
            meta.primaryCategory,
            meta.secondaryCategory ?? undefined,
            identity.platform,
          );
          detail = {
            ...detail,
            classification: classifiedDetail.classification,
          };
          meta = { ...meta, classificationSyncPending: false };
          savedMeta[id] = meta;
          window.localStorage.setItem(metaStorageKey, JSON.stringify(savedMeta));
        } catch {
          notify("分类仍保存在本机，连接恢复后可再次打开视频同步。");
        }
      }
      const loaded = enrichWithMeta(
        mapVideoDetailToRecord(detailForSavedFocus(detail, meta)),
        meta,
      );
      setVideos((items) => items.some((item) => item.id === id)
        ? items.map((item) => (item.id === id ? loaded : item))
        : [loaded, ...items]);
    } catch (error) {
      notify(error instanceof Error ? error.message : "详情加载失败。");
    }
  };

  useEffect(() => {
    if (!initialResourceKey || initialReaderOpenedRef.current || dataMode === "loading") return;
    initialReaderOpenedRef.current = true;
    void openVideo(initialResourceKey, "desktop");
  }, [dataMode, initialResourceKey]);

  const saveDraft = async (draft: SaveDraft) => {
    const id = `saved-${Date.now()}`;
    const matchedLink = draft.file ? null : supportedLinks(draft.sourceUrl)[0];
    if (!draft.file && !matchedLink) {
      notify("没有识别到受支持的视频链接，请返回检查输入。");
      return;
    }
    const platform: ApiPlatform = draft.file
      ? "local_upload"
      : matchedLink!.platform;
    const savedAt = new Intl.DateTimeFormat("zh-CN", {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date());
    const meta: SavedMeta = {
      primaryCategory: draft.primaryCategory,
      secondaryCategory: draft.secondaryCategory || null,
      userTags: draft.tags,
      sparkNote: draft.sparkNote || null,
      sparkCreatedAt: draft.sparkNote ? `收藏于 ${savedAt}` : null,
      sparkSyncPending: Boolean(draft.sparkNote),
      classificationSyncPending: true,
      focusQuery: draft.focusQuery.trim() || undefined,
      titleOverride: draft.titleOverride,
      authorOverride: draft.authorOverride,
      mediaType: draft.file?.type.startsWith("audio/") ? "音频" : "视频",
      coverUrl: draft.coverUrl || null,
    };
    const next: VideoRecord = {
      id,
      platform: platform === "local_upload" ? "local" : platform,
      sourceUrl: /^https?:\/\//i.test(draft.sourceUrl)
        ? draft.sourceUrl
        : null,
      mediaType: draft.file?.type.startsWith("audio/") ? "音频" : "视频",
      title: draft.title,
      author: draft.author,
      duration: 0,
      status: "processing",
      statusText: "AI 解析中",
      summary:
        "字幕尚未获得。当前只保存了公开标题、简介和你的闪念，完成解析后会用字幕重新整理。",
      tags: [],
      userTags: meta.userTags,
      primaryCategory: meta.primaryCategory,
      secondaryCategory: meta.secondaryCategory,
      sparkNote: meta.sparkNote,
      sparkCreatedAt: meta.sparkCreatedAt,
      updatedAt: "刚刚",
      integrity: "暂不可判断",
      subtitleSource: "正在获取",
      coverUrl: meta.coverUrl,
      focusQuery: meta.focusQuery,
      missingInformation: [],
      outline: [],
      points: [],
      evidence: [],
      notes: [],
      questions: [],
    };
    setVideos((current) => [next, ...current]);
    setActiveId(id);
    setExpandedId(id);
    closeNewParse();
    setInitialSource("");
    setDesktopView("library");
    try {
      const job = draft.file
        ? await apiClient.createUploadJob(
            draft.file,
            draft.subtitleFile ?? undefined,
            { retainMedia: draft.retainMedia },
          )
        : await apiClient.createResolutionJob(
            draft.sourceUrl,
            draft.focusQuery,
          );
      setPendingJobs((items) => [
        { job, platform, record: next, meta, focusQuery: draft.focusQuery },
        ...items,
      ]);
      setDataMode("live");
      notify("分类与闪念已保存在本机，真实解析任务已进入队列。");
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "解析任务创建失败。";
      setVideos((items) =>
        items.map((item) =>
          item.id === id
            ? {
                ...item,
                status: "missing",
                statusText: "提交失败",
                summary: message,
              }
            : item,
        ),
      );
      notify(message);
    }
  };

  const readerTarget = initialResourceKey
    ? videos.find((item) => item.id === initialResourceKey)
    : null;
  if (initialResourceKey && !readerTarget?.detailLoaded) {
    return <main className="app-shell embedded-reader-shell"><div className="collection-reader-return"><Button variant="ghost" color="gray" onClick={onCollectionReturn}><ArrowLeft size={19} aria-hidden />返回素材详情</Button><span>深度结果</span></div><div className="reader-target-loading" aria-live="polite"><SpinnerGap className="spin" size={26} aria-hidden /><h1>正在核对深度解析结果</h1><p>只读取当前素材已保存的结果，不会重新执行分析。</p></div></main>;
  }
  if (initialResourceKey && readerTarget) {
    return (
      <main className="app-shell embedded-reader-shell">
        <div className="collection-reader-return">
          <Button variant="ghost" color="gray" onClick={onCollectionReturn}><ArrowLeft size={19} aria-hidden />返回素材详情</Button>
          <span>深度结果</span>
        </div>
        <section className="embedded-result-detail" aria-label="当前素材深度解析结果">
          <ResultDetail
            key={readerTarget.id}
            video={readerTarget}
            scopeId="embedded-reader"
            onNotify={notify}
            job={jobsByRecord.get(readerTarget.id)}
            onCancel={() => void cancelPendingJob(readerTarget.id)}
            onRetry={() => void retryPendingJob(readerTarget.id)}
            onFavorite={apiIdentity(readerTarget) ? () => void toggleFavorite(readerTarget.id) : undefined}
            noteActions={personalNoteActionsFor(readerTarget)}
            onAskQuestion={apiIdentity(readerTarget) ? (question) => askQuestion(readerTarget.id, question) : undefined}
            onPlaybackDeleteRequest={() => requestPlaybackDelete(readerTarget.id)}
          />
        </section>
        <div className="live-notice" aria-live="polite" aria-atomic="true">{notice}</div>
      </main>
    );
  }

  return (
    <main className="app-shell">
      {onCollectionReturn && <div className="collection-reader-return"><Button variant="ghost" color="gray" onClick={onCollectionReturn}><ArrowLeft size={19} aria-hidden />返回素材详情</Button><span>深度解析阅读器</span></div>}
      <div className={`source-mode source-mode-${dataMode}`} role="status">
        {dataMode === "loading"
          ? "正在连接本地解析服务"
          : dataMode === "live"
            ? "已连接本地解析服务"
            : "示例预览：启动后端后自动切换为真实收藏"}
      </div>
      {desktopView === "library" ? (
        <div className="desktop-library-view">
          <LibraryGrid
            videos={filteredVideos}
            facets={facets}
            loading={dataMode === "loading"}
            search={search}
            onSearch={setSearch}
            activeCategory={activeCategory}
            onCategory={(category) => {
              setActiveCategory(category);
              setSecondaryCategoryFilter("全部");
            }}
            onOpen={(id) => void openVideo(id, "desktop")}
            platform={platformFilter}
            onPlatform={setPlatformFilter}
            media={mediaFilter}
            onMedia={setMediaFilter}
            status={status}
            onStatus={setStatus}
            secondaryCategory={secondaryCategoryFilter}
            onSecondaryCategory={setSecondaryCategoryFilter}
            tag={tagFilter}
            onTag={setTagFilter}
            tagSource={tagSourceFilter}
            onTagSource={setTagSourceFilter}
            hasFilters={hasLibraryFilters}
            onClearFilters={clearLibraryFilters}
            jobsByRecord={jobsByRecord}
            onCancel={(id) => void cancelPendingJob(id)}
            onRetry={(id) => void retryPendingJob(id)}
            onFavorite={(id) => void toggleFavorite(id)}
            onImport={() => openNewParse("upload")}
            onNew={() => openNewParse("link")}
            selectionMode={batchSelectionMode}
            selectedIds={selectedBatchIds}
            onToggleSelectionMode={toggleBatchSelectionMode}
            onToggleBatchItem={toggleBatchItem}
            onCreateBatch={() => void createSelectedBatch()}
            batchCreating={batchCreating}
            batchPanelOpen={batchPanelOpen}
            activeBatch={activeBatch}
            batchLoading={batchLoading}
            batchError={batchError}
            cancellingBatchJobId={cancellingBatchJobId}
            onOpenBatchPanel={() => setBatchPanelOpen(true)}
            onCloseBatchPanel={() => setBatchPanelOpen(false)}
            onCancelBatchItem={(jobId) => void cancelBatchItem(jobId)}
          />
        </div>
      ) : (
        <div className="desktop-workspace">
          <LibraryPane
            videos={filteredVideos}
            activeId={activeId}
            onOpen={(id) => void openVideo(id, "desktop")}
            search={search}
            onSearch={setSearch}
            status={status}
            onStatus={setStatus}
          />
          <section className="detail-pane">
            <div className="workspace-topbar">
              <div className="topbar-title">
                <Button
                  size="2"
                  variant="ghost"
                  color="gray"
                  onClick={() => setDesktopView("library")}
                >
                  <GridFour size={17} aria-hidden />
                  收藏库
                </Button>
                <span>快速看懂视频，再决定是否观看</span>
              </div>
              <div className="workspace-topbar-actions">
                <ThemeSwitcher />
                <Button size="3" onClick={() => openNewParse("link")}>
                  <Plus size={18} aria-hidden />
                  新建解析
                </Button>
              </div>
            </div>
            {activeVideo ? (
              <ResultDetail
                key={activeVideo.id}
                video={activeVideo}
                scopeId="desktop"
                onNotify={notify}
                job={jobsByRecord.get(activeVideo.id)}
                onCancel={() => void cancelPendingJob(activeVideo.id)}
                onRetry={() => void retryPendingJob(activeVideo.id)}
                onFavorite={
                  apiIdentity(activeVideo)
                    ? () => void toggleFavorite(activeVideo.id)
                    : undefined
                }
                noteActions={personalNoteActionsFor(activeVideo)}
                onAskQuestion={
                  apiIdentity(activeVideo)
                    ? (question) => askQuestion(activeVideo.id, question)
                    : undefined
                }
                onPlaybackDeleteRequest={() => requestPlaybackDelete(activeVideo.id)}
              />
            ) : (
              <div className="empty-content">
                <BookOpenText size={30} aria-hidden />
                <h2>还没有视频笔记</h2>
                <p>新建解析后，结果会出现在这里。</p>
              </div>
            )}
          </section>
        </div>
      )}

      <div className="mobile-workspace">
        <header className="mobile-header">
          <div>
            <strong>视频知识库</strong>
            <span>快速看懂，再按需核对</span>
          </div>
          <div className="mobile-header-actions">
            <ThemeSwitcher />
            {activeBatch && (
              <IconButton
                size="3"
                variant="soft"
                color="gray"
                onClick={() => setBatchPanelOpen(true)}
                aria-label={`打开任务队列，已完成 ${activeBatch.completed}/${activeBatch.total}`}
              >
                <ListBullets size={20} aria-hidden />
              </IconButton>
            )}
            <IconButton
              size="3"
              onClick={() => openNewParse("link")}
              aria-label="新建解析"
            >
              <Plus size={20} aria-hidden />
            </IconButton>
          </div>
        </header>
        <QuickCapture
          onContinue={(source) => openNewParse("link", source)}
        />
        <div className="mobile-search-row">
          <TextField.Root
            size="3"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索标题、作者、字幕"
            aria-label="搜索视频"
          >
            <TextField.Slot>
              <MagnifyingGlass size={18} aria-hidden />
            </TextField.Slot>
          </TextField.Root>
        </div>
        <div className="mobile-filter-strip" aria-label="按状态筛选">
          {statusFilters.map((item) => (
            <button
              type="button"
              key={item.value}
              className={status === item.value ? "is-active" : ""}
              onClick={() => setStatus(item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="mobile-secondary-filters">
          <label>
            <span className="sr-only">按平台筛选</span>
            <select
              value={platformFilter}
              onChange={(event) =>
                setPlatformFilter(
                  event.target.value as VideoRecord["platform"] | "all",
                )
              }
            >
              <option value="all">全部平台</option>
              <option value="bilibili">B站</option>
              <option value="douyin">抖音</option>
              <option value="local">本地</option>
            </select>
          </label>
          <label>
            <span className="sr-only">按一级分类筛选</span>
            <select
              value={activeCategory}
              onChange={(event) => {
                setActiveCategory(event.target.value);
                setSecondaryCategoryFilter("全部");
              }}
            >
              <option value="全部">全部分类</option>
              {(facets.categories.length
                ? facets.categories
                : categoryOptions.map((category) => ({
                    primaryCategory: category,
                    count: videos.filter(
                      (video) => video.primaryCategory === category,
                    ).length,
                    children: [],
                  }))
              ).map((item) => (
                <option value={item.primaryCategory} key={item.primaryCategory}>
                  {item.primaryCategory}（{item.count}）
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="sr-only">按二级分类筛选</span>
            <select
              value={secondaryCategoryFilter}
              onChange={(event) => setSecondaryCategoryFilter(event.target.value)}
            >
              <option value="全部">全部子类</option>
              {(facets.categories.find(
                (item) => item.primaryCategory === activeCategory,
              )?.children ?? []).map((item) => (
                <option
                  value={item.secondaryCategory}
                  key={item.secondaryCategory}
                >
                  {item.secondaryCategory}（{item.count}）
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="sr-only">按标签来源筛选</span>
            <select
              value={tagSourceFilter}
              onChange={(event) => {
                setTagSourceFilter(event.target.value as TagSourceFilter);
                setTagFilter("all");
              }}
            >
              <option value="platform">平台标签</option>
              <option value="automatic">自动标签</option>
              <option value="personal">个人标签</option>
            </select>
          </label>
          <label className="mobile-tag-filter">
            <span className="sr-only">按标签值筛选</span>
            <select
              aria-label={`按${tagSourceLabels[tagSourceFilter]}标签筛选`}
              value={tagFilter}
              onChange={(event) => setTagFilter(event.target.value)}
            >
              <option value="all">全部{tagSourceLabels[tagSourceFilter]}标签</option>
              {mobileTagOptions.map((item) => (
                <option value={item.name} key={`${item.source}:${item.name}`}>
                  {item.name}（{item.count}）
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="sr-only">按媒体类型筛选</span>
            <select
              value={mediaFilter}
              onChange={(event) =>
                setMediaFilter(
                  event.target.value as VideoRecord["mediaType"] | "all",
                )
              }
            >
              <option value="all">全部媒体</option>
              <option value="视频">视频</option>
              <option value="音频">音频</option>
            </select>
          </label>
        </div>
        <div className="mobile-filter-summary" role="status">
          <span>
            {dataMode === "loading"
              ? "正在加载收藏库"
              : `找到 ${filteredVideos.length} 条`}
          </span>
          {hasLibraryFilters && (
            <button type="button" onClick={clearLibraryFilters}>
              <X size={15} aria-hidden />
              清除筛选
            </button>
          )}
        </div>
        <div className="mobile-batch-entry">
          <button type="button" onClick={toggleBatchSelectionMode}>
            <CheckSquare size={17} aria-hidden />
            {batchSelectionMode ? "退出批量选择" : "批量选择任务"}
          </button>
          <span>只可选择当前收藏中的真实任务</span>
        </div>
        <section className="mobile-feed" aria-label="视频卡片流">
          {filteredVideos.map((video) => {
            const job = jobsByRecord.get(video.id);
            return (
            <MobileVideoCard
              key={video.id}
              video={video}
              expanded={expandedId === video.id}
              onToggle={() =>
                setExpandedId((current) => {
                  const nextId = current === video.id ? null : video.id;
                  if (nextId) void openVideo(nextId, "mobile");
                  return nextId;
                })
              }
              onNotify={notify}
              job={job}
              onCancel={() => void cancelPendingJob(video.id)}
              onRetry={() => void retryPendingJob(video.id)}
              onFavorite={
                apiIdentity(video)
                  ? () => void toggleFavorite(video.id)
                  : undefined
              }
              noteActions={personalNoteActionsFor(video)}
              onAskQuestion={
                apiIdentity(video)
                  ? (question) => askQuestion(video.id, question)
                  : undefined
              }
              selectionMode={batchSelectionMode}
              selected={selectedBatchIds.has(video.id)}
              batchEligible={isBatchEligible(job)}
              onToggleBatchItem={() => toggleBatchItem(video.id)}
              onPlaybackDeleteRequest={() => requestPlaybackDelete(video.id)}
            />
          )})}
          {filteredVideos.length === 0 && (
            <div className="empty-content">
              <MagnifyingGlass size={28} aria-hidden />
              <h2>没有匹配的视频</h2>
              <p>换一个关键词或清除筛选条件。</p>
              {hasLibraryFilters && (
                <Button variant="soft" color="gray" onClick={clearLibraryFilters}>
                  清除筛选
                </Button>
              )}
            </div>
          )}
        </section>
        {batchPanelOpen && (
          <div className="mobile-batch-panel-wrap">
            <BatchQueuePanel
              batch={activeBatch}
              loading={batchLoading}
              error={batchError}
              cancellingJobId={cancellingBatchJobId}
              onClose={() => setBatchPanelOpen(false)}
              onCancelItem={(jobId) => void cancelBatchItem(jobId)}
            />
          </div>
        )}
      </div>

      {batchSelectionMode && (
        <div className="mobile-batch-actionbar" role="status">
          <span>
            已选择 <strong>{selectedBatchIds.size}</strong>/10 项
          </span>
          <Button
            size="3"
            disabled={selectedBatchIds.size < 2 || batchCreating}
            onClick={() => void createSelectedBatch()}
          >
            {batchCreating ? "创建中" : "创建批次"}
          </Button>
        </div>
      )}

      <AlertDialog.Root
        open={Boolean(playbackDeleteTargetId)}
        onOpenChange={(open) => {
          if (!open && !playbackDeleting) setPlaybackDeleteTargetId(null);
        }}
      >
        <AlertDialog.Content
          className="media-delete-dialog"
          maxWidth="440px"
          onCloseAutoFocus={(event) => {
            const trigger = playbackDeleteTriggerRef.current;
            if (!trigger?.isConnected) return;
            event.preventDefault();
            trigger.focus();
          }}
        >
            <span className="media-delete-icon">
              <Trash size={22} aria-hidden />
            </span>
            <div>
              <AlertDialog.Title>只删除可播放媒体？</AlertDialog.Title>
              <AlertDialog.Description>
                字幕、摘要、证据时间戳、闪念和个人备注都会继续保留。
              </AlertDialog.Description>
            </div>
            <div className="media-delete-actions">
              <AlertDialog.Cancel>
                <Button
                  size="3"
                  variant="soft"
                  color="gray"
                  disabled={playbackDeleting}
                >
                  取消
                </Button>
              </AlertDialog.Cancel>
              <AlertDialog.Action>
                <Button
                  size="3"
                  color="red"
                  disabled={playbackDeleting}
                  onClick={() => void confirmPlaybackDelete()}
                >
                  {playbackDeleting ? "删除中" : "确认只删除媒体"}
                </Button>
              </AlertDialog.Action>
            </div>
        </AlertDialog.Content>
      </AlertDialog.Root>

      {showNewParse && (
        <div
          className="review-overlay"
          role="dialog"
          aria-modal="true"
          aria-label="新建收藏"
        >
          <NewParsePanel
            onClose={closeNewParse}
            onSave={saveDraft}
            initialSource={initialSource}
            initialKind={initialKind}
          />
        </div>
      )}

      <div className="live-notice" aria-live="polite" aria-atomic="true">
        {notice}
      </div>
    </main>
  );
}

export function LegacyApp({
  embedded = false,
  initialResourceKey,
  onCollectionReturn,
}: {
  embedded?: boolean;
  initialResourceKey?: string;
  expectedAnalysisJobId?: string;
  expectedResultRevision?: string;
  onCollectionReturn?: () => void;
} = {}) {
  const content = <AppContent initialResourceKey={initialResourceKey} onCollectionReturn={onCollectionReturn} />;
  return embedded ? content : <ThemeProvider>{content}</ThemeProvider>;
}
