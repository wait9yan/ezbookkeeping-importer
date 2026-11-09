"""同卡同交易日的原币/银行结算金额候选；描述不排除疑似重复。"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..domain.accounts import card_numbers, decision_currency
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


def candidate_accounts(accounts, row):
    reference = row.get('card_reference')
    if not reference:
        raise ImporterError('statement has no card reference')
    return {str(a['id']): a for a in accounts if a.get('type') == 1 and a.get('currency') in {'CNY', 'USD'} and any(
        number.endswith(reference) if len(reference) == 4 else number == reference
        for number in card_numbers(a.get('comment') or ''))}


def filter_candidates(accounts, row, records, *, markers=(), decision=None):
    """原币只比原交易额，CNY结算只比银行结算额；未知月报原币保留USD候选。"""
    accounts = candidate_accounts(accounts, row)
    candidates = []
    occurred_date = date.fromisoformat(str(row['occurred_date']))
    from .write import has_marker
    for c in records:
        if any(has_marker(c, marker) for marker in markers):
            candidates.append(c)
            continue
        account = accounts.get(str(c.get('sourceAccountId')))
        if not account or c.get('type') != 3:
            continue
        amounts = []
        currency = account['currency']
        if row.get('settlement_amount') is not None and currency == row.get('settlement_currency', 'CNY'):
            amounts.append(row['settlement_amount'])
        if row.get('original_amount') is not None and (
                currency == row.get('original_currency')
                or row.get('original_currency') is None and currency == 'USD'):
            amounts.append(row['original_amount'])
        if decision and currency == decision_currency({'import_decision': decision}):
            # Older persisted CNY estimates have no bank settlement yet; their
            # already frozen same-currency request is still duplicate evidence.
            amounts.append(Decimal(decision['payload']['sourceAmount']) / 100)
        same_day = datetime.fromtimestamp(c['time'], BANK_TIMEZONE).date() == occurred_date
        if same_day and any(c.get('sourceAmount') == cents(Decimal(amount)) for amount in amounts):
            candidates.append(c)
    return candidates


def query_candidates(ledger, row, *, markers=(), decision=None):
    # Ordinary candidates are narrower than the read window for known markers.
    merged = statement_windows(row)
    accounts = ledger.accounts()
    records = {str(c['id']): c for start, end in merged for c in ledger.search(start, end)}
    candidates = filter_candidates(accounts, row, records.values(), markers=markers, decision=decision)
    return candidates, {'windows': [list(w) for w in merged],
                        'account_ids': sorted(candidate_accounts(accounts, row)),
                        'observed_at': datetime.now(BANK_TIMEZONE).isoformat()}


def exclude_linked_targets(store, candidates, *, own_id):
    """已确认本地目标保留真实数量；未知归属或账户/金额变化不能豁免。"""
    if not candidates:
        return []
    transactions = store.all('''SELECT * FROM bank_transactions WHERE id<>%s
        AND ledger_transaction_id IN (SELECT value FROM jsonb_array_elements_text(%s))''',
        (own_id, [str(c['id']) for c in candidates]))
    by_target = {str(t['ledger_transaction_id']): t for t in transactions}
    remaining = []
    for candidate in candidates:
        transaction = by_target.get(str(candidate['id']))
        payload = (transaction.get('import_decision') or {}).get('payload') if transaction else None
        if transaction and payload and all(
                str(candidate.get(k)) == str(payload[k]) for k in ('type', 'sourceAccountId', 'sourceAmount', 'time')):
            continue
        remaining.append(candidate)
    return remaining


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
