from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import struct
import subprocess
import time
import wave
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from backend.app.services.inspiration import (
    FfprobeInspirationAudioProbe,
    InspirationTranscriptionService,
)
from backend.app.services.pipeline import PipelineError
from backend.app.services.volcengine_inspiration import (
    VolcengineInspirationTranscriptionProvider,
)

TOOLS = Path(__file__).resolve().parents[2] / '.wanan' / 'asr-test-tools'


def wav_bytes(*, seconds=0.1, channels=1, rate=16000, width=2):
    output = io.BytesIO()
    with wave.open(output, 'wb') as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(b'\0' * round(seconds * rate) * channels * width)
    return output.getvalue()


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay=0):
        self.chunks, self.delay, self.closed = chunks, delay, False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


def make_provider(handler, **kwargs):
    return VolcengineInspirationTranscriptionProvider(
        app_id='synthetic-app', access_token='synthetic-secret',
        transport=httpx.MockTransport(handler), **kwargs,
    )


def success(text=' 灵感中文 🐈 '):
    return httpx.Response(200, headers={'X-Api-Status-Code': '20000000'},
                          content=json.dumps({'result': {'text': text}}).encode('utf-8'))


def convert_to(data):
    async def convert(_path):
        return data
    return convert


def test_fixed_request_contains_only_in_memory_audio_and_old_dual_headers():
    calls = []
    def handler(request):
        calls.append(request)
        return success()
    provider = make_provider(handler)
    with patch.object(provider, '_convert', convert_to(wav_bytes())):
        assert provider.transcribe(Path('synthetic.wav')) == '灵感中文 🐈'
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash'
    assert request.headers['X-Api-App-Key'] == 'synthetic-app'
    assert request.headers['X-Api-Access-Key'] == 'synthetic-secret'
    assert request.headers['X-Api-Resource-Id'] == 'volc.bigasr.auc_turbo'
    assert request.headers['X-Api-Sequence'] == '-1'
    assert 'cookie' not in request.headers and 'authorization' not in request.headers
    body = json.loads(request.content)
    assert set(body) == {'user', 'audio', 'request'}
    assert set(body['audio']) == {'data'}
    decoded = base64.b64decode(body['audio']['data'], validate=True)
    with wave.open(io.BytesIO(decoded)) as reader:
        assert (reader.getnchannels(), reader.getframerate(), reader.getsampwidth()) == (1,16000,2)
        assert reader.getnframes() == 1600
    assert 'synthetic-secret' not in repr(provider)


@pytest.mark.parametrize('response', [
    httpx.Response(403), httpx.Response(429), httpx.Response(500),
    httpx.Response(302, headers={'location':'https://example.com'}),
    httpx.Response(200, json={'result':{'text':'private-provider-text'}}),
    httpx.Response(200, headers={'X-Api-Status-Code':'45000030'}, json={'result':{'text':'private-provider-text'}}),
    httpx.Response(200, headers={'X-Api-Status-Code':'20000000'}, content=b'invalid-private-json'),
    httpx.Response(200, headers={'X-Api-Status-Code':'20000000'}, json=[]),
    httpx.Response(200, headers={'X-Api-Status-Code':'20000000'}, json={'result':[]}),
    success(''), success('   '), success(42), success(None), success('x' * 4001),
    success('\ud800'),
])
def test_failures_are_fixed_redacted_and_never_retried(response):
    calls=[]
    def handler(request):
        calls.append(request)
        return response
    provider=make_provider(handler)
    with patch.object(provider, '_convert', convert_to(wav_bytes())):
        with pytest.raises(PipelineError) as exc:
            provider.transcribe(Path('private-recording.wav'))
    assert len(calls)==1
    rendered=str(exc.value)
    assert all(secret not in rendered for secret in ('synthetic-secret','private-provider','private-recording','invalid-private'))
    assert exc.value.__cause__ is None


def test_stream_response_size_is_bounded_and_closed():
    stream=Chunks([b'x' * 32768] * 3)
    provider=make_provider(lambda _: httpx.Response(200, headers={'X-Api-Status-Code':'20000000'}, stream=stream))
    with patch.object(provider, '_convert', convert_to(wav_bytes())):
        with pytest.raises(PipelineError):
            provider.transcribe(Path('synthetic.wav'))
    assert stream.closed


