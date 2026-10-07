"""Explicit, bounded flash ASR for the user's own validated recording only."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import struct
import uuid
import wave
from pathlib import Path

import httpx

from .pipeline import PipelineError
from .safe_http import (
    _PUBLIC_FETCH_ACTIVE,
    _PUBLIC_FETCH_LOG_FILTER,
    _TRANSPORT_LOGGERS,
)


FLASH_ENDPOINT = 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash'
FLASH_RESOURCE = 'volc.bigasr.auc_turbo'
REQUEST_DEADLINE_SECONDS = 60
CONVERSION_DEADLINE_SECONDS = 30
MAX_RESPONSE_BYTES = 64 * 1024
MAX_PCM_BYTES = 120 * 16000 * 2
# ffmpeg may include a small LIST/INFO chunk. This is a hard capture bound,
# separate from the exact PCM duration check below; overrun is never submitted.
MAX_WAV_BYTES = MAX_PCM_BYTES + 4096


def _failure(code: str = 'INSPIRATION_TRANSCRIPTION_FAILED') -> PipelineError:
    messages = {
        'INSPIRATION_TRANSCRIPTION_FAILED': '灵感语音转写失败，请改用文字记录或重新录制。',
        'INSPIRATION_TRANSCRIPTION_TIMEOUT': '灵感语音转写超时，请改用文字记录或重新录制。',
        'INSPIRATION_TRANSCRIPTION_ACCESS_DENIED': '语音服务访问被拒，请维护者核对鉴权及识别资源权限；可先用文字记录。',
        'INSPIRATION_TRANSCRIPTION_RATE_LIMITED': '语音服务请求过于频繁，请稍后再试；可先用文字记录。',
        'INSPIRATION_TRANSCRIPTION_UNAVAILABLE': '语音服务暂时不可用，请稍后再试；可先用文字记录。',
        'INSPIRATION_TRANSCRIPTION_REJECTED': '语音服务未返回有效识别结果，请将错误码提供给维护者；可先用文字记录。',
        'INSPIRATION_TRANSCRIPTION_NETWORK_ERROR': '后端连接语音服务失败，请维护者检查网络；可先用文字记录。',
        'INSPIRATION_TRANSCRIPTION_NO_TEXT': '这段录音未识别到文字，请确认麦克风能收到声音后重新录制，或改用文字记录。',
        'INSPIRATION_AUDIO_INVALID': '无法安全确认转换后的录音，请重新录制或改用文字记录。',
        'INSPIRATION_AUDIO_TOO_LONG': '灵感录音不能超过 120 秒。',
    }
    return PipelineError(code, messages[code])


def _response_failure(status: int, business: str | None) -> PipelineError:
    # Classify only fixed protocol values; never use upstream body/message text.
    code = 'INSPIRATION_TRANSCRIPTION_REJECTED'
    if status in (401, 403):
        code = 'INSPIRATION_TRANSCRIPTION_ACCESS_DENIED'
    elif status == 429:
        code = 'INSPIRATION_TRANSCRIPTION_RATE_LIMITED'
    elif 500 <= status <= 599:
        code = 'INSPIRATION_TRANSCRIPTION_UNAVAILABLE'
    diagnostic = f'HTTP {status}'
    if business is not None and len(business) == 8 and all('0' <= char <= '9' for char in business):
        diagnostic += f'，业务码 {business}'
    return PipelineError(code, f'{_failure(code)}（{diagnostic}）')


def _validated_wave(data: bytes) -> bytes:
    """Check actual PCM extent, including ffmpeg's non-seekable WAV header.

    Streaming RIFF/data lengths can be UINT32_MAX. Only those two sentinel
    lengths are accepted, and a fresh canonical header binds the actual bytes.
    No duration supplied by a client or by an untrusted WAV header is used.
    """
    if len(data) < 44 or len(data) > MAX_WAV_BYTES:
        raise _failure('INSPIRATION_AUDIO_INVALID')
    if data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise _failure('INSPIRATION_AUDIO_INVALID')
    if struct.unpack_from('<I', data, 4)[0] not in (len(data)-8, 0xffffffff):
        raise _failure('INSPIRATION_AUDIO_INVALID')
    offset, audio, format_seen = 12, None, False
    while offset < len(data):
        if offset + 8 > len(data):
            raise _failure('INSPIRATION_AUDIO_INVALID')
        kind, size = struct.unpack_from('<4sI', data, offset)
        offset += 8
        if kind == b'data' and size == 0xffffffff:
            size = len(data)-offset
        if offset+size > len(data):
            raise _failure('INSPIRATION_AUDIO_INVALID')
        chunk = data[offset:offset+size]
        if kind == b'fmt ':
            if format_seen or size not in (16, 18):
                raise _failure('INSPIRATION_AUDIO_INVALID')
            if struct.unpack_from('<HHIIHH', chunk) != (1, 1, 16000, 32000, 2, 16):
                raise _failure('INSPIRATION_AUDIO_INVALID')
            if size == 18 and chunk[16:] != b'\0\0':
                raise _failure('INSPIRATION_AUDIO_INVALID')
            format_seen = True
        elif kind == b'data':
            if not format_seen or audio is not None or not size or size % 2:
                raise _failure('INSPIRATION_AUDIO_INVALID')
            if size > MAX_PCM_BYTES:
                raise _failure('INSPIRATION_AUDIO_TOO_LONG')
            audio = chunk
        elif kind not in (b'LIST', b'JUNK'):
            raise _failure('INSPIRATION_AUDIO_INVALID')
        offset += size + (size % 2)
    if offset != len(data) or audio is None:
        raise _failure('INSPIRATION_AUDIO_INVALID')
    output = io.BytesIO()
    with wave.open(output, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(audio)
    return output.getvalue()


class VolcengineInspirationTranscriptionProvider:
    """No file-based credentials, source-media acquisition, retries or storage.

    Called by InspirationTranscriptionService after its container/duration probe.
    The optional transport is an offline test seam, never runtime configuration.
    """

    def __init__(
        self, *, app_id: str, access_token: str,
        ffmpeg_path: str | Path = 'ffmpeg',
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        for value in (app_id, access_token):
            if not isinstance(value, str) or not value or any(
                ord(character) < 33 or ord(character) > 126 for character in value
            ):
                raise ValueError('Invalid inspiration provider credentials')
        self._app_id = app_id
        self._access_token = access_token
        self._ffmpeg_path = str(ffmpeg_path)
        self._transport = transport

    def transcribe(self, audio_path: Path) -> str:
        # Asyncio debug subprocess diagnostics can include the recording path.
        # Suppress them in this context as well as HTTP header diagnostics.
        for name in (*_TRANSPORT_LOGGERS, 'asyncio'):
            logging.getLogger(name).addFilter(_PUBLIC_FETCH_LOG_FILTER)
        token = _PUBLIC_FETCH_ACTIVE.set(True)
        try:
            return asyncio.run(self._transcribe(audio_path))
        except PipelineError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise _failure('INSPIRATION_TRANSCRIPTION_TIMEOUT') from None
        except httpx.TransportError:
            raise _failure('INSPIRATION_TRANSCRIPTION_NETWORK_ERROR') from None
        except Exception:
            # Never attach transport exceptions: they may contain credentials,
            # response headers, transcript text or the private temporary path.
            raise _failure() from None
        finally:
            _PUBLIC_FETCH_ACTIVE.reset(token)

    async def _convert(self, audio_path: Path) -> bytes:
        process = None
        try:
            async with asyncio.timeout(CONVERSION_DEADLINE_SECONDS):
                process = await asyncio.create_subprocess_exec(
                    self._ffmpeg_path, '-hide_banner', '-loglevel', 'error',
                    '-nostdin', '-protocol_whitelist', 'file,pipe',
                    '-format_whitelist', 'matroska,webm,ogg,mov,wav',
                    '-i', str(audio_path.resolve()),
                    '-map', '0:a:0', '-vn', '-sn', '-dn',
                    '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le',
                    '-f', 'wav', 'pipe:1',
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    shell=False,
                    limit=64 * 1024,
                )
                output = bytearray()
                while chunk := await process.stdout.read(
                    min(64 * 1024, MAX_WAV_BYTES + 1 - len(output))
                ):
                    output.extend(chunk)
                    if len(output) > MAX_WAV_BYTES:
                        raise _failure('INSPIRATION_AUDIO_TOO_LONG')
                if await process.wait() != 0:
                    raise _failure('INSPIRATION_AUDIO_INVALID')
                return bytes(output)
        except PipelineError:
            raise
        except TimeoutError:
            raise _failure('INSPIRATION_TRANSCRIPTION_TIMEOUT') from None
        except Exception:
            raise _failure('INSPIRATION_AUDIO_INVALID') from None
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                # Killing a blocked writer must also drain its bounded pipe so
                # asyncio can close the subprocess transport on Windows.
                async with asyncio.timeout(5):
                    while await process.stdout.read(64 * 1024):
                        pass
                    await process.wait()

    async def _transcribe(self, audio_path: Path) -> str:
        wav = _validated_wave(await self._convert(audio_path))
        payload = {
            'user': {'uid': uuid.uuid4().hex},
            'audio': {'data': base64.b64encode(wav).decode('ascii')},
            'request': {'model_name': 'bigmodel'},
        }
        headers = {
            'X-Api-App-Key': self._app_id,
            'X-Api-Access-Key': self._access_token,
            'X-Api-Resource-Id': FLASH_RESOURCE,
            'X-Api-Request-Id': str(uuid.uuid4()),
            'X-Api-Sequence': '-1',
            'Accept-Encoding': 'identity',
        }
        # Reuse the existing per-context suppression, including header-bearing
        # httpcore DEBUG. Unrelated requests and logger levels remain unchanged.
        for name in _TRANSPORT_LOGGERS:
            logging.getLogger(name).addFilter(_PUBLIC_FETCH_LOG_FILTER)
        token = _PUBLIC_FETCH_ACTIVE.set(True)
        try:
            async with asyncio.timeout(REQUEST_DEADLINE_SECONDS):
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(30, connect=5),
                    follow_redirects=False, trust_env=False,
                    transport=self._transport,
                ) as client:
                    async with client.stream(
                        'POST', FLASH_ENDPOINT, headers=headers, json=payload,
                    ) as response:
                        business = response.headers.get('X-Api-Status-Code')
                        if response.status_code != 200 or business != '20000000':
                            raise _response_failure(response.status_code, business)
                        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                            raise _failure()
                        declared_size = response.headers.get('content-length')
                        if declared_size is not None and (
                            not declared_size.isdecimal() or int(declared_size) > MAX_RESPONSE_BYTES
                        ):
                            raise _failure()
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise _failure()
                            body.extend(chunk)
                        result = json.loads(body)
                        value = result.get('result', {}).get('text')
                        if not isinstance(value, str):
                            raise _failure()
                        text = value.strip()
                        if not text:
                            raise _failure('INSPIRATION_TRANSCRIPTION_NO_TEXT')
                        if len(text) > 4000:
                            raise _failure()
                        # Reject invalid Unicode (e.g. a JSON-escaped surrogate)
                        # before FastAPI encodes a successful draft response.
                        text.encode('utf-8', errors='strict')
                        return text
        finally:
            _PUBLIC_FETCH_ACTIVE.reset(token)
