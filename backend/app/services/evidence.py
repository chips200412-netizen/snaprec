from __future__ import annotations

from ..domain.models import Evidence, ExtractionItem, FocusedAnswer, FullExtraction, Segment


class EvidenceValidationError(ValueError):
    pass


def validate_evidence(
    extraction: FullExtraction, evidence: list[Evidence], segments: list[Segment]
) -> None:
    segment_by_id = {segment.id: segment for segment in segments}
    evidence_by_id = {item.id: item for item in evidence}
    if len(evidence_by_id) != len(evidence):
        raise EvidenceValidationError("evidence ids must be unique")

    for item in evidence:
        if not 0 <= item.confidence <= 1:
            raise EvidenceValidationError("evidence confidence must be between 0 and 1")
        if not item.segment_ids:
            raise EvidenceValidationError("evidence must reference at least one segment")
        referenced = []
        for segment_id in item.segment_ids:
            if segment_id not in segment_by_id:
                raise EvidenceValidationError(f"unknown segment id: {segment_id}")
            referenced.append(segment_by_id[segment_id])
        if not any(item.evidence in segment.text for segment in referenced):
            raise EvidenceValidationError("evidence text is not present in raw segment")
        starts = [s.start for s in referenced if s.start is not None]
        ends = [s.end for s in referenced if s.end is not None]
        expected_start = min(starts) if starts else None
        expected_end = max(ends) if ends else None
        if item.start_time != expected_start or item.end_time != expected_end:
            raise EvidenceValidationError("evidence timestamp does not match its segments")

    for item in extraction.all_items():
        if not 0 <= item.confidence <= 1:
            raise EvidenceValidationError("item confidence must be between 0 and 1")
        if not item.evidence_refs:
            raise EvidenceValidationError("every conclusion requires evidence")
        if any(ref not in evidence_by_id for ref in item.evidence_refs):
            raise EvidenceValidationError("conclusion references unknown evidence")
        if any(evidence_by_id[ref].claim_type != item.claim_type for ref in item.evidence_refs):
            raise EvidenceValidationError("conclusion and evidence claim_type must match")


def validate_focused_evidence(
    answer: FocusedAnswer, segments: list[Segment]
) -> None:
    class _FocusedItems:
        def all_items(self) -> list[ExtractionItem]:
            return answer.all_items()

    validate_evidence(_FocusedItems(), answer.supporting_segments, segments)  # type: ignore[arg-type]
    if answer.mention_status in {"not_mentioned", "unknown_incomplete_transcript"}:
        if answer.key_points or answer.supporting_segments:
            raise EvidenceValidationError(
                "unanswered focused results must not contain conclusions or evidence"
            )
