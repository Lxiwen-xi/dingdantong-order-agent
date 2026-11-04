import json
import sqlite3

import pytest

import runtime


@pytest.mark.parametrize('status,body,expected',[
    (401,{},'authentication'),(403,{},'permission'),(429,{},'rate_limit'),
    (429,{'error':{'code':'insufficient_quota'}},'quota'),(402,{},'quota'),
    (400,{},'model_config'),(404,{},'model_config'),(503,{},'service'),
])
def test_provider_errors_are_classified_without_returning_raw_messages(status,body,expected):
    error=Exception('synthetic-private-key')
    error.status_code=status
    error.body=body
    result=runtime.RequestFailure(runtime.classify_error(error))
    assert result.code==expected
    assert 'synthetic-private-key' not in result.public_message


def test_timeouts_sqlite_errors_and_minimal_logging(tmp_path,monkeypatch):
    assert runtime.classify_error(TimeoutError())=='timeout'
    assert runtime.classify_error(sqlite3.OperationalError('database is locked'))=='database_busy'
    path=tmp_path/'audit.jsonl'
    monkeypatch.setattr(runtime,'LOG_PATH',path)
    runtime.log_event('request-test','model_request',duration_ms=10,outcome='failed',code='timeout')
    data=json.loads(path.read_text(encoding='utf-8'))
    assert set(data)=={'time','request_id','event','duration_ms','outcome','error_code'}
    assert data['request_id']=='request-test'
