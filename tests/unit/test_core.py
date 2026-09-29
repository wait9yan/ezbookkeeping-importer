from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ezbookkeeping_importer.domain.money import cents
from ezbookkeeping_importer.domain.errors import ImporterError
from ezbookkeeping_importer.adapters.evidence_store import EvidenceStore
from ezbookkeeping_importer.adapters.logging import configure_logging
from ezbookkeeping_importer.entrypoints.worker import next_check
from ezbookkeeping_importer.application.collect import source_status


def test_exact_cent_amounts():
    assert cents(Decimal("10.01")) == 1001
    assert cents(Decimal("-10.01")) == -1001
    with pytest.raises(ImporterError):
        cents(Decimal("1.001"))
    with pytest.raises(ImporterError):
        cents(Decimal("NaN"))


def test_evidence_immutable_atomic(tmp_path):
    evidence = EvidenceStore(tmp_path)
    digest, path = evidence.put(b"synthetic evidence")
    assert evidence.put(b"synthetic evidence") == (digest, path)
    assert list(tmp_path.glob(".incoming-*")) == []
    Path(path).write_bytes(b"damaged")
    with pytest.raises(ValueError):
        evidence.put(b"synthetic evidence")


def test_schedule_boundaries():
    assert next_check(datetime(2026, 1, 1, 16, 59)).isoformat() == "2026-01-01T17:00:00"
    assert next_check(datetime(2026, 1, 1, 17, 0)).isoformat() == "2026-01-01T17:10:00"
    assert next_check(datetime(2026, 1, 1, 23, 59)).isoformat() == "2026-01-02T00:00:00"
    assert next_check(datetime(2026, 1, 2, 0, 0)).isoformat() == "2026-01-02T01:00:00"


def test_source_requires_delivery_context():
    raw = b"From: ccsvc@message.cmbchina.com\r\nAuthentication-Results: mx.qq.com; spf=pass; dkim=pass; dmarc=pass header.from=message.cmbchina.com\r\n\r\n"
    settings = SimpleNamespace(mail=SimpleNamespace(host="imap.qq.com"))
    assert source_status(raw, settings, "imap")[0] == "requires_acceptance"
    raw = b"Received: from bank by mx.qq.com; Thu, 1 Jan 2026 12:00:00 +0800\r\n" + raw
    assert source_status(raw, settings, "imap")[0] == "verified"
    assert source_status(raw, settings, "eml")[0] == "requires_acceptance"


def test_logs_persist_and_rotate(tmp_path, capsys):
    settings = SimpleNamespace(log_dir=tmp_path, log_max_bytes=150, log_backups=2, log_level="INFO")
    logger = configure_logging(settings)
    for _ in range(10):
        logger.info(
            "synthetic_event", extra={"error_type": "SyntheticFailure", "password": "DO_NOT_LOG"}
        )
    persisted = "".join(p.read_text() for p in tmp_path.glob("worker.jsonl*"))
    assert "synthetic_event" in persisted
    assert "DO_NOT_LOG" not in persisted + capsys.readouterr().out
    assert len(list(tmp_path.iterdir())) == 3
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()


def test_rules_only_never_calls_configured_ai():
    from ezbookkeeping_importer.application.classify import decide
    from ezbookkeeping_importer.config import MailSettings, Settings

    settings = Settings(
        ledger_url="http://synthetic.invalid",
        mail=MailSettings(username="synthetic@example.test", source_id="synthetic"),
        timezone="Asia/Shanghai",
        classification_mode="rules_only",
        ai_url="http://synthetic-ai.invalid/v1",
        ai_model="synthetic-model",
    )
    ledger = Mock()
    ledger.accounts.return_value = [
        {
            "id": "account",
            "type": 1,
            "currency": "CNY",
            "hidden": False,
            "comment": "4444333322221234",
        }
    ]
    ledger.categories.return_value = [
        {"id": "fallback", "type": 2, "parentId": "parent", "path": "其他杂项 → 待分类"}
    ]
    ai = Mock()
    transaction = {
        "id": "synthetic-row",
        "source_marker": "ebki-synthetic",
        "event_type": "expense",
        "occurred_date": "2026-01-01",
        "occurred_at": "2026-01-01T12:00:00+08:00",
        "time_precision": "second",
        "original_amount": "10.00",
        "original_currency": "CNY",
        "card_reference": "1234",
        "merchant_name": "合成商户",
        "report_row_key": "row",
        "posted_date": None,
        "bank_settlement_amount": None,
        "bank_settlement_currency": None,
        "source_details": {},
    }
    import_decision = decide(transaction, settings, ledger, ai)
    ai.classify.assert_not_called()
    assert import_decision["payload"]["categoryId"] == "fallback"
    assert import_decision["classification"]["classification_status"] == "unmatched"
