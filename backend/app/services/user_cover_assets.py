from __future__ import annotations

import hashlib
import io
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Callable, Protocol

from PIL import Image, ImageOps

from .cover_cache import CoverAsset, CoverUnavailable, validate_image
from .pipeline import PipelineError


MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_DIMENSION = 8192
MAX_PIXELS = 32_000_000
MAX_LONG_SIDE = 2048
TEMP_LIFETIME = timedelta(hours=24)
ALLOWED_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
ASSET_ID_RE = re.compile(r"^[0-9a-f]{32}$")
STORAGE_NAME_RE = re.compile(r"^(?P<asset>[0-9a-f]{32})\.webp$")


class UserCoverRepository(Protocol):
    def create_user_cover_asset(self, record: dict[str, Any]) -> None: ...
    def get_user_cover_asset(self, asset_id: str) -> dict[str, Any] | None: ...
    def get_user_cover_asset_for_item(self, item_id: str) -> dict[str, Any] | None: ...
    def delete_unbound_user_cover_asset(
        self, asset_id: str, claim_token_hash: str
    ) -> dict[str, Any] | None: ...
    def expired_unbound_user_cover_assets(self, now: str) -> list[dict[str, Any]]: ...
    def delete_expired_user_cover_asset(
        self, asset_id: str, now: str
    ) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class UserCoverDraft:
    asset_id: str
    claim_token: str
    media_type: str
    width: int
    height: int
    size_bytes: int
    expires_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "claim_token": self.claim_token,
            "media_type": self.media_type,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "expires_at": self.expires_at,
        }


