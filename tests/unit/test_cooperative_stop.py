"""逐项停止保留当前结果，不把取消报告为业务失败或批次完成。"""
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

from ezbookkeeping_importer.application import classify, collect, reconcile, service, write


def test_collect_stop_during_folder_listing_is_not_completion():
    stopped = Event()
    mail = Mock()

    def folders():
        stopped.set()
        return []

    mail.folders.side_effect = folders
    settings = SimpleNamespace(mail=SimpleNamespace(source_id="synthetic"))
    store = Mock()
    assert collect.collect(store, mail, Mock(), settings, should_stop=stopped.is_set) is None
    store.execute.assert_not_called()
    mail.scan.assert_not_called()


def test_service_requeues_stopped_collection_without_success_or_failure(monkeypatch):
    store = Mock()
    store.one.return_value = {"id": 1, "task_type": "sync", "payload": {}}
    runtime = SimpleNamespace(store=store, mail=Mock(), evidence=Mock(), settings=Mock())
    monkeypatch.setattr(service, "collect", Mock(return_value=None))
    emit = Mock()
    monkeypatch.setattr(service, "emit", emit)
    assert service.cycle(runtime, Mock()) is None
    assert "status='queued'" in store.execute.call_args.args[0]
    assert [call.args[0] for call in emit.call_args_list] == ["sync_started"]
    runtime.mail.return_value.close.assert_called_once()


def test_classification_finishes_current_transaction_only(monkeypatch):
    stopped = Event()
    store = Mock()
    store.all.return_value = [{"id": "one"}, {"id": "two"}]

    def classify_one(*args):
        stopped.set()
        return "queued", {}

    operation = Mock(side_effect=classify_one)
    monkeypatch.setattr(classify, "_classify_one", operation)
    classify.classify_pending(store, Mock(), Mock(), None, should_stop=stopped.is_set)
    assert operation.call_count == 1
    assert operation.call_args.args[4] == {"id": "one"}
    store.execute.assert_not_called()


def test_unknown_verification_finishes_current_job_without_next_read(monkeypatch):
    stopped = Event()
    payload = {"type": 3, "sourceAccountId": "1", "sourceAmount": 100, "time": 1}
    job = {"id": 1, "bank_transaction_id": "one", "decision_version": 1,
           "ledger_transaction_id": "remote", "payload": payload, "task_type": "create"}
    store = Mock()
    store.all.return_value = [job, {**job, "id": 2}]
    store.one.return_value = {"decision_version": 1, "source_marker": "ebki-one"}
    ledger = Mock()

    def get(identifier):
        stopped.set()
        return {**payload, "id": identifier, "comment": "ebki-one"}

    ledger.get.side_effect = get
    monkeypatch.setattr(write, "target_currency", Mock(return_value="CNY"))
    monkeypatch.setattr(write, "validate_accounts", Mock())
    complete = Mock()
    monkeypatch.setattr(write, "complete", complete)
    write.verify_unknown(store, ledger, should_stop=stopped.is_set)
    ledger.get.assert_called_once_with("remote")
    complete.assert_called_once()
    ledger.create.assert_not_called()


def test_reports_stop_before_next_report_or_remote_query(tmp_path, monkeypatch):
    store = Mock()
    store.all.return_value = [{"report_key": "one"}, {"report_key": "two"}]
    snapshot = Mock(side_effect=AssertionError("claimed report after stop"))
    monkeypatch.setattr(reconcile, "snapshot", snapshot)
    assert reconcile.run_reports(
        store, Mock(), tmp_path, datetime.now(timezone.utc), False, should_stop=lambda: True
    ) == (False, True)
    snapshot.assert_not_called()
