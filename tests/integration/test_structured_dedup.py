"""结构化来源关联必须走到同 ID 的人民币结算，不能只测试候选。"""

from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from email import policy
from email.parser import BytesParser

import pytest
import test_pipeline as p

from ezbookkeeping_importer.application.classify import classify_pending
from ezbookkeeping_importer.application.reconcile import reconcile
from ezbookkeeping_importer.application.sources import register_sources
from ezbookkeeping_importer.application.write import write_queued, recover_dispatching
from ezbookkeeping_importer.adapters.banks.cmb import BankParser
from ezbookkeeping_importer.adapters.evidence_store import EvidenceStore
from ezbookkeeping_importer.application.parse import parse_pending

database = p.database
settings = p.settings


def add_monthly(store, *, currency='USD', original='21.90', settlement='147.27', merchant='DMIT, INC.',
                merchants=None, statement_currency=None):
    row = BankParser(context='synthetic').parse(p.raw_daily(currency=currency, amount=original)).rows[0].model_dump(mode='json')
    transaction = dict(report_row_key='unused', event_type=row['event_type'],
                       occurred_date=row['occurred_date'], occurred_at=row['occurred_at'],
                       time_precision=row['time_precision'], card_reference=row['card_reference'],
                       merchant_name=row['merchant_raw'], original_amount=original, original_currency=currency,
                       posted_date=None, bank_settlement_amount=None, bank_settlement_currency=None,
                       source_details={})
    row.update(row_key='monthly-row', occurred_at=None, time_precision='date',
               original_currency=statement_currency, merchant_raw=merchant,
               settlement_amount=settlement, settlement_currency='CNY')
    rows = [{**row, 'row_key': f'monthly-{i}', 'merchant_raw': name}
            for i, name in enumerate(merchants or [merchant])]
    p.statement(store, transaction, settlement, rows=rows)
    with store.transaction():
        register_sources(store, store.one("SELECT * FROM bank_report WHERE report_type='monthly'"))
    return rows[0]


@pytest.mark.parametrize('currency,original,settlement,merchant', [
    ('USD', '21.90', '147.27', 'DMIT, INC.'),
    ('CNY', '139.46', '139.46', 'RESEND'),
])
def test_daily_then_different_description_monthly_reuses_id_through_settlement(
        database, settings, tmp_path, currency, original, settlement, merchant):
    store, ledger = database.store, p.Ledger()
    before = p.queue(store, tmp_path, settings, ledger, currency=currency, amount=original,
                     merchant=merchant + ' +15189920595 VA US')
    write_queued(store, ledger)
    target = store.one('SELECT ledger_transaction_id FROM bank_transactions')['ledger_transaction_id']
    ledger.records[target].update(categoryId='edited-category', comment=before['source_marker'] + ' 用户备注',
                                  tagIds=['tag'], pictures=[{'pictureId': 'picture'}])
    observed = deepcopy(ledger.records[target])
    add_monthly(store, currency=currency, original=original, settlement=settlement, merchant=merchant)
    reconcile(store, ledger, tmp_path)
    assert store.one('SELECT count(*) n FROM bank_transactions')['n'] == 1
    classify_pending(store, settings.model_copy(update={'date_only_time': '12:00:00'}), ledger, None)
    write_queued(store, ledger)
    recover_dispatching(store)
    reconcile(store, ledger, tmp_path)
    write_queued(store, ledger)
    current = store.one('SELECT * FROM bank_transactions')
    assert current['id'] == before['id'] and current['ledger_transaction_id'] == target
    assert current['original_currency'] == currency and current['original_amount'] == before['original_amount']
    assert current['import_decision']['target_currency'] == 'CNY'
    assert len(ledger.create_calls) == 1
    assert len(ledger.modify_calls) == (1 if currency == 'USD' else 0)
    assert ledger.records[target]['sourceAccountId'] == 'account'
    assert ledger.records[target]['sourceAmount'] == int(Decimal(settlement) * 100)
    for field in ('id', 'time', 'categoryId', 'comment', 'tagIds'):
        assert ledger.records[target][field] == observed[field]
    assert store.one('SELECT count(*) n FROM transaction_source WHERE transaction_id=%s', (before['id'],))['n'] == 2


