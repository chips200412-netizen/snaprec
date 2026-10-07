import type { CollectionItem, SourceMetadata } from "./collection-api";
import { editableTagIdentity } from "./collection-edit-model";

export type SourceTopic = SourceMetadata["platform_tags"][number];

/** Identity only: display and persisted evidence retain the original value. */
export function sourceTopicCandidates(topics: SourceTopic[]) {
  const seen = new Set<string>();
  return topics.flatMap((topic, index) => {
    const identity = editableTagIdentity(topic.value);
    if (seen.has(identity)) return [];
    seen.add(identity);
    return [{ ...topic, index, identity }];
  });
}

export function reconcileSourceTopics(previous: SourceTopic[], indices: number[], next: SourceTopic[]): number[] {
  const selected = new Set(indices.flatMap((index) => previous[index] ? [editableTagIdentity(previous[index].value)] : []));
  return sourceTopicCandidates(next).filter((topic) => selected.has(topic.identity)).map((topic) => topic.index);
}

export function withoutSourceTopicSuggestions(tags: string[], topics: SourceTopic[]): string[] {
  const identities = new Set(topics.map((topic) => editableTagIdentity(topic.value)));
  return tags.filter((tag) => !identities.has(editableTagIdentity(tag)));
}

export function displayedSourceTopics(item: CollectionItem): SourceTopic[] {
  return item.selected_source_topic_indices == null
    ? item.metadata.platform_tags
    : item.selected_source_topic_indices.flatMap((index) => item.metadata.platform_tags[index] ? [item.metadata.platform_tags[index]] : []);
}
