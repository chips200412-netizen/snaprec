"""Offline collection smoke using a fresh temporary library, not user data."""
from __future__ import annotations

import json
import ipaddress
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient
from backend.app.api.main import create_app
from backend.app.bootstrap import build_pipeline, build_inspiration_transcription_provider
from backend.app.runtime_config import resolve_runtime_config
from backend.app.services.inspiration import UnconfiguredInspirationTranscriptionProvider
from backend.app.services.safe_http import SafeHttpError


class OfflineFetcher:
    url_validator = SimpleNamespace(resolve=lambda url: object())

    def fetch(self, url):
        raise SafeHttpError("offline-smoke")


_socket_connect = socket.socket.connect


def offline_connect(sock, address):
    # Windows asyncio uses a loopback self-pipe; do not break its local event loop.
    try:
        is_loopback = isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise RuntimeError("offline-smoke-network-denied")
    return _socket_connect(sock, address)


def smoke() -> dict:
    with tempfile.TemporaryDirectory(prefix="snaprec-release-smoke-") as directory:
        config = resolve_runtime_config({}, Path(directory))
        pipeline = build_pipeline(config)
        provider = build_inspiration_transcription_provider(config)
        assert isinstance(provider, UnconfiguredInspirationTranscriptionProvider)
        app = create_app(
            pipeline,
            capture_fetcher=OfflineFetcher(),
            inspiration_transcription_provider=provider,
            upload_root=config.video_upload_root,
            inspiration_temp_root=config.inspiration_temp_root,
        )
        with patch("socket.socket.connect", offline_connect), TestClient(app) as client:
            empty = client.get("/api/v1/collection-items")
            assert empty.status_code == 200 and empty.json()["items"] == []
            preview = client.post("/api/v1/collection-previews", json={"input_text": "https://example.com/alpha-demo"})
            assert preview.status_code == 201
            payload = {
                "preview_id": preview.json()["preview_id"],
                "user_title": "演示收藏 · 中文与 emoji 🌱",
                "user_author": "演示作者",
                "personal_tags": ["演示"],
                "inspiration": {"content": "一条用于隔离测试的个人灵感。", "input_mode": "text", "transcription_status": "not_applicable"},
            }
            headers = {"Idempotency-Key": "release-smoke-save"}
            saved = client.post("/api/v1/collection-items", json=payload, headers=headers)
            assert saved.status_code == 201
            item = saved.json()
            assert item["display_title"] == payload["user_title"]
            assert item["user_author"] == "演示作者"
            assert client.post("/api/v1/collection-items", json=payload, headers=headers).json() == item
            assert client.get(f"/api/v1/collection-items/{item['id']}").json() == item
            found = client.get("/api/v1/collection-items", params={"query": "隔离测试"})
            assert found.status_code == 200 and [value["id"] for value in found.json()["items"]] == [item["id"]]
        assert config.video_db_path.is_file()
    return {"status": "passed", "checks": ["fresh-library", "bookmark-fallback", "unicode-title", "manual-author", "atomic-save", "idempotent-save", "restore", "inspiration-search", "voice-unconfigured", "no-external-network"]}


def main() -> int:
    try:
        print(json.dumps(smoke(), ensure_ascii=False))
    except Exception:
        print(json.dumps({"status": "failed", "code": "RELEASE_SMOKE_FAILED"}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
