from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from backend.app.repositories import collection_search
from backend.app.repositories.sqlite import (
    CollectionExistsError, SQLiteRepository,
)
from backend.app.services.collections import CollectionService
from backend.tests.test_collection_search import collection_graph


def index_graph(number=1, *, title="needle searchable title", inspiration="私有灵感尾词"):
    payload = collection_graph(
        slug=f"index/{number}", title=title, platform="web", author="Public Author",
        source_copy="Public description", primary="参考", secondary="文章",
        platform_tags=["公开标签"], organization_tags=["整理标签"],
        personal_tags=["个人标签"], inspiration=inspiration,
    )
    payload["user_title"] = None
    payload["metadata"]["title"]["value"] = title
    return payload


class CollectionSearchIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "collection.sqlite3"
        self.repo = SQLiteRepository(self.path)
        self.service = CollectionService(self.repo)

    def save(self, number=1, *, title="needle searchable title", inspiration="私有灵感尾词"):
        payload = index_graph(number, title=title, inspiration=inspiration)
        return self.repo.create_collection_item(payload, f"key-{number}", f"hash-{number}")

    def assert_index_integrity(self):
        with self.repo._connect() as db:
            db.execute("""INSERT INTO collection_search_fts(collection_search_fts, rank)
                          VALUES('integrity-check', 1)""")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_save_is_indexed_atomically_and_replay_does_not_duplicate(self):
        stored = self.save()
        self.assertEqual(self.service.search(query="searchable").total, 1)
        replay = self.save()
        self.assertEqual(replay, stored)
        with self.repo._connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(db.execute("SELECT count(*) FROM collection_search_documents").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM collection_search_dirty").fetchone()[0], 0)
        payload = index_graph(title="do not overwrite")
        with self.assertRaises(CollectionExistsError):
            self.repo.create_collection_item(payload, "different-key", "different-hash")
        self.assertEqual(self.service.search(query="searchable").total, 1)
        self.assertEqual(self.service.search(query="overwrite").total, 0)
        self.assert_index_integrity()

    def test_index_write_failure_rolls_back_collection_and_idempotency_key(self):
        with self.repo._connect() as db:
            db.execute("""CREATE TRIGGER fail_index BEFORE INSERT ON collection_search_documents
                          BEGIN SELECT RAISE(ABORT, 'injected index failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.save()
        with self.repo._connect() as db:
            for table in ("library_items", "inspirations", "collection_idempotency_keys",
                          "collection_search_documents", "collection_search_dirty"):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0, table)
            db.execute("DROP TRIGGER fail_index")
        self.save()
        self.assertEqual(self.service.search(query="searchable").total, 1)
        self.assert_index_integrity()

    def test_maintenance_writes_use_read_only_dirty_fallback_then_startup_refreshes(self):
        stored = self.save()
        # Deliberately use a raw connection: older/maintenance writers must not
        # need Python UDFs or silently leave searches stale.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE inspirations SET content=? WHERE collection_item_id=?",
                       ("replacement inspiration", stored["id"]))
            db.execute("UPDATE source_metadata SET title_value=? WHERE collection_item_id=?",
                       ("replacement title", stored["id"]))
        original_connect = self.repo._connect

        @contextmanager
        def query_only():
            with original_connect() as db:
                db.execute("PRAGMA query_only=ON")
                yield db

        with patch.object(self.repo, "_connect", query_only):
            self.assertEqual(self.service.search(query="replacement").total, 1)
            self.assertEqual(self.service.search(query="searchable").total, 0)
            self.assertEqual(self.service.search(query="私有灵感尾词").total, 0)
        with original_connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM collection_search_dirty").fetchone()[0], 1)
            db.execute("PRAGMA user_version=13")  # Simulated old-app downgrade.
        self.repo = SQLiteRepository(self.path)
        self.service = CollectionService(self.repo)
        self.assertEqual(self.service.search(query="replacement").total, 1)
        with self.repo._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM collection_search_dirty").fetchone()[0], 0)
        self.assert_index_integrity()

    def test_all_authoritative_search_sources_mark_dirty(self):
        item = self.save()
        mutations = [
            ("UPDATE library_items SET user_title=? WHERE id=?", "personal override"),
            ("UPDATE organization_confirmations SET primary_category=? WHERE collection_item_id=?", "new category"),
            ("UPDATE organization_confirmations SET organization_tags_json=? WHERE collection_item_id=?", '["new organized tag"]'),
            ("UPDATE source_metadata SET platform_tags_json=? WHERE collection_item_id=?", '[{"value":"new platform tag","source":"platform_public"}]'),
        ]
        for statement, value in mutations:
            with self.subTest(statement=statement):
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute(statement, (value, item["id"]))
                self.assertEqual(self.service.search(query="new" if "new" in value else "override").total, 1)
                self.repo.migrate()
                self.assert_index_integrity()
        with self.repo._connect() as db:
            db.execute("""INSERT INTO collection_personal_tags
                (collection_item_id, normalized_name, display_name, position, created_at)
                VALUES (?, 'new personal tag', 'New Personal Tag', 100, ?)""",
                (item["id"], item["created_at"]))
        self.assertEqual(self.service.search(query="personal tag").total, 1)
        with self.repo._connect() as db:
            db.execute("DELETE FROM collection_personal_tags WHERE normalized_name='new personal tag'")
        self.assertEqual(self.service.search(query="personal tag").total, 0)
        self.repo.migrate()
        self.assert_index_integrity()

    def test_delete_cascades_documents_markers_and_fts(self):
        item = self.save()
        with self.repo._connect() as db:
            db.execute("UPDATE inspirations SET content='changed before deletion'")
            db.execute("DELETE FROM library_items WHERE id=?", (item["id"],))
        self.assertEqual(self.service.search(query="searchable").total, 0)
        self.assertEqual(self.service.search(query="changed").total, 0)
        with self.repo._connect() as db:
            for table in ("collection_search_documents", "collection_search_dirty"):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.assert_index_integrity()

    def test_short_nul_and_boundary_queries_remain_literal(self):
        self.save(title='ＡＢＣ "quote" *star* ?ask? [box] %percent_ 中文短词',
                  inspiration="before\x00after-tail 独立灵感")
        for query in ('abc', '"quote"', '*star*', '?ask?', '[box]', '%percent_',
                      '中', '中文', '中文短', 'after-tail', 'before\x00after', '\x00',
                      '词', '灵感'):
            with self.subTest(query=query):
                self.assertEqual(self.service.search(query=query).total, 1)
        for query in ('abc OR nothing', 'abc*', '词 before', 'quote star', '" OR 1=1 --'):
            self.assertEqual(self.service.search(query=query).total, 0)

    def test_search_uses_two_batch_selects_without_full_graph_hydration(self):
        self.save()
        for number in range(2, 40):
            self.save(number, title=f"other title {number}", inspiration="unrelated")
        original_connect = self.repo._connect
        statements: list[str] = []

        @contextmanager
        def traced():
            with original_connect() as db:
                db.set_trace_callback(statements.append)
                yield db

        with patch.object(self.repo, "_connect", traced), patch.object(
            SQLiteRepository, "_collection_item_on_connection",
            side_effect=AssertionError("list must not hydrate complete graphs"),
        ):
            for query in ("", "searchable", "中"):
                statements.clear()
                self.service.search(query=query)
                # Opening the FTS vtable also emits one internal config SELECT;
                # count the two application statements, not SQLite internals.
                selects = [s for s in statements if s.lstrip().upper().startswith(("SELECT ", "WITH "))
                           and "collection_search_fts_config" not in s]
                self.assertEqual(len(selects), 2, selects)
                self.assertEqual(any("collection_search_fts MATCH" in s for s in selects), len(query) >= 3)
                if not query:
                    self.assertFalse(any("inspiration_content" in s or "m.source_copy_value" in s for s in selects))

    def _make_v13(self):
        path = Path(self.temp.name) / "version13.sqlite3"
        with patch("backend.app.repositories.sqlite.migrate_collection_search"), patch(
            "backend.app.repositories.sqlite.refresh_collection_document"
        ):
            old = SQLiteRepository(path)
            stored = old.create_collection_item(
                index_graph("historical", title="historical indexed title",
                            inspiration="historical private inspiration"), "old-key", "old-hash"
            )
            with old._connect() as db:
                db.execute("PRAGMA user_version=13")
        return path, stored

    def test_v13_migration_backfills_without_changing_authority_or_replay(self):
        path, before = self._make_v13()
        upgraded = SQLiteRepository(path)
        self.assertEqual(upgraded.get_collection_item(before["id"]), before)
        self.assertEqual(upgraded.replay_collection_item("old-key", "old-hash"), before)
        self.assertEqual(CollectionService(upgraded).search(query="historical").total, 1)
        upgraded.migrate()
        with upgraded._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM collection_search_documents").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            db.execute("""INSERT INTO collection_search_fts(collection_search_fts, rank)
                          VALUES('integrity-check', 1)""")

    def test_failed_v14_backfill_rolls_back_schema_and_version_then_retries(self):
        path, before = self._make_v13()
        original_store = collection_search._store_documents

        def fail_after_indexing(db, items):
            original_store(db, items)
            raise sqlite3.IntegrityError("injected migration failure")

        with patch.object(collection_search, "_store_documents", fail_after_indexing):
            with self.assertRaises(sqlite3.IntegrityError):
                SQLiteRepository(path)
        with closing(sqlite3.connect(path)) as db, db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 13)
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'collection_search_%'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM library_items").fetchone()[0], 1)
        upgraded = SQLiteRepository(path)
        self.assertEqual(upgraded.get_collection_item(before["id"]), before)
        self.assertEqual(CollectionService(upgraded).search(query="historical").total, 1)

    def test_page_total_and_facets_share_one_snapshot_during_a_write(self):
        item = self.save()
        with self.repo._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
        before = self.service.search().model_dump()
        original_connect = self.repo._connect
        changed = False

        def write_before_tags(statement):
            nonlocal changed
            if "SELECT t.collection_item_id" not in statement or changed:
                return
            changed = True
            with closing(sqlite3.connect(self.path)) as writer, writer:
                writer.execute("UPDATE organization_confirmations SET primary_category='changed category'")
                writer.execute("""INSERT INTO collection_personal_tags
                    (collection_item_id, normalized_name, display_name, position, created_at)
                    VALUES (?, 'new concurrent tag', 'New Concurrent Tag', 99, ?)""",
                    (item["id"], item["created_at"]))

        @contextmanager
        def racing_read():
            with original_connect() as db:
                db.set_trace_callback(write_before_tags)
                yield db

        with patch.object(self.repo, "_connect", racing_read):
            during = self.service.search().model_dump()
        self.assertTrue(changed)
        self.assertEqual(during, before)
        after = self.service.search().model_dump()
        self.assertNotEqual(after["facets"], before["facets"])


if __name__ == "__main__":
    unittest.main()
