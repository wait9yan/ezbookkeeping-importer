"""真实 PostgreSQL 的迁移历史、权限、结构漂移与逐版本恢复。"""
from dataclasses import asdict, replace
from contextlib import contextmanager
from hashlib import sha256
from threading import Event, Timer
from types import SimpleNamespace
import json
import os
import signal
import subprocess
import sys
import time
import uuid

from psycopg import sql
import pytest

import test_pipeline as pipeline
from ezbookkeeping_importer import bootstrap
from ezbookkeeping_importer.adapters.persistence import postgres
from ezbookkeeping_importer.adapters.persistence.migrations import Migration, apply_migration, load_migrations
from ezbookkeeping_importer.adapters.persistence.schema_contract import schema_signature
from ezbookkeeping_importer.config import MailSettings, Settings
from ezbookkeeping_importer.domain.errors import Conflict, DatabaseDiagnosticError, StartupInterrupted
from ezbookkeeping_importer.entrypoints.cli import _execute

database = pipeline.database


def clear_schema(store):
    namespace = store.one('SELECT current_schema() AS name')['name']
    store.connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(namespace)))
    store.connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(namespace)))


def old_baseline(store):
    clear_schema(store)
    store.execute(load_migrations()[0].script)
    store.execute('INSERT INTO email(id,raw_path,parse_status) VALUES (%s,%s,%s)',
                  ('c' * 64, 'synthetic-preserved', 'ignored'))


def history(store):
    return store.all('SELECT * FROM schema_version ORDER BY version')


@contextmanager
def reader(database):
    admin = database.store
    role = 'ebki_reader_' + uuid.uuid4().hex
    namespace = admin.one('SELECT current_schema() AS name')['name']
    admin.connection.execute(sql.SQL('CREATE ROLE {}').format(sql.Identifier(role)))
    limited = None
    try:
        admin.connection.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(
            sql.Identifier(namespace), sql.Identifier(role)))
        admin.connection.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}').format(
            sql.Identifier(namespace), sql.Identifier(role)))
        limited = database.connect()
        limited.connection.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)))
        yield limited
    finally:
        if limited is not None:
            limited.close()
        admin.connection.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
        admin.connection.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def doctor_settings(database):
    return Settings(database_url=database.isolated_dsn, ledger_url='http://synthetic.invalid',
                    ledger_token='synthetic-ledger-token', classification_mode='rules_only',
                    timezone='Asia/Shanghai', mail=MailSettings(username='synthetic',
                    password='synthetic-password', source_id='synthetic'))


def synthetic_chain(store):
    """从真实SQL派生测试追加链，事务回滚后交给实际迁移引擎。"""
    migrations = list(load_migrations())
    namespace = store.one('SELECT current_schema() AS name')['name']
    with store.connection.transaction(force_rollback=True):
        for version in (3, 4):
            script = f'ALTER TABLE email ADD COLUMN synthetic_v{version} text;'
            migration = Migration(version, f'{version:03d}_synthetic.sql', script,
                                  sha256(script.encode()).hexdigest(), {})
            apply_migration(store.connection, migration, migrations[0].checksum)
            migrations.append(replace(migration, signature=schema_signature(store.connection, namespace)))
    return tuple(migrations)


def test_published_v1_data_and_original_timestamp_survive_upgrade(database):
    store = database.store
    old_baseline(store)
    applied = store.one('SELECT applied_at FROM schema_version')['applied_at']
    assert store.migrate() == len(load_migrations())
    assert store.one('SELECT raw_path FROM email')['raw_path'] == 'synthetic-preserved'
    assert history(store)[0]['applied_at'] == applied
    assert [(row['version'], row['script_sha256']) for row in history(store)] == [
        (migration.version, migration.checksum) for migration in load_migrations()
    ]
    assert store.check_schema() == len(load_migrations())


@pytest.mark.parametrize('change,code', [
    ("UPDATE schema_version SET script_sha256=repeat('0',64) WHERE version=1", 'migration_checksum_mismatch'),
    ('DELETE FROM schema_version WHERE version=1', 'invalid_migration_history'),
    ("INSERT INTO schema_version(version,script_sha256) VALUES (99,repeat('0',64))", 'schema_ahead'),
])
def test_corrupt_history_is_rejected_without_mutation(database, change, code):
    store = database.store
    store.execute(change)
    before = history(store)
    with pytest.raises(DatabaseDiagnosticError) as error:
        store.migrate()
    assert error.value.code == code
    assert history(store) == before
    with pytest.raises(DatabaseDiagnosticError) as error:
        store.check_schema()
    assert error.value.code == code


