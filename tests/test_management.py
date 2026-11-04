import csv
import io
import json
import sqlite3
from datetime import date, timedelta

import pytest

import database
from order_management import (COLUMNS, OrderDataError, import_template, parse_csv,
                              preview_rows, save_rows, validate_row)
from tools import estimate_refund, query_order


def raw_row(**changes):
    value = dict(order_id='B2001', item_id='MOUSE-01', name='无线鼠标', category='electronics',
                 quantity='1', paid_amount='89.90', status='delivered',
                 delivered_at=database.business_today().isoformat(), logistics='测试物流：已签收')
    value.update(changes)
    return value


def csv_bytes(rows, encoding='utf-8-sig'):
    buf = io.StringIO(newline='')
    writer = csv.DictWriter(buf, fieldnames=list(COLUMNS.values()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode(encoding)


def test_template_is_readable_and_imported_orders_reach_existing_agent_tools():
    rows = parse_csv(import_template())
    preview = preview_rows(rows)
    assert all(r['处理方式'] == '新增订单商品' for r in preview)
    assert database.read_order('B2001') is None  # preview never writes
    result = save_rows(rows, expected_revision=0)
    assert result == {'new_orders': 2, 'updated_orders': 0, 'items': 2, 'revision': 1}
    order = json.loads(query_order.invoke({'order_id': 'B2001'}))['order']
    assert order['data_source'] == 'local' and order['total_paid_cents'] == 8990
    assert order['business_date'] == database.business_today().isoformat()
    refund = json.loads(estimate_refund.invoke(dict(order_id='B2001', item_id='E03',
        quantity=1, reason='no_reason', unopened=True)))
    assert refund['refund_cents'] == 8990
    assert '本地录入' in refund['data_kind']


def test_partial_upsert_retains_unlisted_items_and_refund_count():
    rows = [validate_row(raw_row(quantity='2', refunded_quantity='1')),
            validate_row(raw_row(item_id='CABLE_2', category='accessories', name='线材', paid_amount='9.99'))]
    save_rows(rows)
    changed = validate_row(raw_row(quantity='2', paid_amount='69.90', logistics='更新物流'))
    with pytest.raises(OrderDataError, match='已存在'):
        save_rows([changed])
    save_rows([changed], allow_update=True, expected_revision=1)
    order = database.read_order('B2001')
    assert len(order['items']) == 2
    mouse = next(i for i in order['items'] if i['item_id'] == 'MOUSE-01')
    assert mouse['paid_cents'] == 6990 and mouse['refunded_quantity'] == 1
    assert order['logistics'] == '更新物流'
    assert database.data_revision() == 2


def test_numeric_order_number_and_multi_item_csv_with_quotes():
    rows = parse_csv(csv_bytes([raw_row(order_id='202611040000001234', name='鼠标,黑色'),
                               raw_row(order_id='202611040000001234', item_id='KEYBOARD-2', name='键盘')]))
    save_rows(rows)
    order = json.loads(query_order.invoke({'order_id': '202611040000001234'}))['order']
    assert len(order['items']) == 2
    assert order['total_paid_cents'] == 17980


@pytest.mark.parametrize('changes', [
    {'quantity': '1.5'}, {'quantity': '0'}, {'quantity': '9' * 5000},
    {'paid_amount': '-1'}, {'paid_amount': 'NaN'}, {'paid_amount': '1.001'},
    {'paid_amount': '1000000.01'}, {'refunded_quantity': '2'},
    {'delivered_at': ''}, {'delivered_at': '2026-99-01'},
    {'delivered_at': (database.business_today() + timedelta(days=1)).isoformat()},
    {'status': 'pending_ship'}, {'category': 'unknown'},
    {'order_id': "B2001' OR 1=1"}, {'name': ''},
])
def test_invalid_rows_are_rejected_before_write(changes):
    with pytest.raises(OrderDataError):
        parse_csv(csv_bytes([raw_row(**changes)]))
    assert database.read_order('B2001') is None
    assert database.data_revision() == 0


def test_duplicate_and_conflicting_order_headers_are_rejected():
    with pytest.raises(OrderDataError, match='重复'):
        parse_csv(csv_bytes([raw_row(), raw_row()]))
    with pytest.raises(OrderDataError, match='不一致'):
        parse_csv(csv_bytes([raw_row(), raw_row(item_id='NEW2', logistics='不同物流')]))
    with pytest.raises(OrderDataError, match='缺少列'):
        parse_csv('订单号,商品名称\nB2001,鼠标\n'.encode('utf-8'))
    with pytest.raises(OrderDataError, match='表头重复'):
        parse_csv(b'order_id,order_id\nB2001,B2001\n')


def test_gb18030_import_and_file_limits():
    assert parse_csv(csv_bytes([raw_row()], 'gb18030'))[0]['name'] == '无线鼠标'
    with pytest.raises(OrderDataError, match='2MB'):
        parse_csv(b'x' * (2 * 1024 * 1024 + 1))
    with pytest.raises(OrderDataError, match='1000'):
        parse_csv(csv_bytes([raw_row(order_id=f'B{i}') for i in range(1001)]))


def test_stale_preview_blocks_all_changes():
    row = validate_row(raw_row())
    save_rows([row])
    with pytest.raises(OrderDataError, match='预览后'):
        save_rows([validate_row(raw_row(order_id='B2002'))], expected_revision=0)
    assert database.read_order('B2002') is None
    assert database.data_revision() == 1


def test_sql_failure_rolls_back_entire_batch(demo_db):
    with sqlite3.connect(demo_db) as conn:
        conn.execute("CREATE TRIGGER reject_bad_item BEFORE INSERT ON items WHEN NEW.item_id='FAIL' "
                     "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    rows = [validate_row(raw_row()), validate_row(raw_row(order_id='B2002', item_id='FAIL'))]
    with pytest.raises(sqlite3.IntegrityError):
        save_rows(rows)
    assert database.read_order('B2001') is None and database.read_order('B2002') is None
    assert database.data_revision() == 0


def test_demo_and_local_dates_change_independently(monkeypatch):
    monkeypatch.setattr(database, 'business_today', lambda: date(2026, 11, 4))
    save_rows([validate_row(raw_row(delivered_at='2026-11-03'))])
    assert database.read_order('B2001')['days_since_delivery'] == 1
    monkeypatch.setattr(database, 'business_today', lambda: date(2026, 11, 5))
    assert database.read_order('B2001')['days_since_delivery'] == 2
    assert database.read_order('A1001')['days_since_delivery'] == 2
    save_rows([validate_row(raw_row(order_id='A1001', item_id='E01', delivered_at='2026-11-01'))], allow_update=True)
    assert database.read_order('A1001')['days_since_delivery'] == 4
    database.init_db()
    assert database.read_order('A1001')['data_source'] == 'local'
    assert database.read_order('A1001')['items'][1]['paid_cents'] == 8990


def test_legacy_database_upgrade_preserves_values(tmp_path):
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''CREATE TABLE orders(order_id TEXT PRIMARY KEY,status TEXT NOT NULL,
            delivered_at TEXT,logistics TEXT NOT NULL);
            CREATE TABLE items(order_id TEXT,item_id TEXT,name TEXT,category TEXT,quantity INTEGER,
            paid_cents INTEGER,refunded_quantity INTEGER,PRIMARY KEY(order_id,item_id));
            INSERT INTO orders VALUES ('A1001','delivered','2026-11-01','保留物流');
            INSERT INTO items VALUES ('A1001','E01','保留商品','electronics',1,12345,0);''')
    database.init_db(path)
    order = database.read_order('A1001', path)
    assert order['logistics'] == '保留物流'
    assert next(i for i in order['items'] if i['item_id'] == 'E01')['paid_cents'] == 12345
    assert order['data_source'] == 'demo'
    assert database.data_revision(path) == 0