def claim_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class UserCoverAssetService:
    """Own user-provided cover bytes independently from derived source covers."""

    def __init__(
        self,
        repository: UserCoverRepository,
        root: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(UTC))
        self.cleanup_expired()

    def create(self, stream: BinaryIO, media_type: str | None) -> UserCoverDraft:
        normalized_type = (media_type or "").split(";", 1)[0].strip().casefold()
        if normalized_type not in ALLOWED_MEDIA_TYPES:
            raise PipelineError(
                "USER_COVER_TYPE_UNSUPPORTED",
                "图片只支持静态 JPEG、PNG 或 WebP。",
            )
        body = stream.read(MAX_IMAGE_BYTES + 1)
        if len(body) > MAX_IMAGE_BYTES:
            raise PipelineError("USER_COVER_TOO_LARGE", "图片不能超过 5 MiB。")
        try:
            validate_image(
                body,
                normalized_type,
                max_bytes=MAX_IMAGE_BYTES,
                max_dimension=MAX_DIMENSION,
                max_pixels=MAX_PIXELS,
            )
            output, width, height = self._sanitize(body)
            validate_image(
                output,
                "image/webp",
                max_bytes=MAX_IMAGE_BYTES,
                max_dimension=MAX_DIMENSION,
                max_pixels=MAX_PIXELS,
            )
        except Exception as exc:
            raise PipelineError(
                "USER_COVER_TYPE_UNSUPPORTED",
                "图片内容损坏、类型不匹配、含动画或尺寸超出限制。",
            ) from exc
        if len(output) > MAX_IMAGE_BYTES:
            raise PipelineError(
                "USER_COVER_TOO_LARGE", "处理后的图片仍超过 5 MiB，请换一张图片。"
            )

        asset_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        now = self._now()
        expires_at = now + TEMP_LIFETIME
        storage_name = f"{asset_id}.webp"
        path = self._path(asset_id, storage_name)
        try:
            with path.open("xb") as target:
                target.write(output)
            self.repository.create_user_cover_asset(
                {
                    "asset_id": asset_id,
                    "storage_name": storage_name,
                    "media_type": "image/webp",
                    "byte_size": len(output),
                    "content_sha256": hashlib.sha256(output).hexdigest(),
                    "width": width,
                    "height": height,
                    "claim_token_hash": claim_token_hash(token),
                    "created_at": now.isoformat(),
                    "expires_at": expires_at.isoformat(),
                }
            )
        except BaseException:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return UserCoverDraft(
            asset_id=asset_id,
            claim_token=token,
            media_type="image/webp",
            width=width,
            height=height,
            size_bytes=len(output),
            expires_at=expires_at.isoformat(),
        )

    def get_claimed(self, asset_id: str, token: str) -> CoverAsset:
        if not token:
            raise CoverUnavailable()
        record = self.repository.get_user_cover_asset(asset_id)
        if record is None or not secrets.compare_digest(
            record["claim_token_hash"], claim_token_hash(token)
        ):
            raise CoverUnavailable()
        expires_at = record.get("expires_at")
        if expires_at and datetime.fromisoformat(expires_at) <= self._now():
            raise CoverUnavailable()
        return self._load_record(record)

    def get_for_item(self, item_id: str) -> CoverAsset:
        record = self.repository.get_user_cover_asset_for_item(item_id)
        if record is None:
            raise CoverUnavailable()
        return self._load_record(record)

    def delete_claimed_unbound(self, asset_id: str, token: str) -> bool:
        if not token:
            return False
        record = self.repository.delete_unbound_user_cover_asset(
            asset_id, claim_token_hash(token)
        )
        if record is None:
            return False
        try:
            self._path(asset_id, record["storage_name"]).unlink(missing_ok=True)
        except OSError:
            self.repository.create_user_cover_asset(record)
            return False
        return True

    def cleanup_expired(self) -> int:
        now = self._now().isoformat()
        removed = 0
        for candidate in self.repository.expired_unbound_user_cover_assets(now):
            record = self.repository.delete_expired_user_cover_asset(
                candidate["asset_id"], now
            )
            if record is None:
                continue
            try:
                self._path(record["asset_id"], record["storage_name"]).unlink(
                    missing_ok=True
                )
            except OSError:
                self.repository.create_user_cover_asset(record)
                continue
            removed += 1
        return removed

    def _sanitize(self, body: bytes) -> tuple[bytes, int, int]:
        with Image.open(io.BytesIO(body)) as source:
            oriented = ImageOps.exif_transpose(source)
            oriented.load()
            oriented.thumbnail((MAX_LONG_SIDE, MAX_LONG_SIDE), Image.Resampling.LANCZOS)
            has_alpha = "A" in oriented.getbands()
            mode = "RGBA" if has_alpha else "RGB"
            clean = Image.new(mode, oriented.size)
            clean.paste(oriented.convert(mode))
        output = io.BytesIO()
        clean.save(output, format="WEBP", quality=88, method=4, exact=has_alpha)
        return output.getvalue(), clean.width, clean.height

    def _load_record(self, record: dict[str, Any]) -> CoverAsset:
        asset_id = str(record.get("asset_id") or "")
        path = self._path(asset_id, str(record.get("storage_name") or ""))
        try:
            body = path.read_bytes()
        except OSError as exc:
            raise CoverUnavailable() from exc
        if (
            len(body) != record["byte_size"]
            or hashlib.sha256(body).hexdigest() != record["content_sha256"]
        ):
            raise CoverUnavailable()
        try:
            width, height = validate_image(
                body,
                record["media_type"],
                max_bytes=MAX_IMAGE_BYTES,
                max_dimension=MAX_DIMENSION,
                max_pixels=MAX_PIXELS,
            )
        except CoverUnavailable:
            raise
        if (width, height) != (record["width"], record["height"]):
            raise CoverUnavailable()
        return CoverAsset(
            body=body,
            media_type=record["media_type"],
            content_sha256=record["content_sha256"],
            width=width,
            height=height,
            cache_status="HIT",
        )

    def _path(self, asset_id: str, storage_name: str) -> Path:
        match = STORAGE_NAME_RE.fullmatch(storage_name)
        if ASSET_ID_RE.fullmatch(asset_id) is None or match is None or match.group("asset") != asset_id:
            raise CoverUnavailable()
        path = (self.root / storage_name).resolve()
        if path.parent != self.root:
            raise CoverUnavailable()
        return path

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
