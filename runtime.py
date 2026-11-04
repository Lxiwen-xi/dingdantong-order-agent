"""Request failures and deliberately minimal, credential-free event records."""
import json
import logging
import sqlite3
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

import openai

LOG_PATH = Path(__file__).resolve().parent / 'logs' / 'events.jsonl'
MESSAGES = {
    'consent_required': ('请先勾选侧栏的模型调用授权，再重试这条问题。', False),
    'authentication': ('模型密钥无效或已失效，请检查本项目的密钥配置。', False),
    'permission': ('当前密钥没有访问这个模型的权限，请检查账号与模型配置。', False),
    'quota': ('模型账户余额或额度不足，请检查账户后重试。', False),
    'rate_limit': ('模型服务暂时限流，请稍后重试。', True),
    'timeout': ('模型请求超时，请稍后重试。', True),
    'connection': ('暂时无法连接模型服务，请检查网络与接口地址。', True),
    'model_config': ('模型名称、接口地址或请求配置不匹配，请检查配置。', False),
    'service': ('模型服务暂时不可用，请稍后重试。', True),
    'database_busy': ('本地订单数据库正忙，请稍后重试。', True),
    'local_data': ('本地订单或规则文件无法读取，请检查数据文件。', False),
    'invalid_response': ('模型没有返回可用回答，请重新尝试。', True),
    'context_changed': ('订单、规则或日期在处理期间发生变化，请重新查询。', True),
    'configuration': ('尚未配置有效的模型密钥，请运行本项目的 设置DeepSeek密钥.py 后重试。', False),
    'secret_storage': ('本机加密密钥尚未设置或无法读取，请使用原 Windows 用户，或运行 设置DeepSeek密钥.py 重新设置。', False),
    'unexpected': ('本次处理未完成，请重试；若仍失败可凭请求编号排查。', True),
}


def request_id():
    return uuid4().hex[:12]


class RequestFailure(RuntimeError):
    def __init__(self, code, *, request=None, trace=None):
        self.code = code if code in MESSAGES else 'unexpected'
        self.public_message, self.retryable = MESSAGES[self.code]
        self.request_id = request or request_id()
        self.trace = list(trace or [])
        super().__init__(self.code)


def classify_error(exc):
    if isinstance(exc, RequestFailure):
        return exc.code
    status = getattr(exc, 'status_code', None)
    body = getattr(exc, 'body', None)
    # Inspect the code for classification only; never log/return the raw body.
    error = body.get('error', body) if isinstance(body, dict) else {}
    code = str(error.get('code', '')).lower() if isinstance(error, dict) else ''
    if status == 402 or code in {'insufficient_quota', 'insufficient_balance', 'balance_insufficient'}:
        return 'quota'
    if isinstance(exc, openai.AuthenticationError) or status == 401:
        return 'authentication'
    if status == 403:
        return 'permission'
    if status == 429:
        return 'rate_limit'
    if isinstance(exc, (openai.APITimeoutError, TimeoutError)):
        return 'timeout'
    if isinstance(exc, openai.APIConnectionError):
        return 'connection'
    if status in (400, 404, 422):
        return 'model_config'
    if status and status >= 500:
        return 'service'
    if isinstance(exc, sqlite3.OperationalError) and any(s in str(exc).lower() for s in ('locked', 'busy')):
        return 'database_busy'
    if isinstance(exc, (sqlite3.DatabaseError, FileNotFoundError, json.JSONDecodeError)):
        return 'local_data'
    if isinstance(exc, ValueError) and str(exc) == 'missing_api_key':
        return 'configuration'
    return 'unexpected'


def log_event(request, event, *, tool=None, duration_ms=0, outcome='ok', code=None):
    """No prompts, order details, arguments, headers, keys or raw exceptions."""
    record = {'time': datetime.now(timezone.utc).isoformat(), 'request_id': request,
              'event': event, 'duration_ms': round(duration_ms, 2), 'outcome': outcome}
    if tool:
        record['tool'] = tool if tool in {'query_order', 'lookup_policy', 'estimate_refund'} else 'unknown'
    if code:
        record['error_code'] = code
    try:
        path = Path(LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger('order-events.' + str(path.resolve()))
        logger.propagate = False
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(message)s'))
            logger.addHandler(handler)
        logger.info(json.dumps(record, ensure_ascii=False))
    except OSError:
        # An unavailable log directory must not prevent read-only consultation.
        pass


def close_logs():
    """Release only this application's handlers (useful for tests/one-shot diagnostics)."""
    for name,logger in list(logging.Logger.manager.loggerDict.items()):
        if name.startswith('order-events.') and isinstance(logger,logging.Logger):
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
