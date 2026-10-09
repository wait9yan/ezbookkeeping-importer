"""合成月报自动补录、数量与后到证据验证。"""

from datetime import datetime
from copy import deepcopy

import pytest
import test_pipeline as p

from ezbookkeeping_importer.adapters.banks.cmb import BankParser
from ezbookkeeping_importer.application.classify import classify_pending
from ezbookkeeping_importer.application.reconcile import reconcile
from ezbookkeeping_importer.application.sources import register_sources
from ezbookkeeping_importer.application.write import write_queued, recover_dispatching
from ezbookkeeping_importer.domain.errors import LedgerError

database = p.database
settings = p.settings
SOURCE_TIME = int(datetime.fromisoformat('2026-01-01T12:00:00+08:00').timestamp())


class DateBoundLedger(p.Ledger):
    """与实际适配器一致执行包含端点的查询窗口，避免假客户端掩盖日期错误。"""

    def search(self, start, end, marker=None):
        return [row for row in super().search(start, end, marker)
                if start <= row['time'] <= end]


def monthly(store, *, count=1, event='expense'):
    daily = BankParser(context='synthetic').parse(p.raw_daily()).rows[0].model_dump(mode='json')
    rows = [{**daily, 'row_key': f'monthly-{i}', 'occurred_at': None, 'time_precision': 'date',
             'original_currency': None, 'settlement_amount': '70.00' if event == 'expense' else '-70.00',
             'original_amount': '10.00' if event == 'expense' else '-10.00',
             'settlement_currency': 'CNY', 'event_type': event} for i in range(count)]
    # Fixture helper's transaction argument is only needed to derive its default row.
    transaction = dict(report_row_key='unused', event_type='expense', occurred_date=daily['occurred_date'],
                       occurred_at=daily['occurred_at'], time_precision='exact', card_reference='1234',
                       merchant_name=daily['merchant_raw'], original_amount='10.00', original_currency='USD',
                       posted_date=None, bank_settlement_amount=None, bank_settlement_currency=None,
                       source_details={})
    p.statement(store, transaction, '70.00', rows=rows)
    with store.transaction():
        register_sources(store, store.one('SELECT * FROM bank_report'))
    return rows


@pytest.mark.parametrize('event,amount', [('expense', 7000), ('refund', -7000)])
def test_monthly_only_cny_and_original_unknown(database, settings, tmp_path, event, amount):
    store, ledger = database.store, DateBoundLedger()
    monthly(store, event=event)
    assert reconcile(store, ledger, tmp_path)
    classify_pending(store, settings.model_copy(update={'date_only_time': '12:00:00'}), ledger, None)
    write_queued(store, ledger)
    t = store.one('SELECT * FROM bank_transactions')
    assert t['original_currency'] is None and t['occurred_at'] is None
    assert t['import_status'] == 'booked'
    assert t['import_decision']['amount_basis'] == 'statement_settlement'
    assert ledger.create_calls[0]['sourceAmount'] == amount
    assert ledger.create_calls[0]['sourceAccountId'] == 'account'
    assert '来源仅提供日期' in ledger.create_calls[0]['comment']
    reconcile(store, ledger, tmp_path)
    write_queued(store, ledger)
    assert len(ledger.create_calls) == 1


def test_identical_group_restart_and_late_daily_never_duplicate(database, settings, tmp_path):
    store, ledger = database.store, DateBoundLedger()
    monthly(store, count=2)
    reconcile(store, ledger, tmp_path)
    configured = settings.model_copy(update={'date_only_time': '12:00:00'})
    classify_pending(store, configured, ledger, None)
    first = store.one('SELECT id FROM bank_transactions ORDER BY id')['id']
    write_queued(store, ledger, transaction_ids=frozenset({first}))
    recover_dispatching(store)
    reconcile(store, ledger, tmp_path)
    write_queued(store, ledger)
    assert len(ledger.create_calls) == 2
    p.import_daily(store, tmp_path, settings, count=2)
    classify_pending(store, configured, ledger, None)
    write_queued(store, ledger)
    assert len(ledger.create_calls) == 2
    assert store.one('SELECT count(*) n FROM bank_transactions')['n'] == 2
    # Two indistinguishable daily rows remain explicitly unbound instead of guessed.
    assert store.one('SELECT count(*) n FROM transaction_source WHERE transaction_id IS NULL')['n'] == 2


