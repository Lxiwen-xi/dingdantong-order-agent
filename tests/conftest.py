import pytest
import database
import runtime
import secret_store


@pytest.fixture(autouse=True)
def demo_db(tmp_path, monkeypatch):
    path = tmp_path / 'orders.db'
    database.init_db(path)
    monkeypatch.setattr(database, 'DB_PATH', path)
    monkeypatch.setattr(runtime, 'LOG_PATH', tmp_path / 'events.jsonl')
    monkeypatch.setattr(secret_store, 'secret_path', lambda: tmp_path / 'secrets' / 'test-key.dpapi')
    yield path
    runtime.close_logs()
