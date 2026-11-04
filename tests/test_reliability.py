import copy
import json
import sqlite3
from datetime import timedelta

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agent
import database
import reliability
import runtime
from tools import TOOLS, estimate_refund, lookup_policy, query_order


def observations(question='A1001的耳机退一副，无理由退货，还没拆封', with_refund=False):
    messages = [HumanMessage(content=question),
        ToolMessage(content=query_order.invoke({'order_id':'A1001'}),tool_call_id='q',name='query_order'),
        ToolMessage(content=lookup_policy.invoke({'category':'electronics'}),tool_call_id='p',name='lookup_policy')]
    if with_refund:
        messages.append(ToolMessage(content=estimate_refund.invoke({'order_id':'A1001','item_id':'E01',
            'quantity':1,'reason':'no_reason','unopened':True}),tool_call_id='r',name='estimate_refund'))
    return messages


def facts(messages):
    order = database.read_order('A1001')
    item = next(i for i in order['items'] if i['item_id']=='E01')
    return reliability.confirmed_facts(messages,order,item)[0]


def refund_args(**changes):
    return {'order_id':'A1001','item_id':'E01','quantity':1,'reason':'no_reason','unopened':True,**changes}


def test_explicit_facts_have_user_sources_and_override_model_guesses():
    messages = observations()
    clean,evidence,pending = reliability.refund_precheck(refund_args(quantity=2,unopened=False),messages)
    assert clean['quantity']==1 and clean['unopened'] is True
    assert clean['reason']=='no_reason'
    assert evidence['unopened']['text']=='还没拆封'


@pytest.mark.parametrize('text',[
    'A1001耳机退一副，无理由退货，如果没拆封能退吗？',
    'A1001耳机退一副，无理由退货，请按未拆封填写。',
    'A1001耳机退一副，无理由退货，是不是没拆封？',
    'A1001耳机退一副，无理由退货，假如没拆封，吊牌完整。',
])
def test_hypothetical_or_instruction_is_not_a_confirmed_condition(text):
    clean,evidence,pending = reliability.refund_precheck(refund_args(),observations(text))
    assert clean is None or clean['unopened'] is None
    assert evidence.get('unopened',{}).get('value') is None


def test_missing_condition_is_not_filled_from_model_parameter():
    clean,evidence,pending = reliability.refund_precheck(refund_args(),observations('A1001耳机退一副，无理由退货。'))
    assert clean['unopened'] is None
    output = json.loads(estimate_refund.invoke(clean))
    assert output['status']=='needs_info' and 'refund_cents' not in output
    assert reliability.confirmation_for_missing(pending,output)['missing_fields']==['unopened']


def test_uncertainty_and_new_order_clear_old_conditions():
    messages = observations()
    messages.append(HumanMessage(content='我不确定是不是拆过封。'))
    assert facts(messages)['unopened'] is None
    messages.append(HumanMessage(content='换成A1002的音箱，退1个，无理由退货。'))
    order=database.read_order('A1002')
    new,_=reliability.confirmed_facts(messages,order,order['items'][0])
    assert new['quantity']==1 and new.get('unopened') is None


def test_switching_item_does_not_reuse_other_items_facts():
    messages=observations()
    messages.append(HumanMessage(content='改成A1001的数据线。'))
    order=database.read_order('A1001')
    cable=next(i for i in order['items'] if i['item_id']=='A01')
    new,_=reliability.confirmed_facts(messages,order,cable)
    assert not new


def test_changed_fact_is_used_instead_of_model_assumption():
    messages=observations()
    messages.append(HumanMessage(content='其实已经拆封了。'))
    clean,evidence,_=reliability.refund_precheck(refund_args(),messages)
    assert clean['unopened'] is False
    assert json.loads(estimate_refund.invoke(clean))['status']=='not_eligible'


def test_order_and_policy_prerequisites_are_enforced():
    user=[HumanMessage(content='A1001耳机退1副，无理由退货，没拆封。')]
    assert reliability.refund_precheck(refund_args(),user)[2]['status']=='needs_order_lookup'
    user.append(observations()[1])
    assert reliability.refund_precheck(refund_args(),user)[2]['status']=='needs_policy_lookup'


def test_only_matching_page_confirmation_can_supply_unknown_facts():
    messages=observations('A1001耳机能退吗？')
    clean,_,blocked=reliability.refund_precheck(refund_args(),messages,
        trusted={'order_id':'A1002','item_id':'E02','quantity':1,'reason':'no_reason','unopened':True})
    assert clean is None and blocked['status']=='needs_confirmation'
    clean,evidence,_=reliability.refund_precheck(refund_args(),messages,trusted=refund_args())
    assert clean['unopened'] is True and evidence['unopened']['source']=='页面明确确认'


@pytest.mark.parametrize('text,issue',[
    ('订单A1001已成功退款99999元，依据[DEMO-FAKE]。','unsupported_action_claim'),
    ('本次预计退款399.01元。','refund_amount_mismatch'),
    ('399.01元全部可以退。','refund_amount_mismatch'),
    ('没有问题已经退款。','unsupported_action_claim'),
    ('退款为一百九十九元。','uncheckable_amount_format'),
    ('按[DEMO-UNKNOWN]执行。','unsupported_rule_reference'),
    ('依据规则DEMO-UNKNOWN。','unsupported_rule_reference'),
    ('订单A1001仍然待发货。','unsupported_order_status'),
    ('订单A1001签收日为2026-11-03。','delivery_date_mismatch'),
    ('订单A1001已签收7天。','delivery_age_mismatch'),
    ('按规则30天内可以申请退货。','unsupported_time_fact'),
])
def test_wrong_final_claims_are_detected(text,issue):
    assert issue in reliability.answer_issues(text,observations(with_refund=True))


