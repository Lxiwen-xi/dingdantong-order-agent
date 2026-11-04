"""Local order administration, separate from the assistant's read-only tools."""
import hashlib
import sqlite3

import streamlit as st

import database
from order_management import (CATEGORY_LABELS, STATUS_LABELS, OrderDataError, import_template,
                              money_text, parse_csv, preview_rows, save_rows, validate_row)


def order_table(orders):
    return [{'订单号': o['order_id'], '来源': '内置演示' if o['data_source'] == 'demo' else '本地录入',
             '状态': STATUS_LABELS[o['status']],
             '商品': '；'.join(f"{i['item_id']} {i['name']} ×{i['quantity']}" for i in o['items']),
             '实付商品金额': money_text(o['total_paid_cents']), '签收日期': o['delivered_at'] or '',
             '计算日期': o['business_date'], '距签收': f"{o['days_since_delivery']}天" if o['delivered_at'] else '尚未签收'}
            for o in orders]


def _success(result):
    st.session_state['management_notice'] = (f"已保存：新增 {result['new_orders']} 笔订单、"
        f"更新 {result['updated_orders']} 笔订单，共 {result['items']} 条商品明细。助手现在可以查询。")
    st.session_state['model_history'] = []
    st.session_state.pop('edit_snapshot', None)
    st.session_state.pop('csv_fingerprint', None)
    st.rerun()


def render_editor(orders):
    edit_mode = st.radio('录入方式', ['新增订单', '修改已有订单'], horizontal=True)
    existing = edit_mode == '修改已有订单'
    selected = None
    item = None
    if existing:
        if not orders:
            st.info('当前没有可修改的订单，请先新增或导入。')
            return
        selected_id = st.selectbox('选择订单', [o['order_id'] for o in orders])
        selected = next(o for o in orders if o['order_id'] == selected_id)
        item_options = [i['item_id'] for i in selected['items']] + ['＋新增商品明细']
        selected_item = st.selectbox('选择商品明细', item_options)
        item = next((i for i in selected['items'] if i['item_id'] == selected_item), None)
        st.caption('保存时更新订单状态与当前商品，其他商品明细保留。修改内置演示订单后，该订单会按当前日期计算售后期限。')
    else:
        st.caption('一次录入一条商品明细；同一订单的其他商品可在“修改已有订单”中继续添加，或通过 CSV 一次导入。')
    identity = (edit_mode, selected['order_id'] if selected else '', item['item_id'] if item else '')
    if st.session_state.get('edit_identity') != identity or 'edit_snapshot' not in st.session_state:
        st.session_state['edit_identity'] = identity
        st.session_state['edit_snapshot'] = database.data_revision()
    # Separate widget keys per selection, so switching orders cannot retain another order's fields.
    prefix = 'edit_' + hashlib.sha256(repr((identity, st.session_state['edit_snapshot'])).encode()).hexdigest()[:12]
    with st.form('order_editor'):
        left, right = st.columns(2)
        with left:
            order_id = st.text_input('订单号', value=selected['order_id'] if selected else '',
                                    disabled=existing, max_chars=40, key=prefix + '_order')
            states = list(STATUS_LABELS)
            status = st.selectbox('订单状态', states, format_func=STATUS_LABELS.get,
                                  index=states.index(selected['status']) if selected else 0, key=prefix + '_status')
            delivered_at = st.text_input('签收日期（YYYY-MM-DD；未签收留空）',
                value=(selected['delivered_at'] or '') if selected else '', key=prefix + '_date')
            logistics = st.text_input('物流信息', value=selected['logistics'] if selected else '',
                                      max_chars=300, key=prefix + '_logistics')
        with right:
            item_id = st.text_input('商品编号', value=item['item_id'] if item else '',
                                   disabled=bool(item), max_chars=40, key=prefix + '_item')
            name = st.text_input('商品名称', value=item['name'] if item else '', max_chars=100, key=prefix + '_name')
            categories = list(CATEGORY_LABELS)
            category = st.selectbox('商品类别', categories, format_func=CATEGORY_LABELS.get,
                index=categories.index(item['category']) if item else 0, key=prefix + '_category')
            quantity = st.number_input('购买数量', min_value=1, max_value=99, step=1,
                                       value=item['quantity'] if item else 1, key=prefix + '_quantity')
            paid = st.text_input('该商品实付总额（元，已分摊优惠）',
                value=money_text(item['paid_cents']) if item else '0.00', key=prefix + '_paid')
            refunded = st.number_input('已退数量', min_value=0, max_value=99, step=1,
                value=item['refunded_quantity'] if item else 0, key=prefix + '_refunded')
        st.caption('实付总额是该商品全部购买件数的合计金额，不是单价；保存只更新本地记录。')
        submit = st.form_submit_button('保存订单', type='primary')
    if submit:
        try:
            row = validate_row(dict(order_id=order_id, item_id=item_id, name=name, category=category,
                quantity=quantity, paid_amount=paid, status=status, delivered_at=delivered_at,
                logistics=logistics, refunded_quantity=refunded))
            result = save_rows([row], allow_update=existing, expected_revision=st.session_state['edit_snapshot'])
            _success(result)
        except OrderDataError as exc:
            st.error(str(exc))
        except sqlite3.Error:
            st.error('数据库暂时无法保存，请稍后重试。数据未部分写入。')
    if database.data_revision() != st.session_state.get('edit_snapshot'):
        st.warning('订单在此表单打开后已被修改，请先重新载入数据。')
        if st.button('重新载入订单表单'):
            st.session_state.pop('edit_snapshot', None)
            st.rerun()


