"""稳定来源身份、双向唯一关联及来源发布锁。"""

import hashlib
import re
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal

from ..domain.accounts import decision_currency
from ..domain.errors import Conflict, ImporterError
from ..domain.identity import transaction_id
from .records import source_row

RESIDUAL_DAYS = 1
DOMESTIC_COUNTRY = 'CN'


def lock_source(store, source_id: str):
    key = int.from_bytes(hashlib.sha256(source_id.encode()).digest()[:4], 'big', signed=True)
    store.execute('SELECT pg_advisory_xact_lock(780420,%s)', (key,))


def lock_transaction_source(store, tid: str):
    report = store.one('''SELECT r.source_id FROM bank_report r JOIN bank_transactions t
        ON t.report_key=r.report_key WHERE t.id=%s''', (tid,))
    if not report:
        raise ImporterError('transaction source disappeared')
    lock_source(store, report['source_id'])


def normalized(value: str) -> str:
    return ''.join(value.split()).casefold()


def merchant_identity(value: str) -> str:
    compact = normalized(value)
    if re.fullmatch(r'google\*chatgpt(?:[0-9]{3}-[0-9]{3}-[0-9]{4}caus|mountainviewcaus)?', compact):
        return 'google*chatgpt'
    return compact


def card_tail(row: dict) -> str:
    return (row.get('card_reference') or '')[-4:]


def match_key(row: dict) -> tuple:
    return (row['event_type'], str(row['occurred_date']), card_tail(row), Decimal(row['original_amount']))


def transaction_rows(transaction):
    return transaction.get('_source_rows') or [source_row(transaction)]


def source_compatible(row, transaction):
    """未知原币不构成冲突；已由其他来源确认的币种/完整卡号不能丢弃。"""
    facts = transaction_rows(transaction)
    currencies = {r['original_currency'] for r in facts if r.get('original_currency')}
    if transaction.get('original_currency'):
        currencies.add(transaction['original_currency'])
    if len(currencies) > 1:
        return False
    if row.get('original_currency') and currencies and currencies != {row['original_currency']}:
        return False
    references = {r['card_reference'] for r in facts if len(r.get('card_reference') or '') > 4}
    reference = row.get('card_reference') or ''
    return len(references) <= 1 and (len(reference) <= 4 or not references or references == {reference})


def candidate_index(transactions: list[dict]) -> dict[tuple, list[dict]]:
    index: dict[tuple, list[dict]] = defaultdict(list)
    for transaction in transactions:
        for row in transaction_rows(transaction):
            key = match_key(row)
            if not any(t['id'] == transaction['id'] for t in index[key]):
                index[key].append(transaction)
    return index


def source_candidates(rows, transactions):
    index = candidate_index(transactions)
    # Known conflicting currency is still evidence to review, never permission to create.
    return {row['row_key']: [] if row['event_type'] == 'statement' else
            list(index.get(match_key(row), [])) for row in rows}


def automatic_candidates(rows, transactions, bindings):
    """尊重已绑定数量；描述仅在结构多候选时提供辅助唯一配对。"""
    candidates = source_candidates(rows, transactions)
    by_id = {t['id']: t for t in transactions}
    occupied = {s['transaction_id'] for s in bindings.values() if s.get('transaction_id')}
    for row in rows:
        key = row['row_key']
        bound = bindings.get(key, {})
        if bound.get('transaction_id'):
            candidates[key] = [by_id[bound['transaction_id']]]
        elif (bound.get('last_resolution') or {}).get('action') == 'ignore':
            candidates[key] = []
        else:
            group = [t for t in candidates[key] if t['id'] not in occupied]
            references = {f['card_reference'] for t in group for f in transaction_rows(t)
                          if len(f.get('card_reference') or '') > 4}
            preferred = [t for t in group if source_compatible(row, t) and any(
                merchant_identity(f['merchant_raw']) == merchant_identity(row['merchant_raw'])
                for f in transaction_rows(t))]
            tail_ambiguous = len(row.get('card_reference') or '') <= 4 and len(references) > 1
            candidates[key] = preferred if len(preferred) == 1 and not tail_ambiguous else group
    return candidates


