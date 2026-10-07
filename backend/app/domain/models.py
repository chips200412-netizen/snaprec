from __future__ import annotations

import ipaddress
import re
import unicodedata
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator
from pydantic.json_schema import SkipJsonSchema

ClaimType = Literal["video_fact", "creator_opinion", "model_inference"]
Platform = Literal["local_upload", "bilibili", "douyin"]
JobStatus = Literal[
    "queued",
    "resolving",
    "fetching_metadata",
    "fetching_subtitles",
    "transcribing",
    "cleaning",
    "extracting",
    "saving",
    "completed",
    "completed_with_warnings",
    "failed",
    "cancelled",
]
MentionStatus = Literal[
    "explicit",
    "inferred",
    "not_mentioned",
    "unknown_incomplete_transcript",
]
ItemType = Literal[
    "key_point",
    "important_data",
    "case",
    "argument",
    "step",
    "risk",
    "quote",
    "supplementary_context",
]


def _inference_is_labelled(text: str) -> bool:
    return any(marker in text for marker in ("归纳", "推断", "推测", "根据上下文"))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_DNS_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def validate_public_url_shape(value: str, *, label: str) -> str:
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError(f"{label}不能包含空白或控制字符")
    if _INVALID_PERCENT_ESCAPE.search(value):
        raise ValueError(f"{label}包含无效的百分号转义")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"{label}格式无效") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{label}必须是无用户凭据的 HTTP/HTTPS URL")
    hostname = parsed.hostname.rstrip(".")
    if not hostname:
        raise ValueError(f"{label}缺少有效主机")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        try:
            ascii_host = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError(f"{label}主机名无效") from exc
        labels = ascii_host.split(".")
        if len(ascii_host) > 253 or any(not _DNS_LABEL.fullmatch(item) for item in labels):
            raise ValueError(f"{label}主机名无效")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{label}端口无效")
    return value


CollectionSourceKind = Literal["video", "article", "webpage", "audio", "other"]
CollectionPlatform = Literal[
    "douyin",
    "bilibili",
    "xiaohongshu",
    "youtube",
    "web",
    "other",
    "local_upload",
]
MetadataStatus = Literal["recognized", "generic", "metadata_unavailable"]


class MetadataTitle(StrictModel):
    value: str = ""
    source: Literal[
        "open_graph",
        "page_metadata",
        "platform_public",
        "share_text",
        "legacy_import",
        "none",
    ] = "none"
    fetched_at: str = ""

    @model_validator(mode="after")
    def validate_none_source(self):
        if self.source == "none" and self.value:
            raise ValueError("none provenance 不能携带标题值")
        return self


class MetadataAuthor(StrictModel):
    value: str = ""
    source: Literal[
        "open_graph",
        "page_metadata",
        "platform_public",
        "share_text",
        "user",
        "legacy_import",
        "none",
    ] = "none"
    fetched_at: str = ""

    @model_validator(mode="after")
    def validate_none_source(self):
        if self.source == "none" and self.value:
            raise ValueError("none provenance 不能携带作者值")
        return self


class MetadataCover(StrictModel):
    value: str = ""
    source: Literal[
        "open_graph", "platform_public", "legacy_import", "none"
    ] = "none"
    fetched_at: str = ""

    @model_validator(mode="after")
    def validate_none_source(self):
        if self.source == "none" and self.value:
            raise ValueError("none provenance 不能携带封面值")
        if self.value:
            validate_public_url_shape(self.value, label="封面链接")
        return self


class MetadataSourceCopy(StrictModel):
    value: str = ""
    source: Literal[
        "page_description",
        "platform_description",
        "share_text",
        "legacy_import",
        "none",
    ] = "none"
    fetched_at: str = ""

    @model_validator(mode="after")
    def validate_none_source(self):
        if self.source == "none" and self.value:
            raise ValueError("none provenance 不能携带来源文案值")
        return self


class MetadataPlatformTag(StrictModel):
    value: str = Field(min_length=1)
    source: Literal["platform_public", "page_metadata", "share_text", "legacy_import"]

    @field_validator("value")
    @classmethod
    def reject_blank_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("平台标签不能为空")
        return value


