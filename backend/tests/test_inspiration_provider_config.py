from pathlib import Path
from unittest.mock import patch

import pytest

from backend.app.runtime_config import RuntimeConfigError, inspect_runtime_config, resolve_runtime_config

ROOT = Path(__file__).resolve().parents[2]
FLASH = {'INSPIRATION_ASR_PROVIDER': 'volcengine_flash',
         'INSPIRATION_VOLC_APP_ID': '12345678', 'INSPIRATION_VOLC_ACCESS_TOKEN': 'synthetic-token'}


def test_explicit_flash_config_is_offline_and_redacted():
    with patch('socket.socket', side_effect=AssertionError('network forbidden')):
        config = resolve_runtime_config(FLASH, ROOT)
        report = inspect_runtime_config(FLASH, ROOT)
    assert config.inspiration_asr_provider == 'volcengine_flash'
    assert config.inspiration_volc_app_id == '12345678'
    assert config.inspiration_volc_access_token == 'synthetic-token'
    assert config.inspiration_ffmpeg_path == 'ffmpeg'
    for secret in ('12345678', 'synthetic-token'):
        assert secret not in repr(config) + str(report)
    assert not any(i['code'] == 'WARN_PROVIDER_UNCONFIGURED' and i['field'] == 'INSPIRATION_ASR_ENDPOINT' for i in report['issues'])


@pytest.mark.parametrize('extra', [
    {'INSPIRATION_VOLC_APP_ID': ''}, {'INSPIRATION_VOLC_ACCESS_TOKEN': ''},
    {'INSPIRATION_VOLC_APP_ID': 'bad\nvalue'}, {'INSPIRATION_VOLC_ACCESS_TOKEN': 'bad\rvalue'},
    {'INSPIRATION_ASR_ENDPOINT': 'https://example.com'}, {'INSPIRATION_ASR_TOKEN': 'old'},
    {'INSPIRATION_ASR_MODEL': 'old'}, {'INSPIRATION_ASR_PROVIDER': 'unknown'},
])
def test_missing_unsafe_or_mixed_flash_config_rejected(extra):
    with pytest.raises(RuntimeConfigError) as exc:
        resolve_runtime_config(FLASH | extra, ROOT)
    assert 'synthetic-token' not in str(exc.value)


def test_default_generic_compatibility_and_no_implicit_flash_activation():
    from backend.app.bootstrap import build_inspiration_transcription_provider
    from backend.app.services.inspiration import HttpInspirationTranscriptionProvider, UnconfiguredInspirationTranscriptionProvider
    config = resolve_runtime_config({'INSPIRATION_VOLC_APP_ID': '1234', 'INSPIRATION_VOLC_ACCESS_TOKEN': 'unused'}, ROOT)
    assert isinstance(build_inspiration_transcription_provider(config), UnconfiguredInspirationTranscriptionProvider)
    config = resolve_runtime_config({'INSPIRATION_ASR_ENDPOINT': 'https://example.com', 'INSPIRATION_ASR_TOKEN': 'legacy'}, ROOT)
    provider = build_inspiration_transcription_provider(config)
    assert isinstance(provider, HttpInspirationTranscriptionProvider)
    assert provider.token == 'legacy'
    provider.client.close()


def test_flash_builder_uses_explicit_credentials_and_tool_path():
    from backend.app.bootstrap import build_inspiration_transcription_provider
    config = resolve_runtime_config(FLASH | {'INSPIRATION_FFMPEG_PATH': 'tools/ffmpeg.exe'}, ROOT)
    with patch('backend.app.bootstrap.VolcengineInspirationTranscriptionProvider') as factory:
        build_inspiration_transcription_provider(config)
    factory.assert_called_once_with(app_id='12345678', access_token='synthetic-token', ffmpeg_path=str(ROOT / 'tools/ffmpeg.exe'))
