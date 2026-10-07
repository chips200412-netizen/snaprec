from __future__ import annotations

import hashlib
import unicodedata
from datetime import UTC, datetime

from ..domain.models import AutomaticTagging, FullExtraction, Segment
from .interfaces import AutoTagger


AUTOMATIC_TAGGING_FAILED_WARNING = (
    "自动标签生成失败；平台标签、个人标签和视频内容仍可正常使用。"
)


def transcript_hash(raw_transcript: str) -> str:
    return hashlib.sha256(raw_transcript.encode("utf-8")).hexdigest()


def generate_automatic_tagging(
    tagger: AutoTagger,
    raw_transcript: str,
    clean_transcript: str,
    segments: list[Segment],
    extraction: FullExtraction,
    cached: AutomaticTagging | None = None,
) -> AutomaticTagging:
    generator_id = tagger.generator_id
    generator_version = tagger.generator_version
    if not raw_transcript.strip() or not segments:
        return AutomaticTagging(
            status="skipped_no_transcript",
            generator_id=generator_id,
            generator_version=generator_version,
            warning="无可用字幕，已跳过自动标签生成。",
        )
    digest = transcript_hash(raw_transcript)
    if (
        cached is not None
        and cached.status == "generated"
        and cached.transcript_hash == digest
        and cached.generator_id == generator_id
        and cached.generator_version == generator_version
    ):
        return cached
    generated_at = datetime.now(UTC).isoformat()
    try:
        raw_tags = tagger.generate(
            raw_transcript, clean_transcript, segments, extraction
        )
        unique = {}
        for tag in raw_tags:
            display = " ".join(
                unicodedata.normalize("NFKC", tag.name).strip().split()
            )
            if not display or len(display) > 64:
                raise ValueError("invalid automatic tag")
            unique.setdefault(display.casefold(), tag.model_copy(update={"name": display}))
        tags = list(unique.values())[:20]
        return AutomaticTagging(
            status="generated",
            tags=tags,
            generator_id=generator_id,
            generator_version=generator_version,
            transcript_hash=digest,
            generated_at=generated_at,
        )
    except Exception:
        return AutomaticTagging(
            status="failed",
            generator_id=generator_id,
            generator_version=generator_version,
            transcript_hash=digest,
            generated_at=generated_at,
            warning=AUTOMATIC_TAGGING_FAILED_WARNING,
        )
