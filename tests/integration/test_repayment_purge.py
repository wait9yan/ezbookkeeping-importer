"""旧版本合成还款的数据库与文件清理，不访问远端账本。"""
import importlib.util
import json
from hashlib import sha256
from pathlib import Path

import psycopg
import pytest

import test_pipeline as pipeline
from test_versioned_migrations import clear_schema
from ezbookkeeping_importer.adapters.persistence.migrations import apply_migration, load_migrations
from ezbookkeeping_importer.domain.errors import Conflict, DatabaseDiagnosticError

spec = importlib.util.spec_from_file_location('purge_repayment', Path('scripts/purge-repayment-history.py'))
purge_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(purge_module)
database = pipeline.database


def seed(store, root, status='unknown'):
    clear_schema(store)
    chain = load_migrations()
    with store.transaction():
        for migration in chain[:2]:
            apply_migration(store.connection, migration, chain[0].checksum)
    for directory in ('email', 'reports', 'logs'):
        (root / directory).mkdir()
    for number, subject in ((1, '自动还款扣款通知'), (2, 'FW: 转发： 自动还款扣款通知'),
                            (3, '\t转发:自动还款扣款通知\n'), (4, '月账单'), (6, '每日信用管家')):
        identity = str(number) * 64
        path = root / 'email' / (identity + '.eml')
        path.write_text('synthetic mail')
        store.execute('INSERT INTO email(id,raw_path,subject) VALUES (%s,%s,%s)',
                      (identity, str(path), subject))
    with store.transaction():
        store.execute("INSERT INTO bank_report(report_key,source_id,bank_code,report_type,content_fingerprint,source_email_id,parser_version,content) VALUES ('repayment','synthetic','cmb','repayment','hash',%s,'old','{}')", ('1'*64,))
        store.execute("UPDATE email SET report_key='repayment' WHERE id IN (%s,%s)", ('1'*64, '2'*64))
        store.execute("INSERT INTO bank_report(report_key,source_id,bank_code,report_type,content_fingerprint,source_email_id,parser_version,content) VALUES ('monthly','synthetic','cmb','monthly','hash',%s,'old',%s)", ('4'*64, {'rows': [{'row_key': 'r1', 'event_type': 'statement'}]}))
        store.execute("UPDATE email SET report_key='monthly' WHERE id=%s", ('4'*64,))
        store.execute("INSERT INTO bank_transactions(id,report_key,report_row_key,event_type,occurred_date,time_precision,merchant_name,original_amount,import_status) VALUES ('AAAAAAAAAAAAAAAA','repayment','r1','repayment','2026-10-05','date','synthetic',123,%s)", (status,))
        task_status = 'done' if status == 'booked' else status
        task = store.one("INSERT INTO background_task(task_type,bank_transaction_id,decision_version,operation_key,status) VALUES ('create','AAAAAAAAAAAAAAAA',1,'synthetic',%s) RETURNING id", (task_status,))['id']
        store.execute("INSERT INTO ledger_write_attempt(task_id,decision_version,request,outcome) VALUES (%s,1,'{}','unknown')", (task,))
        store.execute("INSERT INTO bank_statement_reconciliation(statement_report_key,check_direction,statement_row_key,bank_transaction_id,match_status,ledger_check_status) VALUES ('monthly','statement_to_transaction','r1','AAAAAAAAAAAAAAAA','matched','not_checked')")
        for number in (1, 2, 3):
            store.execute("INSERT INTO email_source_item(source_id,folder,uid_validity,uid,status,email_id,source_status,source_reason,collected_at) VALUES ('synthetic','INBOX','1',%s,'collected',%s,'verified','synthetic',now())", (number, str(number)*64))
    with store.transaction():
        store.execute("INSERT INTO bank_report(report_key,source_id,bank_code,report_type,content_fingerprint,source_email_id,parser_version,content) VALUES ('daily','synthetic','cmb','daily','hash',%s,'old','{}')", ('6'*64,))
        store.execute("UPDATE email SET report_key='daily' WHERE id=%s", ('6'*64,))
        store.execute("INSERT INTO bank_transactions(id,report_key,report_row_key,event_type,occurred_date,time_precision,merchant_name,original_amount) VALUES ('BBBBBBBBBBBBBBBB','daily','r1','expense','2026-10-05','second','synthetic expense',12)")
        task = store.one("INSERT INTO background_task(task_type,bank_transaction_id,decision_version,operation_key,status) VALUES ('create','BBBBBBBBBBBBBBBB',1,'keep-operation','unknown') RETURNING id")['id']
        store.execute("INSERT INTO ledger_write_attempt(task_id,decision_version,request,outcome) VALUES (%s,1,'{}','unknown')", (task,))
    report = root / 'reports' / (sha256(b'monthly').hexdigest() + '.json')
    report.write_text('synthetic reconciliation')
    (root / 'logs' / 'worker.jsonl').write_text(json.dumps({'transaction_id': 'AAAAAAAAAAAAAAAA'})+'\n'+json.dumps({'event': 'worker_started'})+'\n')


