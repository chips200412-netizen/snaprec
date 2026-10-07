import os
from pathlib import Path

from .api.main import create_app
from .bootstrap import (
    build_inspiration_audio_probe,
    build_inspiration_transcription_provider,
    build_pipeline,
    build_resolution_service,
)
from .runtime_config import resolve_runtime_config
from .services.rendered_cover import PlaywrightRenderedCoverProbe
from .services.rendered_metadata import PlaywrightRenderedMetadataProbe

PROJECT_ROOT = Path(__file__).resolve().parents[2]
runtime_config = resolve_runtime_config(os.environ, PROJECT_ROOT)
pipeline = build_pipeline(runtime_config)
app = create_app(
    pipeline,
    resolution_service=build_resolution_service(pipeline),
    inspiration_transcription_provider=build_inspiration_transcription_provider(
        runtime_config
    ),
    inspiration_audio_probe=build_inspiration_audio_probe(runtime_config),
    upload_root=runtime_config.video_upload_root,
    max_upload_bytes=runtime_config.video_max_upload_bytes,
    inspiration_temp_root=runtime_config.inspiration_temp_root,
    cover_cache_root=runtime_config.cover_cache_root,
    cover_cache_fresh_seconds=runtime_config.cover_cache_fresh_seconds,
    cover_cache_retention_seconds=runtime_config.cover_cache_retention_seconds,
    cover_cache_max_bytes=runtime_config.cover_cache_max_bytes,
    cover_max_response_bytes=runtime_config.cover_max_response_bytes,
    cover_cache_failure_retry_seconds=(
        runtime_config.cover_cache_failure_retry_seconds
    ),
    rendered_cover_probe=PlaywrightRenderedCoverProbe(),
    rendered_metadata_probe=PlaywrightRenderedMetadataProbe(),
)
