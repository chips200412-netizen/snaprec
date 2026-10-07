from __future__ import annotations

import hashlib
import json
import base64
import binascii
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from pydantic import ValidationError

from ..domain.models import (
    CollectionItem,
    CollectionItemCreateRequest,
    CollectionItemUpdateRequest,
    CollectionSearchPage,
    CollectionItemTrustedCreate,
    CollectionOrganizationConfirmationUpdate,
)
from ..repositories.sqlite import (
    CollectionExistsError,
    CollectionRevisionConflictError,
    IdempotencyKeyReusedError,
)
from .pipeline import PipelineError
from .user_cover_assets import claim_token_hash


class CollectionRepository(Protocol):
    def create_collection_item(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
        request_hash: str,
        user_cover_claim_hash: str | None = None,
    ) -> dict[str, Any]: ...

    def get_collection_item(self, collection_item_id: str) -> dict[str, Any] | None: ...
    def update_collection_item_user_fields(
        self,
        collection_item_id: str,
        expected_revision: int,
        payload: dict[str, Any],
        user_cover_claim_hash: str | None = None,
    ) -> dict[str, Any] | None: ...
    def search_collection_items(
        self,
        *,
        query: str,
        platform: str | None,
        primary_category: str,
        secondary_category: str,
        tag: str,
        tag_source: str | None,
        limit: int,
        after: tuple[str, str] | None,
    ) -> dict[str, Any]: ...
    def get_collection_preview(self, preview_id: str) -> dict[str, Any] | None: ...
    def replay_collection_item(
        self, idempotency_key: str, request_hash: str
    ) -> dict[str, Any] | None: ...


def _request_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class PreparedCollectionCreate:
    """One authoritative normalized collection-create payload and its hashes."""

    payload: dict[str, Any]
    request_hash: str
    legacy_request_hash: str


@dataclass(frozen=True)
class PreparedLegacyCollectionCreate:
    payload: dict[str, Any]
    request_hash: str


def _prepare_legacy_collection_create(
    request: CollectionItemCreateRequest,
    preview: dict[str, Any],
) -> PreparedLegacyCollectionCreate:
    user_payload = request.model_dump(mode="json")
    try:
        trusted = CollectionItemTrustedCreate.model_validate(
            {
                "source_kind": preview["source_kind"],
                "platform": preview["platform"],
                "original_input": preview["original_input"],
                "source_url": preview["source_url"],
                "canonical_url": preview["canonical_url"],
                "metadata_status": preview["metadata_status"],
                "metadata": preview["metadata"],
                "user_title": user_payload["user_title"],
                "untitled_confirmed": user_payload["untitled_confirmed"],
                "organization_suggestion": preview.get("organization_suggestion")
                or {
                    "primary_category": "",
                    "secondary_category": "",
                    "tags": [],
                    "basis": "public_metadata",
                    "method": "deterministic",
                    "status": "insufficient_metadata",
                },
                "organization_confirmation": user_payload[
                    "organization_confirmation"
                ],
                "personal_tags": user_payload["personal_tags"],
                "inspiration": user_payload["inspiration"],
            }
        )
    except (KeyError, TypeError, ValidationError) as exc:
        raise PipelineError(
            "COLLECTION_VALIDATION_FAILED", "收藏内容未满足保存条件。"
        ) from exc

    legacy_payload = trusted.model_dump(mode="json")
    if user_payload.get("user_author") is not None:
        legacy_payload["user_author"] = user_payload["user_author"]
    if user_payload.get("user_cover_asset_id") is not None:
        legacy_payload["user_cover_asset_id"] = user_payload[
            "user_cover_asset_id"
        ]
    if request.selected_source_topic_indices is not None:
        indices = request.selected_source_topic_indices
        representatives: dict[str, int] = {}
        for index, topic in enumerate(trusted.metadata.platform_tags):
            identity = " ".join(
                unicodedata.normalize("NFKC", topic.value).strip().split()
            ).casefold()
            representatives.setdefault(identity, index)
        allowed = set(representatives.values())
        # Check the raw count before deduplication so repeated oversized input
        # cannot silently collapse into a valid user selection.
        if (
            len(indices) > len(allowed)
            or len(set(indices)) != len(indices)
            or any(index not in allowed for index in indices)
        ):
            raise PipelineError(
                "COLLECTION_VALIDATION_FAILED", "来源话题选择无效，请核对预览。"
            )
        # Omitted requests retain the exact pre-CQ2 hash. Explicit requests
        # include the selection in both hash paths and can never replay a hash
        # that omitted the field, even when their selection is empty.
        legacy_payload["selected_source_topic_indices"] = sorted(indices)
    return PreparedLegacyCollectionCreate(
        payload=legacy_payload,
        request_hash=_request_hash(legacy_payload),
    )