def test_monthly_then_different_description_usd_daily_keeps_single_cny_record(database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    add_monthly(store)
    configured = settings.model_copy(update={'date_only_time': '12:00:00'})
    reconcile(store, ledger, tmp_path)
    classify_pending(store, configured, ledger, None)
    write_queued(store, ledger)
    before = store.one('SELECT * FROM bank_transactions')
    p.import_daily(store, tmp_path, settings, currency='USD', amount='21.90',
                   merchant='DMIT, INC. +15189920595 VA US')
    classify_pending(store, configured, ledger, None)
    write_queued(store, ledger)
    reconcile(store, ledger, tmp_path)
    assert store.one('SELECT count(*) n FROM bank_transactions')['n'] == 1
    assert store.one('SELECT * FROM bank_transactions') == before
    assert store.one('SELECT count(*) n FROM transaction_source WHERE transaction_id=%s', (before['id'],))['n'] == 2
    assert len(ledger.create_calls) == 1 and ledger.modify_calls == []
    assert ledger.records[before['ledger_transaction_id']]['sourceAmount'] == 14727


@pytest.mark.parametrize('account,amount', [('usd-account', 2190), ('account', 14727)])
def test_monthly_remote_original_or_settlement_amount_blocks_duplicate_despite_description(
        database, settings, tmp_path, account, amount):
    store, ledger = database.store, p.Ledger()
    row = add_monthly(store)
    ledger.records['manual'] = dict(id='manual', type=3, sourceAccountId=account, sourceAmount=amount,
                                    time=int(datetime.fromisoformat(row['occurred_date'] + 'T12:00:00+08:00').timestamp()),
                                    comment='不同描述或用户已修改备注')
    reconcile(store, ledger, tmp_path)
    assert not store.one('SELECT * FROM bank_transactions')
    result = store.one("SELECT * FROM bank_statement_reconciliation WHERE check_direction='statement_to_transaction'")
    assert result['ledger_check_status'] == 'candidate_found'
    assert ledger.create_calls == [] and ledger.modify_calls == []


def test_daily_dispatch_rechecks_different_description_candidate(database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    tx = p.queue(store, tmp_path, settings, ledger)
    ledger.records['manual'] = {**deepcopy(tx['import_decision']['payload']),
                                'id': 'manual', 'comment': '用户修改过的商户备注'}
    write_queued(store, ledger)
    assert ledger.create_calls == []
    assert store.one('SELECT error_code FROM background_task')['error_code'] == 'write_preflight_failed'


def test_same_amount_different_merchants_preserve_two_usd_consumptions_and_settle_each(
        database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    message = BytesParser(policy=policy.default).parsebytes(
        p.raw_daily(count=2, currency='USD', amount='21.90', merchant='FIRST'))
    message.set_content(message.get_content().replace('FIRST', 'SECOND', 1), subtype='html')
    p.ingest_mail(store, EvidenceStore(tmp_path / 'evidence'), message.as_bytes(), settings)
    parse_pending(store, BankParser(context='synthetic'))
    classify_pending(store, settings, ledger, None)
    write_queued(store, ledger)
    # The first send is verified at the end of the batch. Until then it is
    # UNKNOWN, so the equal-value second send waits for the next worker pass.
    write_queued(store, ledger)
    targets = {t['ledger_transaction_id'] for t in store.all('SELECT * FROM bank_transactions')}
    assert len(targets) == 2 and None not in targets, [
        (t['status'], t['error_code'], t['last_error']) for t in store.all('SELECT * FROM background_task')]
    add_monthly(store, merchants=['FIRST', 'SECOND'])
    reconcile(store, ledger, tmp_path)
    assert store.one("SELECT count(*) n FROM background_task WHERE task_type='settle_currency'")['n'] == 2, [
        (r['statement_row_key'], r['match_status'], r['details']['candidate_ids']) for r in store.all(
            "SELECT * FROM bank_statement_reconciliation WHERE check_direction='statement_to_transaction'")]
    write_queued(store, ledger)
    assert len(ledger.create_calls) == len(ledger.modify_calls) == 2, store.all(
        'SELECT task_type,status,error_code,last_error,bank_transaction_id FROM background_task')
    assert store.one('SELECT count(*) n FROM bank_transactions')['n'] == 2
    assert {t['ledger_transaction_id'] for t in store.all('SELECT * FROM bank_transactions')} == targets
    assert all(ledger.records[target]['sourceAccountId'] == 'account' for target in targets)
    assert all(ledger.records[target]['sourceAmount'] == 14727 for target in targets)


def test_explicit_source_currency_conflict_pauses_instead_of_creating_another_transaction(
        database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    p.queue(store, tmp_path, settings, ledger, currency='USD', amount='21.90')
    write_queued(store, ledger)
    add_monthly(store, merchant='合成商户', statement_currency='CNY')
    reconcile(store, ledger, tmp_path)
    classify_pending(store, settings.model_copy(update={'date_only_time': '12:00:00'}), ledger, None)
    write_queued(store, ledger)
    assert store.one('SELECT count(*) n FROM bank_transactions')['n'] == 1
    assert store.one("SELECT transaction_id FROM transaction_source WHERE report_key='monthly-report'")['transaction_id'] is None
    assert store.one("SELECT match_status FROM bank_statement_reconciliation WHERE check_direction='statement_to_transaction'")['match_status'] == 'ambiguous'
    assert len(ledger.create_calls) == 1 and not ledger.modify_calls


def test_monthly_with_changed_description_preserves_unknown_usd_create_without_retry(
        database, settings, tmp_path):
    store, ledger = database.store, p.Ledger('timeout_empty')
    p.queue(store, tmp_path, settings, ledger, currency='USD', amount='21.90', merchant='LONG DESCRIPTION')
    write_queued(store, ledger)
    add_monthly(store, merchant='SHORT DESCRIPTION')
    reconcile(store, ledger, tmp_path)
    classify_pending(store, settings.model_copy(update={'date_only_time': '12:00:00'}), ledger, None)
    recover_dispatching(store)
    write_queued(store, ledger)
    assert store.one('SELECT count(*) n FROM bank_transactions')['n'] == 1
    assert store.one('SELECT import_status FROM bank_transactions')['import_status'] == 'unknown'
    assert len(ledger.create_calls) == 1 and not ledger.modify_calls


def test_legacy_estimate_dispatch_checks_frozen_cny_amount_without_bank_settlement(database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    tx = p.queue_legacy_estimate(store, tmp_path, settings, ledger)
    ledger.records['manual'] = {**deepcopy(tx['import_decision']['payload']),
                                'id': 'manual', 'comment': '旧人民币暂估的不同备注'}
    write_queued(store, ledger)
    assert ledger.create_calls == []
    assert store.one('SELECT error_code FROM background_task')['error_code'] == 'write_preflight_failed'
