"""旧v3带写入身份升级及来源关联约束的真实数据库验证。"""
from psycopg import sql
import pytest
import psycopg
import test_pipeline as p
from test_versioned_migrations import clear_schema
from ezbookkeeping_importer.adapters.persistence.migrations import apply_migration, load_migrations
from ezbookkeeping_importer.application.write import write_queued


database = p.database
settings = p.settings


def test_v3_upgrade_preserves_transaction_marker_target_and_write_history(database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    p.queue(store, tmp_path, settings, ledger)
    write_queued(store, ledger)
    names = ['email', 'bank_report', 'bank_transactions', 'background_task', 'ledger_write_attempt']
    saved = {name: store.all(sql.SQL('SELECT * FROM {}').format(sql.Identifier(name))) for name in names}
    clear_schema(store)
    chain = load_migrations()
    for migration in chain[:3]:
        with store.transaction():
            apply_migration(store.connection, migration, chain[0].checksum)
    with store.transaction():
        for name in names:
            for original in saved[name]:
                row = {k: v for k, v in original.items() if k != 'source_marker'}
                if name == 'email':
                    row['report_key'] = None
                store.execute(sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
                    sql.Identifier(name), sql.SQL(',').join(map(sql.Identifier, row)),
                    sql.SQL(',').join(sql.Placeholder() for _ in row)), tuple(row.values()))
        for email in saved['email']:
            store.execute('UPDATE email SET report_key=%s WHERE id=%s', (email['report_key'], email['id']))
    assert store.migrate() == chain[-1].version
    for name in names:
        assert store.all(sql.SQL('SELECT * FROM {}').format(sql.Identifier(name))) == saved[name]
    transaction = saved['bank_transactions'][0]
    association = store.one('SELECT * FROM transaction_source')
    assert (association['report_key'], association['row_key'], association['transaction_id']) == (
        transaction['report_key'], transaction['report_row_key'], transaction['id'])
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with store.transaction():
            store.execute('UPDATE transaction_source SET transaction_id=NULL')
    assert store.one('SELECT transaction_id FROM transaction_source')['transaction_id'] == transaction['id']