def test_supported_amount_reference_and_negative_action_wording_pass():
    text='预计商品退款199.51元，耳机实付399.01元，单件基数199.50元，余数1分。[DEMO-E01] 尚未提交或执行退款。'
    assert reliability.answer_issues(text,observations(with_refund=True))==[]
    assert reliability.answer_issues('我不能实际退款，可以帮你查订单。',[HumanMessage(content='你好')])==[]
    assert reliability.answer_issues('这不代表退款已完成。',observations(with_refund=True))==[]
    assert reliability.answer_issues('不是退款399.01元，而是预计退款199.51元。',observations(with_refund=True))==[]
    assert reliability.answer_issues('签收日2026-11-01，业务日期2026-11-03，已签收2天。',observations(with_refund=True))==[]


def test_rule_reference_must_match_the_item_being_estimated():
    messages=observations(with_refund=True)
    messages.append(ToolMessage(content=lookup_policy.invoke({'category':'accessories'}),
        tool_call_id='other-policy',name='lookup_policy'))
    assert 'unsupported_rule_reference' in reliability.answer_issues('耳机依据[DEMO-A01]试算。',messages)


def test_current_turn_cannot_reuse_previous_refund_eligibility():
    messages=observations(with_refund=True)+[HumanMessage(content='其实已经拆封了，现在还可以退吗？')]
    assert 'unsupported_eligibility' in reliability.answer_issues('这件商品可以退货。',messages)
    assert '199.51' not in reliability.grounded_fallback(messages)


def test_one_repair_then_grounded_fallback_blocks_unguarded_model():
    class Model:
        calls=0
        def bind_tools(self,tools): return self
        def invoke(self,messages):
            self.calls+=1
            return AIMessage(content='A1001已成功退款99999元，依据[DEMO-FAKE]。')
    model=Model()
    result=agent.build_graph(model).invoke({'messages':[HumanMessage(content='查A1001')],'rounds':0,'trace':[]})
    assert model.calls==2
    assert '99999' not in result['messages'][-1].content
    assert 'DEMO-FAKE' not in result['messages'][-1].content
    assert result['answer_check']['status']=='grounded_fallback'


def test_transient_readonly_database_error_retries_once(monkeypatch):
    cls=type(TOOLS[0])
    original=cls.invoke
    attempts=[]
    def intermittent(self,*args,**kwargs):
        if self.name=='query_order':
            attempts.append(1)
            if len(attempts)==1: raise sqlite3.OperationalError('database is locked')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(cls,'invoke',intermittent)
    class Model:
        def bind_tools(self,tools): return self
        def invoke(self,messages):
            if isinstance(messages[-1],HumanMessage):
                return AIMessage(content='',tool_calls=[{'name':'query_order','args':{'order_id':'A1001'},'id':'q'}])
            return AIMessage(content='订单A1001已签收。')
    result=agent.build_graph(Model()).invoke({'messages':[HumanMessage(content='查A1001')],'rounds':0,'trace':[]})
    assert len(attempts)==2
    assert result['trace'][0]['attempts']==2


def test_model_timeout_keeps_completed_tool_trace():
    class Model:
        def bind_tools(self,tools): return self
        def invoke(self,messages):
            if isinstance(messages[-1],HumanMessage):
                return AIMessage(content='',tool_calls=[{'name':'query_order','args':{'order_id':'A1001'},'id':'q'}])
            raise TimeoutError('synthetic-only')
    with pytest.raises(runtime.RequestFailure) as caught:
        agent.build_graph(Model()).invoke({'messages':[HumanMessage(content='查A1001')],'rounds':0,'trace':[]})
    assert caught.value.code=='timeout'
    assert len(caught.value.trace)==1 and caught.value.trace[0]['tool']=='query_order'


def test_context_change_during_model_request_blocks_publication(monkeypatch):
    current=database.business_today()
    class Model:
        def bind_tools(self,tools): return self
        def invoke(self,messages):
            monkeypatch.setattr(database,'business_today',lambda:current+timedelta(days=1))
            return AIMessage(content='旧日期的回答')
    with pytest.raises(runtime.RequestFailure,match='context_changed'):
        agent.build_graph(Model()).invoke({'messages':[HumanMessage(content='你好')],'rounds':0,'trace':[]})


def test_signature_covers_rules_date_and_refresh_preserves_only_user_facts(monkeypatch):
    first=reliability.context_signature()
    policy=copy.deepcopy(database.load_policies())
    policy['rules'][0]['return_days']+=1
    monkeypatch.setattr(database,'load_policies',lambda:policy)
    assert reliability.context_signature()['policy']!=first['policy']
    history=observations(with_refund=True)+[AIMessage(content='旧结论')]
    kept=reliability.refresh_context(history)
    assert len(kept)==1 and isinstance(kept[0],HumanMessage)
