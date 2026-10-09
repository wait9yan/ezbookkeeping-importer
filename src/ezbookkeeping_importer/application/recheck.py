"""用户主动安排一轮重复候选复查，不建立周期重试或新的业务决定。"""

from .ports import Store

RECHECK_CODES = ("duplicate_candidates", "duplicate_check_failed")


def is_duplicate_recheck(transaction: dict) -> bool:
    return (
        transaction["import_status"] == "pending"
        and (transaction.get("import_error") or {}).get("code") in RECHECK_CODES
    )


def request_recheck(store: Store, targets: list[dict] | None = None) -> dict[str, int]:
    if targets is not None:
        candidates = [
            {"id": item["entity_id"], "decision_version": item["version"]}
            for item in targets
            if item["entity_type"] == "bank_transactions" and item["code"] in RECHECK_CODES
        ]
    else:
        candidates = store.all(
            """SELECT id,decision_version FROM bank_transactions
            WHERE import_error->>'code' IN (%s,%s) ORDER BY id""",
            RECHECK_CODES,
        )
    snapshots = {item["entity_id"]: item for item in targets or []}
    counts = {"scheduled": 0, "already_pending": 0, "skipped": 0}
    for candidate in candidates:
        with store.transaction():
            from .sources import lock_transaction_source

            if not store.one("SELECT id FROM bank_transactions WHERE id=%s", (candidate["id"],)):
                counts["skipped"] += 1
                continue
            lock_transaction_source(store, candidate["id"])
            current = store.one(
                "SELECT * FROM bank_transactions WHERE id=%s FOR UPDATE", (candidate["id"],)
            )
            if (
                not current
                or current["decision_version"] != candidate["decision_version"]
                or (current.get("import_error") or {}).get("code") not in RECHECK_CODES
                or current["ledger_transaction_id"] is not None
                or not (current.get("import_decision") or {}).get("payload")
            ):
                counts["skipped"] += 1
                continue
            selected = snapshots.get(candidate["id"])
            if selected and (
                selected["status"] != current["import_status"]
                or selected["code"] != current["import_error"]["code"]
                or selected["detail"] != current["import_error"].get("detail")
            ):
                counts["skipped"] += 1
                continue
            unsafe_write = store.one(
                """SELECT t.id FROM background_task t WHERE t.bank_transaction_id=%s
                AND (t.status IN ('queued','dispatching','unknown','done')
                    OR EXISTS(SELECT 1 FROM ledger_write_attempt a WHERE a.task_id=t.id
                        AND a.outcome IN ('unknown','confirmed'))) LIMIT 1""",
                (current["id"],),
            )
            if unsafe_write:
                counts["skipped"] += 1
                continue
            if current["import_status"] == "pending":
                counts["already_pending"] += 1
                continue
            if current["import_status"] != "issue":
                counts["skipped"] += 1
                continue
            # Keep the diagnostic while pending: it identifies this one-shot check,
            # without inventing a second queue or changing the frozen decision version.
            changed = store.execute(
                """UPDATE bank_transactions SET import_status='pending'
                WHERE id=%s AND decision_version=%s AND import_status='issue'""",
                (current["id"], candidate["decision_version"]),
            )
            counts["scheduled" if changed.rowcount else "skipped"] += 1
    if targets is None:
        from .source_issues import source_issues

        source_targets = [item for item in source_issues(store) if item["status"] != "ignored"]
    else:
        source_targets = [item for item in targets if item["entity_type"] == "transaction_source"]
    for selected in source_targets:
        counts[_request_source_recheck(store, selected)] += 1
    return counts


def _request_source_recheck(store, selected):
    from ..domain.errors import Conflict
    from .source_issues import source_state
    from .sources import accept_daily, schedule_source

    with store.transaction():
        try:
            state = source_state(store, selected["entity_id"], lock=True)
        except Conflict:
            return "skipped"
        row = state["row"]
        if (row["resolution_version"] != selected["version"] or row["transaction_id"]
                or (row["last_resolution"] or {}).get("action") == "ignore"):
            return "skipped"
        report = store.one("SELECT * FROM bank_report WHERE report_key=%s", (row["report_key"],))
        if report["report_type"] == "daily":
            accept_daily(store, report, row_keys=frozenset({row["row_key"]}))
        schedule_source(store, report["source_id"])
        return "scheduled"


def request_snapshot_recheck(store: Store, snapshot):
    """固定快照集合逐项提交；过期对象显式跳过，不扩大筛选范围。"""
    from ..domain.errors import Conflict, ImporterError
    from .issue_snapshot import assert_snapshot_item, validate_snapshot

    document = validate_snapshot(snapshot)
    unique: dict[str, dict] = {}
    for item in document["items"]:
        issue = item["issue"]
        if not (
            (issue["entity_type"] == "bank_transactions" and issue["code"] in RECHECK_CODES)
            or (issue["entity_type"] == "transaction_source" and issue["status"] != "ignored")
        ):
            raise ImporterError("recheck snapshot requires duplicate transaction or unresolved source issues")
        key = issue["entity_type"] + ":" + issue["entity_id"]
        if key in unique and unique[key] != item:
            raise ImporterError("conflicting snapshots for the same transaction")
        unique[key] = item
    counts = {"scheduled": 0, "already_pending": 0, "skipped": 0}
    results = []
    for item in unique.values():
        issue = item["issue"]
        try:
            with store.transaction():
                assert_snapshot_item(store, item, lock=True)
                result = request_recheck(store, [issue])
            outcome = next(key for key, count in result.items() if count)
            reason = "not eligible for duplicate recheck" if outcome == "skipped" else None
        except Conflict:
            outcome, reason = "skipped", "snapshot changed; query again"
        counts[outcome] += 1
        results.append(
            {
                "entity_type": issue["entity_type"],
                "entity_id": issue["entity_id"],
                "result": outcome,
                "reason": reason,
            }
        )
    return {**counts, "items": results}
