"""月报远端候选查询：同交易日、同卡人民币金额与商户；操作身份独立核实。"""

import re

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..domain.accounts import card_numbers
from ..domain.errors import ImporterError
from ..domain.money import cents

BANK_TIMEZONE = ZoneInfo('Asia/Shanghai')


def statement_windows(row):
    """保留交易日与入账日相邻窗口，供操作身份恢复和显式目标校验。"""
    windows = []
    for day in {row['occurred_date'], row.get('posted_date') or row['occurred_date']}:
        day = date.fromisoformat(str(day))
        start_at = datetime.combine(day - timedelta(days=1), time(), BANK_TIMEZONE)
        end_at = datetime.combine(day + timedelta(days=2), time(), BANK_TIMEZONE)
        windows.append((int(start_at.timestamp()), int(end_at.timestamp()) - 1))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def query_candidates(ledger, row, *, markers=()):
    # Ordinary candidates are narrower than the read window for known markers.
    merged = statement_windows(row)
    reference = row.get('card_reference')
    if not reference:
        raise ImporterError('statement has no card reference')
    accounts = {str(a['id']): a for a in ledger.accounts() if a.get('type') == 1 and a.get('currency') == 'CNY' and any(
        number.endswith(reference) if len(reference) == 4 else number == reference
        for number in card_numbers(a.get('comment') or ''))}
    records = {str(c['id']): c for start, end in merged for c in ledger.search(start, end)}
    candidates = []
    from .sources import merchant_identity
    occurred_date = date.fromisoformat(str(row['occurred_date']))
    from .write import has_marker
    for c in records.values():
        if any(has_marker(c, marker) for marker in markers):
            candidates.append(c)
            continue
        account = accounts.get(str(c.get('sourceAccountId')))
        if not account or c.get('type') != 3:
            continue
        amount = row.get('settlement_amount')
        same_day = datetime.fromtimestamp(c['time'], BANK_TIMEZONE).date() == occurred_date
        comment = c.get('comment', '')
        merchant, marker_count = re.subn(r'(?<![\w-])ebki-[A-Za-z0-9_-]{16}(?![\w-])', '', comment)
        if marker_count:
            merchant = merchant.strip().removeprefix('来源仅提供日期 ')
        if (same_day and merchant_identity(merchant) == merchant_identity(row['merchant_raw'])
                and amount is not None and c.get('sourceAmount') == cents(Decimal(amount))):
            candidates.append(c)
    return candidates, {'windows': [list(w) for w in merged],
                        'account_ids': sorted(accounts),
                        'observed_at': datetime.now(BANK_TIMEZONE).isoformat()}


def exclude_group_targets(store, report, row, candidates, *, own_id=None):
    from .sources import match_key
    group = [r['row_key'] for r in report['content']['rows'] if match_key(r) == match_key(row)]
    members = store.all('''SELECT t.* FROM transaction_source s JOIN bank_transactions t
        ON t.id=s.transaction_id WHERE s.report_key=%s AND s.row_key IN
        (SELECT value FROM jsonb_array_elements_text(%s))''', (report['report_key'], group))
    owned = {str(t['ledger_transaction_id']): t for t in members if t['ledger_transaction_id']
             and t['id'] != own_id}
    remaining = []
    from .write import has_marker
    for c in candidates:
        transaction = owned.get(str(c['id']))
        if transaction is None:
            marked = [t for t in members if t['id'] != own_id and has_marker(c, t['source_marker'])]
            transaction = marked[0] if len(marked) == 1 else None
        payload = (transaction.get('import_decision') or {}).get('payload') if transaction else None
        # A stale or edited group target remains a candidate; ownership alone is insufficient.
        if transaction and payload and all(str(c.get(k)) == str(payload[k]) for k in
                           ('type', 'sourceAccountId', 'sourceAmount', 'time')):
            if has_marker(c, transaction['source_marker']):
                continue
        remaining.append(c)
    return remaining