def test_total_deadline_interrupts_trickle_and_closes_response(monkeypatch):
    import backend.app.services.volcengine_inspiration as module
    monkeypatch.setattr(module, 'REQUEST_DEADLINE_SECONDS', 0.05)
    stream=Chunks([b' '] * 50, delay=0.02)
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={'X-Api-Status-Code':'20000000'}, stream=stream)
    provider=make_provider(handler)
    start=time.monotonic()
    with patch.object(provider, '_convert', convert_to(wav_bytes())):
        with pytest.raises(PipelineError) as exc:
            provider.transcribe(Path('synthetic.wav'))
    assert exc.value.code=='INSPIRATION_TRANSCRIPTION_TIMEOUT'
    assert time.monotonic()-start < 1
    assert len(calls)==1 and stream.closed


def test_sensitive_transport_debug_logs_suppressed_only_inside_request(caplog):
    def handler(request):
        logging.getLogger('httpcore.http11').debug('headers %s', dict(request.headers))
        logging.getLogger('httpx').info('private-transcription')
        raise httpx.ReadError('private-secret-exception')
    provider=make_provider(handler)
    with caplog.at_level(logging.DEBUG):
        with patch.object(provider, '_convert', convert_to(wav_bytes())):
            with pytest.raises(PipelineError) as exc:
                provider.transcribe(Path('synthetic.wav'))
        logging.getLogger('httpx').info('unrelated-after-request')
    assert 'synthetic-secret' not in caplog.text
    assert 'private-transcription' not in caplog.text
    assert 'unrelated-after-request' in caplog.text
    assert exc.value.__cause__ is None


def test_asyncio_debug_recording_path_suppressed_and_context_restored(caplog):
    async def convert(path):
        logging.getLogger('asyncio').debug('spawn %s',path)
        return wav_bytes()
    provider=make_provider(lambda _: success())
    with caplog.at_level(logging.DEBUG):
        with patch.object(provider,'_convert',convert):
            provider.transcribe(Path('private-recording-canary.wav'))
        logging.getLogger('asyncio').debug('unrelated-subprocess-canary')
    assert 'private-recording-canary' not in caplog.text
    assert 'unrelated-subprocess-canary' in caplog.text


def test_packet_count_sentinel_cannot_be_mistaken_for_eof():
    payloads=[{'format':{'format_name':'webm'},'streams':[{'codec_type':'audio'}]},
              {'packets':[{'pts_time':'0','duration_time':'0.001'}]*60001}]
    def runner(args,**kwargs):
        return subprocess.CompletedProcess(args,0,stdout=json.dumps(payloads.pop(0)))
    with pytest.raises(PipelineError):
        FfprobeInspirationAudioProbe(runner=runner).probe(Path('synthetic.webm'),'audio/webm')


def test_probe_timeout_does_not_chain_private_command_or_output():
    def runner(*args,**kwargs):
        raise subprocess.TimeoutExpired('private-path-canary',5,output='private-output-canary')
    with pytest.raises(PipelineError) as exc:
        FfprobeInspirationAudioProbe(runner=runner).probe(Path('synthetic.wav'),'audio/wav')
    assert exc.value.__cause__ is None


@pytest.mark.parametrize('data', [b'not-wave', wav_bytes(channels=2), wav_bytes(rate=48000),
    wav_bytes(width=1), wav_bytes(seconds=0), wav_bytes(seconds=120.001),
    wav_bytes()[:-1]], ids=['non-wave','stereo','rate','sample-width','empty','overlong','truncated'])
def test_invalid_converted_audio_is_rejected_before_any_request(data):
    calls=[]
    provider=make_provider(lambda req: calls.append(req) or success())
    with patch.object(provider, '_convert', convert_to(data)):
        with pytest.raises(PipelineError):
            provider.transcribe(Path('synthetic.wav'))
    assert not calls


def test_streamed_ffmpeg_wave_lengths_are_normalized():
    data=bytearray(wav_bytes())
    struct.pack_into('<I', data, 4, 0xffffffff)
    struct.pack_into('<I', data, 40, 0xffffffff)
    requests=[]
    provider=make_provider(lambda req: requests.append(req) or success())
    with patch.object(provider, '_convert', convert_to(bytes(data))):
        provider.transcribe(Path('synthetic.wav'))
    body=json.loads(requests[0].content)
    output=base64.b64decode(body['audio']['data'])
    assert struct.unpack_from('<I', output, 4)[0]==len(output)-8
    assert struct.unpack_from('<I', output, 40)[0]==3200