class SourceMetadata(StrictModel):
    title: MetadataTitle = Field(default_factory=MetadataTitle)
    author: MetadataAuthor = Field(default_factory=MetadataAuthor)
    cover_url: MetadataCover = Field(default_factory=MetadataCover)
    source_copy: MetadataSourceCopy = Field(default_factory=MetadataSourceCopy)
    platform_tags: list[MetadataPlatformTag] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OrganizationSuggestion(StrictModel):
    primary_category: str = Field(default="", max_length=64)
    secondary_category: str = Field(default="", max_length=64)
    tags: list[str] = Field(default_factory=list, max_length=50)
    basis: Literal["public_metadata"] = "public_metadata"
    method: Literal["deterministic", "llm"]
    status: Literal["generated", "insufficient_metadata", "failed"]

    @field_validator("tags")
    @classmethod
    def normalize_suggestion_tags(cls, values: list[str]) -> list[str]:
        normalized: dict[str, str] = {}
        for value in values:
            display = " ".join(unicodedata.normalize("NFKC", value).strip().split())
            if not display:
                continue
            if len(display) > 64:
                display = display[:64].rstrip()
            normalized.setdefault(display.casefold(), display)
        return list(normalized.values())


_MAX_RAW_EDITABLE_TAGS = 500
_MAX_RAW_EDITABLE_TEXT_LENGTH = 4096


def _normalize_editable_tags(values: Any, *, label: str) -> Any:
    if not isinstance(values, (list, tuple)):
        return values
    if len(values) > _MAX_RAW_EDITABLE_TAGS:
        raise ValueError(f"{label}原始输入不能超过 {_MAX_RAW_EDITABLE_TAGS} 项")
    normalized: dict[str, str] = {}
    for value in values:
        if not isinstance(value, str):
            return values
        if len(value) > _MAX_RAW_EDITABLE_TEXT_LENGTH:
            raise ValueError(
                f"单个{label}原始输入不能超过 "
                f"{_MAX_RAW_EDITABLE_TEXT_LENGTH} 个字符"
            )
        display = " ".join(unicodedata.normalize("NFKC", value).strip().split())
        if not display:
            raise ValueError(f"{label}不能为空")
        if len(display) > 64:
            raise ValueError(f"单个{label}不能超过 64 个字符")
        normalized.setdefault(display.casefold(), display)
    return list(normalized.values())


class OrganizationConfirmation(StrictModel):
    primary_category: str = ""
    secondary_category: str = ""
    organization_tags: list[str] = Field(default_factory=list)