def render_importer():
    st.write('下载模板，每一行是一条商品明细；同一订单的多件商品使用同一个订单号。')
    st.download_button('下载 CSV 模板', import_template(), '订单导入模板.csv', 'text/csv')
    st.caption('支持 UTF-8 或 GB18030 编码、中文或英文模板表头；最多1000行、2MB。已签收订单填签收日期，其余留空。')
    uploaded = st.file_uploader('上传订单 CSV', type=['csv'], key='orders_csv', max_upload_size=2)
    if uploaded is None:
        return
    content = uploaded.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    if st.session_state.get('csv_fingerprint') != digest:
        st.session_state['csv_fingerprint'] = digest
        st.session_state['csv_revision'] = database.data_revision()
    try:
        rows = parse_csv(content)
        preview = preview_rows(rows)
    except OrderDataError as exc:
        st.error(str(exc))
        return
    st.success(f'校验通过：{len({r["order_id"] for r in rows})}笔订单，{len(rows)}条商品明细。尚未保存。')
    st.dataframe(preview, hide_index=True, width='stretch')
    st.caption('同一订单号和商品编号会更新原记录，未列出的商品保留。修改已有订单会同时更新整笔订单的状态、物流和签收日期。')
    allow_update = st.checkbox('允许更新已存在的订单及商品', key='allow_order_updates')
    stale = database.data_revision() != st.session_state['csv_revision']
    if stale:
        st.warning('订单数据在此次预览后已变化，请重新核对预览。')
        if st.button('重新核对导入预览'):
            st.session_state['csv_revision'] = database.data_revision()
            st.rerun()
    if st.button('确认导入', type='primary', disabled=stale):
        try:
            _success(save_rows(rows, allow_update=allow_update,
                               expected_revision=st.session_state['csv_revision']))
        except OrderDataError as exc:
            st.error(str(exc))
        except sqlite3.Error:
            st.error('数据库暂时无法保存，请稍后重试。整批导入未部分写入。')


def render_management():
    st.subheader('订单管理')
    st.caption('数据保存在本机，重新打开项目仍可读取。当前日期：' + database.business_today().isoformat() + '（北京时间）')
    notice = st.session_state.pop('management_notice', '')
    if notice:
        st.success(notice)
    orders = database.list_orders()
    table_tab, edit_tab, csv_tab = st.tabs(['订单列表', '新增 / 修改', 'CSV 导入'])
    with table_tab:
        query = st.text_input('筛选订单号或商品名称', key='order_filter').strip().lower()
        filtered = [o for o in orders if not query or query in o['order_id'].lower() or
                    any(query in i['name'].lower() for i in o['items'])]
        st.caption(f'显示 {len(filtered)} 笔，共 {len(orders)} 笔订单。')
        st.dataframe(order_table(filtered), hide_index=True, width='stretch')
        if filtered:
            inspect_id = st.selectbox('查看订单明细', [o['order_id'] for o in filtered])
            order = next(o for o in filtered if o['order_id'] == inspect_id)
            st.write('物流：' + (order['logistics'] or '尚未填写'))
            st.dataframe([{'商品编号': i['item_id'], '商品名称': i['name'],
                           '商品类别': CATEGORY_LABELS[i['category']], '购买数量': i['quantity'],
                           '实付总额（元）': money_text(i['paid_cents']), '已退数量': i['refunded_quantity']}
                          for i in order['items']], hide_index=True, width='stretch')
    with edit_tab:
        render_editor(orders)
    with csv_tab:
        render_importer()
