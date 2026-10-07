from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from backend.app.api.main import create_app
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.providers import DeterministicFullExtractor, UnconfiguredAsrProvider
from backend.tests.test_collections import trusted_collection_payload


def encoded_image(
    fmt: str = "PNG",
    size: tuple[int, int] = (20, 10),
    *,
    animated: bool = False,
) -> bytes:
    output = BytesIO()
    image = Image.new("RGB", size, (32, 96, 160))
    if animated:
        image.save(
            output,
            format=fmt,
            save_all=True,
            append_images=[Image.new("RGB", size, (160, 96, 32))],
            duration=10,
        )
    else:
        exif = Image.Exif()
        exif[270] = "private metadata"
        image.save(output, format=fmt, exif=exif)
    return output.getvalue()


class UserCoverAssetApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = SQLiteRepository(self.root / "cq3.sqlite3")
        trusted = trusted_collection_payload()
        trusted["metadata"]["cover_url"] = {"value": "", "source": "none", "fetched_at": ""}
        self.repo.create_collection_preview({
            "preview_id": "cq3-preview",
            "original_input": trusted["original_input"],
            "source_url": trusted["source_url"],
            "canonical_url": trusted["canonical_url"],
            "identity_url": "https://example.com/watch?a=1",
            "source_kind": trusted["source_kind"],
            "platform": trusted["platform"],
            "metadata_status": trusted["metadata_status"],
            "metadata": trusted["metadata"],
            "organization_suggestion": trusted["organization_suggestion"],
            "created_at": "2026-09-10T00:00:00+00:00",
            "expires_at": "2099-09-10T00:00:00+00:00",
        })
        self.preview = self.repo.get_collection_preview("cq3-preview")
        self.pipeline = LocalFullPipeline(
            self.repo, UnconfiguredAsrProvider(), DeterministicFullExtractor()
        )
        self.client = TestClient(create_app(
            self.pipeline,
            upload_root=self.root / "uploads",
            user_cover_root=self.root / "user-covers",
        ))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def upload(self, body: bytes, media_type: str = "image/png") -> dict:
        response = self.client.post(
            "/api/v1/user-cover-assets",
            files={"file": ("private-name.png", body, media_type)},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def create_payload(self, asset_id: str | None = None) -> dict:
        payload = {
            "preview_id": "cq3-preview",
            "user_title": "用户标题",
            "user_author": "我的作者",
            "organization_confirmation": {
                "primary_category": "知识",
                "secondary_category": "文章",
                "organization_tags": [],
            },
            "personal_tags": [],
            "inspiration": None,
        }
        if asset_id is not None:
            payload["user_cover_asset_id"] = asset_id
        return payload

    def test_upload_sanitizes_resizes_and_only_claim_token_reads_unbound_asset(self):
        """INT-ACC-CQ3-002/006/008; CQ3-ASSET-001."""
        asset = self.upload(encoded_image(size=(3000, 1000)))
        self.assertEqual((asset["width"], asset["height"]), (2048, 683))
        self.assertEqual(asset["media_type"], "image/webp")
        self.assertGreater(asset["size_bytes"], 0)
        self.assertNotIn("private-name", str(asset))
        bad = self.client.get(
            f"/api/v1/user-cover-assets/{asset['asset_id']}/content",
            headers={"X-User-Cover-Claim-Token": "wrong"},
        )
        self.assertEqual(bad.status_code, 404)
        good = self.client.get(
            f"/api/v1/user-cover-assets/{asset['asset_id']}/content",
            headers={"X-User-Cover-Claim-Token": asset["claim_token"]},
        )
        self.assertEqual(good.status_code, 200, good.text)
        self.assertEqual(good.headers["content-type"], "image/webp")
        with Image.open(BytesIO(good.content)) as image:
            self.assertEqual(image.size, (2048, 683))
            self.assertFalse(image.getexif())

    def test_user_only_cover_never_returns_source_as_a_user_image(self):
        """CQ4-UI-001: an explicit user-image preview must not silently fall back."""
        asset = self.upload(encoded_image())
        created = self.client.post(
            "/api/v1/collection-items", json=self.create_payload(asset["asset_id"]),
            headers={"Idempotency-Key": "user-only", "X-User-Cover-Claim-Token": asset["claim_token"]},
        )
        self.assertEqual(created.status_code, 201, created.text)
        url = f"/api/v1/collection-items/{created.json()['id']}/cover"
        original = self.client.get(url)
        strict = self.client.get(url + "?user=only")
        self.assertEqual(strict.status_code, 200)
        self.assertEqual(strict.content, original.content)
        self.assertEqual(self.client.get(url + "?user=only", headers={"If-None-Match": strict.headers["etag"]}).status_code, 304)
        from backend.app.services.cover_cache import CoverAsset, CoverUnavailable
        source = CoverAsset(encoded_image(), "image/png", "f" * 64, 20, 10, "HIT")
        graph = {"metadata": {"cover_url": {"value": "https://example.com/source.png"}}}
        with patch.object(self.client.app.state.user_cover_service, "get_for_item", side_effect=CoverUnavailable()), \
             patch.object(self.repo, "get_collection_item", return_value=graph), \
             patch.object(self.client.app.state.cover_cache_service, "get", return_value=source) as source_get:
            self.assertEqual(self.client.get(url).content, source.body)
            source_get.reset_mock()
            self.assertEqual(self.client.get(url + "?user=only").status_code, 404)
            source_get.assert_not_called()
        for query in ("?user=only&source=only", "?user=only&user=only", "?user=wrong"):
            self.assertEqual(self.client.get(url + query).status_code, 404)

    def test_bind_is_atomic_item_cover_prefers_user_asset_and_clear_falls_back(self):
        """INT-ACC-CQ3-001/007; CQ3-ASSET-001/CQ3-CONSISTENCY-001."""
        asset = self.upload(encoded_image())
        created = self.client.post(
            "/api/v1/collection-items",
            headers={
                "Idempotency-Key": "cq3-cover-create",
                "X-User-Cover-Claim-Token": asset["claim_token"],
            },
            json=self.create_payload(asset["asset_id"]),
        )
        self.assertEqual(created.status_code, 201, created.text)
        item = created.json()
        self.assertEqual(item["user_cover_asset_id"], asset["asset_id"])
        self.assertEqual(item["metadata"]["cover_url"]["source"], "none")
        cover = self.client.get(f"/api/v1/collection-items/{item['id']}/cover?cache=only")
        self.assertEqual(cover.status_code, 200, cover.text)
        self.assertEqual(cover.content, self.client.get(
            f"/api/v1/user-cover-assets/{asset['asset_id']}/content",
            headers={"X-User-Cover-Claim-Token": asset["claim_token"]},
        ).content)

        cleared = self.client.patch(
            f"/api/v1/collection-items/{item['id']}",
            json={
                "expected_revision": item["revision"],
                "user_title": item["user_title"],
                "user_author": item["user_author"],
                "user_cover_asset_id": None,
                "organization_confirmation": item["organization_confirmation"],
                "personal_tags": item["personal_tags"],
                "inspiration": None,
            },
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertIsNone(cleared.json()["user_cover_asset_id"])
        self.assertEqual(
            self.client.get(f"/api/v1/collection-items/{item['id']}/cover").status_code,
            404,
        )

    def test_rejects_forged_animated_multi_and_unclaimed_reference_without_writes(self):
        """INT-ACC-CQ3-006; CQ3-ASSET-001/CQ3-CONSISTENCY-001."""
        forged = self.client.post(
            "/api/v1/user-cover-assets",
            files={"file": ("fake.png", encoded_image("JPEG"), "image/png")},
        )
        self.assertEqual(forged.status_code, 415, forged.text)
        animated = self.client.post(
            "/api/v1/user-cover-assets",
            files={"file": ("animated.webp", encoded_image("WEBP", animated=True), "image/webp")},
        )
        self.assertEqual(animated.status_code, 415, animated.text)
        multi = self.client.post(
            "/api/v1/user-cover-assets",
            files=[
                ("file", ("one.png", encoded_image(), "image/png")),
                ("file", ("two.png", encoded_image(), "image/png")),
            ],
        )
        self.assertEqual(multi.status_code, 400, multi.text)

        asset = self.upload(encoded_image())
        unclaimed = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "cq3-unclaimed"},
            json=self.create_payload(asset["asset_id"]),
        )
        self.assertEqual(unclaimed.status_code, 400, unclaimed.text)
        with closing(sqlite3.connect(self.root / "cq3.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM library_items").fetchone()[0], 0)

    def test_explicit_delete_only_removes_claimed_unbound_asset(self):
        """INT-ACC-CQ3-002; CQ3-ASSET-001."""
        asset = self.upload(encoded_image())
        wrong = self.client.delete(
            f"/api/v1/user-cover-assets/{asset['asset_id']}",
            headers={"X-User-Cover-Claim-Token": "wrong"},
        )
        self.assertEqual(wrong.status_code, 404)
        deleted = self.client.delete(
            f"/api/v1/user-cover-assets/{asset['asset_id']}",
            headers={"X-User-Cover-Claim-Token": asset["claim_token"]},
        )
        self.assertEqual(deleted.status_code, 204, deleted.text)
        restored = self.client.get(
            f"/api/v1/user-cover-assets/{asset['asset_id']}/content",
            headers={"X-User-Cover-Claim-Token": asset["claim_token"]},
        )
        self.assertEqual(restored.status_code, 404)

    def test_shared_asset_survives_one_reference_clear_and_expired_gc(self):
        """INT-ACC-CQ3-002; CQ3-ASSET-001 reference-aware cleanup."""
        second = dict(self.preview)
        second.update({
            "preview_id": "cq3-preview-second",
            "source_url": "https://example.com/second",
            "canonical_url": "https://example.com/second",
            "identity_url": "https://example.com/second",
        })
        self.repo.create_collection_preview(second)
        asset = self.upload(encoded_image())
        first = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "shared-first", "X-User-Cover-Claim-Token": asset["claim_token"]},
            json=self.create_payload(asset["asset_id"]),
        ).json()
        second_payload = self.create_payload(asset["asset_id"])
        second_payload["preview_id"] = "cq3-preview-second"
        second_item_response = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "shared-second", "X-User-Cover-Claim-Token": asset["claim_token"]},
            json=second_payload,
        )
        self.assertEqual(second_item_response.status_code, 201, second_item_response.text)
        second_item = second_item_response.json()

        cleared = self.client.patch(
            f"/api/v1/collection-items/{first['id']}",
            json={
                "expected_revision": first["revision"],
                "user_title": first["user_title"],
                "user_author": first["user_author"],
                "user_cover_asset_id": None,
                "organization_confirmation": first["organization_confirmation"],
                "personal_tags": first["personal_tags"],
                "inspiration": None,
            },
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        with closing(sqlite3.connect(self.root / "cq3.sqlite3")) as db:
            db.execute("UPDATE user_cover_assets SET expires_at='2000-01-01T00:00:00+00:00' WHERE asset_id=?", (asset["asset_id"],))
            db.commit()
        self.client.close()
        self.client = TestClient(create_app(
            self.pipeline,
            upload_root=self.root / "uploads",
            user_cover_root=self.root / "user-covers",
        ))
        still_bound = self.client.get(f"/api/v1/collection-items/{second_item['id']}/cover")
        self.assertEqual(still_bound.status_code, 200, still_bound.text)

    def test_gc_candidate_bound_concurrently_keeps_file_and_reference(self):
        """INT-ACC-CQ3-002; CQ3-ASSET-001 atomic GC-vs-bind claim."""
        asset = self.upload(encoded_image())
        target = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "cq3-gc-race-target"},
            json=self.create_payload(),
        )
        self.assertEqual(target.status_code, 201, target.text)
        with closing(sqlite3.connect(self.root / "cq3.sqlite3")) as db:
            db.execute(
                "UPDATE user_cover_assets SET expires_at='2000-01-01T00:00:00+00:00' WHERE asset_id=?",
                (asset["asset_id"],),
            )
            db.commit()

        service = self.client.app.state.user_cover_service
        original_candidates = self.repo.expired_unbound_user_cover_assets
        candidate_listed = threading.Event()
        allow_gc = threading.Event()

        def paused_candidates(now: str) -> list[dict]:
            candidates = original_candidates(now)
            candidate_listed.set()
            self.assertTrue(allow_gc.wait(timeout=5))
            return candidates

        result: list[int] = []
        with patch.object(
            self.repo,
            "expired_unbound_user_cover_assets",
            side_effect=paused_candidates,
        ):
            worker = threading.Thread(target=lambda: result.append(service.cleanup_expired()))
            worker.start()
            self.assertTrue(candidate_listed.wait(timeout=5))
            # Model a bind transaction that validated just before expiry, then
            # commits after the GC candidate read but before its delete claim.
            with closing(sqlite3.connect(self.root / "cq3.sqlite3")) as db:
                db.execute(
                    "INSERT INTO collection_user_covers(collection_item_id, asset_id, created_at) VALUES (?, ?, ?)",
                    (
                        target.json()["id"],
                        asset["asset_id"],
                        "2026-09-10T00:00:00+00:00",
                    ),
                )
                db.execute(
                    "UPDATE user_cover_assets SET expires_at=NULL WHERE asset_id=?",
                    (asset["asset_id"],),
                )
                db.commit()
            allow_gc.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [0])
        cover = self.client.get(
            f"/api/v1/collection-items/{target.json()['id']}/cover"
        )
        self.assertEqual(cover.status_code, 200, cover.text)

    def test_failed_unlink_restores_unbound_asset_record_for_later_cleanup(self):
        """CQ3-ASSET-001 explicit cleanup remains recoverable after I/O failure."""
        asset = self.upload(encoded_image())
        service = self.client.app.state.user_cover_service

        class FailingPath:
            def unlink(self, *, missing_ok: bool = False) -> None:
                raise OSError("injected unlink failure")

        with patch.object(service, "_path", return_value=FailingPath()):
            failed = self.client.delete(
                f"/api/v1/user-cover-assets/{asset['asset_id']}",
                headers={"X-User-Cover-Claim-Token": asset["claim_token"]},
            )
        self.assertEqual(failed.status_code, 404)
        restored = self.client.get(
            f"/api/v1/user-cover-assets/{asset['asset_id']}/content",
            headers={"X-User-Cover-Claim-Token": asset["claim_token"]},
        )
        self.assertEqual(restored.status_code, 200, restored.text)

    def test_v17_to_v18_failure_rolls_back_then_preserves_old_row(self):
        """CQ3-CONSISTENCY-001 migration rollback and old-data preservation."""
        old_payload = trusted_collection_payload()
        old = self.repo.create_collection_item(old_payload, "old-key", "old-hash")
        database = self.root / "cq3.sqlite3"
        with closing(sqlite3.connect(database)) as db:
            db.execute("DROP TRIGGER IF EXISTS collection_search_dirty_library_update")
            db.execute("DROP TABLE collection_user_covers")
            db.execute("DROP TABLE user_cover_assets")
            db.execute("ALTER TABLE library_items DROP COLUMN user_author")
            db.execute("PRAGMA user_version=17")
            db.commit()

        def fail(stage: str, _db: sqlite3.Connection) -> None:
            if stage == "after_v18_ddl":
                raise RuntimeError("injected v18 failure")

        with self.assertRaisesRegex(RuntimeError, "injected v18"):
            SQLiteRepository(database, migration_fault=fail)
        with closing(sqlite3.connect(database)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(library_items)")}
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("user_author", columns)
            self.assertNotIn("user_cover_assets", tables)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 17)

        recovered = SQLiteRepository(database)
        restored = recovered.get_collection_item(old["id"])
        self.assertEqual(restored["original_input"], old["original_input"])
        self.assertIsNone(restored["user_author"])
        self.assertIsNone(restored["user_cover_asset_id"])


if __name__ == "__main__":
    unittest.main()
