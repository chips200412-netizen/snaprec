from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import Literal

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    Header,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from ..domain.models import (
    CollectionItem,
    CollectionItemCreateRequest,
    CollectionItemUpdateRequest,
    CollectionDeepAnalysisSnapshot,
    CollectionPlatform,
    CollectionPreview,
    CollectionPreviewRequest,
    CollectionSearchPage,
    InspirationTranscriptionDraft,
    Job,
    JobBatch,
    FocusedHistoryItem,
    Platform,
    PersonalNotes,
    PlaybackCapability,
    VideoDetail,
    VideoQuestion,
    VideoResult,
    VideoSearchPage,
)
from ..repositories.collection_imports import CollectionImportRepository
from ..services.collections import CollectionService
from ..services.collection_imports import (
    RAW_BODY_LIMIT,
    CollectionImportError,
    CollectionImportPreviewCoordinator,
    CollectionImportSaveCoordinator,
    CollectionImportService,
    parse_collection_import_cancel,
    parse_collection_import_confirm,
    parse_collection_import_create,
    parse_collection_import_repreview,
    parse_collection_import_resume,
    parse_collection_import_review,
)
from ..services.cover_cache import CoverAsset, CoverCacheService, CoverUnavailable
from ..services.rendered_cover import RenderedCoverProbe
from ..services.rendered_metadata import RenderedMetadataProbe
from ..services.user_cover_assets import UserCoverAssetService
from ..services.deep_analysis import CollectionDeepAnalysisService
from ..services.capture import CaptureService
from ..services.inspiration import (
    FfprobeInspirationAudioProbe,
    InspirationTranscriptionService,
    UnconfiguredInspirationTranscriptionProvider,
)
from ..services.safe_http import SafePublicFetcher, SafePublicImageFetcher
from ..services.batches import JobBatchService
from ..services.focused import FocusedExtractionService
from ..services.library import LibraryService
from ..services.media import (
    ByteRange,
    RangeNotSatisfiable,
    RetainedMediaError,
    iter_media,
    parse_single_range,
)
from ..services.pipeline import LocalFullPipeline, PipelineError
from ..services.providers import DeterministicFocusedExtractor, SUBTITLE_SUFFIXES
from ..services.questions import QuestionService
from ..services.recovery import (
    JobRecoveryService,
    TEMP_MARKER,
    TemporaryMediaService,
)
from ..services.resolution import ResolutionService


