"""结构唯一关联、真实同值数量和已知原币/卡片冲突。"""

from ezbookkeeping_importer.application.sources import (
    automatic_candidates, source_candidates, source_compatible,
)


def row(key='row', merchant='日报长商户', currency='USD', reference='1234'):
    return dict(row_key=key, event_type='expense', occurred_date='2026-01-01',
                card_reference=reference, original_amount='21.90',
                original_currency=currency, merchant_raw=merchant)


def transaction(identifier='first', **kwargs):
    facts = row(**kwargs)
    return dict(id=identifier, original_currency=facts['original_currency'], _source_rows=[facts])


def test_single_structural_candidate_does_not_require_same_description_or_monthly_currency():
    facts = row(merchant='月报短商户', currency=None)
    existing = transaction()
    assert source_candidates([facts], [existing])['row'] == [existing]
    assert automatic_candidates([facts], [existing], {})['row'] == [existing]
    assert source_compatible(facts, existing)


def test_known_currency_conflict_remains_candidate_but_cannot_be_automatically_linked():
    facts = row(currency='CNY')
    existing = transaction()
    assert source_candidates([facts], [existing])['row'] == [existing]
    assert not source_compatible(facts, existing)


def test_conflicting_known_source_currencies_cannot_accept_unknown_currency_row():
    existing = transaction()
    existing['_source_rows'].append(row(currency='CNY'))
    assert not source_compatible(row(currency=None), existing)


def test_matching_description_only_disambiguates_structurally_equal_candidates():
    rows = [row('first', 'FIRST', None), row('second', 'SECOND', None)]
    first, second = transaction('a', merchant='FIRST'), transaction('b', merchant='SECOND')
    assert automatic_candidates(rows, [first, second], {}) == {'first': [first], 'second': [second]}
    indistinguishable = [transaction('a'), transaction('b')]
    assert len(automatic_candidates([row(currency=None)], indistinguishable, {})['row']) == 2


def test_tail_ambiguity_between_distinct_complete_cards_cannot_use_merchant_to_choose():
    first = transaction('a', reference='4444333322221234')
    second = transaction('b', reference='5555444433331234', merchant='另一商户')
    assert len(automatic_candidates([row(currency=None)], [first, second], {})['row']) == 2
    assert not source_compatible(row(reference='5555444433331234'), first)


def test_existing_report_binding_reserves_the_confirmed_transaction():
    first, second = transaction('a'), transaction('b')
    rows = [row('bound'), row('unbound')]
    bindings = {'bound': {'transaction_id': 'a', 'last_resolution': None}}
    assert automatic_candidates(rows, [first, second], bindings) == {'bound': [first], 'unbound': [second]}