def test_unique_late_daily_keeps_cny_decision(database, settings, tmp_path):
    store, ledger = database.store, DateBoundLedger()
    monthly(store)
    reconcile(store, ledger, tmp_path)
    classify_pending(store, settings.model_copy(update={'date_only_time': '12:00:00'}), ledger, None)
    write_queued(store, ledger)
    before = store.one('SELECT * FROM bank_transactions')
    p.import_daily(store, tmp_path, settings)
    assert store.one('SELECT count(*) n FROM bank_transactions')['n'] == 1
    assert store.one('SELECT count(*) n FROM transaction_source WHERE transaction_id=%s', (before['id'],))['n'] == 2
    assert store.one('SELECT * FROM bank_transactions') == before


@pytest.mark.parametrize('account,amount', [('account', 7000), ('usd-account', 1000)])
def test_manual_candidate_blocks_even_different_merchant(database, tmp_path, account, amount):
    store, ledger = database.store, DateBoundLedger()
    monthly(store)
    ledger.records['manual'] = dict(id='manual', type=3, sourceAccountId=account,
                                    sourceAmount=amount, time=SOURCE_TIME,
                                    comment='完全不同备注')
    reconcile(store, ledger, tmp_path)
    assert not store.one('SELECT * FROM bank_transactions')
    result = store.one('SELECT * FROM bank_statement_reconciliation')
    assert result['ledger_check_status'] == 'candidate_found'
    assert result['details']['duplicate_query']['windows']


def test_query_failure_does_not_create(database, tmp_path):
    store, ledger = database.store, DateBoundLedger()
    monthly(store)
    def fail(*args):
        raise LedgerError('synthetic failure')
    ledger.search = fail
    assert not reconcile(store, ledger, tmp_path)
    assert not store.one('SELECT * FROM bank_transactions')
    assert store.one('SELECT ledger_check_status FROM bank_statement_reconciliation')['ledger_check_status'] == 'query_failed'


def test_missing_date_config_and_preflight_candidate_are_explicit(database, settings, tmp_path):
    store, ledger = database.store, DateBoundLedger()
    monthly(store)
    reconcile(store, ledger, tmp_path)
    classify_pending(store, settings, ledger, None)
    t = store.one('SELECT * FROM bank_transactions')
    assert t['import_status'] == 'issue' and 'date_only_time' in t['import_error']['detail']['reason']
    store.execute("UPDATE bank_transactions SET import_status='pending',import_error=NULL")
    classify_pending(store, settings.model_copy(update={'date_only_time': '12:00:00'}), ledger, None)
    payload = store.one('SELECT import_decision FROM bank_transactions')['import_decision']['payload']
    ledger.records['manual'] = {**deepcopy(payload), 'id': 'manual', 'comment': '手工记录'}
    write_queued(store, ledger)
    assert not ledger.create_calls
    assert store.one('SELECT error_code FROM background_task')['error_code'] == 'write_preflight_failed'


def test_monthly_unknown_never_resends(database, settings, tmp_path):
    store, ledger = database.store, DateBoundLedger('timeout_empty')
    monthly(store)
    reconcile(store, ledger, tmp_path)
    classify_pending(store, settings.model_copy(update={'date_only_time': '12:00:00'}), ledger, None)
    write_queued(store, ledger)
    recover_dispatching(store)
    reconcile(store, ledger, tmp_path)
    write_queued(store, ledger)
    assert len(ledger.create_calls) == 1
    assert store.one('SELECT import_status FROM bank_transactions')['import_status'] == 'unknown'


