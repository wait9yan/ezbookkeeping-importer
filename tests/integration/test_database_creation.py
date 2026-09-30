"""隔离目标数据库的并发建库和建库等待停止；需要合成测试实例管理员。"""

from concurrent.futures import ThreadPoolExecutor
import os
import signal
import subprocess
import sys
from threading import Barrier
import time
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from ezbookkeeping_importer.adapters.persistence.postgres import PostgresStore
from ezbookkeeping_importer.domain.errors import Conflict


@pytest.fixture
def target_database():
    dsn = os.environ.get('EBKI_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('EBKI_TEST_DATABASE_URL is required for real PostgreSQL tests')
    name = 'ebki_creation_' + uuid.uuid4().hex
    admin = psycopg.connect(make_conninfo(dsn, dbname='postgres', options=''), autocommit=True)
    try:
        yield admin, make_conninfo(dsn, dbname=name, options=''), name
    finally:
        admin.execute(sql.SQL('DROP DATABASE IF EXISTS {}').format(sql.Identifier(name)))
        admin.close()


def test_concurrent_missing_database_creation_has_one_worker(target_database):
    admin, dsn, name = target_database
    stores = []
    barrier = Barrier(2)

    def initialize(_):
        store = PostgresStore(dsn, create_database=True)
        stores.append(store)
        barrier.wait(timeout=5)
        try:
            assert store.migrate(hold_worker=True) == 2
            return 'ready'
        except Conflict:
            return 'conflict'

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(initialize, range(2)))
        assert sorted(outcomes) == ['conflict', 'ready']
        assert stores[0].one('SELECT count(*) AS n FROM schema_version')['n'] == 2
        assert admin.execute('SELECT count(*) FROM pg_database WHERE datname=%s', (name,)).fetchone()[0] == 1
    finally:
        for store in stores:
            store.close()


def test_sigterm_during_creation_lock_wait_leaves_no_database(target_database):
    admin, dsn, name = target_database
    application = 'ebki_cancel_' + uuid.uuid4().hex
    dsn = make_conninfo(dsn, application_name=application)
    admin.execute('SELECT pg_advisory_lock(780419)')
    program = '''import os
from ezbookkeeping_importer.entrypoints.worker import StopSignals
from ezbookkeeping_importer.adapters.persistence.postgres import PostgresStore
from ezbookkeeping_importer.domain.errors import StartupInterrupted
with StopSignals() as stopped:
    try:
        PostgresStore(os.environ['SYNTHETIC_DSN'], create_database=True, stop_event=stopped)
    except StartupInterrupted:
        print('cancelled',flush=True)
'''
    child = subprocess.Popen([sys.executable, '-c', program],
                             env={**os.environ, 'SYNTHETIC_DSN': dsn},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not admin.execute('SELECT 1 FROM pg_stat_activity WHERE application_name=%s',
                                (application,)).fetchone():
            assert time.monotonic() < deadline, '测试子进程未建立维护连接'
            time.sleep(.05)
        child.send_signal(signal.SIGTERM)
        stdout, _ = child.communicate(timeout=3)
        assert child.returncode == 0 and stdout.strip() == 'cancelled'
        assert admin.execute('SELECT 1 FROM pg_database WHERE datname=%s', (name,)).fetchone() is None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        admin.execute('SELECT pg_advisory_unlock(780419)')
