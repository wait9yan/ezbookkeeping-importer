"""手动复查只安排一轮，保留冻结决定及现有写入恢复边界。"""

from concurrent.futures import ThreadPoolExecutor
import logging
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

import test_pipeline as pipeline
from ezbookkeeping_importer.adapters.banks.cmb import BankParser
from ezbookkeeping_importer.adapters.evidence_store import EvidenceStore
from ezbookkeeping_importer.adapters.ezbookkeeping.client import EzBookkeepingClient
from ezbookkeeping_importer.application.classify import classify_pending, decide
from ezbookkeeping_importer.application.collect import request_sync
from ezbookkeeping_importer.application.parse import parse_pending
from ezbookkeeping_importer.application.recheck import request_recheck
from ezbookkeeping_importer.application.resolve import resolve
from ezbookkeeping_importer.application.service import cycle
from ezbookkeeping_importer.application.write import recover_dispatching, write_queued
from ezbookkeeping_importer.domain.errors import LedgerError

database = pipeline.database
settings = pipeline.settings


class ObservedLedger(pipeline.Ledger):
    def __init__(self):
        super().__init__()
        self.search_calls = []

    def search(self, start, end, marker=None):
        self.search_calls.append((start, end, marker))
        return super().search(start, end, marker)


def blocked_transactions(store, tmp_path, settings, *, count=1, currency="CNY"):
    ledger, ai = ObservedLedger(), Mock()
    ai.classify.return_value = {
        "classification_status": "matched",
        "category_id": "edited-category",
        "reason": "合成分类",
        "audit": {"model": "synthetic", "usage": {"total_tokens": 12}},
    }
    pipeline.import_daily(store, tmp_path, settings, count=count, currency=currency)
    transaction = store.one("SELECT * FROM bank_transactions ORDER BY id")
    payload = decide(transaction, settings, ledger, None)["payload"]
    ledger.records["7001"] = {
        **payload,
        "id": "7001",
        "comment": f"旧账单 {transaction['merchant_name']}",
    }
    ai_settings = settings.model_copy(update={"classification_mode": "ai"})
    classify_pending(store, ai_settings, ledger, ai)
    rows = store.all("SELECT * FROM bank_transactions ORDER BY id")
    assert len(rows) == count
    assert all(row["import_status"] == "issue" for row in rows)
    assert all(row["import_error"]["code"] == "duplicate_candidates" for row in rows)
    assert ai.classify.call_count == count
    ai.reset_mock()
    ledger.search_calls.clear()
    return SimpleNamespace(ledger=ledger, ai=ai, settings=ai_settings, rows=rows)


def assert_frozen(before, after):
    for field in (
        "id",
        "source_marker",
        "decision_version",
        "import_decision",
        "original_amount",
        "original_currency",
        "occurred_at",
        "card_reference",
    ):
        assert after[field] == before[field], field


def worker_cycle(store, tmp_path, context):
    mail = SimpleNamespace(folders=lambda: [], close=lambda: None)
    runtime = SimpleNamespace(
        store=store,
        settings=context.settings.model_copy(update={"report_dir": tmp_path / "reports"}),
        ledger=context.ledger,
        ai=context.ai,
        evidence=EvidenceStore(tmp_path / "evidence"),
        parser=BankParser(context="synthetic"),
        mail=lambda: mail,
    )
    assert cycle(runtime, logging.getLogger("recheck-test")) is True


@pytest.mark.parametrize("currency", ["CNY", "USD"])
def test_deleted_candidates_book_once_without_reclassifying(database, tmp_path, settings, currency):
    store = database.store
    context = blocked_transactions(store, tmp_path, settings, count=3, currency=currency)
    context.ledger.records.clear()

    assert request_recheck(store) == {"scheduled": 3, "already_pending": 0, "skipped": 0}
    for before, after in zip(
        context.rows, store.all("SELECT * FROM bank_transactions ORDER BY id"), strict=True
    ):
        assert after["import_status"] == "pending"
        assert_frozen(before, after)
    assert context.ledger.search_calls == []
    assert store.all("SELECT * FROM background_task") == []
    assert request_recheck(store) == {"scheduled": 0, "already_pending": 3, "skipped": 0}

    worker_cycle(store, tmp_path, context)

    for before, after in zip(
        context.rows, store.all("SELECT * FROM bank_transactions ORDER BY id"), strict=True
    ):
        assert after["import_status"] == "booked"
        assert after["import_error"] is None
        assert_frozen(before, after)
    assert len(context.ledger.create_calls) == 3
    assert context.ledger.create_calls == [
        row["import_decision"]["payload"] for row in context.rows
    ]
    assert all(
        row["outcome"] == "confirmed" for row in store.all("SELECT * FROM ledger_write_attempt")
    )
    assert store.one("SELECT count(*) AS n FROM ledger_write_attempt")["n"] == 3
    context.ai.classify.assert_not_called()
    assert request_recheck(store) == {"scheduled": 0, "already_pending": 0, "skipped": 0}
    worker_cycle(store, tmp_path, context)
    assert len(context.ledger.create_calls) == 3


