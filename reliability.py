"""Conservative fact extraction, evidence checks and context invalidation."""
import hashlib
import json
import re
from decimal import Decimal
from datetime import date

from langchain_core.messages import HumanMessage, ToolMessage

import database

FACT_LABELS = {'quantity': '退货数量', 'reason': '退货原因', 'unopened': '是否未拆封',
               'intact': '是否完好且未使用', 'tags_intact': '吊牌是否完整'}
AMBIGUOUS = re.compile(r'如果|假如|假设|要是|例如|比如|是否|是不是|不确定|可能|好像|大概|似乎|据说|听说|他说|她说|不是|并非|不代表|请|假装|当作|设为|设置|填写|忽略|```|[“”"「」『』]')
NEGATIVE = re.compile(r'未|没有|并未|尚未|不能|无法|不会|不支持|不代表|不等于|不是|不要|并没有')
MONEY = re.compile(r'[¥￥]\s*(\d[\d,]*(?:\.\d{1,2})?)|(?<![\d.])(\d[\d,]*(?:\.\d{1,2})?)\s*(?:元|块钱|块)(?!素)')
CITATION = re.compile(r'\[([A-Za-z][A-Za-z0-9_-]*-[A-Za-z0-9_-]+)\]')


def _negates_prefix(text, start):
    prefix=text[max(0,start-45):start].rstrip()
    return bool(re.search(r'(?:尚未|并未|没有|未|不能|无法|不会|不支持|不代表|不等于|不是|不)'
        r'(?:(?:实际|真正|帮你|为你|已经|成功|完成|提交|执行|任何|直接|自动|发生|的|或|及|和|、|'
        r'申请|退款|退货|换货|到账|声称|宣称|表示|说成|确认|判断|保证|证明|我|我们|系统|助手))*$',prefix))


