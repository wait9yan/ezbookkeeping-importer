"""同卡同交易日按原币/结算金额找候选，商户描述不作排除条件。"""
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
    {'time': stamp('2026-08-22') - 1},
    {'sourceAmount': -468}, {'sourceAccountId': 'other'},
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
    assert audit['account_ids'] == ['cny', 'usd']


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


@pytest.mark.parametrize('comment', ['不同商户', '', '用户编辑备注'])
def test_description_does_not_remove_structural_candidate(comment):
    candidates, _ = query_candidates(Ledger([record(comment=comment)]), row())
    assert [c['id'] for c in candidates] == ['existing']


@pytest.mark.parametrize('account,amount,expected', [
    ('usd', 2190, True), ('cny', 14727, True),
    ('usd', 14727, False), ('cny', 2190, False),
])
def test_original_usd_and_bank_cny_amounts_are_compared_in_their_own_currency(account, amount, expected):
    source = {**row(), 'original_amount': '21.90', 'original_currency': None,
              'settlement_amount': '147.27', 'settlement_currency': 'CNY'}
    candidates, _ = query_candidates(Ledger([record(sourceAccountId=account, sourceAmount=amount)]), source)
    assert bool(candidates) is expected


def test_explicit_original_currency_conflict_is_not_a_remote_amount_match():
    source = {**row(), 'original_currency': 'CNY'}
    candidates, _ = query_candidates(Ledger([record(sourceAccountId='usd')]), source)
    assert candidates == []


def test_legacy_frozen_cny_estimate_remains_duplicate_evidence_before_bank_settlement():
    source = {**row(), 'original_currency': 'USD', 'original_amount': '10.00',
              'settlement_amount': None, 'settlement_currency': None}
    decision = {'payload': {'sourceAmount': 7000}, 'rate_snapshot': {'adoptedRate': '7'}}
    candidates, _ = query_candidates(Ledger([record(sourceAmount=7000, comment='用户备注')]), source,
                                     decision=decision)
    assert [c['id'] for c in candidates] == ['existing']