def is_foreign(row) -> bool:
    """月报国家列非 CN 的消费/退款；日报无国家列时由调用方按币种判断。"""
    country = (row.get('extra') or {}).get('country')
    return bool(country) and country != DOMESTIC_COUNTRY


def unsettled_usd(transaction) -> bool:
    if transaction.get('settlement_adjustment') or not (transaction.get('import_decision') or {}).get('payload'):
        return False
    try:
        return decision_currency(transaction) == 'USD'
    except ImporterError:
        # 决定币种损坏不算可结算 USD；该行随后按近日未关联交易暂停，不会补录。
        return False


def has_foreign_monthly(transaction) -> bool:
    return any(f.get('_report_type') == 'monthly' and is_foreign(f) for f in transaction_rows(transaction))


def merchant_related(a: str, b: str) -> bool:
    """银行常在一侧追加电话/地区；规范化后一方是另一方前缀才算相关。"""
    left, right = merchant_identity(a), merchant_identity(b)
    short = min(left, right, key=len)
    return len(short) >= 3 and (left.startswith(right) or right.startswith(left))


def residual_match(row, transaction) -> bool:
    """同日只靠结构；跨一天时要求商户描述相关，降低把相邻另一笔消费当成同笔的风险。"""
    if str(transaction['occurred_date']) == str(row['occurred_date']):
        return True
    return any(merchant_related(row['merchant_raw'], f['merchant_raw']) for f in transaction_rows(transaction))


def reverse_daily_pool(row, transactions):
    """外币日报后到：只考虑来源为境外月报行、尚无日报来源的交易。"""
    if row.get('original_currency') in (None, 'CNY'):
        return []
    return [t for t in residual_pool(row, transactions, missing='daily')
            if has_foreign_monthly(t) and residual_match(row, t) and source_compatible(row, t)]


def residual_pool(row, transactions, *, missing: str):
    """同卡同类型、交易日相差不超过一天、尚无 missing 类型来源的交易；不比较金额。"""
    day = date.fromisoformat(str(row['occurred_date']))
    tail = card_tail(row)
    pool = []
    for t in transactions:
        facts = transaction_rows(t)
        if any(f.get('_report_type') == missing for f in facts):
            continue
        if t['event_type'] != row['event_type'] or card_tail(t) != tail:
            continue
        if abs((date.fromisoformat(str(t['occurred_date'])) - day).days) <= RESIDUAL_DAYS:
            pool.append(t)
    return pool


def residual_candidates(rows, transactions, bindings, candidates):
    """精确配对失败的境外月报行：在未结算 USD 日报中做双向唯一配对。

    返回 (resolved, blocked)：resolved 为唯一配对；blocked 为同卡近日仍有未关联
    日报交易、不能按无候选补录的行及其候选 ID。
    """
    occupied = {s['transaction_id'] for s in bindings.values() if s.get('transaction_id')}
    occupied |= {t['id'] for group in candidates.values() for t in group}
    pools = {}
    for row in rows:
        key = row['row_key']
        bound = bindings.get(key, {})
        if (row['event_type'] == 'statement' or candidates.get(key) or not is_foreign(row)
                or bound.get('transaction_id') or (bound.get('last_resolution') or {}).get('action') == 'ignore'):
            continue
        pools[key] = [t for t in residual_pool(row, transactions, missing='monthly') if t['id'] not in occupied]
    by_key = {row['row_key']: row for row in rows}
    usd = {k: [t for t in v if unsettled_usd(t) and residual_match(by_key[k], t)] for k, v in pools.items()}
    counts = Counter(t['id'] for group in usd.values() for t in group)
    resolved, blocked = {}, {}
    for key, group in usd.items():
        if len(group) == 1 and counts[group[0]['id']] == 1:
            resolved[key] = group[0]
        elif pools[key]:
            blocked[key] = [t['id'] for t in pools[key]]
    return resolved, blocked


