from __future__ import annotations

from ..domain.models import (
    PersonalNotes,
    Platform,
    PlaybackCapability,
    VideoDetail,
    VideoSearchPage,
)
from ..repositories.sqlite import AmbiguousVideoIdError, SQLiteRepository
from .evidence import validate_evidence, validate_focused_evidence
from .interfaces import AutoTagger
from .markdown import render_detail_markdown
from .media import RetainedMediaService
from .pipeline import PipelineError
from .tagging import (
    AUTOMATIC_TAGGING_FAILED_WARNING,
    generate_automatic_tagging,
)


class LibraryService:
    def __init__(
        self,
        repository: SQLiteRepository,
        auto_tagger: AutoTagger | None = None,
        media_service: RetainedMediaService | None = None,
    ):
        self.repository = repository
        self.auto_tagger = auto_tagger
        self.media_service = media_service

    def get_detail(
        self, video_id: str, *, platform: Platform | None = None
    ) -> VideoDetail:
        try:
            detail = self.repository.get_video_detail(video_id, platform=platform)
        except AmbiguousVideoIdError as exc:
            raise PipelineError(
                "AMBIGUOUS_VIDEO_ID", "该视频 ID 存在于多个平台，请指定平台。"
            ) from exc
        if detail is None:
            raise PipelineError("NOT_FOUND", "视频不存在。")
        try:
            for item in detail.focused_history:
                validate_focused_evidence(
                    item.focused_answer, detail.result.segments
                )
            for item in detail.question_history:
                validate_focused_evidence(item.answer, detail.result.segments)
        except Exception as exc:
            raise PipelineError(
                "EXTRACTION_FAILED", "已存提取或追问记录校验失败。"
            ) from exc
        playback = (
            self.media_service.capability(
                detail.result.video_id, detail.result.platform
            )
            if self.media_service is not None
            else PlaybackCapability(
                reason=(
                    "not_retained"
                    if detail.result.platform == "local_upload"
                    else "unsupported_source"
                )
            )
        )
        return detail.model_copy(update={"playback": playback})

    def set_favorite(
        self, video_id: str, favorite: bool, *, platform: Platform | None = None
    ) -> VideoDetail:
        try:
            found = self.repository.set_favorite(
                video_id, favorite, platform=platform
            )
        except AmbiguousVideoIdError as exc:
            raise PipelineError(
                "AMBIGUOUS_VIDEO_ID", "该视频 ID 存在于多个平台，请指定平台。"
            ) from exc
        if not found:
            raise PipelineError("NOT_FOUND", "视频不存在。")
        return self.get_detail(video_id, platform=platform)

    def update_tags(
        self,
        video_id: str,
        tags: list[str],
        operation: str,
        *,
        platform: Platform | None = None,
    ) -> VideoDetail:
        try:
            found = self.repository.update_video_tags(
                video_id, tags, operation=operation, platform=platform
            )
        except AmbiguousVideoIdError as exc:
            raise PipelineError(
                "AMBIGUOUS_VIDEO_ID", "该视频 ID 存在于多个平台，请指定平台。"
            ) from exc
        except ValueError as exc:
            raise PipelineError("INVALID_INPUT", str(exc)) from exc
        if not found:
            raise PipelineError("NOT_FOUND", "视频不存在。")
        return self.get_detail(video_id, platform=platform)

    def search(
        self,
        *,
        query: str = "",
        platform: Platform | None = None,
        tag: str = "",
        tag_source: str | None = None,
        primary_category: str = "",
        secondary_category: str = "",
        favorite: bool | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> VideoSearchPage:
        try:
            return self.repository.search_videos(
                query=query,
                platform=platform,
                tag=tag,
                tag_source=tag_source,
                primary_category=primary_category,
                secondary_category=secondary_category,
                favorite=favorite,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise PipelineError("INVALID_INPUT", str(exc)) from exc

    def set_classification(
        self,
        video_id: str,
        primary_category: str,
        secondary_category: str = "",
        *,
        platform: Platform | None = None,
    ) -> VideoDetail:
        try:
            found = self.repository.set_classification(
                video_id,
                primary_category,
                secondary_category,
                platform=platform,
            )
        except AmbiguousVideoIdError as exc:
            raise PipelineError(
                "AMBIGUOUS_VIDEO_ID", "该视频 ID 存在于多个平台，请指定平台。"
            ) from exc
        except ValueError as exc:
            raise PipelineError("INVALID_INPUT", str(exc)) from exc
        if not found:
            raise PipelineError("NOT_FOUND", "视频不存在。")
        return self.get_detail(video_id, platform=platform)

    def generate_automatic_tags(
        self, video_id: str, *, platform: Platform | None = None
    ) -> VideoDetail:
        if self.auto_tagger is None:
            raise PipelineError("NOT_CONFIGURED", "自动标签生成器未配置。")
        detail = self.get_detail(video_id, platform=platform)
        try:
            validate_evidence(
                detail.result.full_extraction,
                detail.result.evidence,
                detail.result.segments,
            )
        except Exception as exc:
            raise PipelineError(
                "EXTRACTION_FAILED", "已存完整提取记录校验失败。"
            ) from exc
        tagging = generate_automatic_tagging(
            self.auto_tagger,
            detail.result.raw_transcript,
            detail.result.clean_transcript,
            detail.result.segments,
            detail.result.full_extraction,
            detail.automatic_tagging,
        )
        warnings = [
            warning
            for warning in detail.result.warnings
            if warning != AUTOMATIC_TAGGING_FAILED_WARNING
        ]
        if tagging.status == "failed":
            warnings.append(AUTOMATIC_TAGGING_FAILED_WARNING)
        try:
            found = self.repository.save_automatic_tagging(
                video_id, tagging, warnings, platform=platform
            )
        except AmbiguousVideoIdError as exc:
            raise PipelineError(
                "AMBIGUOUS_VIDEO_ID", "该视频 ID 存在于多个平台，请指定平台。"
            ) from exc
        if not found:
            raise PipelineError("NOT_FOUND", "视频不存在。")
        return self.get_detail(video_id, platform=platform)

    @staticmethod
    def _notes_or_not_found(notes: PersonalNotes | None) -> PersonalNotes:
        if notes is None:
            raise PipelineError("NOT_FOUND", "视频不存在。")
        return notes

    def get_personal_notes(
        self, video_id: str, *, platform: Platform | None = None
    ) -> PersonalNotes:
        try:
            return self._notes_or_not_found(
                self.repository.get_personal_notes(video_id, platform=platform)
            )
        except AmbiguousVideoIdError as exc:
            raise PipelineError(
                "AMBIGUOUS_VIDEO_ID", "该视频 ID 存在于多个平台，请指定平台。"
            ) from exc

    def upsert_spark(
        self,
        video_id: str,
        content: str,
        author: str,
        *,
        platform: Platform | None = None,
    ) -> PersonalNotes:
        try:
            notes = self.repository.upsert_spark(
                video_id, content, author, platform=platform
            )
        except AmbiguousVideoIdError as exc:
            raise PipelineError(
                "AMBIGUOUS_VIDEO_ID", "该视频 ID 存在于多个平台，请指定平台。"
            ) from exc
        except ValueError as exc:
            raise PipelineError("INVALID_INPUT", str(exc)) from exc
        return self._notes_or_not_found(notes)

    def delete_spark(
        self, video_id: str, *, platform: Platform | None = None
    ) -> PersonalNotes:
        try:
            notes = self.repository.delete_spark(video_id, platform=platform)
        except AmbiguousVideoIdError as exc:
            raise PipelineError(
                "AMBIGUOUS_VIDEO_ID", "该视频 ID 存在于多个平台，请指定平台。"
            ) from exc
        return self._notes_or_not_found(notes)

    def create_annotation(
        self,
        video_id: str,
        target_key: str,
        content: str,
        author: str,
        *,
        platform: Platform | None = None,
    ) -> PersonalNotes:
        try:
            notes = self.repository.create_annotation(
                video_id,
                target_key,
                content,
                author,
                platform=platform,
            )
        except AmbiguousVideoIdError as exc:
            raise PipelineError(
                "AMBIGUOUS_VIDEO_ID", "该视频 ID 存在于多个平台，请指定平台。"
            ) from exc
        except ValueError as exc:
            raise PipelineError("INVALID_INPUT", str(exc)) from exc
        return self._notes_or_not_found(notes)

    def update_annotation(
        self,
        video_id: str,
        annotation_id: int,
        content: str,
        author: str | None = None,
        *,
        platform: Platform | None = None,
    ) -> PersonalNotes:
        try:
            notes = self.repository.update_annotation(
                video_id,
                annotation_id,
                content,
                author,
                platform=platform,
            )
        except AmbiguousVideoIdError as exc:
            raise PipelineError(
                "AMBIGUOUS_VIDEO_ID", "该视频 ID 存在于多个平台，请指定平台。"
            ) from exc
        except ValueError as exc:
            raise PipelineError("INVALID_INPUT", str(exc)) from exc
        except LookupError as exc:
            raise PipelineError("NOT_FOUND", "个人批注不存在。") from exc
        return self._notes_or_not_found(notes)

    def delete_annotation(
        self,
        video_id: str,
        annotation_id: int,
        *,
        platform: Platform | None = None,
    ) -> PersonalNotes:
        try:
            notes = self.repository.delete_annotation(
                video_id, annotation_id, platform=platform
            )
        except AmbiguousVideoIdError as exc:
            raise PipelineError(
                "AMBIGUOUS_VIDEO_ID", "该视频 ID 存在于多个平台，请指定平台。"
            ) from exc
        except LookupError as exc:
            raise PipelineError("NOT_FOUND", "个人批注不存在。") from exc
        return self._notes_or_not_found(notes)

    def export_markdown(
        self,
        video_id: str,
        *,
        platform: Platform | None = None,
        view: str = "all",
        include_personal: bool = True,
        focus_query_hash: str = "",
    ) -> str:
        detail = self.get_detail(video_id, platform=platform)
        if view not in {"all", "overview", "steps", "transcript"}:
            raise PipelineError("INVALID_INPUT", "不支持的 Markdown 导出模式。")
        focused = None
        if focus_query_hash:
            if len(focus_query_hash) != 64 or any(
                value not in "0123456789abcdef" for value in focus_query_hash
            ):
                raise PipelineError("INVALID_INPUT", "focus_query_hash 格式无效。")
            focused = next(
                (
                    item
                    for item in detail.focused_history
                    if item.query_hash == focus_query_hash
                ),
                None,
            )
            if focused is None:
                raise PipelineError("INVALID_INPUT", "选中的定向提取记录不存在。")
        return render_detail_markdown(
            detail,
            view=view,
            include_personal=include_personal,
            focused=focused,
        )