def test_persistent_candidates_pause_after_one_pass_even_after_sync_and_restart(
    database, tmp_path, settings
):
    store = database.store
    context = blocked_transactions(store, tmp_path, settings, count=57)

    assert request_recheck(store)["scheduled"] == 57
    worker_cycle(store, tmp_path, context)

    assert len(context.ledger.search_calls) == 1
    assert context.ledger.create_calls == []
    assert all(
        row["import_status"] == "issue" for row in store.all("SELECT * FROM bank_transactions")
    )
    assert request_sync(store)
    restarted = database.connect()
    recover_dispatching(restarted)
    worker_cycle(restarted, tmp_path, context)
    worker_cycle(restarted, tmp_path, context)
    assert len(context.ledger.search_calls) == 1
    context.ai.classify.assert_not_called()

    assert request_recheck(restarted)["scheduled"] == 57
    worker_cycle(restarted, tmp_path, context)
    assert len(context.ledger.search_calls) == 2
    assert context.ledger.create_calls == []


def test_new_candidate_is_found_after_old_id_disappears(database, tmp_path, settings):
    store = database.store
    context = blocked_transactions(store, tmp_path, settings)
    original = context.ledger.records.pop("7001")
    context.ledger.records["7002"] = {**original, "id": "7002"}

    assert request_recheck(store)["scheduled"] == 1
    worker_cycle(store, tmp_path, context)

    current = store.one("SELECT * FROM bank_transactions")
    assert current["import_status"] == "issue"
    assert current["import_error"]["detail"]["candidate_ids"] == ["7002"]
    assert_frozen(context.rows[0], current)
    assert context.ledger.create_calls == []
    assert len(context.ledger.search_calls) == 1


def test_programming_failure_is_not_reported_as_recoverable_duplicate_query(
    database, tmp_path, settings, monkeypatch
):
    store = database.store
    context = blocked_transactions(store, tmp_path, settings)
    assert request_recheck(store)["scheduled"] == 1
    search = Mock(side_effect=TypeError("合成程序错误，不是外部查询故障"))
    monkeypatch.setattr(context.ledger, "search", search)

    classify_pending(store, context.settings, context.ledger, context.ai)

    current = store.one("SELECT * FROM bank_transactions")
    assert current["import_status"] == "issue"
    assert current["import_error"]["code"] == "classification_failed"
    assert current["import_error"]["detail"]["error_type"] == "TypeError"
    assert_frozen(context.rows[0], current)
    assert request_recheck(store) == {"scheduled": 0, "already_pending": 0, "skipped": 0}
    classify_pending(store, context.settings, context.ledger, context.ai)
    search.assert_called_once()
    context.ai.classify.assert_not_called()
    assert store.all("SELECT * FROM background_task") == []


def test_matching_source_marker_restores_link_without_a_write_attempt(database, tmp_path, settings):
    store = database.store
    context = blocked_transactions(store, tmp_path, settings)
    row = context.rows[0]
    context.ledger.records = {"8001": {"id": "8001", **row["import_decision"]["payload"]}}

    assert request_recheck(store)["scheduled"] == 1
    worker_cycle(store, tmp_path, context)

    current = store.one("SELECT * FROM bank_transactions")
    assert current["import_status"] == "booked"
    assert current["ledger_transaction_id"] == "8001"
    assert_frozen(row, current)
    task = store.one("SELECT * FROM background_task")
    assert task["status"] == "done"
    assert task["completion_method"] == "existing_link"
    assert store.all("SELECT * FROM ledger_write_attempt") == []
    assert context.ledger.create_calls == []
    context.ai.classify.assert_not_called()


