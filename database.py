"""SQLite orders, with fixed-date examples and current-date local records."""
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / 'data' / 'orders.db'
DEMO_DATE = '2026-11-03'


def business_today() -> date:
    return datetime.now(ZoneInfo('Asia/Shanghai')).date()


def init_db(path: Path | None = None):
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as conn:
        with conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    delivered_at TEXT, logistics TEXT NOT NULL,
                    data_source TEXT NOT NULL DEFAULT 'demo');
                CREATE TABLE IF NOT EXISTS items (
                    order_id TEXT NOT NULL, item_id TEXT NOT NULL, name TEXT NOT NULL,
                    category TEXT NOT NULL, quantity INTEGER NOT NULL,
                    paid_cents INTEGER NOT NULL, refunded_quantity INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(order_id, item_id));
            ''')
            conn.execute('BEGIN IMMEDIATE')
            columns = {row[1] for row in conn.execute('PRAGMA table_info(orders)')}
            if 'data_source' not in columns:
                conn.execute("ALTER TABLE orders ADD COLUMN data_source TEXT NOT NULL DEFAULT 'demo'")
            conn.execute('INSERT OR IGNORE INTO metadata VALUES (?, ?)', ('demo_date', DEMO_DATE))
            conn.execute('INSERT OR IGNORE INTO metadata VALUES (?, ?)', ('data_revision', '0'))
            orders = [
                ('A1001', 'delivered', '2026-11-01', '演示快递：已签收'),
                ('A1002', 'delivered', '2026-10-20', '演示快递：已签收'),
                ('A1003', 'in_transit', None, '演示快递：运输中，预计业务日期次日送达'),
                ('A1004', 'pending_ship', None, '演示订单：待发货'),
                ('A1005', 'delivered', '2026-11-02', '演示数字商品：已交付'),
                ('A1006', 'delivered', '2026-10-27', '演示快递：已签收'),
            ]
            conn.executemany('INSERT OR IGNORE INTO orders(order_id,status,delivered_at,logistics) VALUES (?,?,?,?)', orders)
            items = [
                ('A1001', 'E01', '无线耳机', 'electronics', 2, 39901, 0),
                ('A1001', 'A01', '数据线', 'accessories', 1, 4099, 0),
                ('A1002', 'E02', '蓝牙音箱', 'electronics', 1, 15900, 0),
                ('A1003', 'C01', '连帽卫衣', 'apparel', 1, 12900, 0),
                ('A1004', 'A02', '手机支架', 'accessories', 2, 5999, 0),
                ('A1005', 'D01', '在线课程兑换码', 'digital', 1, 9900, 0),
                ('A1006', 'C02', '运动外套', 'apparel', 1, 18900, 0),
            ]
            conn.executemany('INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?,?)', items)


def _order_result(header, items, demo_date):
    result = dict(header)
    result['items'] = [dict(item) for item in items]
    result['business_date'] = demo_date if result['data_source'] == 'demo' else business_today().isoformat()
    result['date_basis'] = '固定演示日期' if result['data_source'] == 'demo' else '当前日期（北京时间）'
    # Backwards-compatible field; new code uses business_date/date_basis.
    result['demo_date'] = result['business_date']
    result['days_since_delivery'] = ((date.fromisoformat(result['business_date']) -
        date.fromisoformat(result['delivered_at'])).days if result['delivered_at'] else None)
    result['total_paid_cents'] = sum(item['paid_cents'] for item in result['items'])
    result['data_kind'] = '虚构演示订单' if result['data_source'] == 'demo' else '本地录入订单（未连接商城核验）'
    return result


def read_order(order_id: str, path: Path | None = None) -> dict | None:
    with closing(sqlite3.connect(Path(path or DB_PATH).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        order = conn.execute('SELECT * FROM orders WHERE order_id=?', (order_id,)).fetchone()
        if order is None:
            return None
        items = list(conn.execute(
            'SELECT item_id,name,category,quantity,paid_cents,refunded_quantity '
            'FROM items WHERE order_id=? ORDER BY item_id', (order_id,)))
        demo_date = conn.execute("SELECT value FROM metadata WHERE key='demo_date'").fetchone()[0]
        return _order_result(order, items, demo_date)


def list_orders(path: Path | None = None) -> list[dict]:
    path = path or DB_PATH
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        headers = list(conn.execute('SELECT * FROM orders ORDER BY order_id'))
        items = {}
        for row in conn.execute('SELECT * FROM items ORDER BY order_id,item_id'):
            items.setdefault(row['order_id'], []).append(dict(row))
        demo_date = conn.execute("SELECT value FROM metadata WHERE key='demo_date'").fetchone()[0]
    return [_order_result(header, items.get(header['order_id'], []), demo_date) for header in headers]


def data_revision(path: Path | None = None) -> int:
    with closing(sqlite3.connect(Path(path or DB_PATH).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        return int(conn.execute("SELECT value FROM metadata WHERE key='data_revision'").fetchone()[0])


def load_policies() -> dict:
    return json.loads((ROOT / 'data' / 'policies.json').read_text(encoding='utf-8'))
