"""ASR-ERR1: synthetic audio/credentials and offline transport only."""
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from backend.app.services.pipeline import PipelineError
from backend.tests.test_volcengine_inspiration import Chunks, make_provider, wav_bytes, success
from backend.tests.test_inspiration_web_integration import _client, _wav, _count


@pytest.mark.parametrize('status,business,code', [
    (401, None, 'INSPIRATION_TRANSCRIPTION_ACCESS_DENIED'),
    (403, '45000030', 'INSPIRATION_TRANSCRIPTION_ACCESS_DENIED'),
    (429, None, 'INSPIRATION_TRANSCRIPTION_RATE_LIMITED'),
    (500, None, 'INSPIRATION_TRANSCRIPTION_UNAVAILABLE'),
    (503, '55000001', 'INSPIRATION_TRANSCRIPTION_UNAVAILABLE'),
    (200, '45000030', 'INSPIRATION_TRANSCRIPTION_REJECTED'),
    (200, None, 'INSPIRATION_TRANSCRIPTION_REJECTED'),
    (302, None, 'INSPIRATION_TRANSCRIPTION_REJECTED'),
])
def test_status_feedback_is_bounded_redacted_and_closes_without_reading(status, business, code):
    calls = []
    stream = Chunks([b'private-upstream synthetic-secret'])
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={'X-Api-Status-Code': business} if business else {}, stream=stream)
    provider = make_provider(handler)
    with patch.object(provider, '_convert', AsyncMock(return_value=wav_bytes())):
        with pytest.raises(PipelineError) as error:
            provider.transcribe(Path('private-recording.wav'))
    assert error.value.code == code
    assert f'HTTP {status}' in str(error.value)
    if business:
        assert business in str(error.value)
    assert len(calls) == 1 and stream.closed
    assert all(value not in str(error.value) for value in ('private-upstream', 'synthetic-secret', 'private-recording'))
    assert error.value.__cause__ is None


@pytest.mark.parametrize('business', [
    'synthetic-secret', '45000030private', '4500003', '450000300',
    '45000030\nprivate', '45000030\x00', ' 45000030', '+4500030',
])
def test_arbitrary_status_header_never_reaches_feedback(business):
    provider = make_provider(lambda _: httpx.Response(200, headers={'X-Api-Status-Code': business}))
    with patch.object(provider, '_convert', AsyncMock(return_value=wav_bytes())):
        with pytest.raises(PipelineError) as error:
            provider.transcribe(Path('private-recording.wav'))
    assert error.value.code == 'INSPIRATION_TRANSCRIPTION_REJECTED'
    assert 'HTTP 200' in str(error.value)
    assert '业务码' not in str(error.value)
    assert business not in str(error.value)


@pytest.mark.parametrize('exception,code', [
    (httpx.ConnectError, 'INSPIRATION_TRANSCRIPTION_NETWORK_ERROR'),
    (httpx.ReadError, 'INSPIRATION_TRANSCRIPTION_NETWORK_ERROR'),
    (httpx.ConnectTimeout, 'INSPIRATION_TRANSCRIPTION_TIMEOUT'),
    (httpx.ReadTimeout, 'INSPIRATION_TRANSCRIPTION_TIMEOUT'),
])
def test_transport_failure_feedback_never_exposes_exception(exception, code):
    calls = []
    def handler(request):
        calls.append(request)
        raise exception('private-path synthetic-secret')
    provider = make_provider(handler)
    with patch.object(provider, '_convert', AsyncMock(return_value=wav_bytes())):
        with pytest.raises(PipelineError) as error:
            provider.transcribe(Path('private-recording.wav'))
    assert error.value.code == code
    assert len(calls) == 1 and error.value.__cause__ is None
    assert 'private' not in str(error.value) and 'synthetic-secret' not in str(error.value)


@pytest.mark.parametrize('text', ['', ' \n\t '])
def test_empty_success_is_not_claimed_as_credentials_failure(text):
    provider = make_provider(lambda _: success(text))
    with patch.object(provider, '_convert', AsyncMock(return_value=wav_bytes())):
        with pytest.raises(PipelineError) as error:
            provider.transcribe(Path('synthetic.wav'))
    assert error.value.code == 'INSPIRATION_TRANSCRIPTION_NO_TEXT'
    assert '未识别到' in str(error.value)


@pytest.mark.parametrize('status,business,expected', [
    (403, '45000030', 'INSPIRATION_TRANSCRIPTION_ACCESS_DENIED'),
    (429, None, 'INSPIRATION_TRANSCRIPTION_RATE_LIMITED'),
    (200, '45000030', 'INSPIRATION_TRANSCRIPTION_REJECTED'),
])
def test_api_returns_safe_feedback_and_text_bookmark_remains_available(tmp_path, status, business, expected):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={'X-Api-Status-Code': business} if business else {}, text='private synthetic-token')
    client, provider, deep = _client(tmp_path, handler)
    with client, patch.object(provider, '_convert', AsyncMock(return_value=_wav())):
        response = client.post('/api/v1/inspiration-transcriptions',
                               files={'file': ('recording.wav', _wav(), 'audio/wav')})
        assert response.status_code == 400
        assert response.json()['error']['code'] == expected
        assert 'private' not in response.text and 'synthetic-token' not in response.text
        assert list((tmp_path / 'recordings').iterdir()) == []
        preview = client.post('/api/v1/collection-previews', json={'input_text': 'https://public.example/offline'})
        saved = client.post('/api/v1/collection-items', headers={'Idempotency-Key': 'after-failure'},
                            json={'preview_id': preview.json()['preview_id'], 'user_title': '文字收藏不受影响'})
        assert saved.status_code == 201 and saved.json()['user_title'] == '文字收藏不受影响'
    assert len(calls) == 1 and deep.calls == 0
    assert _count(tmp_path, 'jobs') == _count(tmp_path, 'transcript_segments') == 0