@pytest.mark.parametrize('extra', [
    "CREATE TYPE synthetic_extra AS ENUM ('synthetic')",
    'CREATE FUNCTION synthetic_extra() RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$',
])
def test_additional_type_or_function_is_schema_drift(database, extra):
    store = database.store
    store.execute(extra)
    before = history(store)
    with pytest.raises(DatabaseDiagnosticError) as error:
        store.migrate()
    assert error.value.code == 'schema_drift'
    assert history(store) == before


def test_existing_worker_prevents_old_database_migration(database):
    old_baseline(database.store)
    assert database.store.lock_worker()
    competing = database.connect()
    before = history(database.store)
    try:
        with pytest.raises(Conflict, match='another worker'):
            competing.migrate()
        assert history(database.store) == before
        assert database.store.one('SELECT raw_path FROM email')['raw_path'] == 'synthetic-preserved'
    finally:
        database.store.unlock_worker()
    assert competing.migrate() == len(load_migrations())


def test_cross_version_chain_preserves_data_and_finishes_same_contract(database, monkeypatch):
    chain = synthetic_chain(database.store)
    old_baseline(database.store)
    monkeypatch.setattr(postgres, 'load_migrations', lambda: chain)
    assert database.store.migrate() == 4
    assert database.store.check_schema() == 4
    assert database.store.one('SELECT raw_path FROM email')['raw_path'] == 'synthetic-preserved'
    assert [row['version'] for row in history(database.store)] == [1, 2, 3, 4]
    upgraded = database.store._schema_signature(database.store.one('SELECT current_schema() AS name')['name'])
    clear_schema(database.store)
    assert database.store.migrate() == 4
    assert database.store._schema_signature(database.store.one('SELECT current_schema() AS name')['name']) == upgraded


@pytest.mark.parametrize('failure', ['sql', 'stop'])
def test_failed_version_rolls_back_only_itself_and_restart_continues(database, monkeypatch, failure):
    store = database.store
    chain = synthetic_chain(store)
    monkeypatch.setattr(postgres, 'load_migrations', lambda: chain)
    stop = Event()
    timer = None

    def fail_fourth(connection, migration, baseline):
        nonlocal timer
        apply_migration(connection, migration, baseline)
        if migration.version != 4:
            return
        if failure == 'sql':
            connection.execute('SELECT 1/0')
        else:
            timer = Timer(0.15, stop.set)
            timer.start()
            connection.execute('SELECT pg_sleep(30)')

    monkeypatch.setattr(postgres, 'apply_migration', fail_fourth)
    try:
        with pytest.raises(StartupInterrupted if failure == 'stop' else DatabaseDiagnosticError):
            store.migrate(stop_event=stop)
    finally:
        if timer is not None:
            timer.join(timeout=2)
    assert [row['version'] for row in history(store)] == [1, 2, 3]
    columns = store.all("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='email'")
    assert 'synthetic_v3' in {row['column_name'] for row in columns}
    assert 'synthetic_v4' not in {row['column_name'] for row in columns}
    committed = history(store)
    competitor = database.connect()
    assert competitor.lock_worker()
    competitor.unlock_worker()
    stop.clear()
    monkeypatch.setattr(postgres, 'apply_migration', apply_migration)
    assert store.migrate(stop_event=stop) == 4
    assert history(store)[:3] == committed
    assert store.check_schema() == 4