def link_eligible(row, transaction, transactions, *, missing: str) -> bool:
    """自动与人工关联共用：精确候选，或满足剩余配对范围（不比较金额）。"""
    if transaction['id'] in {t['id'] for t in source_candidates([row], [transaction])[row['row_key']]}:
        return source_compatible(row, transaction)
    if transaction['id'] not in {t['id'] for t in residual_pool(row, transactions, missing=missing)}:
        return False
    if missing == 'monthly':
        foreign = is_foreign(row)
    else:
        foreign = row.get('original_currency') not in (None, 'CNY') and has_foreign_monthly(transaction)
    # 放宽的只是金额和交易日；完整卡号或已确认原币冲突仍拒绝。
    return foreign and source_compatible(row, transaction)


def load_transactions(store, source_id):
    transactions = store.all('''SELECT t.* FROM bank_transactions t JOIN bank_report r
        ON r.report_key=t.report_key WHERE r.source_id=%s ORDER BY t.id''', (source_id,))
    sources = store.all('''SELECT s.*,r.content,r.content_fingerprint,r.report_type FROM transaction_source s
        JOIN bank_report r ON r.report_key=s.report_key WHERE r.source_id=%s
        ORDER BY s.report_key,s.row_key''', (source_id,))
    by_id = {t['id']: t for t in transactions}
    for t in transactions:
        t['_source_rows'] = []
    for s in sources:
        if s['transaction_id'] in by_id:
            by_id[s['transaction_id']]['_source_rows'].append(
                {**report_row(s, s['row_key']), '_report_type': s['report_type']})
    return transactions, sources


def report_row(report, row_key):
    rows = [r for r in report['content']['rows'] if r['row_key'] == row_key]
    if len(rows) != 1:
        raise ImporterError('source row must exist uniquely in accepted report')
    return rows[0]


def register_sources(store, report):
    for row in report['content']['rows']:
        if row['event_type'] in {'expense', 'refund'}:
            store.execute('''INSERT INTO transaction_source(report_key,row_key)
                VALUES (%s,%s) ON CONFLICT(report_key,row_key) DO NOTHING''',
                (report['report_key'], row['row_key']))


def link_source(store, report_key, row_key, tid, *, resolution=None):
    report = store.one('SELECT * FROM bank_report WHERE report_key=%s', (report_key,))
    if not report:
        raise ImporterError('source report missing')
    lock_source(store, report['source_id'])
    row = report_row(report, row_key)
    transactions, _ = load_transactions(store, report['source_id'])
    target = next((t for t in transactions if t['id'] == tid), None)
    missing = 'monthly' if report['report_type'] == 'monthly' else 'daily'
    if target is None or not link_eligible(row, target, transactions, missing=missing):
        raise ImporterError('target transaction does not match source evidence')
    current = store.one('SELECT * FROM transaction_source WHERE report_key=%s AND row_key=%s FOR UPDATE',
                        (report_key, row_key))
    if not current or current['transaction_id'] not in {None, tid}:
        raise Conflict('source association changed')
    occupied = store.one('SELECT row_key FROM transaction_source WHERE report_key=%s AND transaction_id=%s AND row_key<>%s',
                         (report_key, tid, row_key))
    if occupied:
        raise Conflict('transaction is already associated with another row of this report')
    store.execute('''UPDATE transaction_source SET transaction_id=%s,
        resolution_version=resolution_version+1,last_resolution=COALESCE(%s,last_resolution),updated_at=now()
        WHERE report_key=%s AND row_key=%s AND (transaction_id IS NULL OR %s::jsonb IS NOT NULL)''',
        (tid, resolution, report_key, row_key, resolution))
    schedule_source(store, report['source_id'])


def schedule_source(store, source_id):
    store.execute('''UPDATE bank_report SET reconciliation_next_check_at=now()
        WHERE source_id=%s AND report_type='monthly' ''', (source_id,))


