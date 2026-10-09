"""普通月报候选严格限定交易日、同卡人民币金额和规范化商户。"""
from datetime import datetime

import pytest

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


def row():
    return {'occurred_date': '2026-08-22', 'posted_date': '2026-08-25',
            'card_reference': '1234', 'settlement_amount': '4.68', 'original_amount': '4.68',
            'merchant_raw': '支付宝-上海拉扎斯信息科技有限公司'}


def record(**changes):
    return {'id': 'existing', 'time': stamp('2026-08-22'), 'type': 3,
            'sourceAccountId': 'cny', 'sourceAmount': 468,
            'comment': row()['merchant_raw'], **changes}


@pytest.mark.parametrize('changes', [
    {'time': stamp('2026-08-23')}, {'time': stamp('2026-08-25')},
    {'time': stamp('2026-08-22') - 1}, {'sourceAccountId': 'usd'},
    {'comment': '不同商户'}, {'sourceAmount': -468}, {'sourceAccountId': 'other'},
])
def test_nonmatching_business_fields_are_not_candidates(changes):
    candidates, _ = query_candidates(Ledger([record(**changes)]), row())
    assert candidates == []


@pytest.mark.parametrize('comment', [
    row()['merchant_raw'], 'ebki-0123456789abcdef ' + row()['merchant_raw'],
    'ebki-0123456789abcdef 来源仅提供日期 ' + row()['merchant_raw'],
])
def test_same_day_hidden_cny_account_is_candidate(comment):
    candidates, audit = query_candidates(Ledger([record(comment=comment)]), row())
    assert [c['id'] for c in candidates] == ['existing']
    assert audit['account_ids'] == ['cny']


def test_own_marker_preserves_edited_account_amount_and_adjacent_date():
    marker = 'ebki-0123456789abcdef'
    candidates, _ = query_candidates(Ledger([record(
        time=stamp('2026-08-23'), sourceAccountId='other', sourceAmount=999,
        comment=marker + ' changed')]), row(), markers=[marker])
    assert [c['id'] for c in candidates] == ['existing']


def test_merchant_normalization_and_exact_day_end():
    source = {**row(), 'merchant_raw': 'GOOGLE *ChatGPT'}
    candidates, _ = query_candidates(Ledger([record(
        time=stamp('2026-08-23') - 1, comment='GOOGLE*CHATGPT')]), source)
    assert len(candidates) == 1