class QueryStringAccessLogRedactionMiddleware:
    """Give the app its query while keeping it out of server access logs."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        query_string = scope.get("query_string", b"")
        if scope["type"] != "http" or not query_string:
            await self.app(scope, receive, send)
            return
        app_scope = dict(scope)
        app_scope["query_string"] = query_string
        # Uvicorn retains the original scope for its access-log line. Redact
        # that shared scope, and pass a copy with the real query downstream.
        scope["query_string"] = b""
        await self.app(app_scope, receive, send)


def _validated_subtitle_suffix(filename: str | None) -> str:
    safe_name = Path(filename or "subtitle").name
    suffix = Path(safe_name).suffix.casefold()
    if suffix not in SUBTITLE_SUFFIXES:
        raise PipelineError(
            "UNSUPPORTED_MEDIA_TYPE",
            "字幕文件仅支持 SRT、VTT 或 TXT。",
        )
    return suffix


class UploadResponse(BaseModel):
    job: Job
    result: VideoResult
    markdown: str


class FocusedExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    focus_query: str
    platform: Platform | None = None


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)
    platform: Platform | None = None


class FavoriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    favorite: bool
    platform: Platform | None = None


class TagUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["add", "remove"]
    tags: list[str] = Field(min_length=1, max_length=50)
    platform: Platform | None = None


class ClassificationUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primary_category: str = Field(min_length=1, max_length=64)
    secondary_category: str = Field(default="", max_length=64)
    platform: Platform | None = None


class AutomaticTagsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Platform | None = None


class SparkUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=4000)
    author: str = Field(min_length=1, max_length=100)
    platform: Platform | None = None


class AnnotationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_key: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=4000)
    author: str = Field(min_length=1, max_length=100)
    platform: Platform | None = None


class AnnotationUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=4000)
    author: str | None = Field(default=None, min_length=1, max_length=100)
    platform: Platform | None = None


class ResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_text: str
    focus_query: str = ""


class JobBatchItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    platform: Platform


class JobBatchCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[JobBatchItemRequest]


class ResolutionResponse(BaseModel):
    job: Job
    result: VideoResult
    markdown: str


class ResolutionPreview(BaseModel):
    platform: Platform
    source_url: str
    canonical_url: str
    video_id: str
    author: str
    title: str
    description: str
    tags: list[str]
    duration: float
    cover_url: str
    warnings: list[str]


class UserCoverDraftResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str
    claim_token: str
    media_type: Literal["image/webp"]
    width: int = Field(gt=0, le=2048)
    height: int = Field(gt=0, le=2048)
    size_bytes: int = Field(gt=0, le=5 * 1024 * 1024)
    expires_at: str


def create_app(
    pipeline: LocalFullPipeline,
    *,
    upload_root: str | Path | None = None,
    max_upload_bytes: int = 512 * 1024 * 1024,
    resolution_service: ResolutionService | None = None,
    orphan_max_age_seconds: float = 24 * 60 * 60,
    retry_sleeper: Callable[[float], None] | None = None,
    retry_base_delay_seconds: float = 1,
    capture_fetcher: SafePublicFetcher | None = None,
    rendered_cover_probe: RenderedCoverProbe | None = None,
    rendered_metadata_probe: RenderedMetadataProbe | None = None,
    metadata_cache_ttl_seconds: int = 24 * 60 * 60,
    cover_fetcher: SafePublicImageFetcher | None = None,
    cover_cache_root: str | Path | None = None,
    cover_cache_fresh_seconds: float = 24 * 60 * 60,
    cover_cache_retention_seconds: float = 30 * 24 * 60 * 60,
    cover_cache_max_bytes: int = 512 * 1024 * 1024,
    cover_max_response_bytes: int = 5 * 1024 * 1024,
    cover_cache_lease_seconds: float = 60,
    cover_cache_wait_seconds: float = 2,
    cover_cache_failure_retry_seconds: float = 60,
    user_cover_root: str | Path | None = None,
    organization_suggestion_service=None,
    inspiration_transcription_provider=None,
    inspiration_audio_probe=None,
    inspiration_temp_root: str | Path | None = None,
    inspiration_orphan_max_age_seconds: float = 15 * 60,
    collection_import_clock: Callable[[], datetime] | None = None,
    collection_import_id_factory: Callable[[], str] | None = None,
    collection_import_fault: Callable[[str, dict], None] | None = None,
) -> FastAPI:
    if max_upload_bytes <= 0:
        raise ValueError("max_upload_bytes must be positive")
    root = Path(upload_root) if upload_root else None
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Video Knowledge Prototype", version="0.1.0")
    app.add_middleware(QueryStringAccessLogRedactionMiddleware)
    focused_service = FocusedExtractionService(
        pipeline.repository,
        pipeline.focused_extractor or DeterministicFocusedExtractor(),
    )
    question_service = QuestionService(
        pipeline.repository,
        pipeline.focused_extractor or DeterministicFocusedExtractor(),
    )
    library_service = LibraryService(
        pipeline.repository, pipeline.auto_tagger, pipeline.media_service
    )
    collection_service = CollectionService(pipeline.repository)
    capture_service = CaptureService(
        pipeline.repository,
        fetcher=capture_fetcher,
        adapter_registry=getattr(resolution_service, "registry", None),
        cache_ttl_seconds=metadata_cache_ttl_seconds,
        organization_suggestion_service=organization_suggestion_service,
        rendered_cover_probe=rendered_cover_probe,
        rendered_metadata_probe=rendered_metadata_probe,
    )
    collection_import_repository = CollectionImportRepository(pipeline.repository)
    collection_import_coordinator = CollectionImportPreviewCoordinator(
        collection_import_repository,
        capture_service,
        clock=collection_import_clock,
        id_factory=collection_import_id_factory,
        fault=collection_import_fault,
    )
    collection_import_save_coordinator = CollectionImportSaveCoordinator(
        collection_import_repository,
        collection_service,
        clock=collection_import_clock,
        id_factory=collection_import_id_factory,
        fault=collection_import_fault,
    )
    collection_import_service = CollectionImportService(
        collection_import_repository,
        collection_import_coordinator,
        collection_import_save_coordinator,
        clock=collection_import_clock,
        id_factory=collection_import_id_factory,
    )
    collection_import_service.recover_startup()
    app.state.collection_import_service = collection_import_service
    app.state.collection_import_coordinator = collection_import_coordinator
    app.state.collection_import_save_coordinator = collection_import_save_coordinator
    app.router.add_event_handler("shutdown", collection_import_coordinator.close)
    app.router.add_event_handler(
        "shutdown", collection_import_save_coordinator.close
    )
    repository_path = getattr(pipeline.repository, "database_path", None)
    default_cover_root = (
        Path(repository_path).parent / "cover-cache"
        if repository_path is not None
        else Path("var/cover-cache")
    )
    cover_service = CoverCacheService(
        Path(cover_cache_root) if cover_cache_root is not None else default_cover_root,
        fetcher=cover_fetcher,
        fresh_seconds=cover_cache_fresh_seconds,
        retention_seconds=cover_cache_retention_seconds,
        max_cache_bytes=cover_cache_max_bytes,
        max_image_bytes=cover_max_response_bytes,
        lease_seconds=cover_cache_lease_seconds,
        wait_seconds=cover_cache_wait_seconds,
        failure_retry_seconds=cover_cache_failure_retry_seconds,
    )
    app.state.cover_cache_service = cover_service
    app.router.add_event_handler("shutdown", cover_service.close)
    default_user_cover_root = (
        Path(repository_path).parent / "user-covers"
        if repository_path is not None
        else Path("var/user-covers")
    )
    user_cover_service = UserCoverAssetService(
        pipeline.repository,
        Path(user_cover_root) if user_cover_root is not None else default_user_cover_root,
    )
    app.state.user_cover_service = user_cover_service
    inspiration_service = InspirationTranscriptionService(
        inspiration_transcription_provider
        or UnconfiguredInspirationTranscriptionProvider(),
        inspiration_audio_probe or FfprobeInspirationAudioProbe(),
        inspiration_temp_root
        or (
            Path(pipeline.temp_root).parent / "inspiration-recordings"
            if pipeline.temp_root is not None
            else Path("var/inspiration-recordings")
        ),
        orphan_max_age_seconds=inspiration_orphan_max_age_seconds,
        warning_sink=pipeline.repository.add_system_warning,
    )
    batch_service = JobBatchService(pipeline.repository)
    recovery_service = JobRecoveryService(
        pipeline.repository,
        pipeline,
        resolution_service,
        sleeper=retry_sleeper or time.sleep,
        base_delay_seconds=retry_base_delay_seconds,
    )
    recovery_service.recover_startup()
    collection_deep_analysis_service = CollectionDeepAnalysisService(
        pipeline.repository,
        resolution_service,
        recovery_service,
    )
    temporary_roots = []
    if pipeline.temp_root is not None:
        temporary_roots.append((pipeline.temp_root, ("video-job-",)))
    if root is not None:
        temporary_roots.append((root, ("upload-",)))
    app.state.startup_warnings = TemporaryMediaService(
        pipeline.repository,
        temporary_roots,
        max_age_seconds=orphan_max_age_seconds,
    ).cleanup_orphans()
    app.state.startup_warnings.extend(inspiration_service.cleanup_orphans())

    def run_resolution_job(job: Job, input_text: str, focus_query: str) -> None:
        assert resolution_service is not None
        try:
            resolution_service.process(input_text, focus_query, job)
        except PipelineError as exc:
            persisted = pipeline.repository.get_job(job.id)
            if persisted is not None and persisted.status not in {
                "failed", "cancelled"
            }:
                persisted.status = "failed"
                persisted.error_code = exc.code
                persisted.message = str(exc)
                persisted.retryable = False
                pipeline.repository.save_job(persisted)

    def run_upload_job(
        job: Job,
        staged: Path,
        request_dir: Path,
        retain_media: bool,
    ) -> None:
        try:
            pipeline.process(staged, job=job, retain_media=retain_media)
        except PipelineError:
            pass
        finally:
            try:
                shutil.rmtree(request_dir)
            except OSError:
                pipeline.repository.add_system_warning(
                    "UPLOAD_CLEANUP_FAILED",
                    "后台上传临时文件清理失败，需要人工检查受限请求目录。",
                )

    @app.post(
        "/api/v1/resolution-jobs",
        response_model=Job,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_resolution_job(
        request: ResolutionRequest,
        background_tasks: BackgroundTasks,
    ):
        if resolution_service is None:
            raise PipelineError(
                "UNSUPPORTED_PLATFORM",
                "链接解析服务未配置，请上传本地媒体。",
            )
        job = Job(id=str(uuid.uuid4()), status="queued", progress=0)
        if not pipeline.repository.save_job(job):
            raise PipelineError("JOB_STATE_CONFLICT", "无法创建任务。")
        background_tasks.add_task(
            run_resolution_job,
            job,
            request.input_text,
            request.focus_query,
        )
        return job.model_dump(mode="json")

    @app.post("/api/v1/resolutions", response_model=ResolutionResponse)
    async def resolve_video(request: ResolutionRequest):
        if resolution_service is None:
            raise PipelineError("UNSUPPORTED_PLATFORM", "链接解析服务未配置，请上传本地媒体。")
        job, result, markdown = await run_in_threadpool(
            resolution_service.process, request.input_text, request.focus_query
        )
        return {"job": job, "result": result, "markdown": markdown}

    @app.post("/api/v1/resolution-preview", response_model=ResolutionPreview)
    async def preview_resolution(request: ResolutionRequest):
        if resolution_service is None:
            raise PipelineError(
                "UNSUPPORTED_PLATFORM",
                "链接解析服务未配置，请上传本地媒体。",
            )
        video, metadata = await run_in_threadpool(
            resolution_service.preview,
            request.input_text,
        )
        return {
            "platform": video.platform,
            "source_url": video.source_url,
            "canonical_url": video.canonical_url,
            "video_id": video.video_id,
            "author": metadata.author,
            "title": metadata.title,
            "description": metadata.description,
            "tags": metadata.tags,
            "duration": metadata.duration,
            "cover_url": metadata.cover_url,
            "warnings": metadata.warnings,
        }

    @app.post(
        "/api/v1/upload-jobs",
        response_model=Job,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_upload_job(
        background_tasks: BackgroundTasks,
        file: UploadFile = File(...),
        subtitle: UploadFile | None = File(default=None),
        retain_media: bool = Form(default=False),
    ):
        request_dir = Path(tempfile.mkdtemp(prefix="upload-", dir=root))
        job = Job(id=str(uuid.uuid4()), status="queued", progress=0)
        safe_name = Path(file.filename or "upload.bin").name
        staged = request_dir / safe_name
        try:
            if not pipeline.repository.save_job(job):
                raise PipelineError("JOB_STATE_CONFLICT", "无法创建上传任务。")
            (request_dir / TEMP_MARKER).write_text(
                f"job:{job.id}", encoding="utf-8"
            )
            size = 0
            with staged.open("wb") as stream:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_upload_bytes:
                        raise PipelineError(
                            "UPLOAD_TOO_LARGE", "上传文件超过大小限制。"
                        )
                    stream.write(chunk)
            if subtitle is not None:
                subtitle_path = Path(
                    f"{staged}{_validated_subtitle_suffix(subtitle.filename)}"
                )
                subtitle_size = 0
                with subtitle_path.open("wb") as stream:
                    while chunk := await subtitle.read(1024 * 1024):
                        subtitle_size += len(chunk)
                        if subtitle_size > 10 * 1024 * 1024:
                            raise PipelineError(
                                "UPLOAD_TOO_LARGE", "字幕文件超过大小限制。"
                            )
                        stream.write(chunk)
            background_tasks.add_task(
                run_upload_job, job, staged, request_dir, retain_media
            )
            return job.model_dump(mode="json")
        except BaseException:
            failed = pipeline.repository.get_job(job.id)
            if failed is not None and failed.status not in {"failed", "cancelled"}:
                failed.status = "failed"
                failed.error_code = "UPLOAD_FAILED"
                failed.message = "上传暂存失败。"
                failed.retryable = False
                pipeline.repository.save_job(failed)
            shutil.rmtree(request_dir, ignore_errors=True)
            raise
        finally:
            await file.close()
            if subtitle is not None:
                await subtitle.close()

    @app.exception_handler(PipelineError)
    async def pipeline_error_handler(_, exc: PipelineError):
        status = (
            413
            if exc.code in {
                "UPLOAD_TOO_LARGE",
                "INSPIRATION_AUDIO_TOO_LARGE",
                "USER_COVER_TOO_LARGE",
            }
            else 415
            if exc.code in {
                "INSPIRATION_AUDIO_TYPE_UNSUPPORTED",
                "USER_COVER_TYPE_UNSUPPORTED",
            }
            else 409
            if exc.code
            in {
                "COLLECTION_EXISTS",
                "COLLECTION_REVISION_CONFLICT",
                "IDEMPOTENCY_KEY_REUSED",
            }
            else 404
            if exc.code
            in {"NOT_FOUND", "BATCH_JOB_NOT_FOUND", "BATCH_JOB_NOT_MEMBER"}
            else 400
        )
        error = {"code": exc.code, "message": str(exc)}
        if exc.code == "COLLECTION_EXISTS":
            error["collection_item_id"] = exc.collection_item_id
        elif exc.code == "COLLECTION_REVISION_CONFLICT":
            error.update(
                {
                    "collection_item_id": exc.collection_item_id,
                    "expected_revision": exc.expected_revision,
                    "current_revision": exc.current_revision,
                }
            )
        return JSONResponse(status_code=status, content={"error": error})

    @app.exception_handler(CollectionImportError)
    async def collection_import_error_handler(_, exc: CollectionImportError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    **exc.details,
                }
            },
        )

    def _collection_import_content_type_supported(value: str | None) -> bool:
        if not value:
            return False
        parts = [part.strip() for part in value.split(";")]
        if parts[0].casefold() != "application/json":
            return False
        if len(parts) == 1:
            return True
        if len(parts) != 2 or "=" not in parts[1]:
            return False
        name, charset = (part.strip() for part in parts[1].split("=", 1))
        charset = charset.strip('"').casefold()
        return name.casefold() == "charset" and charset == "utf-8"

    async def _read_collection_import_body(request: Request) -> bytes:
        buffer = bytearray()
        async for chunk in request.stream():
            if not chunk:
                continue
            needed = RAW_BODY_LIMIT + 1 - len(buffer)
            if needed <= 0:
                raise CollectionImportError(
                    "BATCH_BODY_TOO_LARGE",
                    "批量导入请求体超过大小限制。",
                    413,
                )
            buffer.extend(chunk[:needed])
            if len(buffer) > RAW_BODY_LIMIT or len(chunk) > needed:
                raise CollectionImportError(
                    "BATCH_BODY_TOO_LARGE",
                    "批量导入请求体超过大小限制。",
                    413,
                )
        return bytes(buffer)

    @app.post("/api/v1/collection-import-batches")
    async def create_collection_import_batch(request: Request):
        if not _collection_import_content_type_supported(
            request.headers.get("content-type")
        ):
            raise CollectionImportError(
                "BATCH_CONTENT_TYPE_UNSUPPORTED",
                "批量导入只接受 UTF-8 JSON。",
                415,
            )
        body = await _read_collection_import_body(request)
        items, key_hash, request_fingerprint = parse_collection_import_create(
            body, request.headers.get("idempotency-key")
        )
        snapshot, status_code = await run_in_threadpool(
            collection_import_service.create,
            items=items,
            key_hash=key_hash,
            request_fingerprint=request_fingerprint,
        )
        return JSONResponse(
            status_code=status_code,
            content=snapshot.model_dump(mode="json"),
        )

    @app.get("/api/v1/collection-import-batches/active")
    async def get_active_collection_import_batch():
        snapshot = await run_in_threadpool(collection_import_service.get_active)
        if snapshot is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return snapshot

    @app.get("/api/v1/collection-import-batches/{batch_id}")
    async def get_collection_import_batch(batch_id: str):
        return await run_in_threadpool(collection_import_service.get_batch, batch_id)

    @app.get(
        "/api/v1/collection-import-batches/{batch_id}/items/{batch_item_id}"
    )
    async def get_collection_import_batch_item(
        batch_id: str, batch_item_id: str
    ):
        return await run_in_threadpool(
            collection_import_service.get_item, batch_id, batch_item_id
        )

    @app.patch(
        "/api/v1/collection-import-batches/{batch_id}/items/{batch_item_id}"
    )
    async def review_collection_import_batch_item(
        batch_id: str, batch_item_id: str, request: Request
    ):
        if not _collection_import_content_type_supported(
            request.headers.get("content-type")
        ):
            raise CollectionImportError(
                "BATCH_CONTENT_TYPE_UNSUPPORTED",
                "批量导入只接受 UTF-8 JSON。",
                415,
            )
        body = await _read_collection_import_body(request)
        command = parse_collection_import_review(body)
        return await run_in_threadpool(
            collection_import_service.review_item,
            batch_id,
            batch_item_id,
            command,
        )

    @app.post(
        "/api/v1/collection-import-batches/{batch_id}/items/{batch_item_id}/repreview"
    )
    async def repreview_collection_import_batch_item(
        batch_id: str, batch_item_id: str, request: Request
    ):
        if not _collection_import_content_type_supported(
            request.headers.get("content-type")
        ):
            raise CollectionImportError(
                "BATCH_CONTENT_TYPE_UNSUPPORTED",
                "批量导入只接受 UTF-8 JSON。",
                415,
            )
        body = await _read_collection_import_body(request)
        command = parse_collection_import_repreview(body)
        snapshot = await run_in_threadpool(
            collection_import_service.repreview_item,
            batch_id,
            batch_item_id,
            command,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=snapshot.model_dump(mode="json"),
        )

    @app.post("/api/v1/collection-import-batches/{batch_id}/confirm")
    async def confirm_collection_import_batch(batch_id: str, request: Request):
        if not _collection_import_content_type_supported(
            request.headers.get("content-type")
        ):
            raise CollectionImportError(
                "BATCH_CONTENT_TYPE_UNSUPPORTED",
                "批量导入只接受 UTF-8 JSON。",
                415,
            )
        body = await _read_collection_import_body(request)
        command = parse_collection_import_confirm(body)
        snapshot = await run_in_threadpool(
            collection_import_service.confirm_batch,
            batch_id,
            command,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=snapshot.model_dump(mode="json"),
        )

    @app.post("/api/v1/collection-import-batches/{batch_id}/resume")
    async def resume_collection_import_batch(batch_id: str, request: Request):
        if not _collection_import_content_type_supported(
            request.headers.get("content-type")
        ):
            raise CollectionImportError(
                "BATCH_CONTENT_TYPE_UNSUPPORTED",
                "批量导入只接受 UTF-8 JSON。",
                415,
            )
        body = await _read_collection_import_body(request)
        command = parse_collection_import_resume(body)
        snapshot = await run_in_threadpool(
            collection_import_service.resume_batch,
            batch_id,
            command,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=snapshot.model_dump(mode="json"),
        )

    @app.post("/api/v1/collection-import-batches/{batch_id}/cancel")
    async def cancel_collection_import_batch(batch_id: str, request: Request):
        if not _collection_import_content_type_supported(
            request.headers.get("content-type")
        ):
            raise CollectionImportError(
                "BATCH_CONTENT_TYPE_UNSUPPORTED",
                "批量导入只接受 UTF-8 JSON。",
                415,
            )
        body = await _read_collection_import_body(request)
        command = parse_collection_import_cancel(body)
        snapshot, status_code = await run_in_threadpool(
            collection_import_service.cancel_batch,
            batch_id,
            command,
        )
        return JSONResponse(
            status_code=status_code,
            content=snapshot.model_dump(mode="json"),
        )

    @app.post(
        "/api/v1/collection-previews",
        response_model=CollectionPreview,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_collection_preview(request: CollectionPreviewRequest):
        preview = await run_in_threadpool(capture_service.preview, request)
        if request.refresh_metadata and preview.metadata.cover_url.value:
            try:
                await run_in_threadpool(
                    cover_service.mark_stale, preview.metadata.cover_url.value
                )
            except Exception:
                # Cache invalidation is derived state. It must never turn a valid
                # metadata preview into a failed collection request.
                pass
        return preview

    @app.post(
        "/api/v1/inspiration-transcriptions",
        response_model=InspirationTranscriptionDraft,
        status_code=status.HTTP_200_OK,
    )
    async def create_inspiration_transcription(
        request: Request,
        file: UploadFile = File(...),
    ):
        try:
            form = await request.form()
            if request.query_params or [
                key for key, _ in form.multi_items()
            ] != ["file"]:
                raise PipelineError(
                    "INSPIRATION_REQUEST_INVALID",
                    "灵感转写请求只能包含一份用户主动录制的音频文件。",
                )
            return await run_in_threadpool(
                inspiration_service.transcribe,
                file.file,
                file.content_type,
            )
        finally:
            await file.close()

    @app.post(
        "/api/v1/collection-items",
        response_model=CollectionItem,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_collection_item(
        request: CollectionItemCreateRequest,
        idempotency_key: str = Header(
            min_length=1, max_length=200, alias="Idempotency-Key"
        ),
        user_cover_claim_token: str = Header(
            default="", max_length=200, alias="X-User-Cover-Claim-Token"
        ),
    ):
        return await run_in_threadpool(
            collection_service.create,
            request,
            idempotency_key,
            user_cover_claim_token,
        )

    @app.get(
        "/api/v1/collection-items",
        response_model=CollectionSearchPage,
    )
    async def search_collection_items(
        query: str = Query(default="", max_length=500),
        platform: CollectionPlatform | None = Query(default=None),
        primary_category: str = Query(default="", max_length=64),
        secondary_category: str = Query(default="", max_length=64),
        tag: str = Query(default="", max_length=500),
        tag_source: Literal["platform", "organization", "personal"] | None = Query(
            default=None
        ),
        limit: int = Query(default=24, ge=1, le=100),
        cursor: str = Query(default="", max_length=500),
    ):
        return await run_in_threadpool(
            collection_service.search,
            query=query,
            platform=platform,
            primary_category=primary_category,
            secondary_category=secondary_category,
            tag=tag,
            tag_source=tag_source,
            limit=limit,
            cursor=cursor,
        )

    @app.get(
        "/api/v1/collection-items/{collection_item_id}",
        response_model=CollectionItem,
    )
    async def get_collection_item(collection_item_id: str):
        return await run_in_threadpool(collection_service.get, collection_item_id)

    @app.patch(
        "/api/v1/collection-items/{collection_item_id}",
        response_model=CollectionItem,
    )
    async def update_collection_item(
        collection_item_id: str,
        request: CollectionItemUpdateRequest,
        user_cover_claim_token: str = Header(
            default="", max_length=200, alias="X-User-Cover-Claim-Token"
        ),
    ):
        return await run_in_threadpool(
            collection_service.update,
            collection_item_id,
            request,
            user_cover_claim_token,
        )

    def cover_url_from_graph(graph: dict | None, *, preview: bool) -> str:
        if graph is None:
            return ""
        if preview:
            try:
                if datetime.fromisoformat(str(graph["expires_at"])) <= datetime.now(UTC):
                    return ""
            except (KeyError, TypeError, ValueError):
                return ""
        try:
            value = graph["metadata"]["cover_url"]["value"]
        except (KeyError, TypeError):
            return ""
        return value if isinstance(value, str) and value.strip() else ""

    def load_preview_cover(preview_id: str) -> CoverAsset:
        if not preview_id or len(preview_id) > 200:
            raise CoverUnavailable()
        graph = pipeline.repository.get_collection_preview(preview_id)
        source_url = cover_url_from_graph(graph, preview=True)
        if not source_url:
            raise CoverUnavailable()
        return cover_service.get(source_url)

    def load_item_cover(
        collection_item_id: str, *, cache_only: bool = False, source_only: bool = False,
        user_only: bool = False,
    ) -> CoverAsset:
        if not collection_item_id or len(collection_item_id) > 200:
            raise CoverUnavailable()
        if user_only:
            return user_cover_service.get_for_item(collection_item_id)
        if not source_only:
            try:
                return user_cover_service.get_for_item(collection_item_id)
            except CoverUnavailable:
                pass
        graph = pipeline.repository.get_collection_item(collection_item_id)
        source_url = cover_url_from_graph(graph, preview=False)
        if not source_url:
            raise CoverUnavailable()
        if cache_only:
            return cover_service.get_cached(source_url)
        return cover_service.get(source_url)

    def cover_response(asset: CoverAsset, if_none_match: str) -> Response:
        headers = {
            "Cache-Control": "private, no-cache",
            "Cross-Origin-Resource-Policy": "same-origin",
            "ETag": asset.etag,
            "X-Content-Type-Options": "nosniff",
            "X-Cover-Cache": asset.cache_status,
        }
        candidates: set[str] = set()
        for raw_value in if_none_match.split(","):
            value = raw_value.strip()
            if value[:2].casefold() == "w/":
                value = value[2:].strip()
            candidates.add(value)
        if "*" in candidates or asset.etag in candidates:
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
        return Response(content=asset.body, media_type=asset.media_type, headers=headers)

    def cover_unavailable_response() -> Response:
        return Response(
            status_code=status.HTTP_404_NOT_FOUND,
            headers={
                "Cache-Control": "no-store",
                "Cross-Origin-Resource-Policy": "same-origin",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/v1/collection-previews/{preview_id}/cover")
    async def get_collection_preview_cover(
        request: Request,
        preview_id: str,
        if_none_match: str = Header(default="", alias="If-None-Match"),
    ):
        if request.query_params:
            return cover_unavailable_response()
        try:
            asset = await run_in_threadpool(load_preview_cover, preview_id)
        except CoverUnavailable:
            return cover_unavailable_response()
        return cover_response(asset, if_none_match)

    @app.get("/api/v1/collection-items/{collection_item_id}/cover")
    async def get_collection_item_cover(
        request: Request,
        collection_item_id: str,
        if_none_match: str = Header(default="", alias="If-None-Match"),
    ):
        query_items = list(request.query_params.multi_items())
        if query_items not in (
            [],
            [("cache", "only")],
            [("source", "only")],
            [("user", "only")],
        ):
            return cover_unavailable_response()
        cache_only = query_items == [("cache", "only")]
        source_only = query_items == [("source", "only")]
        try:
            asset = await run_in_threadpool(
                load_item_cover,
                collection_item_id,
                cache_only=cache_only,
                source_only=source_only,
                user_only=query_items == [("user", "only")],
            )
        except CoverUnavailable:
            return cover_unavailable_response()
        return cover_response(asset, if_none_match)

    @app.post(
        "/api/v1/user-cover-assets",
        response_model=UserCoverDraftResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_user_cover_asset(
        request: Request,
        file: UploadFile = File(...),
    ):
        try:
            form = await request.form()
            if request.query_params or [key for key, _ in form.multi_items()] != ["file"]:
                raise PipelineError(
                    "USER_COVER_REQUEST_INVALID",
                    "一次只能提交一张图片。",
                )
            draft = await run_in_threadpool(
                user_cover_service.create, file.file, file.content_type
            )
            return draft.as_dict()
        finally:
            await file.close()

    @app.get("/api/v1/user-cover-assets/{asset_id}/content")
    async def get_user_cover_asset_content(
        request: Request,
        asset_id: str,
        claim_token: str = Header(
            default="", max_length=200, alias="X-User-Cover-Claim-Token"
        ),
        if_none_match: str = Header(default="", alias="If-None-Match"),
    ):
        if request.query_params:
            return cover_unavailable_response()
        try:
            asset = await run_in_threadpool(
                user_cover_service.get_claimed, asset_id, claim_token
            )
        except CoverUnavailable:
            return cover_unavailable_response()
        return cover_response(asset, if_none_match)

    @app.delete(
        "/api/v1/user-cover-assets/{asset_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_user_cover_asset(
        request: Request,
        asset_id: str,
        claim_token: str = Header(
            default="", max_length=200, alias="X-User-Cover-Claim-Token"
        ),
    ):
        if request.query_params:
            return cover_unavailable_response()
        deleted = await run_in_threadpool(
            user_cover_service.delete_claimed_unbound, asset_id, claim_token
        )
        if not deleted:
            return cover_unavailable_response()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/api/v1/collection-items/{collection_item_id}/deep-analysis",
        response_model=CollectionDeepAnalysisSnapshot,
    )
    async def get_collection_deep_analysis(collection_item_id: str):
        return await run_in_threadpool(
            collection_deep_analysis_service.get, collection_item_id
        )

    @app.post(
        "/api/v1/collection-items/{collection_item_id}/deep-analysis",
        response_model=CollectionDeepAnalysisSnapshot,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_collection_deep_analysis(
        collection_item_id: str,
        background_tasks: BackgroundTasks,
    ):
        snapshot, launch = await run_in_threadpool(
            collection_deep_analysis_service.start, collection_item_id
        )
        if launch is not None:
            background_tasks.add_task(
                collection_deep_analysis_service.run_start, *launch
            )
        return snapshot

    @app.post(
        "/api/v1/collection-items/{collection_item_id}/deep-analysis/retry",
        response_model=CollectionDeepAnalysisSnapshot,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_collection_deep_analysis(
        collection_item_id: str,
        background_tasks: BackgroundTasks,
        idempotency_key: str = Header(
            min_length=1, max_length=200, alias="Idempotency-Key"
        ),
    ):
        snapshot, job_id = await run_in_threadpool(
            collection_deep_analysis_service.claim_retry,
            collection_item_id,
            idempotency_key,
        )
        if job_id is not None:
            background_tasks.add_task(
                collection_deep_analysis_service.run_retry,
                collection_item_id,
                job_id,
            )
        return snapshot

    @app.post("/api/v1/uploads", response_model=UploadResponse)
    async def upload_media(
        file: UploadFile = File(...),
        subtitle: UploadFile | None = File(default=None),
        retain_media: bool = Form(default=False),
    ):
        request_dir = Path(tempfile.mkdtemp(prefix="upload-", dir=root))
        job = Job(id=str(uuid.uuid4()), status="queued", progress=0)
        if not pipeline.repository.save_job(job):
            raise PipelineError("JOB_STATE_CONFLICT", "无法创建上传任务。")
        safe_name = Path(file.filename or "upload.bin").name
        staged = request_dir / safe_name
        primary_error: BaseException | None = None
        try:
            (request_dir / TEMP_MARKER).write_text(
                f"job:{job.id}", encoding="utf-8"
            )
            size = 0
            with staged.open("wb") as stream:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_upload_bytes:
                        raise PipelineError("UPLOAD_TOO_LARGE", "上传文件超过大小限制。")
                    stream.write(chunk)
            if subtitle is not None:
                subtitle_path = Path(
                    f"{staged}{_validated_subtitle_suffix(subtitle.filename)}"
                )
                subtitle_size = 0
                with subtitle_path.open("wb") as stream:
                    while chunk := await subtitle.read(1024 * 1024):
                        subtitle_size += len(chunk)
                        if subtitle_size > 10 * 1024 * 1024:
                            raise PipelineError(
                                "UPLOAD_TOO_LARGE", "字幕文件超过大小限制。"
                            )
                        stream.write(chunk)
            job, result, markdown = await run_in_threadpool(
                pipeline.process, staged, None, job, retain_media
            )
            return {
                "job": job.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
                "markdown": markdown,
            }
        except BaseException as exc:
            primary_error = exc
            failed = pipeline.repository.get_job(job.id)
            if failed is not None and failed.status not in {
                "failed", "cancelled", "completed", "completed_with_warnings"
            }:
                failed.status = "failed"
                failed.error_code = (
                    exc.code if isinstance(exc, PipelineError) else "UPLOAD_FAILED"
                )
                failed.message = str(exc)
                failed.retryable = False
                pipeline.repository.save_job(failed)
            raise
        finally:
            close_error: BaseException | None = None
            try:
                await file.close()
            except BaseException as exc:
                close_error = exc
            if subtitle is not None:
                try:
                    await subtitle.close()
                except BaseException as exc:
                    close_error = close_error or exc
            cleanup_error: OSError | None = None
            try:
                shutil.rmtree(request_dir)
            except OSError as exc:
                cleanup_error = exc
                message = "上传临时文件清理失败，需要人工检查受限请求目录。"
                pipeline.repository.add_system_warning(
                    "UPLOAD_CLEANUP_FAILED", message
                )
            if close_error is not None:
                pipeline.repository.add_system_warning(
                    "UPLOAD_CLOSE_FAILED",
                    "上传流关闭失败；请求目录清理已继续执行。",
                )
            # Preserve the primary processing/client-interruption error. If
            # processing itself succeeded, cleanup failure becomes primary.
            if primary_error is None and (cleanup_error or close_error):
                raise PipelineError(
                    "CLEANUP_FAILED", "上传临时资源清理失败，需要人工检查。"
                ) from (cleanup_error or close_error)

    @app.get("/api/v1/jobs/{job_id}", response_model=Job)
    async def get_job(job_id: str):
        job = pipeline.repository.get_job(job_id)
        if job is None:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "NOT_FOUND", "message": "任务不存在。"}},
            )
        return job.model_dump(mode="json")

    @app.post("/api/v1/jobs/{job_id}/cancel", response_model=Job)
    async def cancel_job(job_id: str):
        return await run_in_threadpool(recovery_service.cancel, job_id)

    @app.post("/api/v1/jobs/{job_id}/retry", response_model=Job)
    async def retry_job(job_id: str):
        return await run_in_threadpool(recovery_service.retry, job_id)

    @app.post(
        "/api/v1/job-batches",
        response_model=JobBatch,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_job_batch(request: JobBatchCreateRequest):
        return await run_in_threadpool(
            batch_service.create,
            [
                (item.job_id, item.title, item.platform)
                for item in request.items
            ],
        )

    @app.get("/api/v1/job-batches", response_model=list[JobBatch])
    async def list_job_batches(
        limit: int = Query(default=10, ge=1, le=50),
    ):
        return await run_in_threadpool(batch_service.list, limit=limit)

    @app.get("/api/v1/job-batches/{batch_id}", response_model=JobBatch)
    async def get_job_batch(batch_id: str):
        return await run_in_threadpool(batch_service.get, batch_id)

    @app.post(
        "/api/v1/job-batches/{batch_id}/jobs/{job_id}/cancel",
        response_model=JobBatch,
    )
    async def cancel_job_batch_item(batch_id: str, job_id: str):
        return await run_in_threadpool(
            batch_service.cancel_job,
            batch_id,
            job_id,
        )

    @app.post(
        "/api/v1/videos/{video_id}/extractions",
        response_model=VideoResult,
    )
    async def create_focused_extraction(
        video_id: str, request: FocusedExtractionRequest
    ):
        return await run_in_threadpool(
            focused_service.extract,
            video_id,
            request.focus_query,
            platform=request.platform,
        )

    @app.post(
        "/api/v1/videos/{video_id}/questions",
        response_model=VideoQuestion,
    )
    async def ask_video_question(video_id: str, request: QuestionRequest):
        return await run_in_threadpool(
            question_service.ask,
            video_id,
            request.question,
            platform=request.platform,
        )

    @app.get("/api/v1/videos", response_model=VideoSearchPage)
    async def search_videos(
        query: str = Query(default="", max_length=500),
        platform: Platform | None = None,
        tag: str = Query(default="", max_length=64),
        tag_source: Literal["platform", "automatic", "personal"] | None = None,
        primary_category: str = Query(default="", max_length=64),
        secondary_category: str = Query(default="", max_length=64),
        favorite: bool | None = None,
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ):
        return await run_in_threadpool(
            library_service.search,
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

    @app.get("/api/v1/videos/{video_id}", response_model=VideoDetail)
    async def get_video_detail(
        video_id: str, platform: Platform | None = None
    ):
        return await run_in_threadpool(
            library_service.get_detail, video_id, platform=platform
        )

    @app.get("/api/v1/videos/{video_id}/media")
    async def stream_video_media(
        video_id: str,
        platform: Platform | None = None,
        range_header: str | None = Header(default=None, alias="Range"),
    ):
        detail = await run_in_threadpool(
            library_service.get_detail, video_id, platform=platform
        )
        if pipeline.media_service is None:
            raise PipelineError("MEDIA_UNAVAILABLE", "持久媒体服务未配置。")
        try:
            media = await run_in_threadpool(
                pipeline.media_service.open_stream,
                video_id,
                detail.result.platform,
            )
        except RetainedMediaError as exc:
            raise PipelineError(exc.code, str(exc)) from exc
        total = media.record.size_bytes
        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'inline; filename="media"',
        }
        if range_header is None:
            byte_range = ByteRange(0, total - 1)
            response_status = status.HTTP_200_OK
        else:
            try:
                byte_range = parse_single_range(range_header, total)
            except RangeNotSatisfiable:
                return Response(
                    status_code=416,
                    headers={
                        **headers,
                        "Content-Range": f"bytes */{total}",
                    },
                )
            response_status = status.HTTP_206_PARTIAL_CONTENT
            headers["Content-Range"] = (
                f"bytes {byte_range.start}-{byte_range.end}/{total}"
            )
        headers["Content-Length"] = str(byte_range.length)
        return StreamingResponse(
            iter_media(media.path, byte_range),
            status_code=response_status,
            media_type=media.record.mime_type,
            headers=headers,
        )

    @app.delete(
        "/api/v1/videos/{video_id}/media",
        response_model=PlaybackCapability,
    )
    async def delete_video_media(
        video_id: str,
        platform: Platform | None = None,
    ):
        detail = await run_in_threadpool(
            library_service.get_detail, video_id, platform=platform
        )
        if pipeline.media_service is None:
            return PlaybackCapability(
                reason=(
                    "not_retained"
                    if detail.result.platform == "local_upload"
                    else "unsupported_source"
                )
            )
        try:
            return await run_in_threadpool(
                pipeline.media_service.delete,
                video_id,
                detail.result.platform,
            )
        except RetainedMediaError as exc:
            raise PipelineError(exc.code, str(exc)) from exc

    @app.get(
        "/api/v1/videos/{video_id}/extractions",
        response_model=list[FocusedHistoryItem],
    )
    async def get_focused_history(
        video_id: str, platform: Platform | None = None
    ):
        detail = await run_in_threadpool(
            library_service.get_detail, video_id, platform=platform
        )
        return detail.focused_history

    @app.post("/api/v1/videos/{video_id}/favorite", response_model=VideoDetail)
    async def set_video_favorite(video_id: str, request: FavoriteRequest):
        return await run_in_threadpool(
            library_service.set_favorite,
            video_id,
            request.favorite,
            platform=request.platform,
        )

    @app.post("/api/v1/videos/{video_id}/tags", response_model=VideoDetail)
    async def update_video_tags(video_id: str, request: TagUpdateRequest):
        return await run_in_threadpool(
            library_service.update_tags,
            video_id,
            request.tags,
            request.operation,
            platform=request.platform,
        )

    @app.put(
        "/api/v1/videos/{video_id}/classification",
        response_model=VideoDetail,
    )
    async def set_video_classification(
        video_id: str, request: ClassificationUpdateRequest
    ):
        return await run_in_threadpool(
            library_service.set_classification,
            video_id,
            request.primary_category,
            request.secondary_category,
            platform=request.platform,
        )

    @app.post(
        "/api/v1/videos/{video_id}/automatic-tags",
        response_model=VideoDetail,
    )
    async def generate_video_automatic_tags(
        video_id: str, request: AutomaticTagsRequest
    ):
        return await run_in_threadpool(
            library_service.generate_automatic_tags,
            video_id,
            platform=request.platform,
        )

    @app.get(
        "/api/v1/videos/{video_id}/personal-notes",
        response_model=PersonalNotes,
    )
    async def get_video_personal_notes(
        video_id: str, platform: Platform | None = None
    ):
        return await run_in_threadpool(
            library_service.get_personal_notes,
            video_id,
            platform=platform,
        )

    @app.put(
        "/api/v1/videos/{video_id}/spark",
        response_model=PersonalNotes,
    )
    async def upsert_video_spark(video_id: str, request: SparkUpsertRequest):
        return await run_in_threadpool(
            library_service.upsert_spark,
            video_id,
            request.content,
            request.author,
            platform=request.platform,
        )

    @app.delete(
        "/api/v1/videos/{video_id}/spark",
        response_model=PersonalNotes,
    )
    async def delete_video_spark(
        video_id: str, platform: Platform | None = None
    ):
        return await run_in_threadpool(
            library_service.delete_spark,
            video_id,
            platform=platform,
        )

    @app.post(
        "/api/v1/videos/{video_id}/annotations",
        response_model=PersonalNotes,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_video_annotation(
        video_id: str, request: AnnotationCreateRequest
    ):
        return await run_in_threadpool(
            library_service.create_annotation,
            video_id,
            request.target_key,
            request.content,
            request.author,
            platform=request.platform,
        )

    @app.patch(
        "/api/v1/videos/{video_id}/annotations/{annotation_id}",
        response_model=PersonalNotes,
    )
    async def update_video_annotation(
        video_id: str,
        annotation_id: int,
        request: AnnotationUpdateRequest,
    ):
        return await run_in_threadpool(
            library_service.update_annotation,
            video_id,
            annotation_id,
            request.content,
            request.author,
            platform=request.platform,
        )

    @app.delete(
        "/api/v1/videos/{video_id}/annotations/{annotation_id}",
        response_model=PersonalNotes,
    )
    async def delete_video_annotation(
        video_id: str,
        annotation_id: int,
        platform: Platform | None = None,
    ):
        return await run_in_threadpool(
            library_service.delete_annotation,
            video_id,
            annotation_id,
            platform=platform,
        )

    @app.get("/api/v1/videos/{video_id}/export.md")
    async def export_video_markdown(
        video_id: str,
        platform: Platform | None = None,
        view: Literal["all", "overview", "steps", "transcript"] = "all",
        include_personal: bool = True,
        focus_query_hash: str = Query(default="", max_length=64),
    ):
        content = await run_in_threadpool(
            library_service.export_markdown,
            video_id,
            platform=platform,
            view=view,
            include_personal=include_personal,
            focus_query_hash=focus_query_hash,
        )
        return PlainTextResponse(content, media_type="text/markdown")

    return app