def test_write_unknown_after_recheck_is_verified_only_and_never_reposted(
    database, tmp_path, settings
):
    store = database.store
    context = blocked_transactions(store, tmp_path, settings)
    context.ledger.records.clear()
    context.ledger.mode = "timeout_empty"

    assert request_recheck(store)["scheduled"] == 1
    worker_cycle(store, tmp_path, context)

    transaction = store.one("SELECT * FROM bank_transactions")
    task = store.one("SELECT * FROM background_task")
    attempt = store.one("SELECT * FROM ledger_write_attempt")
    assert transaction["import_status"] == task["status"] == attempt["outcome"] == "unknown"
    assert len(context.ledger.create_calls) == 1
    assert request_recheck(store)["scheduled"] == 0
    worker_cycle(store, tmp_path, context)
    assert len(context.ledger.create_calls) == 1
    assert store.one("SELECT * FROM bank_transactions") == transaction
    assert store.one("SELECT count(*) AS n FROM ledger_write_attempt")["n"] == 1
    context.ai.classify.assert_not_called()


@pytest.mark.parametrize("saved_flag", ["allow_new", "reclassify_requested"])
def test_recheck_does_not_reuse_bypass_or_reclassification_intent(
    database, tmp_path, settings, saved_flag
):
    store = database.store
    context = blocked_transactions(store, tmp_path, settings)
    decision = {**context.rows[0]["import_decision"], saved_flag: True}
    store.execute("UPDATE bank_transactions SET import_decision=%s", (decision,))
    before = store.one("SELECT * FROM bank_transactions")

    assert request_recheck(store)["scheduled"] == 1
    worker_cycle(store, tmp_path, context)

    current = store.one("SELECT * FROM bank_transactions")
    assert current["import_status"] == "issue"
    assert current["import_error"]["code"] == "duplicate_candidates"
    assert_frozen(before, current)
    assert context.ledger.create_calls == []
    context.ai.classify.assert_not_called()


