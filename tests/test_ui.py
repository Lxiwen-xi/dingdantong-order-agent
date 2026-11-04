from pathlib import Path
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / 'app.py')


def test_page_offline_workflow_and_export():
    app = AppTest.from_file(APP, default_timeout=45).run()
    assert not app.exception
    next(b for b in app.button if b.label == '运行离线演示').click().run()
    assert not app.exception
    assert any('199.51' in m.value and '未调用大模型' in m.value for m in app.markdown)
    assert len(app.session_state['turns'][0]['trace']) == 3


def test_online_requires_consent_and_does_not_call_model():
    app = AppTest.from_file(APP, default_timeout=45).run()
    next(r for r in app.radio if r.label == '运行模式').set_value('DeepSeek 智能咨询').run()
    app.chat_input[0].set_value('你好').run()
    assert not app.exception
    assert any('模型调用授权' in m.value for m in app.warning)
    assert app.session_state['turns'] == []


def test_new_session_clears_model_context_and_display():
    app = AppTest.from_file(APP, default_timeout=45).run()
    next(b for b in app.button if b.label == '运行离线演示').click().run()
    next(b for b in app.button if b.label == '开始新会话').click().run()
    assert not app.exception
    assert app.session_state['turns'] == []
    assert app.session_state['model_history'] == []
