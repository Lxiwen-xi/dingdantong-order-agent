import io
from pathlib import Path

from langchain_core.messages import AIMessage
from streamlit.testing.v1 import AppTest

import database
import management_ui
from order_management import import_template, save_rows, validate_row

APP = str(Path(__file__).resolve().parents[1] / 'app.py')


def find(elements, label):
    return next(e for e in elements if e.label == label)


def management():
    app = AppTest.from_file(APP, default_timeout=45).run()
    find(app.radio, '工作区').set_value('订单管理').run()
    assert not app.exception
    return app


def test_create_order_edit_and_query_it_from_existing_tools():
    app = management()
    for label, value in [('订单号', 'B9001'), ('商品编号', 'MOUSE-9'), ('商品名称', '测试鼠标'),
                         ('该商品实付总额（元，已分摊优惠）', '39.99')]:
        find(app.text_input, label).set_value(value)
    find(app.button, '保存订单').click().run()
    assert not app.exception
    assert database.read_order('B9001')['total_paid_cents'] == 3999
    assert any('助手现在可以查询' in s.value for s in app.success)
    find(app.radio, '录入方式').set_value('修改已有订单').run()
    find(app.selectbox, '选择订单').set_value('B9001').run()
    assert find(app.text_input, '商品名称').value == '测试鼠标'
    find(app.text_input, '该商品实付总额（元，已分摊优惠）').set_value('29.50')
    find(app.button, '保存订单').click().run()
    assert not app.exception
    assert database.read_order('B9001')['total_paid_cents'] == 2950
    find(app.radio, '工作区').set_value('售后咨询').run()
    assert not app.exception
    assert any('B9001' in str(df.value) for df in app.dataframe)


def test_invalid_form_does_not_write():
    app = management()
    find(app.text_input, '订单号').set_value('NEW1')
    find(app.button, '保存订单').click().run()
    assert not app.exception
    assert app.error
    assert database.read_order('NEW1') is None


def test_csv_preview_then_confirm_import(monkeypatch):
    upload = io.BytesIO(import_template())
    monkeypatch.setattr(management_ui.st, 'file_uploader', lambda *a, **k: upload)
    app = management()
    assert database.read_order('B2001') is None
    assert any('尚未保存' in s.value for s in app.success)
    find(app.button, '确认导入').click().run()
    assert not app.exception
    assert database.read_order('B2001')['total_paid_cents'] == 8990
    assert database.read_order('B2002')['status'] == 'in_transit'
    assert len(database.list_orders()) == 8


def test_invalid_csv_is_shown_without_write(monkeypatch):
    monkeypatch.setattr(management_ui.st, 'file_uploader', lambda *a, **k: io.BytesIO(b'bad\nvalue\n'))
    app = management()
    assert not app.exception
    assert app.error
    assert len(database.list_orders()) == 6


def test_order_changes_in_another_session_invalidate_model_context():
    app = AppTest.from_file(APP, default_timeout=45).run()
    app.session_state['model_history'] = [AIMessage(content='旧订单金额')]
    save_rows([validate_row(dict(order_id='NEW1', item_id='ITEM1', name='新商品', category='electronics',
        quantity=1, paid_amount='10.00', status='pending_ship', delivered_at='', logistics=''))])
    app.run()
    assert not app.exception
    assert app.session_state['model_history'] == []
    assert any('订单数据已更新' in s.value for s in app.info)
