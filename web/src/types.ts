export type ParseStatus =
  | "complete"
  | "warning"
  | "processing"
  | "missing"
  | "failed";
export type ReadingMode = "overview" | "steps" | "transcript";
export type TagSource = "platform" | "automatic" | "personal";
export type AutomaticTagStatus =
  | "generated"
  | "skipped_no_transcript"
  | "failed"
  | "not_generated";

export interface AutomaticTag {
  name: string;
  confidence: number;
  generationMethod: "llm" | "deterministic";
}

export interface AutomaticTagging {
  status: AutomaticTagStatus;
  tags: AutomaticTag[];
  generatorId: string;
  generatorVersion: string;
  transcriptHash: string;
  generatedAt: string;
  warning: string;
}

export interface TagFacet {
  name: string;
  source: TagSource;
  count: number;
}

export interface SecondaryCategoryFacet {
  secondaryCategory: string;
  count: number;
}

export interface CategoryFacet {
  primaryCategory: string;
  count: number;
  children: SecondaryCategoryFacet[];
}

export interface VideoFacets {
  tags: TagFacet[];
  categories: CategoryFacet[];
}

export interface Evidence {
  id: string;
  start: number | null;
  end: number | null;
  text: string;
  segmentIds?: string[];
}

export interface VideoQuestion {
  id: string;
  question: string;
  questionHash: string;
  mentionStatus:
    | "explicit"
    | "inferred"
    | "not_mentioned"
    | "unknown_incomplete_transcript";
  directAnswer: string;
  evidence: Evidence[];
  missingInformation: string[];
  createdAt: string;
}

export interface KnowledgePoint {
  id: string;
  title: string;
  body: string;
  evidenceIds: string[];
  kind: "point" | "step" | "risk";
  sectionId?: string;
  annotationTargetKey?: string;
}

export interface OutlineItem {
  id: string;
  title: string;
  start: number | null;
  end: number | null;
}

export interface VideoNote {
  id: string;
  pointId: string;
  targetKey: string;
  text: string;
  targetType: "claim" | "step";
  author: string;
  createdAt: string;
  updatedAt: string;
}

export interface PlaybackCapability {
  availability: "available" | "unavailable";
  kind: "retained_local" | "none";
  streamUrl: string;
  mimeType: string;
  sizeBytes: number;
  supportsRange: boolean;
  reason:
    | "available"
    | "not_retained"
    | "unsupported_source"
    | "unsupported_browser_format"
    | "missing_file";
}

export interface VideoRecord {
  id: string;
  platform: "bilibili" | "douyin" | "local";
  sourceUrl: string | null;
  mediaType: "视频" | "音频";
  title: string;
  author: string;
  duration: number;
  status: ParseStatus;
  statusText: string;
  summary: string;
  tags: string[];
  userTags: string[];
  automaticTags?: AutomaticTag[];
  automaticTagging?: AutomaticTagging;
  primaryCategory: string;
  secondaryCategory: string | null;
  classificationUpdatedAt?: string;
  sparkNote: string | null;
  sparkCreatedAt: string | null;
  sparkId?: string | null;
  sparkAuthor?: string | null;
  sparkUpdatedAt?: string | null;
  updatedAt: string;
  integrity: string;
  subtitleSource: string;
  warnings?: string[];
  coverUrl?: string | null;
  focusQuery?: string;
  focusQueryHash?: string;
  missingInformation?: string[];
  outline: OutlineItem[];
  points: KnowledgePoint[];
  evidence: Evidence[];
  transcript?: Evidence[];
  notes: VideoNote[];
  questions: VideoQuestion[];
  playback?: PlaybackCapability;
  favorite?: boolean;
  detailLoaded?: boolean;
}
