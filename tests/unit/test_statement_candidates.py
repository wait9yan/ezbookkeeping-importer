"""月报候选严格使用银行日期窗口，并保留跨币与隐藏账户证据。"""
from datetime import datetime

from ezbookkeeping_importer.application.statement_candidates import query_candidates


def stamp(day):
    return int(datetime.fromisoformat(day + 'T00:00:00+08:00').timestamp())


class Ledger:
    def __init__(self, records):
        self.records = records
        self.windows = []

    def accounts(self):
        return [{'id': 'cny', 'type': 1, 'currency': 'CNY', 'comment': '4444333322221234', 'hidden': True},
                {'id': 'usd', 'type': 1, 'currency': 'USD', 'comment': '4444333322221234'}]

    def search(self, start, end):
        self.windows.append((start, end))
        return [r for r in self.records if start <= r['time'] <= end]


def row(posted_date):
    return {'occurred_date': '2026-01-02', 'posted_date': posted_date,
            'card_reference': '1234', 'settlement_amount': '70.00', 'original_amount': '10.00'}


def test_disjoint_transaction_and_posting_windows_preserve_endpoints_and_exclude_gap():
    records = [{'id': str(i), 'time': when, 'type': 3,
                'sourceAccountId': 'cny', 'sourceAmount': 7000, 'comment': '不同商户'}
               for i, when in enumerate([stamp('2026-01-01') - 1, stamp('2026-01-01'),
                                         stamp('2026-01-04') - 1, stamp('2026-01-04'),
                                         stamp('2026-01-09'), stamp('2026-01-12') - 1,
                                         stamp('2026-01-12')])]
    ledger = Ledger(records)
    candidates, audit = query_candidates(ledger, row('2026-01-10'))
    assert [r['id'] for r in candidates] == ['1', '2', '4', '5']
    assert ledger.windows == [(stamp('2026-01-01'), stamp('2026-01-04') - 1),
                              (stamp('2026-01-09'), stamp('2026-01-12') - 1)]
    assert audit['windows'] == [list(w) for w in ledger.windows]
    assert audit['account_ids'] == ['cny', 'usd']


def test_overlapping_windows_merge_and_original_amount_only_generates_cross_currency_candidate():
    ledger = Ledger([{'id': 'usd-row', 'time': stamp('2026-01-03'), 'type': 3,
                      'sourceAccountId': 'usd', 'sourceAmount': 1000}])
    candidates, _ = query_candidates(ledger, row('2026-01-03'))
    assert [r['id'] for r in candidates] == ['usd-row']
    assert ledger.windows == [(stamp('2026-01-01'), stamp('2026-01-05') - 1)]
