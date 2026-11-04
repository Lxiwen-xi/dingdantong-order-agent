"""Validated admin writes/CSV import; never exposed as tools to the model."""
import csv
import io
import re
import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal, InvalidOperation

import database

ID_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$'
STATUS_LABELS = {'pending_ship': '待发货', 'in_transit': '运输中', 'delivered': '已签收'}
CATEGORY_LABELS = {'electronics': '数码设备', 'accessories': '普通配件', 'apparel': '服饰', 'digital': '数字商品'}
COLUMNS = {'订单号': 'order_id', '商品编号': 'item_id', '商品名称': 'name', '商品类别': 'category',
           '数量': 'quantity', '实付金额': 'paid_amount', '订单状态': 'status', '签收日期': 'delivered_at',
           '物流信息': 'logistics', '已退数量': 'refunded_quantity'}
REQUIRED = set(COLUMNS.values()) - {'refunded_quantity'}
MAX_ROWS, MAX_BYTES = 1000, 2 * 1024 * 1024


class OrderDataError(ValueError):
    pass


def money_text(cents):
    return f'{cents // 100}.{cents % 100:02d}'


def _text(value):
    return '' if value is None else str(value).strip()


def _integer(value, name, minimum, maximum):
    text = _text(value)
    if len(text) > 3 or not re.fullmatch(r'[0-9]+', text):
        raise OrderDataError(f'{name}必须为整数。')
    result = int(text)
    if not minimum <= result <= maximum:
        raise OrderDataError(f'{name}需在 {minimum}～{maximum} 之间。')
    return result


def validate_row(raw: dict) -> dict:
    result = {key: _text(raw.get(key)) for key in REQUIRED}
    for key, label in [('order_id', '订单号'), ('item_id', '商品编号')]:
        if not re.fullmatch(ID_PATTERN, result[key]):
            raise OrderDataError(f'{label}需为1～40位字母、数字、下划线或短横线，首位为字母或数字。')
    if not 1 <= len(result['name']) <= 100:
        raise OrderDataError('商品名称需为1～100个字符。')
    if len(result['logistics']) > 300:
        raise OrderDataError('物流信息不能超过300个字符。')
    for key, labels, label in [('status', STATUS_LABELS, '订单状态'), ('category', CATEGORY_LABELS, '商品类别')]:
        result[key] = {v: k for k, v in labels.items()}.get(result[key], result[key])
        if result[key] not in labels:
            raise OrderDataError(f'{label}请选择：' + '、'.join(labels.values()) + '。')
    result['quantity'] = _integer(raw.get('quantity'), '数量', 1, 99)
    # A missing count preserves an existing count at commit time.
    refunded = raw.get('refunded_quantity')
    result['refunded_quantity'] = (None if _text(refunded) == '' else
        _integer(refunded, '已退数量', 0, result['quantity']))
    amount = result.pop('paid_amount')
    try:
        if len(amount) > 16 or not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', amount):
            raise InvalidOperation
        cents = Decimal(amount) * 100
        if not 0 <= cents <= 100_000_000:
            raise InvalidOperation
        result['paid_cents'] = int(cents)
    except InvalidOperation:
        raise OrderDataError('实付金额需为0～100万元之间的非负数字，最多两位小数。') from None
    value = result['delivered_at']
    if result['status'] == 'delivered':
        try:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError
            delivered = date.fromisoformat(value)
        except ValueError:
            raise OrderDataError('已签收订单必须填写有效签收日期，格式为 YYYY-MM-DD。') from None
        if delivered > database.business_today():
            raise OrderDataError('签收日期不能晚于当前北京时间的日期。')
    elif value:
        raise OrderDataError('待发货或运输中的订单，签收日期应留空。')
    result['delivered_at'] = value or None
    return result


def _check_batch(rows):
    if not rows or len(rows) > MAX_ROWS:
        raise OrderDataError(f'每批需包含1～{MAX_ROWS}条商品明细。')
    seen, headers = set(), {}
    for row in rows:
        key = (row['order_id'], row['item_id'])
        if key in seen:
            raise OrderDataError(f"同一批中商品明细重复：{key[0]} / {key[1]}。")
        seen.add(key)
        header = (row['status'], row['delivered_at'], row['logistics'])
        if row['order_id'] in headers and headers[row['order_id']] != header:
            raise OrderDataError(f"订单 {row['order_id']} 的状态、签收日期或物流信息在多行中不一致。")
        headers[row['order_id']] = header