@pytest.mark.parametrize('streams', [[], [{'codec_type':'video'}], [{'codec_type':'audio'}, {'codec_type':'video'}]])
def test_probe_rejects_non_audio_or_mixed_streams(streams):
    payload={'format':{'format_name':'webm','duration':'1'}, 'streams': streams}
    runner=lambda *a,**k: subprocess.CompletedProcess(a[0],0,stdout=json.dumps(payload))
    with pytest.raises(PipelineError):
        FfprobeInspirationAudioProbe(runner=runner).probe(Path('synthetic.webm'),'audio/webm')


def test_missing_browser_webm_duration_requires_finite_bounded_packet_proof():
    calls=[]
    payloads=[{'format':{'format_name':'matroska,webm'}, 'streams':[{'codec_type':'audio'}]},
              {'packets':[{'pts_time':'0', 'duration_time':'0.02'}, {'pts_time':'0.02','duration_time':'0.02'}]}]
    def runner(args,**kwargs):
        calls.append((args,kwargs))
        return subprocess.CompletedProcess(args,0,stdout=json.dumps(payloads.pop(0)))
    assert FfprobeInspirationAudioProbe(runner=runner).probe(Path('synthetic.webm'),'audio/webm')==0.04
    assert len(calls)==2
    assert '-read_intervals' in calls[1][0]
    for args, kwargs in calls:
        assert args[args.index('-protocol_whitelist')+1]=='file,pipe'
        assert kwargs['shell'] is False and kwargs['timeout']<=5


@pytest.mark.parametrize('packets', [[], [{'pts_time':'nan','duration_time':'1'}],
    [{'pts_time':'0','duration_time':'inf'}], [{'pts_time':'0'}],
    [{'pts_time':'0','duration_time':'121'}]])
def test_missing_duration_uncertain_packet_proof_rejected(packets):
    payloads=[{'format':{'format_name':'webm'}, 'streams':[{'codec_type':'audio'}]}, {'packets':packets}]
    def runner(args,**kwargs):
        return subprocess.CompletedProcess(args,0,stdout=json.dumps(payloads.pop(0)))
    with pytest.raises(PipelineError):
        FfprobeInspirationAudioProbe(runner=runner).probe(Path('synthetic.webm'),'audio/webm')


def test_real_synthetic_wave_probe_convert_mock_request_and_cleanup(tmp_path):
    ffmpeg, ffprobe = TOOLS/'ffmpeg.exe', TOOLS/'ffprobe.exe'
    if not ffmpeg.is_file() or not ffprobe.is_file():
        pytest.skip('controlled local ffmpeg/ffprobe not installed')
    calls=[]
    provider=make_provider(lambda request: calls.append(request) or success(), ffmpeg_path=ffmpeg)
    root=tmp_path/'recordings'
    service=InspirationTranscriptionService(provider,FfprobeInspirationAudioProbe(ffprobe),root)
    draft=service.transcribe(io.BytesIO(wav_bytes(channels=2,rate=48000)), 'audio/wav')
    assert draft.content=='灵感中文 🐈' and draft.transcription_status=='draft'
    assert draft.input_mode=='voice' and len(calls)==1
    assert not list(root.iterdir())


@pytest.mark.parametrize('format_name,codec,mime,extra', [
    ('webm', 'libopus', 'audio/webm', []),
    ('ogg', 'libopus', 'audio/ogg', []),
    ('mp4', 'aac', 'audio/mp4', ['-movflags','frag_keyframe+empty_moov']),
])
def test_real_synthetic_browser_containers_are_accepted_and_cleaned(tmp_path, format_name,codec,mime,extra):
    ffmpeg, ffprobe=TOOLS/'ffmpeg.exe', TOOLS/'ffprobe.exe'
    if not ffmpeg.is_file() or not ffprobe.is_file():
        pytest.skip('controlled local ffmpeg/ffprobe not installed')
    generated=subprocess.run(
        [str(ffmpeg),'-hide_banner','-loglevel','error','-nostdin',
         '-protocol_whitelist','file,pipe','-f','wav','-i','pipe:0',
         '-ac','1','-c:a',codec,*extra,'-f',format_name,'pipe:1'],
        input=wav_bytes(seconds=0.12),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
        check=True,shell=False,timeout=10,
    ).stdout
    calls=[]
    provider=make_provider(lambda request: calls.append(request) or success(),ffmpeg_path=ffmpeg)
    root=tmp_path/'recordings'
    service=InspirationTranscriptionService(provider,FfprobeInspirationAudioProbe(ffprobe),root)
    assert service.transcribe(io.BytesIO(generated),mime).content=='灵感中文 🐈'
    assert len(calls)==1 and not list(root.iterdir())


