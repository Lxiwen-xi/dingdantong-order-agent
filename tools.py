"""Three read-only business tools; currency calculations are integer cents."""
import json
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, StrictBool

import database
from order_management import ID_PATTERN


class OrderInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    order_id: str = Field(pattern=ID_PATTERN, description='从用户取得的订单号，如 A1001、B2001 或数字订单号；缺少时先询问用户')


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    category: Literal['electronics', 'accessories', 'apparel', 'digital', 'all'] = Field(
        default='all', description='商品类别；不明确时查询 all')


class RefundInput(OrderInput):
    item_id: str = Field(pattern=ID_PATTERN, description='从订单查询结果取得的商品编号')
    quantity: int = Field(strict=True, ge=1, le=99, description='本次退货件数，需用户确认')
    reason: Literal['no_reason', 'quality'] = Field(description='无理由或质量问题')
    unopened: StrictBool | None = Field(default=None, description='用户是否明确表示未拆封；未知填 null')
    intact: StrictBool | None = Field(default=None, description='用户是否确认商品完好且未使用；未知填 null')
    tags_intact: StrictBool | None = Field(default=None, description='用户是否确认吊牌完整；未知填 null')


def dump(value):
    return json.dumps(value, ensure_ascii=False)


@tool(args_schema=OrderInput)
def query_order(order_id: str) -> str:
    """查询本地数据库中的订单状态、物流、商品编号和实付金额，返回数据来源及计算日期。查不到时不能编造。"""
    order = database.read_order(order_id)
    return dump({'ok': bool(order), 'order': order, 'message': '' if order else '订单不存在，请核对订单号，或先在订单管理中录入/导入。'})


@tool(args_schema=PolicyInput)
def lookup_policy(category: str = 'all') -> str:
    """查询自定义售后演示规则及规则编号。支持退货、质量换货和退款金额说明。"""
    data = database.load_policies()
    return dump({**data, 'rules': [r for r in data['rules'] if category == 'all' or r['category'] == category]})


@tool(args_schema=RefundInput)
def estimate_refund(order_id: str, item_id: str, quantity: int, reason: str,
                    unopened: bool | None = None, intact: bool | None = None,
                    tags_intact: bool | None = None) -> str:
    """按订单实付金额和演示规则试算商品退款。不接受模型指定价格，不执行退款。

    未知商品状态传 null，返回需要补充的问题；质量问题转人工审核，不自动批准。
    """
    order = database.read_order(order_id)
    if order is None:
        return dump({'ok': False, 'status': 'not_found', 'message': '订单不存在，请核对。'})
    item = next((i for i in order['items'] if i['item_id'] == item_id), None)
    if item is None:
        return dump({'ok': False, 'status': 'not_found', 'message': '该订单没有此商品，请重新查询。'})
    if quantity > item['quantity'] - item['refunded_quantity']:
        return dump({'ok': False, 'status': 'invalid_quantity', 'message': '退货数量超过可退数量。'})
    data = database.load_policies()
    rule = next((r for r in data['rules'] if r['category'] == item['category']), None)
    result = {'ok': True, 'order_id': order_id, 'item_id': item_id, 'item_name': item['name'],
              'quantity': quantity, 'business_date': order['business_date'], 'date_basis': order['date_basis'],
              'data_kind': order['data_kind'],
              'rule_ids': ([rule['id']] if rule else []) + ['DEMO-S01', 'DEMO-S02'],
              'notice': '仅试算商品金额，不含运费或补偿；未提交或执行售后。'}
    def finish(status, message, **extra):
        return dump({**result, 'status': status, 'message': message, **extra})
    if rule is None:
        return finish('manual_review', '缺少该类别的演示规则，需要人工核对。')
    if reason == 'quality':
        return finish('manual_review', '质量问题需核对凭证并人工审核，当前不能确认退款金额。')
    if order['status'] == 'in_transit':
        return finish('manual_review', '订单运输中，请先联系商家核实拦截或退回流程。')
    if order['status'] not in ('delivered', 'pending_ship'):
        return finish('manual_review', '当前订单状态需要人工核对。')
    if order['status'] == 'delivered':
        days = order['days_since_delivery']
        if days is None or days < 0:
            return finish('manual_review', '签收日期异常，需要人工核对。')
        if not rule['return_supported']:
            return finish('not_eligible', '该类别不支持演示规则中的无理由退货，质量问题可转人工。')
        if days > rule['return_days']:
            return finish('not_eligible', f"签收已 {days} 天，超出本演示规则的 {rule['return_days']} 天期限。")
        # Test known disqualifiers before asking for unrelated missing fields.
        if rule['requires_unopened'] and unopened is False:
            return finish('not_eligible', '已拆封不符合该类别演示无理由退货条件；质量问题可转人工。')
        if intact is False:
            return finish('manual_review', '商品已使用或不完好，需要人工审核。')
        if rule['requires_tags'] and tags_intact is False:
            return finish('not_eligible', '吊牌不完整不符合该类别演示无理由退货条件。')
        missing = []
        if rule['requires_unopened'] and unopened is None:
            missing.append('是否未拆封')
        if intact is None and not rule['requires_unopened']:
            missing.append('商品是否完好且未使用')
        if rule['requires_tags'] and tags_intact is None:
            missing.append('吊牌是否完整')
        if missing:
            return finish('needs_info', '请补充：' + '、'.join(missing) + '。', missing=missing)
    base, remainder = divmod(item['paid_cents'], item['quantity'])
    start = item['refunded_quantity']
    cents = sum(base + (index < remainder) for index in range(start, start + quantity))
    return finish('estimated', '按已确认的信息符合演示试算条件，最终仍需商家审核。',
                  refund_cents=cents, refund_yuan=f'{cents // 100}.{cents % 100:02d}',
                  calculation={'line_paid_cents': item['paid_cents'], 'purchased_quantity': item['quantity'],
                               'unit_base_cents': base, 'extra_cent_units': remainder,
                               'previous_refunded_quantity': start, 'requested_quantity': quantity,
                               'refund_cents': cents})


TOOLS = [query_order, lookup_policy, estimate_refund]
