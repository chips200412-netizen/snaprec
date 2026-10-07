"""Derived FTS candidates; authoritative, field-local literal search remains final.

No network, query logging, or writes are allowed on the search path. Dirty
markers also cover writes by an older application that does not refresh FTS.
"""
from __future__ import annotations

import json
import sqlite3
import unicodedata


def normalize_search_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().split()).casefold()


def _tag_groups(item: dict) -> dict[str, list[str]]:
    return {
        "platform": [entry["value"] for entry in item["metadata"]["platform_tags"]],
        "organization": item["organization_confirmation"]["organization_tags"],
        "personal": item["personal_tags"],
    }


def _search_fields(item: dict) -> list[str]:
    confirmation = item["organization_confirmation"]
    groups = _tag_groups(item)
    return [
        item["display_title"],
        item["metadata"]["title"]["value"],
        item["metadata"]["author"]["value"],
        item.get("user_author") or "",
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


def _load_candidates(
    db: sqlite3.Connection,
    *,
    cte: str = "",
    parameters: tuple = (),
    include_search: bool,
) -> list[dict]:
    # Both SELECTs are bounded in number, not one query per material. The CTE
    # contains only code-owned SQL; all search text and IDs are parameters.
    candidate_join = "JOIN candidates k ON k.id=i.id" if cte else ""
    search_columns = (
        ", m.source_copy_value, n.content AS inspiration_content"
        if include_search else ""
    )
    inspiration_join = (
        "LEFT JOIN inspirations n ON n.collection_item_id=i.id"
        if include_search else ""
    )
    rows = db.execute(
        f"""{cte}
        SELECT i.id, i.platform, i.source_url, i.canonical_url, i.user_title,
               i.user_author,
               i.created_at, i.updated_at, m.title_value, m.title_source,
               m.author_value,
               m.cover_url_value, m.platform_tags_json,
               EXISTS(SELECT 1 FROM collection_user_covers u
                      WHERE u.collection_item_id=i.id) AS has_user_cover,
               c.primary_category, c.secondary_category, c.organization_tags_json
               {search_columns}
        FROM library_items i {candidate_join}
        JOIN source_metadata m ON m.collection_item_id=i.id
        JOIN organization_confirmations c ON c.collection_item_id=i.id
        {inspiration_join}
        ORDER BY i.updated_at DESC, i.id ASC""",
        parameters,
    ).fetchall()
    items: dict[str, dict] = {}
    for row in rows:
        source_title = row["title_value"].strip() if row["title_source"] != "none" else ""
        items[row["id"]] = {
            "id": row["id"],
            "platform": row["platform"],
            "display_title": (row["user_title"] or "").strip() or source_title or "未命名收藏",
            "user_author": row["user_author"],
            "has_user_cover": bool(row["has_user_cover"]),
            "source_url": row["source_url"],
            "canonical_url": row["canonical_url"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "metadata": {
                "title": {"value": row["title_value"]},
                "author": {"value": row["author_value"]},
                "source_copy": {"value": row["source_copy_value"] if include_search else ""},
                "cover_url": {"value": row["cover_url_value"]},
                "platform_tags": json.loads(row["platform_tags_json"]),
            },
            "organization_confirmation": {
                "primary_category": row["primary_category"],
                "secondary_category": row["secondary_category"],
                "organization_tags": json.loads(row["organization_tags_json"]),
            },
            "personal_tags": [],
            "inspiration": {"content": row["inspiration_content"] or ""} if include_search else None,
        }
    tags = db.execute(
        f"""{cte}
        SELECT t.collection_item_id, t.display_name
        FROM library_items i {candidate_join}
        JOIN collection_personal_tags t ON t.collection_item_id=i.id
        ORDER BY i.updated_at DESC, i.id ASC, t.position ASC""",
        parameters,
    )
    for row in tags:
        if row["collection_item_id"] in items:
            items[row["collection_item_id"]]["personal_tags"].append(row["display_name"])
    return list(items.values())


def _store_documents(db: sqlite3.Connection, items: list[dict]) -> None:
    for item in items:
        # The separator can create false positives, never false negatives;
        # field-local authoritative rechecking removes those false positives.
        # FTS tokenizers stop at NUL. Replacing it keeps the suffix searchable;
        # queries containing NUL deliberately bypass FTS altogether.
        text = "\x1f".join(normalize_search_text(value) for value in _search_fields(item))
        db.execute(
            """INSERT INTO collection_search_documents(collection_item_id, indexed_text)
               VALUES (?, ?) ON CONFLICT(collection_item_id)
               DO UPDATE SET indexed_text=excluded.indexed_text""",
            (item["id"], text.replace("\x00", "\ufffd")),
        )
        db.execute(
            "DELETE FROM collection_search_dirty WHERE collection_item_id=?", (item["id"],)
        )


def refresh_collection_document(db: sqlite3.Connection, item_id: str) -> None:
    """Called by the write owner, within the collection's existing transaction."""
    _store_documents(db, _load_candidates(
        db, cte="WITH candidates(id) AS (VALUES (?))", parameters=(item_id,), include_search=True
    ))


def migrate_collection_search(db: sqlite3.Connection) -> None:
    """v14 expand/backfill. The caller owns the explicit migration transaction."""
    db.execute("""CREATE TABLE IF NOT EXISTS collection_search_documents (
        document_id INTEGER PRIMARY KEY,
        collection_item_id TEXT NOT NULL UNIQUE,
        indexed_text TEXT NOT NULL,
        FOREIGN KEY(collection_item_id) REFERENCES library_items(id) ON DELETE CASCADE
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS collection_search_dirty (
        collection_item_id TEXT PRIMARY KEY,
        FOREIGN KEY(collection_item_id) REFERENCES library_items(id) ON DELETE CASCADE
    )""")
    db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS collection_search_fts USING fts5(
        indexed_text, content='collection_search_documents', content_rowid='document_id',
        tokenize='trigram case_sensitive 1'
    )""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS collection_search_documents_ai
        AFTER INSERT ON collection_search_documents BEGIN
        INSERT INTO collection_search_fts(rowid, indexed_text)
            VALUES(new.document_id, new.indexed_text);
    END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS collection_search_documents_ad
        AFTER DELETE ON collection_search_documents BEGIN
        INSERT INTO collection_search_fts(collection_search_fts, rowid, indexed_text)
            VALUES('delete', old.document_id, old.indexed_text);
    END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS collection_search_documents_au
        AFTER UPDATE ON collection_search_documents BEGIN
        INSERT INTO collection_search_fts(collection_search_fts, rowid, indexed_text)
            VALUES('delete', old.document_id, old.indexed_text);
        INSERT INTO collection_search_fts(rowid, indexed_text)
            VALUES(new.document_id, new.indexed_text);
    END""")
    for table in (
        "library_items", "source_metadata", "organization_confirmations",
        "collection_personal_tags", "inspirations",
    ):
        column = "id" if table == "library_items" else "collection_item_id"
        for event, references in (("INSERT", ("new",)), ("UPDATE", ("old", "new")), ("DELETE", ("old",))):
            if table == "library_items" and event == "DELETE":
                continue  # FK cascades remove both the document and dirty marker.
            trigger_event = event
            if table == "library_items" and event == "UPDATE":
                trigger_event = "UPDATE OF user_title, user_author, source_url, canonical_url"
            body = "\n".join(
                f"""INSERT OR IGNORE INTO collection_search_dirty(collection_item_id)
                    SELECT {ref}.{column} WHERE EXISTS(
                        SELECT 1 FROM library_items WHERE id={ref}.{column});"""
                for ref in references
            )
            trigger_name = f"collection_search_dirty_{table}_{event.lower()}"
            if table == "library_items" and event == "UPDATE":
                db.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS {trigger_name}
                AFTER {trigger_event} ON {table} BEGIN {body} END""")

    # Keyset batches cap memory during initial backfill, also refreshing dirty
    # rows from maintenance or older-app writes on subsequent startups.
    after_id = ""
    while True:
        pending = db.execute(
            """SELECT i.id FROM library_items i
               LEFT JOIN collection_search_documents d ON d.collection_item_id=i.id
               LEFT JOIN collection_search_dirty p ON p.collection_item_id=i.id
               WHERE i.id>? AND (d.document_id IS NULL OR p.collection_item_id IS NOT NULL)
               ORDER BY i.id LIMIT 256""", (after_id,)
        ).fetchall()
        if not pending:
            break
        ids = tuple(row["id"] for row in pending)
        cte = "WITH candidates(id) AS (VALUES " + ",".join("(?)" for _ in ids) + ")"
        _store_documents(db, _load_candidates(db, cte=cte, parameters=ids, include_search=True))
        after_id = ids[-1]


def search_collection_items(
    db: sqlite3.Connection, *, query: str, platform: str | None,
    primary_category: str, secondary_category: str, tag: str, tag_source: str | None,
    limit: int, after: tuple[str, str] | None,
) -> dict:
    normalized_query = normalize_search_text(query)
    normalized_primary = normalize_search_text(primary_category)
    normalized_secondary = normalize_search_text(secondary_category)
    normalized_tag = normalize_search_text(tag)
    cte, parameters = "", ()
    if len(normalized_query) >= 3 and "\x00" not in normalized_query:
        # A bound quoted phrase is literal even for quotes, operators or '*'.
        phrase = '"' + normalized_query.replace('"', '""') + '"'
        cte = """WITH candidates(id) AS (
            SELECT d.collection_item_id FROM collection_search_fts
            JOIN collection_search_documents d ON d.document_id=collection_search_fts.rowid
            WHERE collection_search_fts MATCH ?
            UNION SELECT collection_item_id FROM collection_search_dirty
        )"""
        parameters = (phrase,)
    graphs = _load_candidates(db, cte=cte, parameters=parameters, include_search=bool(normalized_query))
    keyword_matches = [
        item for item in graphs
        if not normalized_query or any(
            normalized_query in normalize_search_text(value) for value in _search_fields(item)
        )
    ]

    def matches_filters(item: dict, *, exclude: str | None = None) -> bool:
        confirmation = item["organization_confirmation"]
        if exclude != "platform" and platform and item["platform"] != platform:
            return False
        if exclude != "category":
            if normalized_primary and normalize_search_text(confirmation["primary_category"]) != normalized_primary:
                return False
            if normalized_secondary and normalize_search_text(confirmation["secondary_category"]) != normalized_secondary:
                return False
        if exclude != "tag" and normalized_tag:
            if normalized_tag not in {
                normalize_search_text(value) for value in _tag_groups(item).get(tag_source or "", [])
            }:
                return False
        return True

    matched = [item for item in keyword_matches if matches_filters(item)]
    total = len(matched)
    if after is not None:
        updated_at, item_id = after
        matched = [item for item in matched if item["updated_at"] < updated_at or (
            item["updated_at"] == updated_at and item["id"] > item_id
        )]
    page = matched[:limit]

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
            for source, values in _tag_groups(item).items():
                for value in values:
                    key = (source, normalize_search_text(value))
                    tag_counts[key] = tag_counts.get(key, 0) + 1
                    tag_displays.setdefault(key, value)
    return {
        "items": [{
            "id": item["id"], "display_title": item["display_title"],
            "platform": item["platform"],
            "primary_category": item["organization_confirmation"]["primary_category"],
            "source_author": item["metadata"]["author"]["value"],
            "user_author": item.get("user_author"),
            "has_user_cover": bool(item.get("has_user_cover")),
            "cover_url": item["metadata"]["cover_url"]["value"],
            "created_at": item["created_at"], "updated_at": item["updated_at"],
        } for item in page],
        "total": total,
        "has_more": len(matched) > limit,
        "facets": {
            "platforms": [
                {"platform": value, "count": count} for value, count in sorted(
                    platform_counts.items(), key=lambda entry: (-entry[1], entry[0])
                )
            ],
            "categories": [{
                "primary_category": value, "count": count,
                "children": [
                    {"secondary_category": child, "count": child_count}
                    for child, child_count in sorted(
                        child_counts.get(value, {}).items(),
                        key=lambda entry: (-entry[1], normalize_search_text(entry[0])),
                    )
                ],
            } for value, count in sorted(
                category_counts.items(), key=lambda entry: (-entry[1], normalize_search_text(entry[0])),
            )],
            "tags": [
                {"name": tag_displays[key], "source": key[0], "count": count}
                for key, count in sorted(tag_counts.items(), key=lambda entry: (-entry[1], entry[0][0], entry[0][1]))
            ],
        },
    }
