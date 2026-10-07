from __future__ import annotations

import os
import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.adapters.base import ResolvedVideo, VideoMetadata
from backend.app.api.main import create_app
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.inspiration import (
    FfprobeInspirationAudioProbe,
    InspirationTranscriptionService,
)
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.pipeline import PipelineError
from backend.app.services.providers import DeterministicFullExtractor
from backend.app.services.safe_http import SafeFetchResult, SafeHttpError


class _Validator:
    def resolve(self, url):
        return object()


class _Fetcher:
    def __init__(self, html: str = "", *, fail: bool = False):
        self.html = html
        self.fail = fail
        self.url_validator = _Validator()

    def fetch(self, url):
        if self.fail:
            raise SafeHttpError("FETCH_FAILED")
        return SafeFetchResult(
            original_url=url,
            final_url=url,
            status_code=200,
            media_type="text/html",
            body=self.html.encode("utf-8"),
            redirects=(),
            content_type="text/html; charset=utf-8",
        )


class _ShareOnlyAdapter:
    def resolve(self, url):
        return ResolvedVideo(
            platform="bilibili",
            source_url=url,
            canonical_url="https://www.bilibili.com/video/BV1shareonly",
            video_id="BV1shareonly",
            aliases=(url,),
        )

    def get_metadata(self, video):
        return VideoMetadata()

    def get_share_metadata(self, input_text):
        return VideoMetadata(description=input_text.split("https://", 1)[0].strip())


class _Registry:
    def matching(self, url):
        return _ShareOnlyAdapter()


class _ResolutionWithRegistry:
    registry = _Registry()


class _ForbiddenDeepAsr:
    def __init__(self):
        self.calls = 0

    def transcribe(self, media_path):
        self.calls += 1
        raise AssertionError("R1.3 must not call deep-analysis ASR")