class CollectionOrganizationConfirmationUpdate(StrictModel):
    primary_category: str = Field(max_length=64)
    secondary_category: str = Field(max_length=64)
    organization_tags: list[str] = Field(max_length=50)

    @field_validator("primary_category", "secondary_category", mode="before")
    @classmethod
    def normalize_categories(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return unicodedata.normalize("NFKC", value).strip()

    @field_validator("organization_tags", mode="before")
    @classmethod
    def normalize_organization_tags(cls, values: Any) -> Any:
        return _normalize_editable_tags(values, label="整理标签")


class PersonalInspirationDraft(StrictModel):
    content: str = Field(min_length=1, max_length=4000)
    input_mode: Literal["text", "voice"]
    transcription_status: Literal[
        "not_applicable", "draft", "completed", "failed"
    ]

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("灵感内容不能为空白")
        return value

    @model_validator(mode="after")
    def validate_saved_state(self):
        expected = "not_applicable" if self.input_mode == "text" else "completed"
        if self.transcription_status != expected:
            raise ValueError("保存的灵感必须是已确认文字或已完成的语音转写")
        return self


class CollectionInspirationUpdate(StrictModel):
    content: str = Field(min_length=1, max_length=4000)
    input_mode: Literal["text", "voice"]
    transcription_status: Literal[
        "not_applicable", "draft", "completed", "failed"
    ]

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("灵感内容不能为空白")
        return value


class PersonalInspiration(PersonalInspirationDraft):
    id: str = Field(min_length=1)
    collection_item_id: str = Field(min_length=1)
    created_at: str
    updated_at: str


class InspirationTranscriptionDraft(StrictModel):
    content: str = Field(min_length=1, max_length=4000)
    input_mode: Literal["voice"] = "voice"
    transcription_status: Literal["draft"] = "draft"

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("语音转写草稿不能为空")
        return value


class CollectionPreviewRequest(StrictModel):
    input_text: str = Field(min_length=1, max_length=10000)
    refresh_metadata: bool = False


class CollectionPreview(StrictModel):
    preview_id: str = Field(min_length=1)
    original_input: str
    source_url: str
    canonical_url: str
    identity_url: str
    source_kind: CollectionSourceKind
    platform: CollectionPlatform
    metadata_status: MetadataStatus
    metadata: SourceMetadata
    organization_suggestion: OrganizationSuggestion = Field(
        default_factory=lambda: OrganizationSuggestion(
            primary_category="",
            secondary_category="",
            tags=[],
            method="deterministic",
            status="insufficient_metadata",
        )
    )
    created_at: str
    expires_at: str


class CollectionItemCreateRequest(StrictModel):
    preview_id: str = Field(min_length=1, max_length=200)
    selected_source_topic_indices: list[Annotated[int, Field(strict=True, ge=0)]] | SkipJsonSchema[None] = None
    user_title: str | None = Field(default=None, max_length=500)
    user_author: str | None = Field(default=None, max_length=200)
    user_cover_asset_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    untitled_confirmed: bool = False
    organization_confirmation: OrganizationConfirmation = Field(
        default_factory=OrganizationConfirmation
    )
    personal_tags: list[str] = Field(default_factory=list)
    inspiration: PersonalInspirationDraft | None = None

    @field_validator("selected_source_topic_indices", mode="before")
    @classmethod
    def require_explicit_topic_array(cls, value: Any) -> Any:
        # Only omission activates the legacy mode; JSON null is not a choice.
        if not isinstance(value, list):
            raise ValueError("来源话题选择必须为索引数组")
        return value

    @model_serializer(mode="wrap")
    def preserve_omitted_topic_choice(self, handler):
        payload = handler(self)
        if self.selected_source_topic_indices is None:
            payload.pop("selected_source_topic_indices", None)
        return payload

    @field_validator("user_title", mode="before")
    @classmethod
    def normalize_empty_user_title(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        normalized = value.strip()
        return normalized or None

    @field_validator("user_author", mode="before")
    @classmethod
    def normalize_user_author(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        normalized = " ".join(unicodedata.normalize("NFKC", value).strip().split())
        if len(normalized) > 200:
            raise ValueError("我补充的作者最多 200 个字符")
        return normalized or None

    @field_validator("organization_confirmation", mode="before")
    @classmethod
    def limit_raw_organization_tags(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for field_name in ("primary_category", "secondary_category"):
                field_value = value.get(field_name, "")
                if (
                    isinstance(field_value, str)
                    and len(field_value) > _MAX_RAW_EDITABLE_TEXT_LENGTH
                ):
                    raise ValueError(
                        "分类原始输入不能超过 "
                        f"{_MAX_RAW_EDITABLE_TEXT_LENGTH} 个字符"
                    )
            tags = value.get("organization_tags", [])
            if isinstance(tags, (list, tuple)) and len(tags) > _MAX_RAW_EDITABLE_TAGS:
                raise ValueError(
                    "整理标签原始输入不能超过 "
                    f"{_MAX_RAW_EDITABLE_TAGS} 项"
                )
        return value

    @field_validator("personal_tags", mode="before")
    @classmethod
    def normalize_personal_tags(cls, values: Any) -> Any:
        normalized = _normalize_editable_tags(values, label="个人标签")
        if isinstance(normalized, list) and len(normalized) > 50:
            raise ValueError("个人标签最终不能超过 50 项")
        return normalized


class CollectionItemUpdateRequest(StrictModel):
    expected_revision: int = Field(ge=1, strict=True)
    user_title: str | None = Field(max_length=500)
    user_author: str | None = Field(default=None, max_length=200)
    user_cover_asset_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    organization_confirmation: CollectionOrganizationConfirmationUpdate
    personal_tags: list[str] = Field(max_length=50)
    inspiration: CollectionInspirationUpdate | None

    @field_validator("user_title", mode="before")
    @classmethod
    def normalize_empty_user_title(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        normalized = value.strip()
        return normalized or None

    @field_validator("user_author", mode="before")
    @classmethod
    def normalize_user_author(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        normalized = " ".join(unicodedata.normalize("NFKC", value).strip().split())
        if len(normalized) > 200:
            raise ValueError("我补充的作者最多 200 个字符")
        return normalized or None

    @field_validator("personal_tags", mode="before")
    @classmethod
    def normalize_personal_tags(cls, values: Any) -> Any:
        return _normalize_editable_tags(values, label="个人标签")


class CollectionItemTrustedCreate(StrictModel):
    source_kind: CollectionSourceKind
    platform: CollectionPlatform
    original_input: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    canonical_url: str = ""
    metadata_status: MetadataStatus
    user_title: str | None = Field(default=None, max_length=500)
    untitled_confirmed: bool = False
    metadata: SourceMetadata
    organization_suggestion: OrganizationSuggestion
    organization_confirmation: OrganizationConfirmation
    personal_tags: list[str] = Field(default_factory=list, max_length=50)
    inspiration: PersonalInspirationDraft | None = None

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        return cls._validated_http_url(value, allow_empty=False)

    @field_validator("canonical_url")
    @classmethod
    def validate_canonical_url(cls, value: str) -> str:
        return cls._validated_http_url(value, allow_empty=True)

    @staticmethod
    def _validated_http_url(value: str, *, allow_empty: bool) -> str:
        if allow_empty and not value:
            return value
        return validate_public_url_shape(value, label="收藏链接")

    @field_validator("user_title")
    @classmethod
    def normalize_empty_user_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("personal_tags")
    @classmethod
    def normalize_personal_tags(cls, values: list[str]) -> list[str]:
        normalized: dict[str, str] = {}
        for value in values:
            display = " ".join(unicodedata.normalize("NFKC", value).strip().split())
            if not display:
                raise ValueError("个人标签不能为空")
            if len(display) > 64:
                raise ValueError("单个个人标签不能超过 64 个字符")
            normalized.setdefault(display.casefold(), display)
        return list(normalized.values())

    @model_validator(mode="after")
    def validate_title_eligibility(self):
        user_title = (self.user_title or "").strip()
        source_title = (
            self.metadata.title.value.strip()
            if self.metadata.title.source != "none"
            else ""
        )
        if not user_title and not source_title and not self.untitled_confirmed:
            raise ValueError("无可用标题时必须明确确认以未命名收藏保存")
        sources = (
            self.metadata.title.source,
            self.metadata.author.source,
            self.metadata.cover_url.source,
            self.metadata.source_copy.source,
            *(tag.source for tag in self.metadata.platform_tags),
        )
        if "legacy_import" in sources:
            raise ValueError("legacy_import 仅允许数据库迁移生成")
        sourced_values = (
            (self.metadata.title.value, self.metadata.title.source),
            (self.metadata.author.value, self.metadata.author.source),
            (self.metadata.cover_url.value, self.metadata.cover_url.source),
            (self.metadata.source_copy.value, self.metadata.source_copy.source),
            *((tag.value, tag.source) for tag in self.metadata.platform_tags),
        )
        public_sources = {
            source
            for value, source in sourced_values
            if value.strip() and source not in {"none", "share_text", "user"}
        }
        if self.metadata_status == "recognized" and not public_sources.intersection(
            {"platform_public", "platform_description"}
        ):
            raise ValueError("recognized 必须携带平台公开元信息")
        if self.metadata_status == "generic" and not public_sources.intersection(
            {"open_graph", "page_metadata", "page_description"}
        ):
            raise ValueError("generic 必须携带网页公开元信息")
        if self.metadata_status == "metadata_unavailable" and public_sources:
            raise ValueError("metadata_unavailable 不能携带可用公开元信息")
        return self


class CollectionItem(StrictModel):
    id: str = Field(min_length=1)
    source_kind: CollectionSourceKind
    platform: CollectionPlatform
    original_input: str
    source_url: str
    canonical_url: str
    identity_url: str = Field(min_length=1)
    metadata_status: MetadataStatus
    user_title: str | None = None
    user_author: str | None = None
    user_cover_asset_id: str | None = None
    display_title: str = Field(min_length=1)
    metadata: SourceMetadata
    organization_suggestion: OrganizationSuggestion
    organization_confirmation: OrganizationConfirmation
    personal_tags: list[str] = Field(default_factory=list)
    selected_source_topic_indices: list[Annotated[int, Field(strict=True, ge=0)]] | None = None
    inspiration: PersonalInspiration | None = None
    deep_analysis_resource_key: str | None = None
    created_at: str
    revision: int = Field(ge=1)
    updated_at: str


class CollectionListItem(StrictModel):
    id: str = Field(min_length=1)
    display_title: str = Field(min_length=1)
    platform: CollectionPlatform
    primary_category: str = Field(default="", max_length=64)
    source_author: str = ""
    user_author: str | None = None
    has_user_cover: bool = False
    cover_url: str = ""
    created_at: str
    updated_at: str


class CollectionPlatformFacet(StrictModel):
    platform: CollectionPlatform
    count: int = Field(ge=1)


class CollectionSecondaryCategoryFacet(StrictModel):
    secondary_category: str = Field(min_length=1, max_length=64)
    count: int = Field(ge=1)


class CollectionCategoryFacet(StrictModel):
    primary_category: str = Field(min_length=1, max_length=64)
    count: int = Field(ge=1)
    children: list[CollectionSecondaryCategoryFacet] = Field(default_factory=list)


class CollectionTagFacet(StrictModel):
    # Historical imports may contain platform-owned tags longer than the
    # current 64-character limit for user-authored tags. The list contract
    # must restore those values losslessly so they remain filterable.
    name: str = Field(min_length=1, max_length=500)
    source: Literal["platform", "organization", "personal"]
    count: int = Field(ge=1)


class CollectionListFacets(StrictModel):
    platforms: list[CollectionPlatformFacet] = Field(default_factory=list)
    categories: list[CollectionCategoryFacet] = Field(default_factory=list)
    tags: list[CollectionTagFacet] = Field(default_factory=list)


class CollectionSearchPage(StrictModel):
    items: list[CollectionListItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None = None
    facets: CollectionListFacets


DeepAnalysisState = Literal[
    "unavailable",
    "ready",
    "queued",
    "running",
    "completed",
    "limited",
    "failed",
    "timed_out",
]


class CollectionDeepAnalysisSnapshot(StrictModel):
    material_id: str = Field(min_length=1)
    analysis_job_id: str | None = None
    state: DeepAnalysisState
    config_version: str = Field(min_length=1)
    job_revision: int = Field(default=0, ge=0)
    failed_stage: str = ""
    can_start: bool = False
    can_retry: bool = False
    can_view_result: bool = False
    result_id: str | None = None
    result_revision: str = ""
    result_kind: Literal["none", "complete", "limited"] = "none"
    updated_at: str = ""
    limitation: str = ""
    error_code: str | None = None
    error_message: str = ""


class Segment(StrictModel):
    id: str = Field(min_length=1)
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, ge=0)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_times(self):
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("segment end must not precede start")
        return self


class Evidence(StrictModel):
    id: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    claim_type: ClaimType
    evidence: str = Field(min_length=1)
    segment_ids: list[str] = Field(min_length=1)
    start_time: float | None = Field(default=None, ge=0)
    end_time: float | None = Field(default=None, ge=0)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_inference_label(self):
        if self.claim_type == "model_inference" and not _inference_is_labelled(self.claim):
            raise ValueError("model_inference claim must be explicitly labelled as inference")
        return self


class ExtractionItem(StrictModel):
    text: str = Field(min_length=1)
    item_type: ItemType
    claim_type: ClaimType
    evidence_refs: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_inference_label(self):
        if self.claim_type == "model_inference" and not _inference_is_labelled(self.text):
            raise ValueError("model_inference item must be explicitly labelled as inference")
        return self


class FullExtraction(StrictModel):
    key_points: list[ExtractionItem] = Field(default_factory=list)
    important_data: list[ExtractionItem] = Field(default_factory=list)
    cases_and_arguments: list[ExtractionItem] = Field(default_factory=list)
    steps: list[ExtractionItem] = Field(default_factory=list)
    risks: list[ExtractionItem] = Field(default_factory=list)
    quotes: list[ExtractionItem] = Field(default_factory=list)

    def all_items(self) -> list[ExtractionItem]:
        return [
            *self.key_points,
            *self.important_data,
            *self.cases_and_arguments,
            *self.steps,
            *self.risks,
            *self.quotes,
        ]


class AutomaticTag(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    confidence: float = Field(ge=0, le=1)
    generation_method: Literal["llm", "deterministic"]


class AutomaticTagging(StrictModel):
    status: Literal[
        "generated", "skipped_no_transcript", "failed", "not_generated"
    ] = "not_generated"
    tags: list[AutomaticTag] = Field(default_factory=list, max_length=20)
    generator_id: str = ""
    generator_version: str = ""
    transcript_hash: str = ""
    generated_at: str = ""
    warning: str = ""

    @model_validator(mode="after")
    def validate_status_payload(self):
        if self.status != "generated" and self.tags:
            raise ValueError("only generated automatic tagging may contain tags")
        return self


class VideoClassification(StrictModel):
    primary_category: str = Field(default="暂未分类", min_length=1, max_length=64)
    secondary_category: str = Field(default="", max_length=64)
    updated_at: str = ""


class RetainedMedia(StrictModel):
    platform: Literal["local_upload"] = "local_upload"
    video_id: str = Field(min_length=1)
    storage_key: str = Field(pattern=r"^[0-9a-f]{32}$")
    mime_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(gt=0)
    created_at: str = ""
    updated_at: str = ""


class PlaybackCapability(StrictModel):
    availability: Literal["available", "unavailable"] = "unavailable"
    kind: Literal["retained_local", "none"] = "none"
    stream_url: str = ""
    mime_type: str = ""
    size_bytes: int = Field(default=0, ge=0)
    supports_range: bool = False
    reason: Literal[
        "available",
        "not_retained",
        "unsupported_source",
        "unsupported_browser_format",
        "missing_file",
    ] = "not_retained"


class FocusedAnswer(StrictModel):
    mention_status: MentionStatus
    is_mentioned: bool | None = None
    direct_answer: str = Field(min_length=1)
    key_points: list[ExtractionItem] = Field(default_factory=list)
    supporting_segments: list[Evidence] = Field(default_factory=list)
    supplementary_context: list[ExtractionItem] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def apply_compatibility_mapping(self):
        expected = {
            "explicit": True,
            "inferred": True,
            "not_mentioned": False,
            "unknown_incomplete_transcript": None,
        }[self.mention_status]
        self.is_mentioned = expected
        if (
            self.mention_status == "not_mentioned"
            and self.direct_answer != "该视频没有明确讨论这个问题。"
        ):
            raise ValueError("not_mentioned must use the fixed direct answer")
        return self

    def all_items(self) -> list[ExtractionItem]:
        return [*self.key_points, *self.supplementary_context]


class VideoResult(StrictModel):
    platform: Platform
    source_url: str
    canonical_url: str
    video_id: str = Field(min_length=1)
    author: str
    title: str
    description: str
    tags: list[str]
    duration: float = Field(ge=0)
    cover_url: str
    subtitle_source: Literal["official", "public_page", "asr", "user_upload", "none"]
    raw_transcript: str
    clean_transcript: str
    segments: list[Segment]
    focus_query: str
    extraction_mode: Literal["full", "focused"]
    summary: str = Field(max_length=100)
    full_extraction: FullExtraction
    evidence: list[Evidence]
    focused_answer: FocusedAnswer | None = None
    automatic_tagging: AutomaticTagging = Field(
        default_factory=AutomaticTagging
    )
    warnings: list[str] = Field(default_factory=list)

    @field_validator("focused_answer", mode="before")
    @classmethod
    def accept_legacy_empty_focused_answer(cls, value):
        return None if value == {} else value

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class Job(StrictModel):
    id: str
    status: JobStatus
    progress: int = Field(ge=0, le=100)
    error_code: str | None = None
    message: str = ""
    video_id: str | None = None
    retry_count: int = Field(default=0, ge=0)
    max_retries: int = Field(default=2, ge=0)
    retryable: bool = False
    cancel_requested: bool = False
    warnings: list[str] = Field(default_factory=list)
    checkpoint: str = "none"
    heartbeat_at: str = ""
    revision: int = Field(default=0, ge=0)
    lease_owner: str = ""
    lease_expires_at: str = ""


class JobBatchItem(StrictModel):
    position: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=500)
    platform: Platform
    job: Job


class JobBatch(StrictModel):
    id: str = Field(min_length=1)
    status: Literal[
        "queued", "running", "completed", "completed_with_issues"
    ]
    total: int = Field(ge=2, le=10)
    completed: int = Field(ge=0)
    active: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    created_at: str
    updated_at: str
    items: list[JobBatchItem] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def validate_counts(self):
        if (
            self.completed
            + self.active
            + self.failed
            + self.cancelled
            != self.total
        ):
            raise ValueError("batch counts must add up to total")
        if len(self.items) != self.total:
            raise ValueError("batch item count must match total")
        return self


class PersonalSpark(StrictModel):
    id: int = Field(ge=1)
    kind: Literal["spark"] = "spark"
    target_key: None = None
    content: str = Field(min_length=1, max_length=4000)
    author: str = Field(min_length=1, max_length=100)
    created_at: str
    updated_at: str


class PersonalAnnotation(StrictModel):
    id: int = Field(ge=1)
    kind: Literal["annotation"] = "annotation"
    target_key: str = Field(min_length=1, max_length=200)
    target_type: Literal["claim", "step"]
    content: str = Field(min_length=1, max_length=4000)
    author: str = Field(min_length=1, max_length=100)
    created_at: str
    updated_at: str


class PersonalNotes(StrictModel):
    spark: PersonalSpark | None = None
    annotations: list[PersonalAnnotation] = Field(default_factory=list)


class AnnotationTarget(StrictModel):
    target_key: str = Field(min_length=1, max_length=200)
    display_key: str = Field(min_length=1, max_length=200)
    target_type: Literal["claim", "step"]
    mode: Literal["full", "focused"]
    focus_query: str
    query_hash: str


class LibraryVideo(StrictModel):
    platform: Platform
    video_id: str
    source_url: str
    title: str
    author: str
    description: str
    summary: str
    cover_url: str
    subtitle_source: Literal["official", "public_page", "asr", "user_upload", "none"]
    source_tags: list[str]
    warnings: list[str]
    personal_tags: list[str]
    automatic_tagging: AutomaticTagging = Field(
        default_factory=AutomaticTagging
    )
    classification: VideoClassification = Field(
        default_factory=VideoClassification
    )
    spark: PersonalSpark | None = None
    favorite: bool
    created_at: str
    updated_at: str


class FocusedHistoryItem(StrictModel):
    focus_query: str
    query_hash: str
    focused_answer: FocusedAnswer
    created_at: str


class VideoQuestion(StrictModel):
    id: int = Field(ge=1)
    question: str = Field(min_length=1, max_length=500)
    question_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    answer: FocusedAnswer
    created_at: str


class VideoDetail(StrictModel):
    result: VideoResult
    favorite: bool
    personal_tags: list[str]
    automatic_tagging: AutomaticTagging = Field(
        default_factory=AutomaticTagging
    )
    classification: VideoClassification = Field(
        default_factory=VideoClassification
    )
    focused_history: list[FocusedHistoryItem]
    question_history: list[VideoQuestion] = Field(default_factory=list)
    personal_notes: PersonalNotes = Field(default_factory=PersonalNotes)
    annotation_targets: list[AnnotationTarget] = Field(default_factory=list)
    playback: PlaybackCapability = Field(default_factory=PlaybackCapability)


class TagFacet(StrictModel):
    name: str
    source: Literal["platform", "automatic", "personal"]
    count: int = Field(ge=1)


class SecondaryCategoryFacet(StrictModel):
    secondary_category: str
    count: int = Field(ge=1)


class CategoryFacet(StrictModel):
    primary_category: str
    count: int = Field(ge=1)
    children: list[SecondaryCategoryFacet] = Field(default_factory=list)


class LibraryFacets(StrictModel):
    tags: list[TagFacet] = Field(default_factory=list)
    categories: list[CategoryFacet] = Field(default_factory=list)


class VideoSearchPage(StrictModel):
    items: list[LibraryVideo]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    facets: LibraryFacets = Field(default_factory=LibraryFacets)
