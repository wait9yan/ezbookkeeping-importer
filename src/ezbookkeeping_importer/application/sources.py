"""稳定来源身份、双向唯一关联及来源发布锁。"""

import hashlib
import re
from collections import Counter, defaultdict
from decimal import Decimal

from ..domain.errors import Conflict, ImporterError
from ..domain.identity import transaction_id
from .records import source_row


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


def match_key(row: dict) -> tuple:
    return (row['event_type'], str(row['occurred_date']),
            (row['card_reference'] or '')[-4:], Decimal(row['original_amount']))


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


def load_transactions(store, source_id):
    transactions = store.all('''SELECT t.* FROM bank_transactions t JOIN bank_report r
        ON r.report_key=t.report_key WHERE r.source_id=%s ORDER BY t.id''', (source_id,))
    sources = store.all('''SELECT s.*,r.content,r.content_fingerprint FROM transaction_source s
        JOIN bank_report r ON r.report_key=s.report_key WHERE r.source_id=%s
        ORDER BY s.report_key,s.row_key''', (source_id,))
    by_id = {t['id']: t for t in transactions}
    for t in transactions:
        t['_source_rows'] = []
    for s in sources:
        if s['transaction_id'] in by_id:
            by_id[s['transaction_id']]['_source_rows'].append(report_row(s, s['row_key']))
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
    if tid not in {t['id'] for t in source_candidates([row], transactions)[row_key]
                   if source_compatible(row, t)}:
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
    created = 0
    for row in report['content']['rows']:
        if row_keys is not None and row['row_key'] not in row_keys:
            continue
        source = current_sources.get(row['row_key'])
        if source and (source['transaction_id'] or (source['last_resolution'] or {}).get('action') == 'ignore'):
            continue
        group = candidates[row['row_key']]
        if len(group) == 1 and counts[group[0]['id']] == 1 and source_compatible(row, group[0]):
            link_source(store, report['report_key'], row['row_key'], group[0]['id'])
        elif not group:
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