class _ForbiddenAutoTagger:
    generator_id = "forbidden"
    generator_version = "1"

    def __init__(self):
        self.calls = 0

    def generate(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("R1.3 must not call transcript automatic tags")


class _Probe:
    def __init__(self, *, duration: float = 12.5, error: Exception | None = None):
        self.duration = duration
        self.error = error
        self.calls: list[tuple[Path, str, bool]] = []

    def probe(self, media_path: Path, mime_type: str) -> float:
        path = Path(media_path)
        self.calls.append((path, mime_type, path.is_file()))
        if self.error is not None:
            raise self.error
        return self.duration


class _Transcriber:
    def __init__(
        self,
        text: str = "可编辑的语音灵感草稿",
        *,
        error: Exception | None = None,
    ):
        self.text = text
        self.error = error
        self.calls: list[tuple[Path, bool]] = []

    def transcribe(self, media_path: Path) -> str:
        path = Path(media_path)
        self.calls.append((path, path.is_file()))
        if self.error is not None:
            raise self.error
        return self.text


class _SuggestionService:
    def __init__(self, suggestion: dict | None = None, *, error: Exception | None = None):
        self.suggestion = suggestion or {
            "primary_category": "创意与设计",
            "secondary_category": "界面设计",
            "tags": ["设计", "灵感"],
            "basis": "public_metadata",
            "method": "deterministic",
            "status": "generated",
        }
        self.error = error
        self.calls = 0

    def suggest(self, metadata, source_kind, platform):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.suggestion


def _trusted_preview_payload(identity: str, *, suggestion: dict | None = None) -> dict:
    return {
        "preview_id": f"preview-{identity}",
        "original_input": f"https://example.com/{identity}",
        "source_url": f"https://example.com/{identity}",
        "canonical_url": f"https://example.com/{identity}",
        "identity_url": f"https://example.com/{identity}",
        "source_kind": "webpage",
        "platform": "web",
        "metadata_status": "generic",
        "metadata": {
            "title": {
                "value": "公开设计文章",
                "source": "open_graph",
                "fetched_at": "2026-08-23T00:00:00+00:00",
            },
            "author": {"value": "", "source": "none", "fetched_at": ""},
            "cover_url": {"value": "", "source": "none", "fetched_at": ""},
            "source_copy": {
                "value": "界面与视觉设计灵感",
                "source": "page_description",
                "fetched_at": "2026-08-23T00:00:00+00:00",
            },
            "platform_tags": [
                {"value": "设计", "source": "page_metadata"},
            ],
            "warnings": [],
        },
        "organization_suggestion": suggestion
        or {
            "primary_category": "创意与设计",
            "secondary_category": "界面设计",
            "tags": ["设计", "灵感"],
            "basis": "public_metadata",
            "method": "deterministic",
            "status": "generated",
        },
        "created_at": "2026-08-23T00:00:00+00:00",
        "expires_at": "2099-08-23T00:00:00+00:00",
    }


class OrganizeInspireApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = SQLiteRepository(self.root / "test.sqlite3")
        self.deep_asr = _ForbiddenDeepAsr()
        self.auto_tagger = _ForbiddenAutoTagger()
        self.pipeline = LocalFullPipeline(
            self.repo,
            self.deep_asr,
            DeterministicFullExtractor(),
            auto_tagger=self.auto_tagger,
        )

    def tearDown(self):
        self.temp.cleanup()

    def _client(self, **kwargs) -> TestClient:
        kwargs.setdefault("upload_root", self.root / "uploads")
        kwargs.setdefault("inspiration_temp_root", self.root / "inspiration-temp")
        return TestClient(create_app(self.pipeline, **kwargs))

    def _seed_preview(self, identity: str, *, suggestion: dict | None = None) -> str:
        payload = _trusted_preview_payload(identity, suggestion=suggestion)
        self.repo.create_collection_preview(payload)
        return payload["preview_id"]

    def test_organize_suggest_001_default_is_deterministic_and_uses_public_metadata(self):
        client = self._client(capture_fetcher=_Fetcher("""
            <html><head>
            <title>Python 编程工具教程</title>
            <meta name="description" content="开发者学习自动化与代码工具">
            <meta name="keywords" content="Python, 编程, 工具">
            </head></html>
        """))
        first = client.post(
            "/api/v1/collection-previews",
            json={"input_text": "https://public.example/python"},
        )
        second = client.post(
            "/api/v1/collection-previews",
            json={"input_text": "https://public.example/python", "refresh_metadata": True},
        )
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 201, second.text)
        suggestion = first.json()["organization_suggestion"]
        self.assertEqual(suggestion, second.json()["organization_suggestion"])
        self.assertEqual(suggestion["method"], "deterministic")
        self.assertEqual(suggestion["basis"], "public_metadata")
        self.assertEqual(suggestion["status"], "generated")
        self.assertEqual(suggestion["primary_category"], "技术与工具")
        self.assertLessEqual(len(suggestion["tags"]), 50)
        self.assertEqual(self.deep_asr.calls, 0)
        self.assertEqual(self.auto_tagger.calls, 0)

    def test_organize_suggest_001_share_text_and_unavailable_metadata_are_excluded(self):
        client = self._client(
            capture_fetcher=_Fetcher(fail=True),
            resolution_service=_ResolutionWithRegistry(),
        )
        response = client.post(
            "/api/v1/collection-previews",
            json={
                "input_text": "Python 编程、商业营销、视觉设计 https://public.example/share"
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        preview = response.json()
        self.assertEqual(preview["metadata_status"], "metadata_unavailable")
        self.assertEqual(preview["metadata"]["source_copy"]["source"], "share_text")
        self.assertEqual(
            preview["organization_suggestion"],
            {
                "primary_category": "",
                "secondary_category": "",
                "tags": [],
                "basis": "public_metadata",
                "method": "deterministic",
                "status": "insufficient_metadata",
            },
        )

    def test_organize_suggest_001_preview_snapshot_is_authoritative_on_save(self):
        suggestion_service = _SuggestionService()
        client = self._client(
            capture_fetcher=_Fetcher("<title>设计灵感</title>"),
            organization_suggestion_service=suggestion_service,
        )
        preview = client.post(
            "/api/v1/collection-previews",
            json={"input_text": "https://public.example/design"},
        )
        self.assertEqual(preview.status_code, 201, preview.text)
        snapshot = preview.json()["organization_suggestion"]
        forged = client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "forged-suggestion"},
            json={
                "preview_id": preview.json()["preview_id"],
                "organization_suggestion": {
                    **snapshot,
                    "primary_category": "伪造分类",
                },
            },
        )
        self.assertEqual(forged.status_code, 422, forged.text)
        suggestion_service.suggestion = {
            **suggestion_service.suggestion,
            "primary_category": "不得在保存时重算",
        }
        saved = client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "snapshot-save"},
            json={"preview_id": preview.json()["preview_id"]},
        )
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(saved.json()["organization_suggestion"], snapshot)
        self.assertEqual(suggestion_service.calls, 1)

    def test_organize_suggest_001_rule_failure_does_not_block_preview_or_save(self):
        client = self._client(
            capture_fetcher=_Fetcher("<title>设计灵感</title>"),
            organization_suggestion_service=_SuggestionService(
                error=RuntimeError("injected suggestion failure")
            ),
        )
        preview = client.post(
            "/api/v1/collection-previews",
            json={"input_text": "https://public.example/failure"},
        )
        self.assertEqual(preview.status_code, 201, preview.text)
        self.assertEqual(
            preview.json()["organization_suggestion"]["status"], "failed"
        )
        saved = client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "failed-suggestion-save"},
            json={"preview_id": preview.json()["preview_id"]},
        )
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(saved.json()["organization_suggestion"]["status"], "failed")

    def test_organize_suggest_001_old_preview_without_suggestion_stays_compatible(self):
        payload = _trusted_preview_payload("r12-preview")
        payload.pop("organization_suggestion")
        self.repo.create_collection_preview(payload)
        client = self._client()
        saved = client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "r12-preview-save"},
            json={"preview_id": payload["preview_id"]},
        )
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(
            saved.json()["organization_suggestion"],
            {
                "primary_category": "",
                "secondary_category": "",
                "tags": [],
                "basis": "public_metadata",
                "method": "deterministic",
                "status": "insufficient_metadata",
            },
        )

    def test_insight_text_001_null_and_unicode_newlines_are_exactly_preserved(self):
        client = self._client()
        null_preview = self._seed_preview("null-inspiration")
        without = client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "null-inspiration"},
            json={"preview_id": null_preview, "inspiration": None},
        )
        self.assertEqual(without.status_code, 201, without.text)
        self.assertIsNone(without.json()["inspiration"])

        exact = "第一行：灵感🌙\r\n第二行\n　保留全角空格与结尾换行\n"
        text_preview = self._seed_preview("exact-text")
        with_text = client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "exact-text"},
            json={
                "preview_id": text_preview,
                "inspiration": {
                    "content": exact,
                    "input_mode": "text",
                    "transcription_status": "not_applicable",
                },
            },
        )
        self.assertEqual(with_text.status_code, 201, with_text.text)
        self.assertEqual(with_text.json()["inspiration"]["content"], exact)
        restored = client.get(
            f"/api/v1/collection-items/{with_text.json()['id']}"
        )
        self.assertEqual(restored.json()["inspiration"]["content"], exact)

    def test_insight_text_001_blank_only_inspiration_is_rejected_without_writes(self):
        client = self._client()
        preview_id = self._seed_preview("blank-text")
        response = client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "blank-text"},
            json={
                "preview_id": preview_id,
                "inspiration": {
                    "content": " \t\r\n　",
                    "input_mode": "text",
                    "transcription_status": "not_applicable",
                },
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        with closing(sqlite3.connect(self.root / "test.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM library_items").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM inspirations").fetchone()[0], 0)

    def test_insight_voice_001_accepts_four_mimes_and_returns_only_editable_draft(self):
        for index, mime in enumerate(("audio/webm", "audio/ogg", "audio/mp4", "audio/wav")):
            with self.subTest(mime=mime):
                probe = _Probe()
                provider = _Transcriber(text=f"草稿 {index}")
                client = self._client(
                    inspiration_audio_probe=probe,
                    inspiration_transcription_provider=provider,
                )
                before = self._collection_counts()
                response = client.post(
                    "/api/v1/inspiration-transcriptions",
                    files={"file": (f"recording-{index}.bin", b"recording", mime)},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(
                    response.json(),
                    {
                        "content": f"草稿 {index}",
                        "input_mode": "voice",
                        "transcription_status": "draft",
                    },
                )
                self.assertEqual(probe.calls[0][1:], (mime, True))
                self.assertTrue(provider.calls[0][1])
                self.assertFalse(provider.calls[0][0].exists())
                self.assertEqual(self._collection_counts(), before)
                self.assertEqual(list((self.root / "inspiration-temp").iterdir()), [])
        self.assertEqual(self.deep_asr.calls, 0)
        self.assertEqual(self.auto_tagger.calls, 0)

    def test_insight_voice_001_rejects_source_fields_at_public_boundary(self):
        provider = _Transcriber()
        client = self._client(
            inspiration_audio_probe=_Probe(),
            inspiration_transcription_provider=provider,
        )
        response = client.post(
            "/api/v1/inspiration-transcriptions",
            data={"source_url": "https://example.com/source-video"},
            files={"file": ("recording.wav", b"recording", "audio/wav")},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(
            response.json()["error"]["code"], "INSPIRATION_REQUEST_INVALID"
        )
        self.assertEqual(provider.calls, [])
        self.assertEqual(list((self.root / "inspiration-temp").iterdir()), [])

    def test_insight_voice_001_rejects_limits_and_probe_failures_before_transcription(self):
        cases = (
            ("unsupported", "audio/mpeg", b"data", _Probe()),
            ("too-large", "audio/webm", b"x" * (10 * 1024 * 1024 + 1), _Probe()),
            ("too-long", "audio/ogg", b"data", _Probe(duration=120.001)),
            ("probe-failed", "audio/mp4", b"data", _Probe(error=RuntimeError("bad media"))),
        )
        for name, mime, body, probe in cases:
            with self.subTest(name=name):
                provider = _Transcriber()
                client = self._client(
                    inspiration_audio_probe=probe,
                    inspiration_transcription_provider=provider,
                )
                response = client.post(
                    "/api/v1/inspiration-transcriptions",
                    files={"file": ("recording.bin", body, mime)},
                )
                self.assertIn(response.status_code, {400, 413, 415, 422}, response.text)
                self.assertEqual(provider.calls, [])
                self.assertEqual(list((self.root / "inspiration-temp").iterdir()), [])
        self.assertEqual(self.deep_asr.calls, 0)
        self.assertEqual(self.auto_tagger.calls, 0)

    def test_insight_voice_001_default_unconfigured_provider_is_local_failure_and_cleans(self):
        probe = _Probe()
        client = self._client(inspiration_audio_probe=probe)
        response = client.post(
            "/api/v1/inspiration-transcriptions",
            files={"file": ("recording.wav", b"recording", "audio/wav")},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(
            response.json()["error"]["code"],
            "INSPIRATION_TRANSCRIPTION_UNAVAILABLE",
        )
        self.assertEqual(list((self.root / "inspiration-temp").iterdir()), [])

    def test_insight_voice_001_configured_provider_failure_also_cleans(self):
        provider = _Transcriber(error=RuntimeError("provider failed"))
        client = self._client(
            inspiration_audio_probe=_Probe(),
            inspiration_transcription_provider=provider,
        )
        response = client.post(
            "/api/v1/inspiration-transcriptions",
            files={"file": ("recording.webm", b"recording", "audio/webm")},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(
            response.json()["error"]["code"], "INSPIRATION_TRANSCRIPTION_FAILED"
        )
        self.assertEqual(len(provider.calls), 1)
        self.assertFalse(provider.calls[0][0].exists())
        self.assertEqual(list((self.root / "inspiration-temp").iterdir()), [])

    def test_insight_voice_001_only_cleans_old_orphans_in_dedicated_root(self):
        inspiration_root = self.root / "inspiration-temp"
        orphan = inspiration_root / "inspiration-orphan"
        orphan.mkdir(parents=True)
        (orphan / "recording.webm").write_bytes(b"old")
        unrelated_root = self.root / "uploads"
        unrelated_root.mkdir()
        unrelated = unrelated_root / "inspiration-lookalike.webm"
        unrelated.write_bytes(b"keep")
        old = time.time() - 901
        os.utime(orphan, (old, old))
        os.utime(orphan / "recording.webm", (old, old))
        os.utime(unrelated, (old, old))

        self._client(
            inspiration_audio_probe=_Probe(),
            inspiration_transcription_provider=_Transcriber(),
            inspiration_orphan_max_age_seconds=900,
        )
        self.assertFalse(orphan.exists())
        self.assertTrue(unrelated.exists())

    def test_insight_voice_001_restart_nonce_cleans_old_owner_but_keeps_current_active(self):
        inspiration_root = self.root / "inspiration-temp"
        first = InspirationTranscriptionService(
            _Transcriber(), _Probe(), inspiration_root, orphan_max_age_seconds=900
        )
        stale = inspiration_root / "inspiration-recording-stale"
        stale.mkdir()
        stale_marker = stale / ".inspiration-recording-owner"
        stale_marker.write_text(
            json.dumps(
                {"instance_id": first.instance_id, "request_id": "old-request"}
            ),
            encoding="utf-8",
        )
        old = time.time() - 901
        os.utime(stale_marker, (old, old))
        os.utime(stale, (old, old))

        restarted = InspirationTranscriptionService(
            _Transcriber(), _Probe(), inspiration_root, orphan_max_age_seconds=900
        )
        restarted.cleanup_orphans()
        self.assertFalse(stale.exists())

        active = inspiration_root / "inspiration-recording-active"
        active.mkdir()
        active_marker = active / ".inspiration-recording-owner"
        active_marker.write_text(
            json.dumps(
                {"instance_id": restarted.instance_id, "request_id": "active-request"}
            ),
            encoding="utf-8",
        )
        os.utime(active_marker, (old, old))
        os.utime(active, (old, old))
        with restarted._active_lock:
            restarted._active_request_ids.add("active-request")
        restarted.cleanup_orphans()
        self.assertTrue(active.exists())
        with restarted._active_lock:
            restarted._active_request_ids.discard("active-request")
        restarted.cleanup_orphans()
        self.assertFalse(active.exists())

    def test_insight_voice_001_rejects_temp_root_through_parent_link(self):
        target = self.root / "actual-temp"
        target.mkdir()
        link = self.root / "linked-temp"
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlinks are unavailable on this host")
        with self.assertRaises(ValueError):
            InspirationTranscriptionService(
                _Transcriber(), _Probe(), link / "nested"
            )

    def _collection_counts(self) -> tuple[int, int]:
        with closing(sqlite3.connect(self.root / "test.sqlite3")) as db:
            return (
                db.execute("SELECT COUNT(*) FROM library_items").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM inspirations").fetchone()[0],
            )


class FfprobeSafetyContractTests(unittest.TestCase):
    def test_controlled_ffprobe_uses_argument_array_no_shell_and_finite_timeout(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(
                args,
                0,
                stdout='{"format":{"format_name":"matroska,webm","duration":"120.0"},"streams":[{"codec_type":"audio"}]}',
                stderr="",
            )

        suspicious = Path("recording;do-not-execute-$(payload).webm")
        duration = FfprobeInspirationAudioProbe(
            executable="controlled-ffprobe",
            timeout_seconds=3.5,
            runner=runner,
        ).probe(suspicious, "audio/webm")
        self.assertEqual(duration, 120.0)
        args, kwargs = calls[0]
        self.assertIsInstance(args, list)
        self.assertEqual(args[0], "controlled-ffprobe")
        self.assertEqual(args[-1], str(suspicious))
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], 3.5)
        self.assertIs(kwargs["check"], False)

    def test_controlled_ffprobe_rejects_container_mismatch_and_timeout(self):
        mismatch = subprocess.CompletedProcess(
            ["ffprobe"],
            0,
            stdout='{"format":{"format_name":"ogg","duration":"1"},"streams":[{"codec_type":"audio"}]}',
            stderr="",
        )
        with self.assertRaises(PipelineError) as mismatch_error:
            FfprobeInspirationAudioProbe(
                runner=lambda *args, **kwargs: mismatch
            ).probe(Path("recording.webm"), "audio/webm")
        self.assertEqual(mismatch_error.exception.code, "INSPIRATION_AUDIO_INVALID")

        with self.assertRaises(PipelineError) as timeout_error:
            FfprobeInspirationAudioProbe(
                runner=lambda *args, **kwargs: (_ for _ in ()).throw(
                    subprocess.TimeoutExpired("ffprobe", 5)
                )
            ).probe(Path("recording.wav"), "audio/wav")
        self.assertEqual(
            timeout_error.exception.code, "INSPIRATION_AUDIO_PROBE_FAILED"
        )


if __name__ == "__main__":
    unittest.main()
