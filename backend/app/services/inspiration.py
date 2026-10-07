from __future__ import annotations

import json
import math
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Protocol

import httpx

from ..domain.models import InspirationTranscriptionDraft
from .pipeline import PipelineError


ALLOWED_AUDIO_TYPES = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".mp4",
    "audio/wav": ".wav",
}
_EXPECTED_FORMATS = {
    "audio/webm": {"matroska", "webm"},
    "audio/ogg": {"ogg"},
    "audio/mp4": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
    "audio/wav": {"wav"},
}
_REQUEST_PREFIX = "inspiration-recording-"
_OWNER_MARKER = ".inspiration-recording-owner"


class InspirationTranscriptionProvider(Protocol):
    def transcribe(self, audio_path: Path) -> str: ...


class InspirationAudioProbe(Protocol):
    def probe(self, audio_path: Path, mime_type: str) -> float: ...


class UnconfiguredInspirationTranscriptionProvider:
    def transcribe(self, audio_path: Path) -> str:
        del audio_path
        raise PipelineError(
            "INSPIRATION_TRANSCRIPTION_UNAVAILABLE",
            "灵感语音转写服务尚未配置，请改用文字记录或稍后重试。",
        )


class HttpInspirationTranscriptionProvider:
    """Provider-neutral adapter enabled only by explicit inspiration ASR config."""

    def __init__(
        self,
        endpoint: str,
        *,
        token: str | None = None,
        model: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.token = token
        self.model = model
        self.client = client or httpx.Client(timeout=httpx.Timeout(30, connect=5))

    def transcribe(self, audio_path: Path) -> str:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        data = {"model": self.model} if self.model else {}
        try:
            with audio_path.open("rb") as stream:
                response = self.client.post(
                    self.endpoint,
                    headers=headers,
                    data=data,
                    files={"file": (audio_path.name, stream, "application/octet-stream")},
                )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise PipelineError(
                "INSPIRATION_TRANSCRIPTION_TIMEOUT",
                "灵感语音转写超时，请改用文字记录或重新录制。",
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise PipelineError(
                "INSPIRATION_TRANSCRIPTION_FAILED",
                "灵感语音转写失败，请改用文字记录或重新录制。",
            ) from exc
        text = str(payload.get("text", "")).strip()
        if not text:
            raise PipelineError(
                "INSPIRATION_TRANSCRIPTION_EMPTY",
                "没有识别到可编辑文字，请重新录制或改用文字记录。",
            )
        return text


class FfprobeInspirationAudioProbe:
    def __init__(
        self,
        executable: str | Path = "ffprobe",
        *,
        timeout_seconds: float = 5,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("probe timeout must be positive")
        self.executable = str(executable)
        self.timeout_seconds = timeout_seconds
        self.runner = runner

    def probe(self, audio_path: Path, mime_type: str) -> float:
        started = time.monotonic()
        try:
            completed = self.runner(
                [
                    self.executable,
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-format_whitelist",
                    "matroska,webm,ogg,mov,wav",
                    "-show_entries",
                    "format=format_name,duration:stream=codec_type",
                    "-of",
                    "json",
                    str(audio_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            raise PipelineError(
                "INSPIRATION_AUDIO_PROBE_FAILED",
                "无法安全确认录音格式与时长，请检查录音后重试。",
            ) from None
        if completed.returncode != 0:
            raise PipelineError(
                "INSPIRATION_AUDIO_PROBE_FAILED",
                "无法安全确认录音格式与时长，请检查录音后重试。",
            )
        try:
            payload = json.loads(completed.stdout)
            format_payload = payload["format"]
            formats = {
                value.strip().casefold()
                for value in str(format_payload["format_name"]).split(",")
                if value.strip()
            }
            streams = payload.get("streams", [])
            if not streams or any(
                not isinstance(stream, dict) or stream.get("codec_type") != "audio"
                for stream in streams
            ):
                raise PipelineError(
                    "INSPIRATION_AUDIO_INVALID",
                    "灵感录音必须只包含音频流。",
                )
            raw_duration = format_payload.get("duration")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise PipelineError(
                "INSPIRATION_AUDIO_PROBE_FAILED",
                "无法安全确认录音格式与时长，请检查录音后重试。",
            ) from None
        if not formats.intersection(_EXPECTED_FORMATS[mime_type]):
            raise PipelineError(
                "INSPIRATION_AUDIO_INVALID",
                "录音实际格式与声明类型不一致或时长无效。",
            )
        if raw_duration in (None, "N/A") and mime_type == "audio/webm":
            # MediaRecorder WebM is often non-seekable and omits duration.
            # Probe bounded packet timestamps, never client-supplied duration.
            return self._packet_duration(audio_path, started, len(streams))
        try:
            duration = float(raw_duration)
        except (ValueError, TypeError):
            raise PipelineError(
                "INSPIRATION_AUDIO_INVALID",
                "录音实际格式与声明类型不一致或时长无效。",
            ) from None
        if not math.isfinite(duration) or duration <= 0:
            raise PipelineError(
                "INSPIRATION_AUDIO_INVALID",
                "录音实际格式与声明类型不一致或时长无效。",
            )
        return duration

    def _packet_duration(self, audio_path: Path, started: float, stream_count: int) -> float:
        # 60,000 packets cover 120 seconds even for 2.5 ms Opus packets.
        # Hitting the sentinel cannot prove EOF and therefore fails closed.
        packet_limit = 60001
        remaining = self.timeout_seconds - (time.monotonic() - started)
        try:
            if remaining <= 0 or stream_count != 1:
                raise ValueError("Uncertain audio timing")
            completed = self.runner(
                [self.executable, "-v", "error", "-protocol_whitelist", "file,pipe",
                 "-format_whitelist", "matroska,webm", "-select_streams", "a:0",
                 "-read_intervals", f"%+#{packet_limit}", "-show_entries",
                 "packet=pts_time,duration_time", "-of", "json", str(audio_path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=remaining, check=False, shell=False,
            )
            if completed.returncode != 0 or len(completed.stdout) > 8 * 1024 * 1024:
                raise ValueError("Invalid audio timing")
            packets = json.loads(completed.stdout)["packets"]
            if not isinstance(packets, list) or not 0 < len(packets) < packet_limit:
                raise ValueError("Uncertain audio timing")
            first, previous, end = None, None, 0.0
            for packet in packets:
                pts = float(packet["pts_time"])
                length = float(packet["duration_time"])
                if not math.isfinite(pts) or not math.isfinite(length) or length <= 0:
                    raise ValueError("Invalid audio timing")
                if first is None:
                    first = pts
                    if abs(first) > 0.25:
                        raise ValueError("Uncertain audio start")
                if previous is not None and pts < previous:
                    raise ValueError("Uncertain audio timing")
                previous = pts
                end = max(end, pts + length)
            duration = end - min(0.0, first)
            if not math.isfinite(duration) or not 0 < duration <= 120:
                raise ValueError("Invalid audio timing")
            return duration
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError):
            raise PipelineError(
                "INSPIRATION_AUDIO_PROBE_FAILED",
                "无法安全确认录音格式与时长，请检查录音后重试。",
            ) from None


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


class InspirationTranscriptionService:
    def __init__(
        self,
        provider: InspirationTranscriptionProvider,
        audio_probe: InspirationAudioProbe,
        temp_root: str | Path,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        max_duration_seconds: float = 120,
        orphan_max_age_seconds: float = 15 * 60,
        warning_sink: Callable[[str, str], None] | None = None,
    ) -> None:
        if max_bytes <= 0 or max_duration_seconds <= 0 or orphan_max_age_seconds <= 0:
            raise ValueError("inspiration audio limits must be positive")
        self.provider = provider
        self.audio_probe = audio_probe
        self.temp_root = Path(temp_root)
        self.max_bytes = max_bytes
        self.max_duration_seconds = max_duration_seconds
        self.orphan_max_age_seconds = orphan_max_age_seconds
        self.warning_sink = warning_sink
        self.instance_id = uuid.uuid4().hex
        self._active_request_ids: set[str] = set()
        self._active_lock = threading.Lock()
        self._prepare_root()

    def _prepare_root(self) -> None:
        self.temp_root.mkdir(parents=True, exist_ok=True)
        absolute = self.temp_root.absolute()
        ancestors = [absolute, *absolute.parents]
        for ancestor in ancestors:
            if ancestor.exists() and _is_link_or_reparse(ancestor):
                raise ValueError(
                    "inspiration temporary root and its parents must not be links"
                )
        resolved = self.temp_root.resolve()
        if resolved == Path(resolved.anchor):
            raise ValueError("inspiration temporary root is too broad")

    def cleanup_orphans(self) -> list[str]:
        warnings: list[str] = []
        now = time.time()
        for child in self.temp_root.iterdir():
            if not child.is_dir() or _is_link_or_reparse(child):
                continue
            marker = child / _OWNER_MARKER
            try:
                age = now - child.stat().st_mtime
            except OSError:
                continue
            if age <= self.orphan_max_age_seconds:
                continue
            if marker.is_file() and not _is_link_or_reparse(marker):
                try:
                    owner = json.loads(marker.read_text(encoding="utf-8"))
                    instance_id = str(owner["instance_id"])
                    request_id = str(owner["request_id"])
                except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                    warnings.append("INSPIRATION_ORPHAN_MARKER_INVALID")
                else:
                    with self._active_lock:
                        is_active = (
                            instance_id == self.instance_id
                            and request_id in self._active_request_ids
                        )
                    if is_active:
                        continue
            try:
                shutil.rmtree(child)
            except OSError:
                warnings.append("INSPIRATION_ORPHAN_CLEANUP_FAILED")
        if self.warning_sink is not None:
            for code in dict.fromkeys(warnings):
                try:
                    self.warning_sink(
                        code,
                        "灵感录音孤儿清理未完成，需要检查受控临时根。",
                    )
                except Exception:
                    pass
        return warnings

    def transcribe(self, stream: BinaryIO, content_type: str | None) -> InspirationTranscriptionDraft:
        mime_type = (content_type or "").split(";", 1)[0].strip().casefold()
        suffix = ALLOWED_AUDIO_TYPES.get(mime_type)
        if suffix is None:
            raise PipelineError(
                "INSPIRATION_AUDIO_TYPE_UNSUPPORTED",
                "灵感录音仅支持 WebM、Ogg、MP4 或 WAV 音频。",
            )

        request_id = uuid.uuid4().hex
        request_dir = Path(
            tempfile.mkdtemp(prefix=_REQUEST_PREFIX, dir=self.temp_root)
        )
        marker = request_dir / _OWNER_MARKER
        audio_path = request_dir / f"recording-{uuid.uuid4().hex}{suffix}"
        primary_error: BaseException | None = None
        with self._active_lock:
            self._active_request_ids.add(request_id)
        try:
            marker.write_text(
                json.dumps(
                    {
                        "instance_id": self.instance_id,
                        "request_id": request_id,
                    }
                ),
                encoding="utf-8",
            )
            size = 0
            with audio_path.open("wb") as target:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise PipelineError(
                            "INSPIRATION_AUDIO_TOO_LARGE",
                            "灵感录音不能超过 10 MiB。",
                        )
                    target.write(chunk)
            if size == 0:
                raise PipelineError(
                    "INSPIRATION_AUDIO_EMPTY",
                    "灵感录音为空，请重新录制。",
                )
            try:
                duration = self.audio_probe.probe(audio_path, mime_type)
            except PipelineError:
                raise
            except Exception as exc:
                raise PipelineError(
                    "INSPIRATION_AUDIO_PROBE_FAILED",
                    "无法安全确认录音格式与时长，请检查录音后重试。",
                ) from exc
            if duration > self.max_duration_seconds:
                raise PipelineError(
                    "INSPIRATION_AUDIO_TOO_LONG",
                    "灵感录音不能超过 120 秒。",
                )
            try:
                content = self.provider.transcribe(audio_path)
            except PipelineError:
                raise
            except Exception as exc:
                raise PipelineError(
                    "INSPIRATION_TRANSCRIPTION_FAILED",
                    "灵感语音转写失败，请改用文字记录或重新录制。",
                ) from exc
            try:
                return InspirationTranscriptionDraft(content=content)
            except ValueError as exc:
                raise PipelineError(
                    "INSPIRATION_TRANSCRIPTION_INVALID",
                    "灵感语音转写结果无效，请重新录制或改用文字记录。",
                ) from exc
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                shutil.rmtree(request_dir)
            except OSError:
                if self.warning_sink is not None:
                    try:
                        self.warning_sink(
                            "INSPIRATION_CLEANUP_FAILED",
                            "灵感录音临时目录清理失败，需要检查受控临时根。",
                        )
                    except Exception:
                        pass
                if primary_error is None:
                    raise PipelineError(
                        "INSPIRATION_CLEANUP_FAILED",
                        "灵感录音处理完成，但临时文件清理失败。",
                    )
            finally:
                with self._active_lock:
                    self._active_request_ids.discard(request_id)
