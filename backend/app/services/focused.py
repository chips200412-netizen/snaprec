from __future__ import annotations

from ..domain.models import FullExtraction, VideoResult
from ..repositories.sqlite import (
    AmbiguousVideoIdError,
    SQLiteRepository,
    focus_query_hash,
    normalize_focus_query,
)
from .evidence import validate_focused_evidence
from .interfaces import FocusedExtractor
from .pipeline import PipelineError


class FocusedExtractionService:
    def __init__(
        self,
        repository: SQLiteRepository,
        extractor: FocusedExtractor,
    ):
        self.repository = repository
        self.extractor = extractor

    def extract(
        self, video_id: str, focus_query: str, platform: str | None = None
    ) -> VideoResult:
        if not normalize_focus_query(focus_query):
            raise PipelineError("INVALID_INPUT", "focus_query 不能为空。")
        try:
            base = self.repository.load_result(video_id, platform=platform)
        except AmbiguousVideoIdError as exc:
            raise PipelineError(
                "AMBIGUOUS_VIDEO_ID", "该视频 ID 存在于多个平台，请指定平台。"
            ) from exc
        if base is None:
            raise PipelineError("NOT_FOUND", "视频不存在。")

        query_hash = focus_query_hash(focus_query)
        answer = self.repository.load_focused_answer(
            video_id, query_hash, platform=base.platform
        )
        try:
            if answer is None:
                incomplete = any(
                    marker in warning.casefold()
                    for warning in base.warnings
                    for marker in ("字幕残缺", "字幕不完整", "incomplete transcript")
                )
                answer = self.extractor.extract(
                    focus_query,
                    base.raw_transcript,
                    base.clean_transcript,
                    base.segments,
                    transcript_incomplete=incomplete,
                )
                validate_focused_evidence(answer, base.segments)
                answer = self.repository.save_focused_answer(
                    video_id,
                    focus_query,
                    query_hash,
                    answer,
                    platform=base.platform,
                )
            # Cached payloads are shape-validated by Pydantic and must also be
            # rechecked against the canonical transcript before every response.
            validate_focused_evidence(answer, base.segments)
        except PipelineError:
            raise
        except Exception as exc:
            raise PipelineError("EXTRACTION_FAILED", "定向提取失败。") from exc

        # The cached answer is combined with the current request text and the
        # canonical transcript held by videos; extraction rows never copy it.
        return base.model_copy(
            update={
                "focus_query": focus_query,
                "extraction_mode": "focused",
                "summary": "",
                "full_extraction": FullExtraction(),
                "focused_answer": answer,
                "evidence": answer.supporting_segments,
            }
        )

    def extract_unsaved(self, base: VideoResult, focus_query: str) -> VideoResult:
        """Build and validate focused output without writing history."""
        if not normalize_focus_query(focus_query):
            raise PipelineError("INVALID_INPUT", "focus_query 不能为空。")
        try:
            incomplete = any(
                marker in warning.casefold()
                for warning in base.warnings
                for marker in ("字幕残缺", "字幕不完整", "incomplete transcript")
            )
            answer = self.extractor.extract(
                focus_query,
                base.raw_transcript,
                base.clean_transcript,
                base.segments,
                transcript_incomplete=incomplete,
            )
            validate_focused_evidence(answer, base.segments)
        except PipelineError:
            raise
        except Exception as exc:
            raise PipelineError("EXTRACTION_FAILED", "定向提取失败。") from exc
        return base.model_copy(
            update={
                "focus_query": focus_query,
                "extraction_mode": "focused",
                "summary": "",
                "full_extraction": FullExtraction(),
                "focused_answer": answer,
                "evidence": answer.supporting_segments,
            }
        )