def context_signature():
    policy = json.dumps(database.load_policies(), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {'orders': database.data_revision(), 'date': database.business_today().isoformat(),
            'policy': hashlib.sha256(policy.encode()).hexdigest()}


def refresh_context(messages):
    # Preserve the user's own facts/identifiers while discarding stale tool/AI claims.
    return [m for m in messages if isinstance(m, HumanMessage)][-5:]


def tool_observations(messages):
    result = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        try:
            data = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            result.append((message.name, data))
    return result


def mentioned_orders(text, orders):
    return [o['order_id'] for o in orders if re.search(
        r'(?<![A-Za-z0-9_-])' + re.escape(o['order_id']) + r'(?![A-Za-z0-9_-])', text)]


def active_orders(messages):
    orders = database.list_orders()
    active = []
    for message in messages:
        if isinstance(message, HumanMessage):
            found = mentioned_orders(str(message.content), orders)
            if found:
                active = found
    return active


def _item_mentions(text, items):
    found = []
    for item in items:
        name = item['name']
        suffix = name[-2:]
        unique_suffix = len(name) >= 2 and sum(i['name'].endswith(suffix) for i in items) == 1
        by_id = re.search(r'(?<![A-Za-z0-9_-])' + re.escape(item['item_id']) + r'(?![A-Za-z0-9_-])', text)
        if by_id or name in text or (unique_suffix and suffix in text):
            found.append(item['item_id'])
    return found


def _facts_from_clause(clause, item_name):
    """Accept explicit assertions only; ambiguous wording remains unconfirmed."""
    if re.search(r'不确定|好像|可能|不是|并非|没.*没.*(?:拆|封)|不.*不.*(?:拆|封)', clause):
        uncertain = {}
        if re.search(r'拆|开封', clause):
            uncertain['unopened'] = None
        if re.search(r'完好|使用|穿过|用过', clause):
            uncertain['intact'] = None
        if '吊牌' in clause:
            uncertain['tags_intact'] = None
        return uncertain
    if AMBIGUOUS.search(clause):
        return {}
    value = clause.strip(' ：:、 ')
    subject = '(?:' + '|'.join(re.escape(s) for s in sorted(
        {item_name, item_name[-2:], '这个商品', '这件商品', '这个', '这件', '商品', '我', '它'}, key=len, reverse=True) if s) + ')'
    value = re.sub(r'^' + subject, '', value)
    value = re.sub(r'^(?:其实|现在|目前|还是|一直|也|还|已经|确实|是)+', '', value)
    value = re.sub(r'[了啊呀呢吧的]+$', '', value)
    facts = {}
    if re.fullmatch(r'(?:没(?:有)?|未)(?:拆封|开封|拆开)(?:过)?|全新未拆封', value):
        facts['unopened'] = True
    elif re.fullmatch(r'(?:已)?(?:拆封|开封|拆开)(?:过)?|拆过封|不是全新', value):
        facts['unopened'] = False
    if re.fullmatch(r'(?:完好(?:且|也|并且|而且)?(?:没(?:有)?|未)使用(?:过)?|(?:没(?:有)?|未)使用(?:过)?(?:且|也|并且)?完好)', value):
        facts['intact'] = True
    elif re.fullmatch(r'(?:已)?(?:使用|用|穿)(?:过)|有损坏|破损|损坏|坏', value):
        facts['intact'] = False
    if re.fullmatch(r'吊牌(?:完整|还在|没(?:有)?剪|未剪)|(?:没(?:有)?|未)剪吊牌', value):
        facts['tags_intact'] = True
    elif re.fullmatch(r'吊牌(?:不完整|不在|剪掉|剪|破损|丢)|剪了吊牌|剪掉吊牌', value):
        facts['tags_intact'] = False
    if re.search(r'无理由|不想要|不喜欢|买错|没有质量问题|没质量问题|取消订单', value):
        facts['reason'] = 'no_reason'
    elif re.search(r'质量问题|故障|不能用|损坏|坏了', value):
        facts['reason'] = 'quality'
    count = re.search(r'(?:退(?:货)?|换)(?:回)?\s*([0-9]{1,2}|[一二两三四五六七八九十])\s*(?:件|个|副|条|套)', clause)
    if not count:
        count = re.fullmatch(r'([0-9]{1,2}|[一二两三四五六七八九十])\s*(?:件|个|副|条|套)', value)
    if count:
        word = count[1]
        facts['quantity'] = int(word) if word.isdigit() else {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}[word]
    return facts


def confirmed_facts(messages, order, item, trusted=None):
    orders = database.list_orders()
    active_order, active_item = None, None
    facts, evidence = {}, {}
    for message in messages:
        if not isinstance(message, HumanMessage):
            continue
        text = str(message.content)
        found = mentioned_orders(text, orders)
        if found:
            new_order = found[0] if len(found) == 1 else None
            if new_order != active_order:
                facts, evidence, active_item = {}, {}, None
            active_order = new_order
        if active_order != order['order_id']:
            continue
        found_items = _item_mentions(text, order['items'])
        if found_items:
            new_item = found_items[0] if len(found_items) == 1 else None
            if new_item != active_item:
                facts, evidence = {}, {}
            active_item = new_item
        elif len(order['items']) == 1:
            active_item = order['items'][0]['item_id']
        if active_item != item['item_id']:
            continue
        if re.search(r'如果|假如|假设|要是|举例|例如|比如|[“”"「」『』]',text):
            continue
        for clause in re.split(r'[，,。；;！？?\n]', text):
            for key, value in _facts_from_clause(clause, item['name']).items():
                facts[key] = value
                evidence[key] = {'value': value, 'source': '用户原话', 'text': clause.strip()[:200]}
    if trusted and trusted.get('order_id') == order['order_id'] and trusted.get('item_id') == item['item_id']:
        for key in FACT_LABELS:
            if trusted.get(key) is not None:
                facts[key] = trusted[key]
                evidence[key] = {'value': trusted[key], 'source': '页面明确确认'}
    return facts, evidence


def refund_precheck(args, messages, trusted=None):
    order = database.read_order(args['order_id'])
    if not order:
        return dict(args), {}, None
    item = next((i for i in order['items'] if i['item_id'] == args['item_id']), None)
    if not item:
        return dict(args), {}, None
    observations = tool_observations(messages)
    if not any(name == 'query_order' and data.get('order', {}).get('order_id') == args['order_id']
               for name, data in observations if data.get('order')):
        return None, {}, {'ok': False, 'order_id':args['order_id'],'item_id':args['item_id'],
                          'status': 'needs_order_lookup', 'message': '请先调用 query_order 查询这笔订单，再试算。'}
    if not any(name == 'lookup_policy' and any(r.get('category') == item['category'] for r in data.get('rules', []))
               for name, data in observations):
        return None, {}, {'ok': False, 'order_id':args['order_id'],'item_id':args['item_id'],
                          'status': 'needs_policy_lookup', 'message': '请先调用 lookup_policy 查询对应商品规则，再试算。'}
    facts, evidence = confirmed_facts(messages, order, item, trusted)
    cleaned = {**args, **{key: facts.get(key) for key in ('unopened', 'intact', 'tags_intact')}}
    missing = [key for key in ('quantity', 'reason') if facts.get(key) is None]
    for key in ('quantity', 'reason'):
        if facts.get(key) is not None:
            cleaned[key] = facts[key]
    pending = {'order_id': order['order_id'], 'item_id': item['item_id'], 'item_name': item['name'],
               'category': item['category'], 'order_status': order['status'], 'facts': facts, 'evidence': evidence}
    required = ['quantity', 'reason']
    if order['status'] == 'delivered':
        rule = next((r for r in database.load_policies()['rules'] if r['category'] == item['category']), {})
        in_window = rule.get('return_supported') and 0 <= order['days_since_delivery'] <= rule.get('return_days',0)
        if in_window:
            if rule.get('requires_unopened'):
                required.append('unopened')
            else:
                required.append('intact')
            if rule.get('requires_tags'):
                required.append('tags_intact')
    pending['required_fields'] = required
    if missing:
        return None, evidence, {'ok': True, 'status': 'needs_confirmation',
            'order_id':args['order_id'],'item_id':args['item_id'],
            'message': '还需确认：' + '、'.join(FACT_LABELS[k] for k in missing) + '。可直接补充，也可使用页面确认。不要猜测参数重试。',
            'missing_fields': missing, 'confirmation': {**pending, 'missing_fields': missing}}
    return cleaned, evidence, pending


def confirmation_for_missing(pending, output):
    if not pending or output.get('status') != 'needs_info':
        return None
    labels = output.get('missing', [])
    mapping = {'是否未拆封': 'unopened', '商品是否完好且未使用': 'intact', '吊牌是否完整': 'tags_intact'}
    return {**pending, 'missing_fields': [mapping[x] for x in labels if x in mapping]}


def answer_issues(text, messages):
    observations = tool_observations(messages)
    scope = active_orders(messages)
    orders = [data['order'] for name, data in observations if name == 'query_order' and data.get('order')
              and (not scope or data['order']['order_id'] in scope)]
    estimates = [data for name, data in observations if name == 'estimate_refund'
                 and (not scope or data.get('order_id') in scope)]
    last_user = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=0)
    current_estimates = [d for n, d in tool_observations(messages[last_user:]) if n == 'estimate_refund'
                         and (not scope or d.get('order_id') in scope)]
    targeted={(d.get('order_id'),d.get('item_id')) for d in current_estimates if d.get('item_id')}
    categories={i['category'] for o in orders for i in o['items']
                if not targeted or (o['order_id'],i['item_id']) in targeted}
    rule_records = [r for name,data in observations if name=='lookup_policy'
                    for r in [*data.get('rules',[]),*data.get('shared',[])]
                    if not scope or not r.get('category') or r.get('category') in categories]
    rules = {r['id'] for r in rule_records}
    allowed_days={0}|{o['days_since_delivery'] for o in orders if o.get('days_since_delivery') is not None}
    allowed_days.update(r[k] for r in rule_records for k in ('return_days','exchange_days') if k in r)
    allowed = set()
    for order in orders:
        allowed.add(order['total_paid_cents'])
        allowed.update(i['paid_cents'] for i in order['items'])
    for data in estimates:
        if data.get('status') == 'estimated':
            allowed.add(data['refund_cents'])
            calc = data.get('calculation', {})
            allowed.update(calc[k] for k in ('line_paid_cents', 'unit_base_cents') if k in calc)
            if calc.get('extra_cent_units'):
                allowed.add(calc['unit_base_cents'] + 1)
            allowed.update((0,1))
    issues = []
    for clause in re.split(r'[，,。；;！？?\n]', text):
        clause = clause.replace('*','').replace('`','')
        hypothetical = bool(re.search(r'如果|假如|假设|例如|比如|是否|不能保证', clause))
        action = re.search(r'(?:已|已经|成功|现已).{0,8}(?:退款|提交.{0,5}(?:申请|售后)|批准|办理.{0,4}(?:退款|换货))|(?:退款|退货|换货)(?:已)?(?:成功|到账|完成)', clause)
        if action and not hypothetical and not _negates_prefix(clause,action.start()):
            issues.append('unsupported_action_claim')
        for match in MONEY.finditer(clause):
            cents = int(Decimal((match[1] or match[2]).replace(',', '')) * 100)
            if cents not in allowed:
                # A user's own quoted amount can be repeated only as explicitly unverified.
                user_quote = re.search(r'你(?:说|提到|输入)|按你提供|假设|例如|比如', clause)
                in_user = any((match[1] or match[2]) in str(m.content) for m in messages if isinstance(m, HumanMessage))
                if not (user_quote and in_user):
                    issues.append('unsupported_amount')
            labels = list(re.finditer(r'实付|原价|单价|基数|每件|合计|退款|可退|能退|退回|退给',clause[:match.start()]))
            suffix_refund=re.match(r'(?:全部|都)?(?:可以|会|将|预计|能够|能|可)?(?:退还|退款|退给|退回|退)',clause[match.end():].strip())
            refund_label=bool(labels and labels[-1].group() in {'退款','可退','能退','退回','退给'})
            role_negated=bool(refund_label and _negates_prefix(clause,labels[-1].start()))
            if (refund_label or suffix_refund) and not role_negated:
                source = estimates if re.search(r'上次|上一轮|之前|刚才|此前',clause) else current_estimates
                expected = {d['refund_cents'] for d in source if d.get('status') == 'estimated'}
                if cents not in expected:
                    issues.append('refund_amount_mismatch')
        for match in re.finditer(r'(?<![\d.])(\d+)\s*分(?!钟|配)',clause):
            if int(match[1]) not in allowed:
                issues.append('unsupported_amount')
        if re.search(r'[零一二两三四五六七八九十百千万亿点]+(?:元|块钱|块)', clause):
            issues.append('uncheckable_amount_format')
        positive = re.search(r'(?:可以|可|能够|能|允许|支持)(?:申请)?(?:无理由)?(?:退货|退款|换货|退(?=\s*(?:[¥￥\d零一二]|$)))|符合.{0,8}(?:退货|退款|换货)条件', clause)
        conditional = re.search(r'如果|若|要求|规则|需|前提|内.*未拆封|内.*完好|内.*吊牌', clause)
        if positive and not hypothetical and not conditional and not _negates_prefix(clause,positive.start()):
            if scope and (not current_estimates or current_estimates[-1].get('status') != 'estimated'):
                issues.append('unsupported_eligibility')
        negative = re.search(r'(?:不能|不可以|不支持|不符合)(?:申请|无理由)?(?:退款|退货|换货)', clause)
        capability = re.search(r'我|助手|系统|工具|实际|执行|提交|批准', clause)
        if negative and not conditional and not capability and current_estimates and current_estimates[-1].get('status') == 'estimated':
            issues.append('contradictory_eligibility')
        if scope and not hypothetical and not re.search(r'不确定|可能|假设|状态包括|分为',clause):
            targets = [o for o in orders if o['order_id'] in clause] or orders
            for match in re.finditer(r'(?<!\d)(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?(?!\d)',clause):
                try:
                    value=date(*(int(match[i]) for i in (1,2,3))).isoformat()
                except ValueError:
                    issues.append('invalid_date')
                    continue
                labels=list(re.finditer(r'签收(?:日期|日|时间|于)?|业务日期|演示日期|计算日期|当前日期|今天',clause[:match.start()]))
                if labels and labels[-1].group().startswith('签收'):
                    if not any(o.get('delivered_at')==value for o in targets):
                        issues.append('delivery_date_mismatch')
                elif labels and labels[-1].group() in {'业务日期','演示日期','计算日期'}:
                    if not any(o.get('business_date')==value for o in targets):
                        issues.append('business_date_mismatch')
                elif value not in {database.business_today().isoformat(),*(o.get('delivered_at') for o in targets),*(o.get('business_date') for o in targets)}:
                    issues.append('unsupported_date')
            if re.search(r'签收|期限|退货|换货|规则',clause):
                for match in re.finditer(r'(?<!\d)(\d{1,3})\s*天',clause):
                    days=int(match[1])
                    if days not in allowed_days:
                        issues.append('unsupported_time_fact')
                    age=re.search(r'(?:已签收|签收后第|签收第|距签收)(?:\s|[为是：:])*$',clause[:match.start()])
                    window=clause[match.end():].lstrip().startswith('内')
                    if age and not window and not any(o.get('days_since_delivery')==days for o in targets):
                        issues.append('delivery_age_mismatch')
            asserted = []
            if re.search(r'已签收|已送达|已交付',clause):
                asserted.append({'delivered'})
            if re.search(r'待发货|尚未发货|还没发货',clause):
                asserted.append({'pending_ship'})
            if re.search(r'运输中|配送中|在途',clause):
                asserted.append({'in_transit'})
            if re.search(r'未签收|没有签收|还没签收',clause):
                asserted.append({'pending_ship','in_transit'})
            if len(asserted) == 1 and not any(o['status'] in asserted[0] for o in targets):
                issues.append('unsupported_order_status')
    references=set(CITATION.findall(text))|set(re.findall(r'(?<![A-Za-z0-9_-])DEMO-[A-Za-z0-9_-]+',text))
    if any(c not in rules for c in references):
        issues.append('unsupported_rule_reference')
    return sorted(set(issues))