@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGKILL])
def test_real_process_signal_rolls_back_current_version_and_releases_lock(database, monkeypatch,
                                                                         tmp_path, signum):
    chain = synthetic_chain(database.store)
    manifest = tmp_path / 'synthetic-chain.json'
    manifest.write_text(json.dumps([asdict(migration) for migration in chain]))
    marker = tmp_path / 'fourth-version-in-progress'
    child_code = '''
import json, os, signal, sys
from pathlib import Path
from threading import Event
from ezbookkeeping_importer.adapters.persistence import postgres
from ezbookkeeping_importer.adapters.persistence.migrations import Migration, apply_migration
from ezbookkeeping_importer.domain.errors import StartupInterrupted
stop = Event()
signal.signal(signal.SIGTERM, lambda *args: stop.set())
chain = tuple(Migration(**item) for item in json.loads(Path(sys.argv[1]).read_text()))
postgres.load_migrations = lambda: chain
def pause_fourth(connection, migration, baseline):
    apply_migration(connection, migration, baseline)
    if migration.version == 4:
        Path(sys.argv[2]).write_text(str(connection.info.backend_pid))
        connection.execute('SELECT pg_sleep(2)')
postgres.apply_migration = pause_fourth
store = postgres.PostgresStore(os.environ['EBKI_TEST_DATABASE_URL'])
try:
    store.migrate(stop_event=stop)
except StartupInterrupted:
    sys.exit(143)
finally:
    store.close()
'''
    process = subprocess.Popen([sys.executable, '-c', child_code, str(manifest), str(marker)],
                               env={**os.environ, 'EBKI_TEST_DATABASE_URL': database.isolated_dsn},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() or not marker.stat().st_size:
            if process.poll() is not None or time.monotonic() > deadline:
                raise AssertionError('隔离迁移进程未进入第四版本事务')
            time.sleep(0.02)
        backend = int(marker.read_text())
        while True:
            activity = database.store.one('SELECT state,query FROM pg_stat_activity WHERE pid=%s',
                                          (backend,))
            if activity and activity['state'] == 'active' and activity['query'] == 'SELECT pg_sleep(2)':
                break
            assert time.monotonic() < deadline, '隔离迁移SQL未进入服务端阻塞点'
            time.sleep(0.02)
        interrupted_at = time.monotonic()
        process.send_signal(signum)
        process.communicate(timeout=5)
        assert process.returncode == (143 if signum == signal.SIGTERM else -signal.SIGKILL)
        if signum == signal.SIGTERM:
            assert time.monotonic() - interrupted_at < 1.5, 'SIGTERM未及时取消正在执行的迁移SQL'
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    assert [row['version'] for row in history(database.store)] == [1, 2, 3]
    competitor = database.connect()
    # SIGKILL closes the client; the server notices disconnect at its next query boundary.
    deadline = time.monotonic() + 5
    while not competitor.lock_worker():
        assert time.monotonic() < deadline, '终止的隔离迁移会话未释放锁'
        time.sleep(0.02)
    competitor.unlock_worker()
    monkeypatch.setattr(postgres, 'load_migrations', lambda: chain)
    assert database.store.migrate() == 4
    assert database.store.check_schema() == 4


def test_latest_database_low_privilege_migrate_and_doctor_are_read_only(database, monkeypatch):
    admin = database.store
    with reader(database) as limited:
        assert not limited.one("SELECT has_schema_privilege(current_schema(),'CREATE') AS allowed")['allowed']
        before = history(admin)
        assert limited.migrate() == len(load_migrations())
        assert limited.check_schema() == len(load_migrations())
        monkeypatch.setattr(bootstrap, 'PostgresStore', lambda *args, **kwargs: limited)
        monkeypatch.setattr(bootstrap, 'EzBookkeepingClient', lambda *args, **kwargs: SimpleNamespace(
            close=lambda: None, accounts=lambda: [], categories=lambda: []))
        runtime = bootstrap.Runtime(doctor_settings(database), command='doctor')
        try:
            result = _execute(SimpleNamespace(command='doctor'), runtime)
            assert result['schema_ready'] is True
            assert result['schema_version'] == len(load_migrations())
        finally:
            runtime.close()
        assert history(admin) == before


def test_drifted_v1_cannot_be_adopted_as_trusted_history(database):
    store = database.store
    old_baseline(store)
    store.execute('ALTER TABLE email DROP COLUMN subject')
    before = history(store)
    with pytest.raises(DatabaseDiagnosticError) as error:
        store.migrate()
    assert error.value.code == 'schema_drift'
    assert history(store) == before
    assert 'script_sha256' not in history(store)[0]


def test_v1_low_privilege_upgrade_fails_without_partial_history(database):
    old_baseline(database.store)
    before = history(database.store)
    with reader(database) as limited:
        with pytest.raises(DatabaseDiagnosticError) as error:
            limited.migrate()
        assert error.value.code == 'permission_denied'
        assert history(database.store) == before
        assert 'script_sha256' not in history(database.store)[0]
    assert database.store.migrate() == len(load_migrations())


@pytest.mark.parametrize('legacy', [False, True])
def test_doctor_empty_or_old_schema_is_not_ready_and_does_not_mutate(database, monkeypatch, legacy):
    if legacy:
        old_baseline(database.store)
    else:
        clear_schema(database.store)
    namespace = database.store.one('SELECT current_schema() AS name')['name']
    before = database.store._schema_signature(namespace)
    monkeypatch.setattr(bootstrap, 'PostgresStore', lambda *args, **kwargs: database.connect())
    with pytest.raises(DatabaseDiagnosticError) as error:
        bootstrap.Runtime(doctor_settings(database), command='doctor')
    assert error.value.code == 'schema_not_ready'
    assert database.store._schema_signature(namespace) == before
