import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent import OfflineDemoModel, build_graph, trim_history


def invoke(model, limit=3, messages=None):
    return build_graph(model, max_rounds=limit).invoke(
        {'messages': messages or [HumanMessage(content='退一副未拆封的耳机')], 'rounds': 0, 'trace': []})


def test_offline_executes_all_business_tools_and_summarizes_final_results():
    result = invoke(OfflineDemoModel())
    assert result['rounds'] == 3
    assert [t['tool'] for t in result['trace']] == ['query_order', 'lookup_policy', 'estimate_refund']
    assert '199.51' in result['messages'][-1].content
    assert '未调用大模型' in result['messages'][-1].content


def test_tool_error_observation_and_per_round_limit():
    class Model:
        def bind_tools(self, tools):
            return self
        def invoke(self, messages):
            if isinstance(messages[-1], HumanMessage):
                calls = [{'name': 'query_order', 'args': {'order_id': 'A1001'}, 'id': f'c{i}'} for i in range(7)]
                calls[0].update(name='delete_order')
                calls[1]['args'] = {'order_id': "bad' OR 1=1"}
                return AIMessage(content='', tool_calls=calls)
            return AIMessage(content='请核对订单号。')
    result = invoke(Model())
    outputs = [m for m in result['messages'] if isinstance(m, ToolMessage)]
    assert len(outputs) == 7
    assert {m.tool_call_id for m in outputs} == {f'c{i}' for i in range(7)}
    assert not result['trace'][0]['success']
    assert '允许列表' in outputs[0].content
    assert '校验失败' in outputs[1].content
    assert '上限' in outputs[-1].content


def test_final_summary_uses_tools_disabled_base_model():
    class Bound:
        def invoke(self, messages):
            return AIMessage(content='', tool_calls=[{'name': 'query_order', 'args': {'order_id': 'A1001'}, 'id': 'first'}])
    class Model:
        def bind_tools(self, tools):
            return Bound()
        def invoke(self, messages):
            assert any(isinstance(m, ToolMessage) for m in messages)
            return AIMessage(content='已查到订单，尚缺退货商品与数量。')
    result = invoke(Model(), limit=1)
    assert result['rounds'] == 1
    assert '已查到订单' in result['messages'][-1].content


def test_noncompliant_model_cannot_loop_forever():
    class Model:
        def bind_tools(self, tools):
            return self
        def invoke(self, messages):
            return AIMessage(content='', tool_calls=[{'name': 'query_order', 'args': {'order_id': 'A1001'}, 'id': str(len(messages))}])
    result = invoke(Model(), limit=2)
    assert result['rounds'] == 2
    assert len(result['trace']) == 2
    assert '上限' in result['messages'][-1].content


def test_full_tool_context_survives_followup_and_pruning():
    history = []
    for i in range(7):
        history.extend([HumanMessage(content=f'订单{i}'),
                        AIMessage(content='', tool_calls=[{'name': 'query_order', 'args': {'order_id': 'A1001'}, 'id': f'c{i}'}]),
                        ToolMessage(content='{}', tool_call_id=f'c{i}', name='query_order'),
                        AIMessage(content=f'结果{i}')])
    kept = trim_history(history)
    assert len(kept) == 20
    assert kept[0].content == '订单2'
    assert len([m for m in kept if isinstance(m, ToolMessage)]) == 5
    class Model:
        def bind_tools(self, tools):
            return self
        def invoke(self, messages):
            assert any(isinstance(m, ToolMessage) and m.tool_call_id == 'c6' for m in messages)
            assert messages[-1].content == '那换货呢？'
            return AIMessage(content='可以核对换货规则。')
    result = invoke(Model(), messages=[*kept, HumanMessage(content='那换货呢？')])
    assert result['trace'] == []
    assert '换货规则' in result['messages'][-1].content
