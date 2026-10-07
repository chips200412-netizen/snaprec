from __future__ import annotations

from ..domain.models import VideoQuestion
from ..repositories.sqlite import (
    AmbiguousVideoIdError,
    SQLiteRepository,
    focus_query_hash,
    normalize_focus_query,
)
from .evidence import validate_focused_evidence
from .interfaces import FocusedExtractor
from .pipeline import PipelineError


class QuestionService:
    """Answer a saved video's questions without resolving or acquiring media."""

    def __init__(self, repository: SQLiteRepository, extractor: FocusedExtractor):
        self.repository = repository
        self.extractor = extractor

    def ask(
        self, video_id: str, question: str, *, platform: str | None = None
    ) -> VideoQuestion:
        normalized = normalize_focus_query(question)
        if not normalized:
            raise PipelineError("INVALID_INPUT", "问题不能为空。")
        if len(question) > 500:
            raise PipelineError("INVALID_INPUT", "问题不能超过 500 个字符。")
        try:
            base = self.repository.load_result(video_id, platform=platform)
        except AmbiguousVideoIdError as exc:
            raise PipelineError(
                "AMBIGUOUS_VIDEO_ID", "该视频 ID 存在于多个平台，请指定平台。"
            ) from exc
        if base is None:
            raise PipelineError("NOT_FOUND", "视频不存在。")

        question_hash = focus_query_hash(question)
        try:
            cached = self.repository.load_video_question(
                video_id, question_hash, platform=base.platform
            )
            if cached is not None:
                validate_focused_evidence(cached.answer, base.segments)
                return cached.model_copy(update={"question": question})

            incomplete = (
                base.subtitle_source == "none"
                or not base.raw_transcript.strip()
                or not base.segments
                or any(
                    marker in warning.casefold()
                    for warning in base.warnings
                    for marker in (
                        "字幕残缺",
                        "字幕不完整",
                        "incomplete transcript",
                    )
                )
            )
            answer = self.extractor.extract(
                question,
                base.raw_transcript,
                base.clean_transcript,
                base.segments,
                transcript_incomplete=incomplete,
            )
            validate_focused_evidence(answer, base.segments)
            saved = self.repository.save_video_question(
                video_id,
                question,
                question_hash,
                answer,
                platform=base.platform,
            )
            validate_focused_evidence(saved.answer, base.segments)
            return saved
        except PipelineError:
            raise
        except Exception as exc:
            raise PipelineError("EXTRACTION_FAILED", "继续提问失败。") from exc
