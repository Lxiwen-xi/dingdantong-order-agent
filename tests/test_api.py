"""Exercise the real client against a local HTTP stub; no external API charges."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from langchain_core.messages import HumanMessage

import agent
import config


def test_real_client_tool_roundtrip_and_final_summary(monkeypatch):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(request)
            if len(requests) == 1:
                message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': 'http-order', 'type': 'function', 'function': {
                        'name': 'query_order', 'arguments': '{"order_id":"A1001"}'}}]}
                finish = 'tool_calls'
            else:
                message = {'role': 'assistant', 'content': '演示订单A1001已签收，共含两种商品。'}
                finish = 'stop'
            data = json.dumps({'id': 'local-test', 'object': 'chat.completion', 'created': 1,
                               'model': 'deepseek-flash', 'choices': [{'index': 0, 'message': message,
                                                                      'finish_reason': finish}],
                               'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(agent, 'read_settings', lambda: config.Settings(
        'synthetic-local-test-only', 'deepseek-flash', f'http://127.0.0.1:{server.server_port}/v1'))
    try:
        result = agent.build_graph(max_rounds=1).invoke(
            {'messages': [HumanMessage(content='查A1001')], 'rounds': 0, 'trace': []})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert len(requests) == 2
    assert requests[0]['thinking'] == {'type': 'disabled'}
    assert {t['function']['name'] for t in requests[0]['tools']} == {'query_order', 'lookup_policy', 'estimate_refund'}
    assert not requests[1].get('tools')
    tool_result = next(m for m in requests[1]['messages'] if m['role'] == 'tool')
    assert tool_result['tool_call_id'] == 'http-order'
    assert json.loads(tool_result['content'])['order']['order_id'] == 'A1001'
    assert '已签收' in result['messages'][-1].content


def test_config_reads_project_env_and_keeps_key_out_of_repr(tmp_path, monkeypatch):
    for name in ('DEEPSEEK_API_KEY', 'DEEPSEEK_MODEL', 'DEEPSEEK_BASE_URL'):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / '.env').write_text('DEEPSEEK_API_KEY=synthetic-local-test-only\nDEEPSEEK_MODEL=test-model\n', encoding='utf-8')
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    settings = config.read_settings()
    assert settings.api_key == 'synthetic-local-test-only'
    assert settings.model == 'test-model'
    assert 'synthetic-local-test-only' not in repr(settings)
    monkeypatch.setenv('DEEPSEEK_MODEL', 'override')
    assert config.read_settings().model == 'override'