def grounded_fallback(messages, confirmations=None):
    scope = active_orders(messages)
    observations = [(n, d) for n, d in tool_observations(messages)
                    if not scope or not d.get('order_id') or d['order_id'] in scope]
    last_user = max((i for i,m in enumerate(messages) if isinstance(m,HumanMessage)),default=0)
    estimates = [d for n,d in tool_observations(messages[last_user:]) if n == 'estimate_refund'
                 and (not scope or not d.get('order_id') or d['order_id'] in scope)]
    if estimates:
        result = estimates[-1]
        if result.get('status') == 'estimated':
            refs = ' '.join(f'[{x}]' for x in result.get('rule_ids', []))
            return (f"根据已确认的信息，订单 {result['order_id']} 的{result['item_name']}，退 {result['quantity']} 件，"
                    f"预计商品退款为 **{result['refund_yuan']} 元**。{refs}\n\n"
                    '这是按项目演示规则的试算，不含运费或补偿，最终需商家审核；尚未提交或执行售后。')
        if result.get('status') in {'needs_order_lookup','needs_policy_lookup'}:
            return '我还需要重新核对订单和对应售后规则，暂时不能确认退款结论。请重新发送本次问题；尚未执行售后。'
        if result.get('message'):
            return result['message'] + '\n\n尚未提交或执行售后。'
    orders = [d['order'] for n, d in observations if n == 'query_order' and d.get('order')
              and (not scope or d['order']['order_id'] in scope)]
    if orders:
        order = orders[-1]
        return f"已查到订单 {order['order_id']}：{order['logistics'] or '物流信息尚未填写'}。退款条件与金额仍需核对，尚未执行售后。"
    return '目前没有足够的订单或规则依据来确认这个结论。请提供订单号，或补充需要核对的信息；尚未执行任何售后操作。'
