"""Frozen R1.5 scan from 61e55ca, for synthetic benchmarks only.

Not imported by production. Keeping the original method makes before/after
measurements include the same historical N+1 hydration and facet algorithm.
"""
from backend.app.repositories.sqlite import SQLiteRepository, normalize_search_text


class LegacyScanRepository(SQLiteRepository):
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
    ) -> dict:
        normalized_query = normalize_search_text(query)
        normalized_primary = normalize_search_text(primary_category)
        normalized_secondary = normalize_search_text(secondary_category)
        normalized_tag = normalize_search_text(tag)

        with self._connect() as db:
            # Keep the page, total, and self-excluding facets on one SQLite
            # snapshot so a concurrent save cannot make the envelope disagree.
            db.execute("BEGIN")
            rows = db.execute(
                "SELECT id FROM library_items ORDER BY updated_at DESC, id ASC"
            ).fetchall()
            graphs = [
                graph
                for row in rows
                if (graph := self._collection_item_on_connection(db, row["id"]))
                is not None
            ]

            def tag_groups(item: dict) -> dict[str, list[str]]:
                return {
                    "platform": [
                        entry["value"] for entry in item["metadata"]["platform_tags"]
                    ],
                    "organization": item["organization_confirmation"][
                        "organization_tags"
                    ],
                    "personal": item["personal_tags"],
                }

            def matches_keyword(item: dict) -> bool:
                if not normalized_query:
                    return True
                confirmation = item["organization_confirmation"]
                groups = tag_groups(item)
                searchable = [
                    item["display_title"],
                    item["metadata"]["title"]["value"],
                    item["metadata"]["author"]["value"],
                    item["metadata"]["source_copy"]["value"],
                    item["source_url"],
                    item["canonical_url"],
                    confirmation["primary_category"],
                    confirmation["secondary_category"],
                    *groups["platform"],
                    *groups["organization"],
                    *groups["personal"],
                    (item["inspiration"] or {}).get("content", ""),
                ]
                return any(
                    normalized_query in normalize_search_text(value)
                    for value in searchable
                )

            def matches_filters(
                item: dict,
                *,
                exclude: str | None = None,
            ) -> bool:
                confirmation = item["organization_confirmation"]
                if exclude != "platform" and platform and item["platform"] != platform:
                    return False
                if exclude != "category":
                    if normalized_primary and normalize_search_text(
                        confirmation["primary_category"]
                    ) != normalized_primary:
                        return False
                    if normalized_secondary and normalize_search_text(
                        confirmation["secondary_category"]
                    ) != normalized_secondary:
                        return False
                if exclude != "tag" and normalized_tag:
                    values = tag_groups(item).get(tag_source or "", [])
                    if normalized_tag not in {
                        normalize_search_text(value) for value in values
                    }:
                        return False
                return True

            keyword_matches = [item for item in graphs if matches_keyword(item)]
            matched = [item for item in keyword_matches if matches_filters(item)]
            total = len(matched)

            if after is not None:
                updated_at, item_id = after
                matched = [
                    item
                    for item in matched
                    if item["updated_at"] < updated_at
                    or (item["updated_at"] == updated_at and item["id"] > item_id)
                ]
            page_graphs = matched[: limit + 1]
            has_more = len(page_graphs) > limit
            page_graphs = page_graphs[:limit]

            def compact(item: dict) -> dict:
                return {
                    "id": item["id"],
                    "display_title": item["display_title"],
                    "platform": item["platform"],
                    "primary_category": item["organization_confirmation"][
                        "primary_category"
                    ],
                    "cover_url": item["metadata"]["cover_url"]["value"],
                    "created_at": item["created_at"],
                    "updated_at": item["updated_at"],
                }

            platform_counts: dict[str, int] = {}
            category_counts: dict[str, int] = {}
            child_counts: dict[str, dict[str, int]] = {}
            tag_counts: dict[tuple[str, str], int] = {}
            tag_displays: dict[tuple[str, str], str] = {}

            for item in keyword_matches:
                if matches_filters(item, exclude="platform"):
                    value = item["platform"]
                    platform_counts[value] = platform_counts.get(value, 0) + 1
                if matches_filters(item, exclude="category"):
                    confirmation = item["organization_confirmation"]
                    primary_value = confirmation["primary_category"].strip()
                    secondary_value = confirmation["secondary_category"].strip()
                    if primary_value:
                        category_counts[primary_value] = category_counts.get(primary_value, 0) + 1
                        if secondary_value:
                            children = child_counts.setdefault(primary_value, {})
                            children[secondary_value] = children.get(secondary_value, 0) + 1
                if matches_filters(item, exclude="tag"):
                    for source, values in tag_groups(item).items():
                        for value in values:
                            normalized = normalize_search_text(value)
                            key = (source, normalized)
                            tag_counts[key] = tag_counts.get(key, 0) + 1
                            tag_displays.setdefault(key, value)

            platforms = [
                {"platform": value, "count": count}
                for value, count in sorted(
                    platform_counts.items(), key=lambda entry: (-entry[1], entry[0])
                )
            ]
            categories = [
                {
                    "primary_category": value,
                    "count": count,
                    "children": [
                        {"secondary_category": child, "count": child_count}
                        for child, child_count in sorted(
                            child_counts.get(value, {}).items(),
                            key=lambda entry: (-entry[1], normalize_search_text(entry[0])),
                        )
                    ],
                }
                for value, count in sorted(
                    category_counts.items(),
                    key=lambda entry: (-entry[1], normalize_search_text(entry[0])),
                )
            ]
            tags = [
                {"name": tag_displays[key], "source": key[0], "count": count}
                for key, count in sorted(
                    tag_counts.items(),
                    key=lambda entry: (-entry[1], entry[0][0], entry[0][1]),
                )
            ]
            return {
                "items": [compact(item) for item in page_graphs],
                "total": total,
                "has_more": has_more,
                "facets": {
                    "platforms": platforms,
                    "categories": categories,
                    "tags": tags,
                },
            }
