import json
import sqlite3

import pytest
from pydantic import ValidationError

import database
from tools import estimate_refund, lookup_policy, query_order


def refund(**changes):
    args = dict(order_id='A1001', item_id='E01', quantity=1, reason='no_reason', unopened=True)
    args.update(changes)
    return json.loads(estimate_refund.invoke(args))


def test_order_facts_and_missing_order():
    order = json.loads(query_order.invoke({'order_id': 'A1001'}))['order']
    assert order['total_paid_cents'] == 44000
    assert order['days_since_delivery'] == 2
    assert len(order['items']) == 2
    assert json.loads(query_order.invoke({'order_id': 'A9999'}))['ok'] is False


def test_rule_category_has_sources_and_shared_calculation_rule():
    data = json.loads(lookup_policy.invoke({'category': 'electronics'}))
    assert [r['id'] for r in data['rules']] == ['DEMO-E01']
    assert {r['id'] for r in data['shared']} == {'DEMO-S01', 'DEMO-S02'}
    assert len(json.loads(lookup_policy.invoke({'category': 'all'}))['rules']) == 4


def test_partial_refund_remainder_and_total_conservation(demo_db):
    one, two = refund(), refund(quantity=2)
    assert one['refund_cents'] == 19951
    assert one['refund_yuan'] == '199.51'
    assert two['refund_cents'] == 39901
    with sqlite3.connect(demo_db) as conn:
        conn.execute("UPDATE items SET refunded_quantity=1 WHERE order_id='A1001' AND item_id='E01'")
    later = refund()
    assert later['refund_cents'] == 19950
    assert later['refund_cents'] + one['refund_cents'] == two['refund_cents']
    assert refund(quantity=2)['status'] == 'invalid_quantity'


@pytest.mark.parametrize('changes,status', [
    ({'order_id': 'A9999'}, 'not_found'),
    ({'item_id': 'E99'}, 'not_found'),
    ({'quantity': 3}, 'invalid_quantity'),
    ({'unopened': None}, 'needs_info'),
    ({'unopened': False}, 'not_eligible'),
    ({'intact': False}, 'manual_review'),
    ({'reason': 'quality'}, 'manual_review'),
    ({'order_id': 'A1002', 'item_id': 'E02'}, 'not_eligible'),
    ({'order_id': 'A1003', 'item_id': 'C01'}, 'manual_review'),
    ({'order_id': 'A1005', 'item_id': 'D01'}, 'not_eligible'),
    ({'order_id': 'A1006', 'item_id': 'C02', 'intact': True, 'tags_intact': None}, 'needs_info'),
    ({'order_id': 'A1006', 'item_id': 'C02', 'intact': True, 'tags_intact': False}, 'not_eligible'),
    ({'order_id': 'A1006', 'item_id': 'C02', 'intact': True, 'tags_intact': True}, 'estimated'),
    ({'order_id': 'A1004', 'item_id': 'A02', 'unopened': None}, 'estimated'),
])
def test_business_branches(changes, status):
    data = refund(**changes)
    assert data['status'] == status
    if status != 'estimated':
        assert 'refund_cents' not in data


def test_declined_conditions_do_not_ask_unrelated_questions():
    data = refund(unopened=False, intact=None)
    assert data['status'] == 'not_eligible'
    assert 'missing' not in data


@pytest.mark.parametrize('changes', [
    {'order_id': "A1001' OR 1=1--"}, {'quantity': 0}, {'quantity': True},
    {'quantity': '1'}, {'quantity': 1.5}, {'reason': 'exchange'},
    {'unopened': 'yes'}, {'price': 1},
])
def test_tool_rejects_invalid_or_model_supplied_price(changes):
    with pytest.raises(ValidationError):
        refund(**changes)


def test_accessories_need_intact_but_not_unopened():
    data = refund(item_id='A01', unopened=False, intact=None)
    assert data['missing'] == ['商品是否完好且未使用']
    assert refund(item_id='A01', unopened=False, intact=True)['refund_cents'] == 4099


def test_pending_cancellation_and_read_only_service(demo_db):
    before = demo_db.read_bytes()
    assert refund(order_id='A1004', item_id='A02', quantity=2)['refund_cents'] == 5999
    query_order.invoke({'order_id': 'A1001'})
    refund()
    assert demo_db.read_bytes() == before


def test_database_reinitialization_is_idempotent(demo_db):
    database.init_db(demo_db)
    assert len(database.list_orders()) == 6