def parse_csv(content: bytes) -> list[dict]:
    if len(content) > MAX_BYTES:
        raise OrderDataError('CSV 文件不能超过2MB。')
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise OrderDataError('无法识别文件编码，请另存为 UTF-8 CSV。')
    try:
        reader = csv.DictReader(io.StringIO(text, newline=''), strict=True)
        original = reader.fieldnames
        if not original:
            raise OrderDataError('CSV 为空或缺少表头。')
        fields = [COLUMNS.get(h.strip(), h.strip()) for h in original]
        if len(fields) != len(set(fields)):
            raise OrderDataError('表头重复，请每个字段只保留一列。')
        missing = REQUIRED - set(fields)
        if missing:
            labels = {v: k for k, v in COLUMNS.items()}
            raise OrderDataError('缺少列：' + '、'.join(labels[k] for k in sorted(missing)) + '。请下载模板。')
        unknown = set(fields) - set(COLUMNS.values())
        if unknown:
            raise OrderDataError('存在不支持的列：' + '、'.join(sorted(unknown)) + '。请按模板整理。')
        reader.fieldnames = fields
        rows = []
        for raw in reader:
            if len(rows) >= MAX_ROWS:
                raise OrderDataError(f'最多导入{MAX_ROWS}条商品明细。')
            if None in raw or any(value is None for value in raw.values()):
                raise OrderDataError(f'第{reader.line_num}行列数与表头不一致。')
            try:
                rows.append(validate_row(raw))
            except OrderDataError as exc:
                raise OrderDataError(f'第{reader.line_num}行：{exc}') from None
        _check_batch(rows)
        return rows
    except csv.Error:
        raise OrderDataError('CSV 格式不正确，请检查逗号与双引号。') from None


def import_template() -> bytes:
    buf = io.StringIO(newline='')
    writer = csv.writer(buf)
    writer.writerow(list(COLUMNS))
    writer.writerow(['B2001', 'E03', '无线鼠标', '数码设备', 1, '89.90', '已签收', database.business_today().isoformat(), '示例物流：已签收', 0])
    writer.writerow(['B2002', 'C03', '短袖', '服饰', 2, '119.00', '运输中', '', '示例物流：运输中', 0])
    return buf.getvalue().encode('utf-8-sig')


def preview_rows(rows):
    existing = {o['order_id']: o for o in database.list_orders()}
    result = []
    for row in rows:
        order = existing.get(row['order_id'])
        is_item = bool(order and any(i['item_id'] == row['item_id'] for i in order['items']))
        result.append({'订单号': row['order_id'], '商品编号': row['item_id'], '商品名称': row['name'],
                       '商品类别': CATEGORY_LABELS[row['category']], '数量': row['quantity'],
                       '实付金额（元）': money_text(row['paid_cents']), '订单状态': STATUS_LABELS[row['status']],
                       '签收日期': row['delivered_at'] or '', '物流信息': row['logistics'],
                       '处理方式': '更新商品' if is_item else ('新增商品到已有订单' if order else '新增订单商品')})
    return result


def save_rows(rows, *, allow_update=False, expected_revision=None, path=None):
    # Validate again at the write boundary, even when the caller already previewed.
    rows = [validate_row({**r, 'paid_amount': money_text(r['paid_cents'])}) for r in rows]
    _check_batch(rows)
    path = path or database.DB_PATH
    with closing(sqlite3.connect(path, timeout=10)) as conn:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            revision = int(conn.execute("SELECT value FROM metadata WHERE key='data_revision'").fetchone()[0])
            if expected_revision is not None and revision != expected_revision:
                raise OrderDataError('订单数据在预览后已发生变化，请重新核对后保存。')
            ids = {r['order_id'] for r in rows}
            existing = {r[0] for r in conn.execute('SELECT order_id FROM orders') if r[0] in ids}
            if existing and not allow_update:
                raise OrderDataError('订单已存在：' + '、'.join(sorted(existing)) + '。请使用修改模式或允许更新。')
            # Check every item before updating any header; failures roll back the entire batch.
            for row in rows:
                previous = conn.execute('SELECT refunded_quantity FROM items WHERE order_id=? AND item_id=?',
                                        (row['order_id'], row['item_id'])).fetchone()
                if row['refunded_quantity'] is None:
                    row['refunded_quantity'] = previous[0] if previous else 0
                if row['refunded_quantity'] > row['quantity']:
                    raise OrderDataError(f"订单 {row['order_id']} / {row['item_id']} 的数量不能少于已退数量。")
            for row in rows:
                conn.execute('''INSERT INTO orders(order_id,status,delivered_at,logistics,data_source)
                    VALUES (?,?,?,?,'local') ON CONFLICT(order_id) DO UPDATE SET
                    status=excluded.status,delivered_at=excluded.delivered_at,
                    logistics=excluded.logistics,data_source='local' ''',
                    (row['order_id'], row['status'], row['delivered_at'], row['logistics']))
                conn.execute('''INSERT INTO items(order_id,item_id,name,category,quantity,paid_cents,refunded_quantity)
                    VALUES (?,?,?,?,?,?,?) ON CONFLICT(order_id,item_id) DO UPDATE SET
                    name=excluded.name,category=excluded.category,quantity=excluded.quantity,
                    paid_cents=excluded.paid_cents,refunded_quantity=excluded.refunded_quantity''',
                    tuple(row[k] for k in ('order_id','item_id','name','category','quantity','paid_cents','refunded_quantity')))
            conn.execute("UPDATE metadata SET value=? WHERE key='data_revision'", (str(revision + 1),))
    return {'new_orders': len(ids - existing), 'updated_orders': len(existing), 'items': len(rows), 'revision': revision + 1}