def _normalize_collection_create(
    legacy: PreparedLegacyCollectionCreate,
) -> PreparedCollectionCreate:
    try:
        normalized_confirmation = (
            CollectionOrganizationConfirmationUpdate.model_validate(
                legacy.payload["organization_confirmation"]
            ).model_dump(mode="json")
        )
    except ValidationError as exc:
        raise PipelineError(
            "COLLECTION_VALIDATION_FAILED", "收藏内容未满足保存条件。"
        ) from exc
    payload = {
        **legacy.payload,
        "organization_confirmation": normalized_confirmation,
    }
    return PreparedCollectionCreate(
        payload=payload,
        request_hash=_request_hash(payload),
        legacy_request_hash=legacy.request_hash,
    )


def prepare_collection_create(
    request: CollectionItemCreateRequest,
    preview: dict[str, Any],
) -> PreparedCollectionCreate:
    """Normalize a create exactly as :class:`CollectionService` will persist it.

    Batch import freezes this result before dispatch.  Keeping the helper here
    prevents the batch path from growing a subtly different canonicalization or
    request-hash algorithm from the existing single-link save path.
    """

    return _normalize_collection_create(
        _prepare_legacy_collection_create(request, preview)
    )


def _collection_item(stored: dict[str, Any]) -> CollectionItem:
    response = dict(stored)
    # v15 keeps create idempotency replay compatible with response snapshots
    # written by v14, before CollectionItem exposed a revision.
    response.setdefault("revision", 1)
    response.setdefault("user_author", None)
    response.setdefault("user_cover_asset_id", None)
    metadata = response.get("metadata") or {}
    title_field = metadata.get("title") or {}
    source_title = (
        (title_field.get("value") or "").strip()
        if title_field.get("source") != "none"
        else ""
    )
    response["display_title"] = (
        (response.get("user_title") or "").strip()
        or source_title
        or "未命名收藏"
    )
    return CollectionItem.model_validate(response)


def _validate_patch_inspiration(inspiration: dict[str, Any] | None) -> None:
    if inspiration is None:
        return
    expected_status = (
        "not_applicable" if inspiration["input_mode"] == "text" else "completed"
    )
    if inspiration["transcription_status"] != expected_status:
        raise PipelineError(
            "COLLECTION_VALIDATION_FAILED",
            "收藏内容未满足保存条件。",
        )