def test_hidden_card_account_remains_duplicate_evidence(database, tmp_path):
    store, ledger = database.store, DateBoundLedger()
    monthly(store)
    original = ledger.accounts
    ledger.accounts = lambda: [*original(), {'id': 'hidden', 'type': 1, 'currency': 'CNY',
                                            'hidden': True, 'comment': '4444333322221234'}]
    ledger.records['manual'] = dict(id='manual', type=3, sourceAccountId='hidden', sourceAmount=7000,
                                    time=SOURCE_TIME, comment='历史手工')
    reconcile(store, ledger, tmp_path)
    assert not store.one('SELECT * FROM bank_transactions')


def test_changed_amount_same_marker_cannot_be_recreated(database, settings, tmp_path):
    from ezbookkeeping_importer.domain.identity import transaction_id
    store, ledger = database.store, DateBoundLedger()
    monthly(store)
    marker = 'ebki-' + transaction_id('monthly-report', 'monthly-0')
    ledger.records['existing'] = dict(id='existing', type=3, sourceAccountId='another', sourceAmount=1,
                                      time=SOURCE_TIME, comment=marker + ' edited')
    reconcile(store, ledger, tmp_path)
    classify_pending(store, settings.model_copy(update={'date_only_time': '12:00:00'}), ledger, None)
    write_queued(store, ledger)
    assert not ledger.create_calls
    assert store.one('SELECT import_status FROM bank_transactions')['import_status'] == 'unknown'


def test_group_target_edited_after_first_write_blocks_second(database, settings, tmp_path):
    store, ledger = database.store, DateBoundLedger()
    monthly(store, count=2)
    reconcile(store, ledger, tmp_path)
    classify_pending(store, settings.model_copy(update={'date_only_time': '12:00:00'}), ledger, None)
    first = store.one('SELECT id FROM bank_transactions ORDER BY id')['id']
    write_queued(store, ledger, transaction_ids=frozenset({first}))
    target = next(iter(ledger.records))
    ledger.records[target]['sourceAmount'] += 1
    write_queued(store, ledger)
    assert len(ledger.create_calls) == 1
    assert store.one("SELECT error_code FROM background_task WHERE status='queued'")['error_code'] == 'write_preflight_failed'


def test_late_daily_during_remote_create_reuses_claimed_identity(database, settings, tmp_path):
    store, ledger = database.store, DateBoundLedger()
    monthly(store)
    reconcile(store, ledger, tmp_path)
    configured = settings.model_copy(update={'date_only_time': '12:00:00'})
    classify_pending(store, configured, ledger, None)
    original = store.one('SELECT id,source_marker FROM bank_transactions')
    peer = database.connect()
    ledger.before_create = lambda: p.import_daily(peer, tmp_path, settings)
    write_queued(store, ledger)
    assert store.one('SELECT id,source_marker FROM bank_transactions') == original
    assert store.one('SELECT count(*) n FROM transaction_source WHERE transaction_id=%s',
                     (original['id'],))['n'] == 2
    assert store.one('SELECT import_status FROM bank_transactions')['import_status'] == 'booked'
    classify_pending(store, configured, ledger, None)
    write_queued(store, ledger)
    assert len(ledger.create_calls) == 1


def test_identical_group_preserves_unknown_member_without_resending(database, settings, tmp_path):
    store, ledger = database.store, DateBoundLedger('timeout_empty')
    monthly(store, count=2)
    reconcile(store, ledger, tmp_path)
    classify_pending(store, settings.model_copy(update={'date_only_time': '12:00:00'}), ledger, None)
    first = store.one('SELECT id FROM bank_transactions ORDER BY id')['id']
    write_queued(store, ledger, transaction_ids=frozenset({first}))
    ledger.mode = 'success'
    recover_dispatching(store)
    write_queued(store, ledger)
    assert len(ledger.create_calls) == 2
    assert len({c['clientSessionId'] for c in ledger.create_calls}) == 2
    assert [t['import_status'] for t in store.all('SELECT import_status FROM bank_transactions ORDER BY id')] == ['unknown', 'booked']
    write_queued(store, ledger)
    assert len(ledger.create_calls) == 2
