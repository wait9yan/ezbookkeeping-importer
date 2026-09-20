import hashlib
import json
import uuid
from pathlib import Path

from .ports import Store


def parse_pending(store: Store, parser):
    for message in store.all("SELECT * FROM messages WHERE status='pending' ORDER BY created_at"):
        try:
            result = parser.parse(Path(message["evidence_path"]).read_bytes())
            parsed = result.model_dump(mode="json")
            fingerprint = hashlib.sha256(
                json.dumps(
                    sorted(
                        [
                            {k: v for k, v in row.items() if k != "evidence"}
                            for row in parsed["rows"]
                        ],
                        key=lambda row: row["row_key"],
                    ),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            with store.transaction():
                store.execute(
                    "UPDATE messages SET parsed=%s,status='parsed' WHERE id=%s",
                    (parsed, message["id"]),
                )
                for issue in parsed["issues"]:
                    store.issue(issue["code"], message["id"], issue)
                if result.kind == "other":
                    store.execute(
                        "UPDATE messages SET status='ignored' WHERE id=%s", (message["id"],)
                    )
                    continue
                if result.issues or result.kind == "unknown":
                    store.issue("parse_incomplete", message["id"], {"issues": parsed["issues"]})
                    continue
                if message["source_status"] != "verified" and not message["accepted"]:
                    store.issue(
                        "source_acceptance",
                        message["id"],
                        {"reason": message["source_reason"], "version": 1},
                    )
                    continue
                existing = store.one(
                    "SELECT * FROM reports WHERE report_key=%s", (result.report_key,)
                )
                if existing and existing["fingerprint"] != fingerprint:
                    store.issue("report_revision", message["id"], {"report_key": result.report_key})
                    continue
                store.execute(
                    """INSERT INTO reports(report_key,kind,message_id,fingerprint,parsed)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (result.report_key, result.kind, message["id"], fingerprint, parsed),
                )
                if result.kind == "monthly":
                    continue
                for row in parsed["rows"]:
                    transaction_id = str(
                        uuid.uuid5(uuid.NAMESPACE_URL, result.report_key + ":" + row["row_key"])
                    )
                    store.execute(
                        """INSERT INTO transactions(id,report_key,row_key,facts,marker)
                        VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (
                            transaction_id,
                            result.report_key,
                            row["row_key"],
                            row,
                            "ebki-" + transaction_id,
                        ),
                    )
                    store.execute(
                        """INSERT INTO transaction_evidence(transaction_id,message_id,row_key)
                        VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (transaction_id, message["id"], row["row_key"]),
                    )
                store.audit(
                    "mail_parsed", message["id"], {"kind": result.kind, "rows": len(result.rows)}
                )
        except Exception as exc:
            with store.transaction():
                store.issue("parse_failed", message["id"], {"error_type": type(exc).__name__})
                store.execute("UPDATE messages SET status='failed' WHERE id=%s", (message["id"],))
