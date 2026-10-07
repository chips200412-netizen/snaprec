"""Offline API/provider/storage contract: all audio and credentials are synthetic."""
import io
import json
import sqlite3
import wave
from contextlib import closing
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from backend.app.api.main import create_app
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.providers import DeterministicFullExtractor
from backend.app.services.volcengine_inspiration import (
    FLASH_ENDPOINT, VolcengineInspirationTranscriptionProvider,
)
from backend.tests.test_organize_inspire import _Fetcher, _ForbiddenDeepAsr, _Probe


def _wav():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 1600)
    return buffer.getvalue()


def _client(root, handler):
    repo = SQLiteRepository(root / "library.sqlite3")
    deep = _ForbiddenDeepAsr()
    pipeline = LocalFullPipeline(repo, deep, DeterministicFullExtractor())
    provider = VolcengineInspirationTranscriptionProvider(
        app_id="synthetic-app", access_token="synthetic-token",
        transport=httpx.MockTransport(handler),
    )
    app = create_app(
        pipeline, upload_root=root / "uploads", user_cover_root=root / "user-covers",
        cover_cache_root=root / "covers", inspiration_temp_root=root / "recordings",
        capture_fetcher=_Fetcher("<title>公开测试素材</title>"),
        inspiration_audio_probe=_Probe(duration=0.1),
        inspiration_transcription_provider=provider,
    )
    return TestClient(app), provider, deep


def _count(root, table):
    # All callers pass fixed table literals, never a request value.
    with closing(sqlite3.connect(root / "library.sqlite3")) as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_flash_draft_requires_explicit_adoption_and_survives_rebuild(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == FLASH_ENDPOINT
        payload = json.loads(request.content)
        assert "data" in payload["audio"] and "url" not in payload["audio"]
        return httpx.Response(200, headers={"X-Api-Status-Code": "20000000"},
                              json={"result": {"text": "中文灵感，保留表情🍍。"}})

    client, provider, deep = _client(tmp_path, handler)
    with client, patch.object(provider, "_convert", AsyncMock(return_value=_wav())):
        draft = client.post("/api/v1/inspiration-transcriptions",
                            files={"file": ("recording.wav", _wav(), "audio/wav")})
        assert draft.status_code == 200, draft.text
        assert draft.json()["transcription_status"] == "draft"
        assert draft.json()["input_mode"] == "voice"
        assert _count(tmp_path, "library_items") == _count(tmp_path, "inspirations") == 0
        assert list((tmp_path / "recordings").iterdir()) == []
        preview = client.post("/api/v1/collection-previews",
                              json={"input_text": "https://public.example/offline"})
        assert preview.status_code == 201, preview.text
        saved = client.post("/api/v1/collection-items", headers={"Idempotency-Key": "adopt"},
                            json={"preview_id": preview.json()["preview_id"],
                                  "user_title": "个人标题", "user_author": "个人作者",
                                  "personal_tags": ["人工标签"],
                                  "inspiration": {**draft.json(), "transcription_status": "completed"}})
        assert saved.status_code == 201, saved.text
        item = saved.json()
    reopened, _, _ = _client(tmp_path, handler)
    with reopened:
        result = reopened.get(f"/api/v1/collection-items/{item['id']}")
        assert result.status_code == 200
        for field in ("user_title", "user_author", "personal_tags", "inspiration", "metadata"):
            assert result.json()[field] == item[field]
        assert result.json()["inspiration"]["content"] == "中文灵感，保留表情🍍。"
    assert len(requests) == 1 and deep.calls == 0
    for table in ("jobs", "videos", "transcript_segments", "extractions"):
        assert _count(tmp_path, table) == 0


def test_invalid_source_input_never_calls_provider_and_failure_keeps_bookmarks(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(403, text="private upstream detail synthetic-token")

    client, provider, deep = _client(tmp_path, handler)
    with client, patch.object(provider, "_convert", AsyncMock(return_value=_wav())):
        invalid = client.post("/api/v1/inspiration-transcriptions",
                              data={"source_url": "https://public.example/video"},
                              files={"file": ("recording.wav", _wav(), "audio/wav")})
        assert invalid.status_code >= 400
        assert requests == []
        failed = client.post("/api/v1/inspiration-transcriptions",
                             files={"file": ("recording.wav", _wav(), "audio/wav")})
        assert failed.status_code >= 400
        assert "synthetic-token" not in failed.text and "private upstream" not in failed.text
        assert list((tmp_path / "recordings").iterdir()) == []
        preview = client.post("/api/v1/collection-previews",
                              json={"input_text": "https://public.example/bookmark"})
        saved = client.post("/api/v1/collection-items", headers={"Idempotency-Key": "text-only"},
                            json={"preview_id": preview.json()["preview_id"], "user_title": "文字仍可保存"})
        assert saved.status_code == 201, saved.text
        assert saved.json()["inspiration"] is None
    assert len(requests) == 1 and deep.calls == 0
