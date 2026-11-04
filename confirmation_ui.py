"""An optional explicit confirmation for information that language alone left unclear."""
import hashlib
import json

import streamlit as st

from reliability import FACT_LABELS


def render_confirmation(pending):
    """Return (natural-language confirmation, trusted values) after user action."""
    facts = pending.get('facts',{})
    prefix = 'confirm_' + hashlib.sha256(json.dumps(pending,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:12]
    with st.expander('核对本次试算信息',expanded=True):
        st.write(f"订单 {pending['order_id']} · {pending['item_name']}（{pending['item_id']}）")
        st.caption('已明确的信息已带入；只需补充缺项。也可以直接在对话中继续说明。确认只用于试算。')
        with st.form(prefix):
            quantity = st.number_input('本次退货件数',min_value=1,max_value=99,step=1,
                                      value=facts.get('quantity'),key=prefix+'_quantity')
            reasons = [None,'no_reason','quality']
            reason_labels = {None:'请选择退货原因','no_reason':'无理由退货','quality':'质量问题'}
            reason = st.selectbox('退货原因',reasons,format_func=reason_labels.get,
                index=reasons.index(facts.get('reason')),key=prefix+'_reason')
            values = {'order_id':pending['order_id'],'item_id':pending['item_id'],
                      'quantity':quantity,'reason':reason}
            needed = pending.get('required_fields',pending.get('missing_fields',[]))
            for field in ('unopened','intact','tags_intact'):
                if field not in needed:
                    values[field] = facts.get(field)
                    continue
                options = [None,True,False]
                values[field] = st.selectbox(FACT_LABELS[field], options,
                    format_func=lambda value:{None:'尚未确认',True:'是',False:'否'}[value],
                    index=options.index(facts.get(field)),key=prefix+'_'+field)
            submitted = st.form_submit_button('确认信息并继续试算',type='primary')
        if not submitted:
            return None
        required = ['quantity','reason'] + ([] if reason == 'quality' else [k for k in needed if k not in ('quantity','reason')])
        missing = [FACT_LABELS[k] for k in required if values.get(k) is None]
        if missing:
            st.warning('请补充：'+'、'.join(missing)+'。')
            return None
        phrases = [f"订单 {pending['order_id']} 的{pending['item_name']}，退{quantity}件",
                   '无理由退货' if reason == 'no_reason' else '有质量问题']
        wording = {'unopened':('还没拆封','已经拆封了'), 'intact':('商品完好且未使用','商品使用过了'),
                   'tags_intact':('吊牌完整','吊牌不完整')}
        for key, labels in wording.items():
            if values.get(key) is not None:
                phrases.append(labels[0] if values[key] else labels[1])
        return '我已核对：'+'，'.join(phrases)+'。请继续试算。',values
