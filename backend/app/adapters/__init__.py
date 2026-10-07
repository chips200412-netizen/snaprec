from .base import ResolvedVideo, SubtitlePayload, VideoAdapter, VideoMetadata
from .bilibili import BilibiliAdapter
from .douyin import DouyinAdapter
from .local_upload import LocalUploadAdapter
from .registry import AdapterRegistry

__all__ = [
    "AdapterRegistry",
    "BilibiliAdapter",
    "DouyinAdapter",
    "LocalUploadAdapter",
    "ResolvedVideo",
    "SubtitlePayload",
    "VideoAdapter",
    "VideoMetadata",
]
