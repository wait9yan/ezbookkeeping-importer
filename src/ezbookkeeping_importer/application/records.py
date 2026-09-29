"""明确列到解析边界的唯一投影，不持久化第二份交易事实。"""

from ..domain.models import SourceRow


def source_row(transaction: dict) -> dict:
    return SourceRow(
        row_key=transaction["report_row_key"],
        event_type=transaction["event_type"],
        occurred_date=transaction["occurred_date"],
        occurred_at=transaction["occurred_at"],
        time_precision=transaction["time_precision"],
        card_reference=transaction["card_reference"],
        merchant_raw=transaction["merchant_name"],
        original_amount=transaction["original_amount"],
        original_currency=transaction["original_currency"],
        posted_date=transaction["posted_date"],
        settlement_amount=transaction["bank_settlement_amount"],
        settlement_currency=transaction["bank_settlement_currency"],
        evidence={},
        extra=transaction["source_details"],
    ).model_dump(mode="json")


def trusted_source(store, email_id: str) -> bool:
    return bool(
        store.one(
            """SELECT id FROM email_source_item WHERE email_id=%s
        AND status='collected' AND (source_status='verified' OR accepted_at IS NOT NULL)
        LIMIT 1""",
            (email_id,),
        )
    )
