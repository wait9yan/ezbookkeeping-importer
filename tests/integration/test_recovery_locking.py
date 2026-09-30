"""启动恢复与单次人工操作遵守同一个交易、任务行锁顺序。"""

import threading
import time

import test_pipeline as pipeline

from ezbookkeeping_importer.application.write import recover_dispatching

database = pipeline.database
settings = pipeline.settings


def test_recovery_waits_for_transaction_before_locking_task(database, settings, tmp_path):
    store, recovering, observer = database.store, database.connect(), database.connect()
    pipeline.queue(store, tmp_path, settings, pipeline.Ledger())
    store.execute("UPDATE background_task SET status='dispatching'")
    store.execute("UPDATE bank_transactions SET import_status='dispatching'")
    job = store.one("SELECT * FROM background_task")
    backend_pid = recovering.one("SELECT pg_backend_pid() AS pid")["pid"]
    results, errors = [], []

    def recover():
        try:
            results.append(recover_dispatching(recovering))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=recover)
    try:
        with store.transaction():
            store.execute("SET LOCAL lock_timeout='1s'")
            store.one("SELECT id FROM bank_transactions WHERE id=%s FOR UPDATE",
                      (job["bank_transaction_id"],))
            thread.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                activity = observer.one(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (backend_pid,)
                )
                if activity["wait_event_type"] == "Lock":
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("recovery did not wait for the held transaction lock")
            # Recovery must not hold this task while waiting for our transaction.
            store.one("SELECT id FROM background_task WHERE id=%s FOR UPDATE", (job["id"],))
            # A concurrent decision completes before recovery acquires both locks.
            store.execute("UPDATE background_task SET status='cancelled' WHERE id=%s", (job["id"],))
    finally:
        if thread.ident is not None:
            thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors
    assert results == [0]
    assert store.one("SELECT status FROM background_task WHERE id=%s", (job["id"],))["status"] == "cancelled"
