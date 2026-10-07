from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .models import (
    CollectionItemCreateRequest,
    CollectionPreview,
    CollectionOrganizationConfirmationUpdate,
    OrganizationConfirmation,
    PersonalInspirationDraft,
    StrictModel,
)


CollectionImportBatchStatus = Literal[
    "previewing",
    "awaiting_review",
    "saving",
    "cancelling",
    "interrupted",
    "completed",
    "completed_with_issues",
    "cancelled",
]
CollectionImportItemState = Literal[
    "queued",
    "previewing",
    "ready",
    "needs_review",
    "preview_expired",
    "duplicate_in_batch",
    "save_queued",
    "saving",
    "saved",
    "already_exists",
    "skipped",
    "failed",
    "cancelled",
    "interrupted",
    "outcome_unknown",
]
CollectionImportDecision = Literal["pending", "save", "skip"]


class CollectionImportCreateItem(StrictModel):
    client_item_id: str
    input_text: str


class CollectionImportDraft(StrictModel):
    user_title: str | None = Field(max_length=500)
    untitled_confirmed: bool
    organization_confirmation: OrganizationConfirmation
    personal_tags: list[str]
    inspiration: PersonalInspirationDraft | None

    @model_validator(mode="before")
    @classmethod
    def reuse_collection_create_validation(cls, value: Any) -> Any:
        """Normalize batch drafts through the existing single-save contract.

        The client never supplies ``preview_id``.  A fixed internal placeholder
        lets the shared request model validate exactly the five user-owned
        fields, after which the same confirmation model used by CollectionService
        applies the authoritative category/tag limits.
        """

        if not isinstance(value, dict):
            return value
        draft_fields = {
            "user_title",
            "untitled_confirmed",
            "organization_confirmation",
            "personal_tags",
            "inspiration",
        }
        if not draft_fields.issubset(value):
            return value
        draft_payload = {field: value[field] for field in draft_fields}
        validated = CollectionItemCreateRequest.model_validate(
            {"preview_id": "server-bound", **draft_payload}
        ).model_dump(mode="json")
        validated["organization_confirmation"] = (
            CollectionOrganizationConfirmationUpdate.model_validate(
                validated["organization_confirmation"]
            ).model_dump(mode="json")
        )
        normalized = dict(value)
        validated.pop("preview_id", None)
        # CQ3-S1 is deliberately excluded from the bounded batch-review lane.
        validated.pop("user_author", None)
        validated.pop("user_cover_asset_id", None)
        normalized.update(validated)
        return normalized


class CollectionImportReviewCommand(CollectionImportDraft):
    expected_batch_revision: int = Field(ge=1, strict=True)
    expected_item_revision: int = Field(ge=1, strict=True)
    decision: Literal["save", "skip"]


class CollectionImportRepreviewCommand(StrictModel):
    expected_batch_revision: int = Field(ge=1, strict=True)
    expected_item_revision: int = Field(ge=1, strict=True)
    input_text: str | None = Field(default=None, max_length=10_000)

    @field_validator("input_text", mode="before")
    @classmethod
    def reject_explicit_null_or_blank(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError("replacement input must be a non-empty string")
        return value


class CollectionImportConfirmCommand(StrictModel):
    expected_batch_revision: int = Field(ge=1, strict=True)


class CollectionImportResumeCommand(StrictModel):
    expected_batch_revision: int = Field(ge=1, strict=True)


class CollectionImportCancelCommand(StrictModel):
    expected_batch_revision: int = Field(ge=1, strict=True)


class CollectionImportBatchItemSummary(StrictModel):
    batch_item_id: str
    client_item_id: str
    position: int = Field(ge=0)
    display_label: str = Field(max_length=120)
    state: CollectionImportItemState
    decision: CollectionImportDecision
    item_revision: int = Field(ge=1)
    preview_generation: int = Field(ge=0, le=5)
    duplicate_of_batch_item_id: str | None = None
    collection_item_id: str | None = None
    error_code: str | None = None
    terminal_reason: str | None = None


class CollectionImportBatchSnapshot(StrictModel):
    batch_id: str
    status: CollectionImportBatchStatus
    revision: int = Field(ge=1)
    total: int = Field(ge=2, le=10)
    queued: int = Field(ge=0)
    previewing: int = Field(ge=0)
    ready: int = Field(ge=0)
    needs_review: int = Field(ge=0)
    duplicates: int = Field(ge=0)
    already_exists: int = Field(ge=0)
    failed: int = Field(ge=0)
    selected: int = Field(ge=0)
    created_at: str
    updated_at: str
    terminal_at: str | None = None
    items: list[CollectionImportBatchItemSummary]


class CollectionImportBatchItemDetail(CollectionImportBatchItemSummary):
    batch_id: str
    batch_revision: int = Field(ge=1)
    input_available: bool
    input_text: str | None = None
    preview: CollectionPreview | None = None
    draft: CollectionImportDraft
    error_stage: str | None = None
