from pathlib import Path

from ..domain.identity import content_fingerprint, transaction_id
from ..domain.errors import ImporterError
from .ports import Store
from .records import trusted_source


def parse_pending(store: Store, parser):
    for message in store.all(
        "SELECT * FROM email WHERE parse_status='pending' ORDER BY collected_at"
    ):
        parser_version = parser.version
        header_issues = [
            issue for issue in message["parse_issues"]
            if issue["code"].startswith("invalid_header_")
        ]
        try:
            result = parser.parse(Path(message["raw_path"]).read_bytes())
            parsed = result.model_dump(mode="json")
            metadata = parsed["metadata"]
            content = {
                "rows": parsed["rows"],
                "controls": metadata.get("controls", {}),
                "extensions": {
                    k: v
                    for k, v in metadata.items()
                    if k
                    not in {
                        "parser_version",
                        "subject",
                        "normalized_subject",
                        "forwarded",
                        "message_id",
                        "from",
                        "authentication_results",
                        "cycle_start",
                        "cycle_end",
                        "controls",
                    }
                },
            }
            keys = [row["row_key"] for row in content["rows"]]
            if len(keys) != len(set(keys)):
                raise ImporterError("duplicate report row keys")
            for key in keys:
                transaction_id(result.report_key, key)
            with store.transaction():
                store.execute(
                    """UPDATE email SET parse_status=%s,parse_issues=%s,
                    parser_version=%s,parsed_at=now() WHERE id=%s""",
                    (
                        "ignored" if result.kind == "other" else "parsed",
                        header_issues + parsed["issues"],
                        parser_version,
                        message["id"],
                    ),
                )
                if result.kind in {"other", "unknown"} or result.issues:
                    continue
                if not trusted_source(store, message["id"]):
                    continue
                sources = store.all(
                    "SELECT DISTINCT source_id FROM email_source_item WHERE email_id=%s",
                    (message["id"],),
                )
                if len(sources) != 1 or sources[0]["source_id"] != parser.context:
                    raise ImporterError("email source context does not match parser context")
                fingerprint = content_fingerprint(content)
                existing = store.one(
                    "SELECT * FROM bank_report WHERE report_key=%s", (result.report_key,)
                )
                if existing:
                    store.execute(
                        "UPDATE email SET report_key=%s WHERE id=%s",
                        (result.report_key, message["id"]),
                    )
                    if existing["content_fingerprint"] != fingerprint:
                        store.execute(
                            "UPDATE email SET parse_issues=%s WHERE id=%s",
                            (
                                header_issues + [
                                    {
                                        "code": "report_revision",
                                        "locator": "report",
                                        "detail": result.report_key,
                                    }
                                ],
                                message["id"],
                            ),
                        )
                    continue
                store.execute(
                    """INSERT INTO bank_report(report_key,source_id,bank_code,report_type,
                    report_date,period_start,period_end,content_fingerprint,source_email_id,parser_version,content)
                    VALUES (%s,%s,'cmb',%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        result.report_key,
                        sources[0]["source_id"],
                        result.kind,
                        result.report_date,
                        metadata.get("cycle_start"),
                        metadata.get("cycle_end"),
                        fingerprint,
                        message["id"],
                        parser_version,
                        content,
                    ),
                )
                store.execute(
                    "UPDATE email SET report_key=%s WHERE id=%s", (result.report_key, message["id"])
                )
                if result.kind == "monthly":
                    continue
                for row in result.rows:
                    tid = transaction_id(result.report_key, row.row_key)
                    store.execute(
                        """INSERT INTO bank_transactions(id,report_key,report_row_key,event_type,
                        occurred_date,occurred_at,time_precision,merchant_name,card_reference,original_amount,
                        original_currency,posted_date,bank_settlement_amount,bank_settlement_currency,source_details)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(report_key,report_row_key) DO NOTHING""",
                        (
                            tid,
                            result.report_key,
                            row.row_key,
                            row.event_type,
                            row.occurred_date,
                            row.occurred_at,
                            row.time_precision,
                            row.merchant_raw,
                            row.card_reference,
                            row.original_amount,
                            row.original_currency,
                            row.posted_date,
                            row.settlement_amount,
                            row.settlement_currency,
                            row.extra,
                        ),
                    )
        except Exception as exc:
            # A report is the batch boundary: facts and report roll back together, while
            # the source keeps an explicit current failure for inspection and parser retry.
            with store.transaction():
                store.execute(
                    """UPDATE email SET parse_status='failed',parsed_at=now(),parser_version=%s,parse_issues=%s WHERE id=%s""",
                    (
                        parser_version,
                        header_issues + [
                            {
                                "code": "parse_failed",
                                "locator": "report",
                                "detail": str(exc)
                                if isinstance(exc, ImporterError)
                                else type(exc).__name__,
                            }
                        ],
                        message["id"],
                    ),
                )
