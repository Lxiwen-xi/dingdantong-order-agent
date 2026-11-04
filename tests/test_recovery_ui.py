import copy
from datetime import timedelta
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from streamlit.testing.v1 import AppTest

import agent
import config
import database

APP=str(Path(__file__).resolve().parents[1]/'app.py')


def find(elements,label): return next(e for e in elements if e.label==label)


def online_app(monkeypatch,model):
    monkeypatch.setattr(config,'read_settings',lambda:config.Settings(api_key='synthetic-test-key'))
    monkeypatch.setattr(agent,'create_model',lambda:model)
    app=AppTest.from_file(APP,default_timeout=45).run()
    find(app.radio,'运行模式').set_value('DeepSeek 智能咨询').run()
    app.checkbox[0].set_value(True).run()
    return app


def test_failed_question_and_partial_trace_can_be_retried_without_retyping(monkeypatch):
    class Model:
        failing=True
        def bind_tools(self,tools): return self
        def invoke(self,messages):
            if isinstance(messages[-1],HumanMessage):
                return AIMessage(content='',tool_calls=[{'name':'query_order','args':{'order_id':'A1001'},'id':'q'}])
            if self.failing: raise TimeoutError('synthetic secret should not be shown')
            return AIMessage(content='订单A1001已签收。')
    model=Model()
    app=online_app(monkeypatch,model)
    question='查一下A1001'
    app.chat_input[0].set_value(question).run()
    assert not app.exception
    pending=app.session_state['pending_task']
    assert pending['question']==question and pending['code']=='timeout'
    assert len(pending['trace'])==1
    assert 'secret' not in app.error[0].value
    model.failing=False
    find(app.button,'重试本次问题').click().run()
    assert not app.exception
    assert app.session_state['pending_task'] is None
    assert len(app.session_state['turns'])==1
    assert app.session_state['turns'][0]['question']==question


def test_date_and_policy_refresh_discard_old_tools_but_keep_user_input(monkeypatch):
    app=AppTest.from_file(APP,default_timeout=45).run()
    app.session_state['model_history']=[HumanMessage(content='A1001耳机没拆封'),AIMessage(content='旧结论')]
    today=database.business_today()
    monkeypatch.setattr(database,'business_today',lambda:today+timedelta(days=1))
    app.run()
    kept=app.session_state['model_history']
    assert len(kept)==1 and isinstance(kept[0],HumanMessage)
    assert any('日期或售后规则已变化' in i.value for i in app.info)
    policy=copy.deepcopy(database.load_policies())
    policy['version']='NEW-TEST-VERSION'
    monkeypatch.setattr(database,'load_policies',lambda:policy)
    app.session_state['model_history'].append(AIMessage(content='另一个旧结论'))
    app.run()
    assert all(isinstance(m,HumanMessage) for m in app.session_state['model_history'])


def test_unclear_conditions_can_be_explicitly_confirmed_in_page(monkeypatch):
    class Model:
        def bind_tools(self,tools): return self
        def invoke(self,messages):
            if isinstance(messages[-1],HumanMessage):
                return AIMessage(content='',tool_calls=[
                    {'name':'query_order','args':{'order_id':'A1001'},'id':'q'},
                    {'name':'lookup_policy','args':{'category':'electronics'},'id':'p'},
                    {'name':'estimate_refund','args':{'order_id':'A1001','item_id':'E01',
                        'quantity':1,'reason':'no_reason','unopened':True},'id':'r'}])
            import json
            result=json.loads(next(m.content for m in reversed(messages) if isinstance(m,ToolMessage) and m.name=='estimate_refund'))
            if result.get('status')=='estimated':
                return AIMessage(content='预计商品退款199.51元。[DEMO-E01] 尚未执行退款。')
            return AIMessage(content='请补充退货原因和拆封情况，也可以在页面核对。')
    app=online_app(monkeypatch,Model())
    app.chat_input[0].set_value('A1001耳机退一副，能退多少钱？').run()
    assert not app.exception
    assert app.session_state['pending_confirmation']
    assert '199.51' not in app.session_state['turns'][0]['answer']
    find(app.selectbox,'退货原因').set_value('no_reason')
    find(app.selectbox,'是否未拆封').set_value(True)
    find(app.button,'确认信息并继续试算').click().run()
    assert not app.exception
    assert '199.51' in app.session_state['turns'][-1]['answer']
    assert app.session_state['pending_confirmation'] is None


def test_management_navigation_preserves_online_mode_and_consent(monkeypatch):
    class Model:
        def bind_tools(self,tools): return self
        def invoke(self,messages): return AIMessage(content='你好。')
    app=online_app(monkeypatch,Model())
    app.chat_input[0].set_value('你好').run()
    find(app.radio,'工作区').set_value('订单管理').run()
    find(app.radio,'工作区').set_value('售后咨询').run()
    assert not app.exception
    assert find(app.radio,'运行模式').value=='DeepSeek 智能咨询'
    assert app.checkbox[0].value is True
    assert len(app.session_state['turns'])==1


def test_missing_configuration_keeps_question_for_later_retry(monkeypatch):
    monkeypatch.setattr(config,'read_settings',lambda:config.Settings())
    app=AppTest.from_file(APP,default_timeout=45).run()
    find(app.radio,'运行模式').set_value('DeepSeek 智能咨询').run()
    app.checkbox[0].set_value(True).run()
    app.chat_input[0].set_value('查A1001').run()
    assert not app.exception
    assert app.session_state['pending_task']['question']=='查A1001'
    assert app.session_state['pending_task']['code']=='configuration'
