from __future__ import annotations

from .adapters import (
    AdapterRegistry,
    BilibiliAdapter,
    DouyinAdapter,
    LocalUploadAdapter,
)
from .repositories.sqlite import SQLiteRepository
from .runtime_config import RuntimeConfig
from .services.pipeline import LocalFullPipeline
from .services.inspiration import (
    FfprobeInspirationAudioProbe,
    HttpInspirationTranscriptionProvider,
    UnconfiguredInspirationTranscriptionProvider,
)
from .services.volcengine_inspiration import VolcengineInspirationTranscriptionProvider
from .services.resolution import ResolutionService
from .services.providers import (
    DeterministicFocusedExtractor,
    DeterministicFullExtractor,
    HttpAsrProvider,
    HttpFocusedExtractor,
    HttpFullExtractor,
    SidecarSubtitleProvider,
    UnconfiguredAsrProvider,
)


def build_pipeline(config: RuntimeConfig) -> LocalFullPipeline:
    """Build the local prototype from explicit environment configuration."""
    asr_endpoint = config.video_asr_endpoint
    asr = (
        HttpAsrProvider(
            asr_endpoint,
            token=config.video_asr_token,
            model=config.video_asr_model,
        )
        if asr_endpoint
        else UnconfiguredAsrProvider()
    )
    extraction_endpoint = config.video_extraction_endpoint
    extractor = (
        HttpFullExtractor(
            extraction_endpoint,
            token=config.video_extraction_token,
        )
        if extraction_endpoint
        else DeterministicFullExtractor()
    )
    focused_endpoint = config.video_focused_extraction_endpoint
    focused_extractor = (
        HttpFocusedExtractor(
            focused_endpoint,
            token=config.video_extraction_token,
        )
        if focused_endpoint
        else DeterministicFocusedExtractor()
    )
    return LocalFullPipeline(
        SQLiteRepository(config.video_db_path),
        asr,
        extractor,
        temp_root=config.video_temp_root,
        subtitle_provider=SidecarSubtitleProvider(),
        focused_extractor=focused_extractor,
        max_media_bytes=config.video_max_upload_bytes,
        retained_media_root=config.video_media_root,
    )


def build_resolution_service(pipeline: LocalFullPipeline) -> ResolutionService:
    return ResolutionService(
        pipeline.repository,
        AdapterRegistry([
            BilibiliAdapter(),
            DouyinAdapter(),
            LocalUploadAdapter(pipeline.repository),
        ]),
        pipeline.extractor,
        pipeline.focused_extractor or DeterministicFocusedExtractor(),
        pipeline.auto_tagger,
    )


def build_inspiration_transcription_provider(config: RuntimeConfig):
    if config.inspiration_asr_provider == "volcengine_flash":
        return VolcengineInspirationTranscriptionProvider(
            app_id=config.inspiration_volc_app_id,
            access_token=config.inspiration_volc_access_token,
            ffmpeg_path=config.inspiration_ffmpeg_path,
        )
    endpoint = config.inspiration_asr_endpoint
    if not endpoint:
        return UnconfiguredInspirationTranscriptionProvider()
    return HttpInspirationTranscriptionProvider(
        endpoint,
        token=config.inspiration_asr_token,
        model=config.inspiration_asr_model,
    )


def build_inspiration_audio_probe(
    config: RuntimeConfig,
) -> FfprobeInspirationAudioProbe:
    return FfprobeInspirationAudioProbe(config.inspiration_ffprobe_path)