@pytest.mark.parametrize('status', ['queued', 'dispatching', 'unknown', 'booked'])
def test_purge_deletes_all_local_history_and_preserves_monthly(database, tmp_path, status):
    root = tmp_path.resolve()
    store = database.store
    seed(store, root, status)
    with pytest.raises(DatabaseDiagnosticError) as error:
        store.migrate()
    assert error.value.code == 'repayment_cleanup_required'
    assert store.one('SELECT count(*) AS n FROM bank_transactions')['n'] == 2
    assert max(row['version'] for row in store.all('SELECT version FROM schema_version')) == 2
    result = purge_module.purge(store, root)
    assert result['deleted']['email_id'] == 3
    for table, count in (('bank_transactions', 1), ('background_task', 1), ('ledger_write_attempt', 1), ('email_source_item', 0), ('bank_statement_reconciliation', 0)):
        assert store.one(f'SELECT count(*) AS n FROM {table}')['n'] == count
    assert store.one('SELECT id FROM bank_transactions')['id'] == 'BBBBBBBBBBBBBBBB'
    assert store.one('SELECT operation_key FROM background_task')['operation_key'] == 'keep-operation'
    assert store.one('SELECT report_type,reconciliation_version FROM bank_report WHERE report_key=\'monthly\'') == {'report_type': 'monthly', 'reconciliation_version': 1}
    assert {row['id'] for row in store.all('SELECT id FROM email')} == {'4'*64, '6'*64}
    assert {path.name for path in (root / 'email').iterdir()} == {str(n)*64+'.eml' for n in (4, 6)}
    assert not list((root / 'reports').iterdir())
    assert (root / 'logs' / 'worker.jsonl').read_text() == '{"event": "worker_started"}\n'
    assert not (root / purge_module.MANIFEST).exists()
    assert store.check_schema() == 3
    with pytest.raises(psycopg.errors.CheckViolation):
        store.execute("UPDATE bank_report SET report_type='repayment'")
    # A successful repeat neither recreates history nor changes retained month facts.
    purge_module.purge(store, root)


def test_purge_resume_after_database_commit(database, tmp_path, monkeypatch):
    root = tmp_path.resolve()
    seed(database.store, root)
    clean = purge_module.clean_files
    monkeypatch.setattr(purge_module, 'clean_files', lambda *args: (_ for _ in ()).throw(OSError('synthetic interruption')))
    with pytest.raises(OSError, match='interruption'):
        purge_module.purge(database.store, root)
    manifest = root / purge_module.MANIFEST
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert database.store.check_schema() == 3
    assert (root / 'email' / ('1'*64+'.eml')).exists()
    monkeypatch.setattr(purge_module, 'clean_files', clean)
    purge_module.purge(database.store, root)
    assert not manifest.exists()
    assert not (root / 'email' / ('1'*64+'.eml')).exists()


def test_purge_requires_worker_exclusion(database, tmp_path):
    seed(database.store, tmp_path.resolve())
    assert database.store.lock_worker()
    with pytest.raises(Conflict, match='another worker'):
        purge_module.purge(database.connect(), tmp_path.resolve())
    assert not (tmp_path / purge_module.MANIFEST).exists()


def test_purge_rejects_external_raw_path_before_migration(database, tmp_path):
    seed(database.store, tmp_path.resolve())
    database.store.execute('UPDATE email SET raw_path=%s WHERE id=%s',
                           ('/outside/synthetic.eml', '1'*64))
    with pytest.raises(ValueError):
        purge_module.purge(database.store, tmp_path.resolve())
    assert database.store.one('SELECT max(version) AS version FROM schema_version')['version'] == 2


def test_resume_before_sql_refreshes_manifest_for_new_history(database, tmp_path, monkeypatch):
    root = tmp_path.resolve()
    store = database.store
    seed(store, root)
    migrate = store.migrate
    monkeypatch.setattr(store, 'migrate', lambda **kwargs: (_ for _ in ()).throw(RuntimeError('synthetic SQL failure')))
    with pytest.raises(RuntimeError):
        purge_module.purge(store, root)
    identity = '5'*64
    path = root / 'email' / (identity+'.eml')
    path.write_text('synthetic new history')
    store.execute('INSERT INTO email(id,raw_path,subject) VALUES (%s,%s,%s)',
                  (identity, str(path), '自动还款扣款通知'))
    monkeypatch.setattr(store, 'migrate', migrate)
    purge_module.purge(store, root)
    assert not path.exists()
    assert not (root / purge_module.MANIFEST).exists()


def test_relative_raw_path_resolves_against_explicit_data_parent(database, tmp_path, monkeypatch):
    root = tmp_path.resolve()
    seed(database.store, root)
    database.store.execute('UPDATE email SET raw_path=%s WHERE id=%s',
                           (root.name + '/email/' + '1'*64 + '.eml', '1'*64))
    monkeypatch.chdir(root.parent)
    purge_module.purge(database.store, root)
    assert not (root / 'email' / ('1'*64+'.eml')).exists()
