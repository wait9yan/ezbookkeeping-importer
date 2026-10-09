"""人工确认仅排除当时观察到且未变化的候选，不授予永久查重豁免。"""

from .write import has_marker

SNAPSHOT_FIELDS = (
    "id", "type", "sourceAccountId", "sourceAmount", "destinationAccountId",
    "destinationAmount", "time", "comment",
)


def candidate_snapshot(candidate):
    return {key: str(candidate[key]) if key in {"id", "sourceAccountId", "destinationAccountId"}
            and candidate.get(key) is not None else candidate.get(key)
            for key in SNAPSHOT_FIELDS}


def unconfirmed_candidates(ledger, transaction, candidates):
    confirmed = (transaction.get("import_decision") or {}).get("confirmed_candidates") or []
    records = {str(c["id"]): c for c in candidates}
    for snapshot in confirmed:
        # Re-read explicit identities even if edits moved them outside the ordinary query.
        current = ledger.get(snapshot["id"])
        if current is None:
            records.pop(snapshot["id"], None)
        elif (candidate_snapshot(current) == snapshot
              and not has_marker(current, transaction["source_marker"])):
            records.pop(snapshot["id"], None)
        else:
            records[str(current["id"])] = current
    return list(records.values())