@pytest.mark.parametrize('mode', ['oversize','timeout','nonzero'])
def test_bounded_converter_kills_failed_writer_before_cleanup_and_zero_requests(monkeypatch,tmp_path,mode):
    import backend.app.services.volcengine_inspiration as module
    calls=[]
    command=[]
    class FakeProcess:
        returncode=None
        killed=False
        reads=0
        stdout=None
        def __init__(self):
            self.stdout=self
        async def read(self,size):
            self.reads+=1
            if self.killed or mode=='nonzero':
                return b''
            if mode=='timeout':
                await asyncio.sleep(10)
            return b'x'*size
        async def wait(self):
            self.returncode=1
            return 1
        def kill(self):
            self.killed=True
    process=FakeProcess()
    async def spawn(*args,**kwargs):
        command.append((args,kwargs))
        return process
    monkeypatch.setattr(module.asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr(module,'CONVERSION_DEADLINE_SECONDS',0.03)
    provider=make_provider(lambda req: calls.append(req) or success())
    class Probe:
        def probe(self,*args): return 0.1
    root=tmp_path/'recordings'
    service=InspirationTranscriptionService(provider,Probe(),root)
    start=time.monotonic()
    with pytest.raises(PipelineError):
        service.transcribe(io.BytesIO(wav_bytes()),'audio/wav')
    assert time.monotonic()-start < 1
    assert not calls and not list(root.iterdir())
    if mode!='nonzero':
        assert process.killed
    assert process.reads <= 62
    args, kwargs=command[0]
    assert args[args.index('-protocol_whitelist')+1]=='file,pipe'
    assert '-t' not in args and '-fs' not in args
    assert kwargs['shell'] is False and kwargs['stderr']==asyncio.subprocess.DEVNULL


@pytest.mark.parametrize('exception', [httpx.ConnectTimeout('private'),httpx.ReadTimeout('private')])
def test_http_timeouts_are_fixed_and_single_attempt(exception):
    calls=[]
    def handler(request):
        calls.append(request)
        raise exception
    provider=make_provider(handler)
    with patch.object(provider,'_convert',convert_to(wav_bytes())):
        with pytest.raises(PipelineError) as exc:
            provider.transcribe(Path('synthetic.wav'))
    assert len(calls)==1 and exc.value.code=='INSPIRATION_TRANSCRIPTION_TIMEOUT'
    assert exc.value.__cause__ is None and 'private' not in str(exc.value)


def test_http_client_has_no_proxy_redirect_cookie_inheritance():
    actual=httpx.AsyncClient
    constructed=[]
    def factory(**kwargs):
        constructed.append(kwargs)
        return actual(**kwargs)
    provider=make_provider(lambda _: success())
    with patch('backend.app.services.volcengine_inspiration.httpx.AsyncClient',factory):
        with patch.object(provider,'_convert',convert_to(wav_bytes())):
            provider.transcribe(Path('synthetic.wav'))
    assert len(constructed)==1
    options=constructed[0]
    assert options['follow_redirects'] is False and options['trust_env'] is False
    assert options['timeout'].connect==5 and options['timeout'].read==30
    assert 'cookies' not in options


@pytest.mark.parametrize('raw', [b'forged-audio', wav_bytes(seconds=120.1)],ids=['forged','overlong'])
def test_real_probe_rejects_forged_or_overlong_audio_and_cleans(tmp_path,raw):
    ffprobe=TOOLS/'ffprobe.exe'
    if not ffprobe.is_file():
        pytest.skip('controlled local ffprobe not installed')
    calls=[]
    provider=make_provider(lambda request: calls.append(request) or success())
    root=tmp_path/'recordings'
    service=InspirationTranscriptionService(provider,FfprobeInspirationAudioProbe(ffprobe),root)
    with pytest.raises(PipelineError):
        service.transcribe(io.BytesIO(raw),'audio/wav')
    assert not calls and not list(root.iterdir())
