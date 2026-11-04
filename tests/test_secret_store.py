"""All credential tests use synthetic values and a temporary vault, never the user's key."""
import os

import pytest

import agent
import config
import secret_store as store
from runtime import RequestFailure

KEY = 'synthetic-order-agent-test-key'
windows = pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI required')


@windows
def test_dpapi_roundtrip_and_tamper():
    blob = store.encrypt_key(KEY)
    assert KEY.encode() not in blob
    assert store.decrypt_key(blob) == KEY
    damaged = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(store.SecretStoreError):
        store.decrypt_key(damaged)


@windows
def test_project_scope_is_distinct(monkeypatch):
    blob = store.encrypt_key(KEY)
    monkeypatch.setattr(store, 'ENTROPY', b'AnotherProject.DeepSeek.v1')
    with pytest.raises(store.SecretStoreError):
        store.decrypt_key(blob)


@windows
def test_migrate_duplicates_comments_and_preserve_configuration(tmp_path):
    env = tmp_path / '.env'
    old = 'synthetic-previous-key'
    env.write_text(f'export DEEPSEEK_API_KEY="{old}"\nDEEPSEEK_API_KEY={KEY}\n'
                   f'# {KEY} / {old}\nDEEPSEEK_MODEL=my-model\nOTHER=value\n', encoding='utf-8')
    path = store.configure_encrypted_key(KEY, env)
    assert store.read_deepseek_key() == KEY
    assert path == store.secret_path()
    cleaned = env.read_text(encoding='utf-8')
    assert KEY not in cleaned and old not in cleaned
    assert cleaned.count('DEEPSEEK_API_KEY=') == 1
    assert 'DEEPSEEK_KEY_STORAGE=windows_dpapi' in cleaned
    assert 'DEEPSEEK_MODEL=my-model' in cleaned and 'OTHER=value' in cleaned
    assert KEY.encode() not in path.read_bytes()


@windows
def test_failed_cipher_write_keeps_plaintext_configuration(tmp_path, monkeypatch):
    env = tmp_path / '.env'
    original = f'DEEPSEEK_API_KEY={KEY}\n'
    env.write_text(original, encoding='utf-8')

    def fail(*args):
        raise store.SecretStoreError('synthetic-write-failure')

    monkeypatch.setattr(store, '_atomic_write', fail)
    with pytest.raises(store.SecretStoreError):
        store.configure_encrypted_key(KEY, env)
    assert env.read_text(encoding='utf-8') == original


@windows
def test_failed_readback_does_not_clear_plaintext(tmp_path, monkeypatch):
    env = tmp_path / '.env'
    original = f'DEEPSEEK_API_KEY={KEY}\n'
    env.write_text(original, encoding='utf-8')
    monkeypatch.setattr(store, 'read_deepseek_key', lambda: 'wrong-synthetic-key')
    with pytest.raises(store.SecretStoreError):
        store.configure_encrypted_key(KEY, env)
    assert env.read_text(encoding='utf-8') == original


@windows
def test_config_uses_encrypted_key_and_ignores_plaintext_override(tmp_path, monkeypatch):
    store.configure_encrypted_key(KEY, tmp_path / '.env')
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'another-plaintext-test-key')
    settings = config.read_settings()
    assert settings.api_key == KEY
    assert settings.key_source == 'windows_dpapi' and not settings.key_error
    assert KEY not in repr(settings)


@windows
def test_short_placeholder_does_not_damage_other_settings(tmp_path):
    env = tmp_path / '.env'
    env.write_text('DEEPSEEK_API_KEY=x\nDEEPSEEK_MODEL=example-model\n', encoding='utf-8')
    store.configure_encrypted_key(KEY, env)
    assert 'DEEPSEEK_MODEL=example-model' in env.read_text(encoding='utf-8')


def test_missing_or_corrupt_vault_never_falls_back_to_plaintext(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    (tmp_path / '.env').write_text('DEEPSEEK_KEY_STORAGE=windows_dpapi\n'
                                  f'DEEPSEEK_API_KEY={KEY}\n', encoding='utf-8')
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    for blob in (None, b'broken-file'):
        if blob is not None:
            path = store.secret_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
        settings = config.read_settings()
        assert not settings.api_key and settings.key_error == 'secret_storage'
        monkeypatch.setattr(agent, 'read_settings', lambda: settings)
        with pytest.raises(RequestFailure) as caught:
            agent.create_model()
        assert caught.value.code == 'secret_storage'
        assert KEY not in caught.value.public_message


@pytest.mark.parametrize('value', ['', 'short', 'white space synthetic key', None])
def test_reject_invalid_key(value):
    with pytest.raises(store.SecretStoreError):
        store.encrypt_key(value)


@windows
def test_real_client_uses_decrypted_credential(tmp_path, monkeypatch):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from langchain_core.messages import HumanMessage

    authorized = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            authorized.append(self.headers.get('Authorization') == 'Bearer ' + KEY)
            data = json.dumps({'id':'synthetic', 'object':'chat.completion', 'created':1,
                'model':'test', 'choices':[{'index':0, 'message':{'role':'assistant','content':'ok'},
                                          'finish_reason':'stop'}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    store.configure_encrypted_key(KEY, tmp_path / '.env')
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    monkeypatch.setenv('DEEPSEEK_BASE_URL', f'http://127.0.0.1:{server.server_port}/v1')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert agent.create_model().invoke([HumanMessage(content='test')]).content == 'ok'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert authorized == [True]