def paginated_search(monkeypatch, ledger, handler):
    client = EzBookkeepingClient(
        "https://synthetic.invalid",
        "synthetic-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(ledger, "search", client.search)
    return client


def response_page(items, cursor):
    return httpx.Response(
        200, json={"success": True, "result": {"items": items, "nextTimeSequenceId": cursor}}
    )


def test_candidate_on_second_http_page_blocks_creation(database, tmp_path, settings, monkeypatch):
    store = database.store
    context = blocked_transactions(store, tmp_path, settings)
    candidate = {**context.ledger.records["7001"], "id": "8001"}
    context.ledger.records.clear()
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path.endswith("transactions/list.json")
        if len(requests) == 1:
            cursor = str(candidate["time"] * 1000)
            return response_page(
                [{**candidate, "id": str(i), "comment": "其他商户"} for i in range(1, 51)],
                cursor,
            )
        assert request.url.params["max_time"] == str(candidate["time"] * 1000)
        return response_page([candidate], None)

    client = paginated_search(monkeypatch, context.ledger, handler)
    try:
        request_recheck(store)
        classify_pending(store, context.settings, context.ledger, context.ai)
        write_queued(store, context.ledger)
    finally:
        client.close()

    assert len(requests) == 2
    current = store.one("SELECT * FROM bank_transactions")
    assert current["import_status"] == "issue"
    assert current["import_error"]["detail"]["candidate_ids"] == ["8001"]
    assert context.ledger.create_calls == []
    context.ai.classify.assert_not_called()


@pytest.mark.parametrize("failure", ["timeout", "missing_cursor", "stalled_cursor"])
def test_incomplete_http_search_pauses_and_next_manual_recheck_can_recover(
    database, tmp_path, settings, monkeypatch, failure
):
    store = database.store
    context = blocked_transactions(store, tmp_path, settings, count=3)
    context.ledger.records.clear()
    requests = []
    failing = True

    def handler(request):
        requests.append(request)
        if not failing:
            return response_page([], None)
        if len(requests) == 1:
            return response_page(
                [], str(context.rows[0]["import_decision"]["payload"]["time"] * 1000)
            )
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic transport failure", request=request)
        if failure == "missing_cursor":
            return httpx.Response(200, json={"success": True, "result": {"items": []}})
        return response_page([], request.url.params["max_time"])

    client = paginated_search(monkeypatch, context.ledger, handler)
    try:
        assert request_recheck(store)["scheduled"] == 3
        worker_cycle(store, tmp_path, context)
        assert len(requests) == 2
        for before, after in zip(
            context.rows, store.all("SELECT * FROM bank_transactions ORDER BY id"), strict=True
        ):
            assert after["import_status"] == "issue"
            assert after["import_error"]["code"] == "duplicate_check_failed"
            assert_frozen(before, after)
        assert context.ledger.create_calls == []
        assert store.all("SELECT * FROM background_task") == []
        worker_cycle(store, tmp_path, context)
        assert len(requests) == 2

        failing = False
        assert request_recheck(store)["scheduled"] == 3
        worker_cycle(store, tmp_path, context)
        assert len(requests) == 3
        assert len(context.ledger.create_calls) == 3
        assert all(
            row["import_status"] == "booked" for row in store.all("SELECT * FROM bank_transactions")
        )
        context.ai.classify.assert_not_called()
    finally:
        client.close()


@pytest.mark.parametrize("state", ["unknown", "dispatching", "booked", "ignored", "queued"])
def test_nonissue_transaction_state_is_never_restored(database, tmp_path, settings, state):
    store = database.store
    blocked_transactions(store, tmp_path, settings)
    store.execute("UPDATE bank_transactions SET import_status=%s", (state,))
    before = store.one("SELECT * FROM bank_transactions")

    assert request_recheck(store)["scheduled"] == 0
    assert store.one("SELECT * FROM bank_transactions") == before


@pytest.mark.parametrize("state", ["queued", "dispatching", "unknown", "done"])
def test_existing_write_task_is_never_restarted(database, tmp_path, settings, state):
    store = database.store
    context = blocked_transactions(store, tmp_path, settings)
    row = context.rows[0]
    store.execute(
        """INSERT INTO background_task(task_type,bank_transaction_id,decision_version,
        operation_key,payload,status) VALUES ('create',%s,%s,%s,%s,%s)""",
        (
            row["id"],
            row["decision_version"],
            row["source_marker"],
            row["import_decision"]["payload"],
            state,
        ),
    )
    before = store.one("SELECT * FROM bank_transactions")
    task = store.one("SELECT * FROM background_task")

    assert request_recheck(store)["scheduled"] == 0
    assert store.one("SELECT * FROM bank_transactions") == before
    assert store.one("SELECT * FROM background_task") == task


@pytest.mark.parametrize("outcome", ["unknown", "confirmed"])
def test_unresolved_or_confirmed_attempt_blocks_recheck_even_when_task_is_cancelled(
    database, tmp_path, settings, outcome
):
    store = database.store
    context = blocked_transactions(store, tmp_path, settings)
    row = context.rows[0]
    task = store.one(
        """INSERT INTO background_task(task_type,bank_transaction_id,decision_version,
        operation_key,payload,status) VALUES ('create',%s,%s,%s,%s,'cancelled') RETURNING id""",
        (
            row["id"],
            row["decision_version"],
            row["source_marker"],
            row["import_decision"]["payload"],
        ),
    )
    store.execute(
        "INSERT INTO ledger_write_attempt(task_id,decision_version,request,outcome) VALUES (%s,%s,%s,%s)",
        (task["id"], row["decision_version"], row["import_decision"]["payload"], outcome),
    )
    before = store.one("SELECT * FROM bank_transactions")

    assert request_recheck(store)["scheduled"] == 0
    assert store.one("SELECT * FROM bank_transactions") == before
    assert store.one("SELECT outcome FROM ledger_write_attempt")["outcome"] == outcome


def test_empty_database_and_unaccepted_source_do_not_schedule_recheck(database, tmp_path, settings):
    store = database.store
    assert request_recheck(store) == {"scheduled": 0, "already_pending": 0, "skipped": 0}
    pipeline.ingest_mail(
        store, EvidenceStore(tmp_path / "evidence"), pipeline.raw_daily(), settings, accept=False
    )
    parse_pending(store, BankParser(context="synthetic"))
    source = store.one("SELECT * FROM email_source_item")
    assert source["source_status"] == "requires_acceptance"
    assert source["accepted_at"] is None
    assert store.all("SELECT * FROM bank_transactions") == []

    assert request_recheck(store) == {"scheduled": 0, "already_pending": 0, "skipped": 0}
    assert store.one("SELECT * FROM email_source_item") == source


@pytest.mark.parametrize("change", ["missing_payload", "different_issue", "already_linked"])
def test_other_problem_or_missing_frozen_decision_is_not_reclassified(
    database, tmp_path, settings, change
):
    store = database.store
    blocked_transactions(store, tmp_path, settings)
    if change == "missing_payload":
        store.execute("UPDATE bank_transactions SET import_decision=NULL")
    elif change == "different_issue":
        store.execute(
            "UPDATE bank_transactions SET import_error=%s",
            ({"code": "classification_failed", "detail": {"reason": "合成错误"}},),
        )
    else:
        store.execute("UPDATE bank_transactions SET ledger_transaction_id='7001'")
    before = store.one("SELECT * FROM bank_transactions")

    assert request_recheck(store)["scheduled"] == 0
    assert store.one("SELECT * FROM bank_transactions") == before


def after_candidate_snapshot(monkeypatch, store, identifier, callback):
    original = store.all
    triggered = False

    def read(sql, params=()):
        nonlocal triggered
        rows = original(sql, params)
        if not triggered and any(row.get("id") == identifier for row in rows):
            triggered = True
            callback()
        return rows

    monkeypatch.setattr(store, "all", read)


@pytest.mark.parametrize("action", ["ignore", "link", "version_change"])
def test_changed_decision_between_selection_and_lock_is_preserved(
    database, tmp_path, settings, monkeypatch, action
):
    store, resolver = database.store, database.connect()
    context = blocked_transactions(store, tmp_path, settings)
    row = context.rows[0]
    decided = []

    def concurrent_change():
        if action == "version_change":
            resolver.execute(
                "UPDATE bank_transactions SET decision_version=decision_version+1 WHERE id=%s",
                (row["id"],),
            )
        else:
            resolve(
                resolver,
                context.ledger,
                "bank_transactions",
                row["id"],
                row["decision_version"],
                action,
                "并发人工决定",
                target_id="7001" if action == "link" else None,
            )
        decided.append(resolver.one("SELECT * FROM bank_transactions"))

    after_candidate_snapshot(monkeypatch, store, row["id"], concurrent_change)
    result = request_recheck(store)

    assert len(decided) == 1
    assert result == {"scheduled": 0, "already_pending": 0, "skipped": 1}
    assert resolver.one("SELECT * FROM bank_transactions") == decided[0]
    assert resolver.all("SELECT * FROM background_task") == []


def test_concurrent_recheck_requests_only_schedule_once(database, tmp_path, settings, monkeypatch):
    first, second = database.store, database.connect()
    context = blocked_transactions(first, tmp_path, settings)
    barrier = Barrier(2)
    for store in (first, second):
        after_candidate_snapshot(monkeypatch, store, context.rows[0]["id"], lambda: barrier.wait(5))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request_recheck, store) for store in (first, second)]
        results = [future.result(timeout=10) for future in futures]

    assert sum(result["scheduled"] for result in results) == 1
    assert sum(result["already_pending"] + result["skipped"] for result in results) == 1
    current = first.one("SELECT * FROM bank_transactions")
    assert current["import_status"] == "pending"
    assert_frozen(context.rows[0], current)
    assert first.all("SELECT * FROM background_task") == []


@pytest.mark.parametrize("action", ["ignore", "link"])
@pytest.mark.parametrize("query_failed", [False, True], ids=["query-complete", "query-failed"])
def test_manual_decision_during_remote_query_is_not_overwritten(
    database, tmp_path, settings, monkeypatch, action, query_failed
):
    store, resolver = database.store, database.connect()
    context = blocked_transactions(store, tmp_path, settings)
    row = context.rows[0]
    request_recheck(store)
    decided = []

    def search(start, end, marker=None):
        resolve(
            resolver,
            context.ledger,
            "bank_transactions",
            row["id"],
            row["decision_version"],
            action,
            "远端查询期间的人工决定",
            target_id="7001" if action == "link" else None,
        )
        decided.append(resolver.one("SELECT * FROM bank_transactions"))
        if query_failed:
            raise LedgerError("合成查重失败")
        return []

    monkeypatch.setattr(context.ledger, "search", search)
    classify_pending(store, context.settings, context.ledger, context.ai)
    write_queued(store, context.ledger)

    assert len(decided) == 1
    assert store.one("SELECT * FROM bank_transactions") == decided[0]
    assert store.all("SELECT * FROM background_task") == []
    assert context.ledger.create_calls == []
    context.ai.classify.assert_not_called()