def _query_fingerprint(
    *,
    query: str,
    platform: str | None,
    primary_category: str,
    secondary_category: str,
    tag: str,
    tag_source: str | None,
    limit: int,
) -> str:
    def normalized(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).strip().split()).casefold()

    payload = {
        "query": normalized(query),
        "platform": platform or "",
        "primary_category": normalized(primary_category),
        "secondary_category": normalized(secondary_category),
        "tag": normalized(tag),
        "tag_source": tag_source or "",
        "limit": limit,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _decode_cursor(cursor: str, fingerprint: str) -> tuple[str, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode((cursor + padding).encode("ascii")).decode("utf-8")
        )
        if (
            set(payload) != {"v", "updated_at", "id", "fingerprint"}
            or payload["v"] != 1
            or payload["fingerprint"] != fingerprint
            or not isinstance(payload["updated_at"], str)
            or not isinstance(payload["id"], str)
            or not payload["updated_at"]
            or not payload["id"]
        ):
            raise ValueError
        return payload["updated_at"], payload["id"]
    except (
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        UnicodeError,
        binascii.Error,
    ):
        raise PipelineError(
            "COLLECTION_CURSOR_INVALID", "列表游标无效，请从第一页重新加载。"
        ) from None


def _encode_cursor(updated_at: str, item_id: str, fingerprint: str) -> str:
    raw = json.dumps(
        {
            "v": 1,
            "updated_at": updated_at,
            "id": item_id,
            "fingerprint": fingerprint,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class CollectionService:
    def __init__(self, repository: CollectionRepository):
        self.repository = repository

    def create(
        self,
        request: CollectionItemCreateRequest,
        idempotency_key: str,
        user_cover_claim_token: str = "",
    ) -> CollectionItem:
        preview = self.repository.get_collection_preview(request.preview_id)
        if preview is None:
            raise PipelineError("PREVIEW_NOT_FOUND", "收藏预览不存在，请重新预览。")
        try:
            preview_expired = (
                datetime.fromisoformat(preview["expires_at"]) <= datetime.now(UTC)
            )
        except (KeyError, TypeError, ValueError):
            raise PipelineError(
                "PREVIEW_INVALID", "收藏预览无效，请重新预览。"
            ) from None
        legacy = _prepare_legacy_collection_create(request, preview)
        legacy_request_hash = legacy.request_hash
        legacy_hash_mismatch: IdempotencyKeyReusedError | None = None
        replay = None
        try:
            replay = self.repository.replay_collection_item(
                idempotency_key, legacy_request_hash
            )
        except IdempotencyKeyReusedError as exc:
            legacy_hash_mismatch = exc
        if replay is not None:
            return _collection_item(replay)

        # Keep the raw trusted response model compatible with v14 reads and old
        # idempotency hashes, but make every new v15 create pass through the same
        # authoritative editable-field normalization as PATCH.
        prepared = _normalize_collection_create(legacy)
        payload = prepared.payload
        request_hash = prepared.request_hash
        if request_hash != legacy_request_hash:
            try:
                replay = self.repository.replay_collection_item(
                    idempotency_key, request_hash
                )
            except IdempotencyKeyReusedError as exc:
                raise PipelineError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "该 Idempotency-Key 已用于不同的保存内容。",
                ) from exc
            if replay is not None:
                return _collection_item(replay)
        elif legacy_hash_mismatch is not None:
            raise PipelineError(
                "IDEMPOTENCY_KEY_REUSED",
                "该 Idempotency-Key 已用于不同的保存内容。",
            ) from legacy_hash_mismatch
        if preview_expired:
            raise PipelineError("PREVIEW_EXPIRED", "收藏预览已过期，请重新预览。")
        try:
            stored = self.repository.create_collection_item(
                payload,
                idempotency_key,
                request_hash,
                claim_token_hash(user_cover_claim_token)
                if payload.get("user_cover_asset_id") and user_cover_claim_token
                else None,
            )
        except IdempotencyKeyReusedError as exc:
            raise PipelineError(
                "IDEMPOTENCY_KEY_REUSED",
                "该 Idempotency-Key 已用于不同的保存内容。",
            ) from exc
        except CollectionExistsError as exc:
            error = PipelineError("COLLECTION_EXISTS", "该素材已存在于收藏库中。")
            error.collection_item_id = exc.collection_item_id
            raise error from exc
        except ValueError as exc:
            raise PipelineError(
                "COLLECTION_VALIDATION_FAILED",
                "用户封面引用无效、已过期或不属于当前草稿。",
            ) from exc
        return _collection_item(stored)

    def get(self, collection_item_id: str) -> CollectionItem:
        stored = self.repository.get_collection_item(collection_item_id)
        if stored is None:
            raise PipelineError("NOT_FOUND", "收藏素材不存在。")
        return _collection_item(stored)

    def update(
        self,
        collection_item_id: str,
        request: CollectionItemUpdateRequest,
        user_cover_claim_token: str = "",
    ) -> CollectionItem:
        payload = request.model_dump(mode="json")
        if "user_author" not in request.model_fields_set:
            payload.pop("user_author", None)
        if "user_cover_asset_id" not in request.model_fields_set:
            payload.pop("user_cover_asset_id", None)
        expected_revision = payload.pop("expected_revision")
        _validate_patch_inspiration(payload["inspiration"])
        try:
            stored = self.repository.update_collection_item_user_fields(
                collection_item_id,
                expected_revision,
                payload,
                claim_token_hash(user_cover_claim_token)
                if payload.get("user_cover_asset_id") and user_cover_claim_token
                else None,
            )
        except CollectionRevisionConflictError as exc:
            error = PipelineError(
                "COLLECTION_REVISION_CONFLICT",
                "收藏已在其他位置更新，请重新比较后再保存。",
            )
            error.collection_item_id = exc.collection_item_id
            error.expected_revision = exc.expected_revision
            error.current_revision = exc.current_revision
            raise error from exc
        except ValueError as exc:
            raise PipelineError(
                "COLLECTION_VALIDATION_FAILED",
                "收藏内容未满足保存条件。",
            ) from exc
        if stored is None:
            raise PipelineError("NOT_FOUND", "收藏素材不存在。")
        return _collection_item(stored)

    def search(
        self,
        *,
        query: str = "",
        platform: str | None = None,
        primary_category: str = "",
        secondary_category: str = "",
        tag: str = "",
        tag_source: Literal["platform", "organization", "personal"] | None = None,
        limit: int = 24,
        cursor: str = "",
    ) -> CollectionSearchPage:
        if bool(tag.strip()) != bool(tag_source):
            raise PipelineError(
                "COLLECTION_FILTER_INVALID",
                "标签筛选必须同时提供标签值和标签来源。",
            )
        fingerprint = _query_fingerprint(
            query=query,
            platform=platform,
            primary_category=primary_category,
            secondary_category=secondary_category,
            tag=tag,
            tag_source=tag_source,
            limit=limit,
        )
        after = _decode_cursor(cursor, fingerprint) if cursor else None
        result = self.repository.search_collection_items(
            query=query,
            platform=platform,
            primary_category=primary_category,
            secondary_category=secondary_category,
            tag=tag,
            tag_source=tag_source,
            limit=limit,
            after=after,
        )
        next_cursor = None
        if result.pop("has_more") and result["items"]:
            last = result["items"][-1]
            next_cursor = _encode_cursor(last["updated_at"], last["id"], fingerprint)
        return CollectionSearchPage.model_validate(
            {**result, "limit": limit, "next_cursor": next_cursor}
        )