def create_transaction(store, report, row):
    """调用方持有来源锁；初始事实和初始关联在同一事务插入。"""
    tid = transaction_id(report['report_key'], row['row_key'])
    store.execute('''INSERT INTO bank_transactions(id,report_key,report_row_key,event_type,
        occurred_date,occurred_at,time_precision,merchant_name,card_reference,original_amount,
        original_currency,posted_date,bank_settlement_amount,bank_settlement_currency,source_details)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (tid, report['report_key'], row['row_key'], row['event_type'], row['occurred_date'],
         row['occurred_at'], row['time_precision'], row['merchant_raw'], row['card_reference'],
         row['original_amount'], row['original_currency'], row.get('posted_date'),
         row.get('settlement_amount'), row.get('settlement_currency'), row.get('extra', {})))
    store.execute('''UPDATE transaction_source SET transaction_id=%s,
        resolution_version=resolution_version+1,updated_at=now()
        WHERE report_key=%s AND row_key=%s AND transaction_id IS NULL''',
        (tid, report['report_key'], row['row_key']))
    return tid


def accept_daily(store, report, *, row_keys: frozenset[str] | None = None):
    transactions, sources = load_transactions(store, report['source_id'])
    # Existing rows in another daily report are separate facts, not monthly recovery identities.
    monthly_ids = {s['transaction_id'] for s in sources
                   if s['transaction_id'] and s['report_key'] != report['report_key']
                   and store.one('SELECT report_type FROM bank_report WHERE report_key=%s',
                                 (s['report_key'],))['report_type'] == 'monthly'}
    current_sources = {s['row_key']: s for s in sources if s['report_key'] == report['report_key']}
    candidates = automatic_candidates(report['content']['rows'],
                                     [t for t in transactions if t['id'] in monthly_ids
                                      or t['id'] in {s['transaction_id'] for s in current_sources.values()}],
                                     current_sources)
    counts = Counter(t['id'] for group in candidates.values() for t in group)
    # 外币日报后到的反向候选：排除本报告已关联或精确候选占用的交易，再做双向唯一。
    occupied = {s['transaction_id'] for s in current_sources.values() if s['transaction_id']}
    occupied |= {t['id'] for group in candidates.values() for t in group}
    def settled(row):
        source = current_sources.get(row['row_key'])
        return bool(source and (source['transaction_id']
                                or (source['last_resolution'] or {}).get('action') == 'ignore'))
    reverse = {row['row_key']: [t for t in reverse_daily_pool(row, transactions) if t['id'] not in occupied]
               for row in report['content']['rows'] if not candidates[row['row_key']] and not settled(row)}
    reverse_counts = Counter(t['id'] for group in reverse.values() for t in group)
    created = 0
    for row in report['content']['rows']:
        if row_keys is not None and row['row_key'] not in row_keys:
            continue
        if settled(row):
            continue
        group = candidates[row['row_key']]
        if len(group) == 1 and counts[group[0]['id']] == 1 and source_compatible(row, group[0]):
            link_source(store, report['report_key'], row['row_key'], group[0]['id'])
        elif not group:
            # 外币日报后到：境外月报行可能已按人民币补录且原额不同，近日唯一则复用。
            pool = reverse[row['row_key']]
            if len(pool) == 1 and reverse_counts[pool[0]['id']] == 1:
                link_source(store, report['report_key'], row['row_key'], pool[0]['id'])
                continue
            if pool:
                continue
            create_transaction(store, report, row)
            created += 1
    schedule_source(store, report['source_id'])
    return created


def group_evidence(store, report, row):
    group = [r['row_key'] for r in report['content']['rows'] if match_key(r) == match_key(row)]
    sources = store.all("""SELECT row_key,transaction_id,resolution_version FROM transaction_source
        WHERE report_key=%s AND row_key IN (SELECT value FROM jsonb_array_elements_text(%s))
        ORDER BY row_key""", (report['report_key'], group))
    return {'report_key': report['report_key'], 'content_fingerprint': report['content_fingerprint'],
            'sources': sources}


def assert_group_evidence(store, transaction):
    decision = transaction.get('import_decision') or {}
    saved = decision.get('group_evidence')
    if decision.get('amount_basis') != 'statement_settlement' or not saved:
        return
    report = store.one('SELECT * FROM bank_report WHERE report_key=%s', (saved['report_key'],))
    if not report or group_evidence(store, report, report_row(report, transaction['report_row_key'])) != saved:
        raise Conflict('statement recovery source group changed; recheck required')


def group_markers(store, report, row):
    evidence = group_evidence(store, report, row)
    return tuple('ebki-' + (source['transaction_id'] or transaction_id(report['report_key'], source['row_key']))
                 for source in evidence['sources'])
