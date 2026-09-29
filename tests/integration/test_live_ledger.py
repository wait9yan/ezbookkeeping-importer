"""显式指定隔离账本上下文后执行真实 HTTP 回归，默认不访问网络。"""

import json
import os
import time
import uuid
from pathlib import Path

import pytest

from ezbookkeeping_importer.adapters.ezbookkeeping.client import EzBookkeepingClient


@pytest.fixture
def live_ledger():
    context_path = os.environ.get("EBKI_LIVE_CONTEXT")
    if not context_path:
        pytest.skip("EBKI_LIVE_CONTEXT 未指定隔离测试账本上下文")
    context = json.loads(Path(context_path).read_text())
    client = EzBookkeepingClient(context["base_url"], context["token"])
    try:
        yield client, context
    finally:
        client.close()


def test_live_create_refund_transfer_and_settlement(live_ledger):
    ledger, context = live_ledger
    source_marker = "ebki-" + str(uuid.uuid4())
    timestamp = int(time.time()) - 60
    base = {
        "type": 3,
        "categoryId": context["categories"][0]["id"],
        "time": timestamp,
        "utcOffset": 480,
        "sourceAccountId": context["accounts"][0]["id"],
        "sourceAmount": 1234,
        "hideAmount": True,
        "tagIds": [],
        "pictureIds": [],
        "comment": source_marker,
        "clientSessionId": source_marker,
        "geoLocation": {"latitude": 31.2, "longitude": 121.4},
    }
    created = ledger.create(base)
    current = ledger.get(created["id"])
    assert current is not None and current["sourceAmount"] == 1234
    ledger.modify(ledger.settlement_payload(current, 1250))
    updated = ledger.get(created["id"])
    assert updated is not None and updated["sourceAmount"] == 1250
    for field in (
        "categoryId",
        "time",
        "utcOffset",
        "sourceAccountId",
        "hideAmount",
        "tagIds",
        "comment",
        "geoLocation",
    ):
        assert updated[field] == current[field]
    refund = ledger.create(
        {
            **base,
            "sourceAmount": -99,
            "comment": source_marker + " refund",
            "clientSessionId": str(uuid.uuid4()),
        }
    )
    assert ledger.get(refund["id"])["sourceAmount"] == -99
    transfer = ledger.create(
        {
            **base,
            "type": 4,
            "categoryId": context["categories"][1]["id"],
            "sourceAccountId": context["accounts"][1]["id"],
            "destinationAccountId": context["accounts"][0]["id"],
            "sourceAmount": 1000,
            "destinationAmount": 1000,
            "comment": source_marker + " transfer",
            "clientSessionId": str(uuid.uuid4()),
        }
    )
    assert ledger.get(transfer["id"])["type"] == 4
    assert len(ledger.search(timestamp, timestamp, source_marker)) == 3
    assert ledger.get("1") is None


def test_live_same_second_pagination(live_ledger):
    ledger, context = live_ledger
    source_marker = "ebki-" + str(uuid.uuid4())
    timestamp = int(time.time()) - 60
    expected = set()
    for amount in range(1, 54):
        created = ledger.create(
            {
                "type": 3,
                "categoryId": context["categories"][0]["id"],
                "time": timestamp,
                "utcOffset": 480,
                "sourceAccountId": context["accounts"][0]["id"],
                "sourceAmount": amount,
                "comment": source_marker,
                "clientSessionId": str(uuid.uuid4()),
            }
        )
        expected.add(created["id"])
    actual = ledger.search(timestamp, timestamp, source_marker)
    assert len(actual) == 53
    assert {transaction["id"] for transaction in actual} == expected


def test_live_usd_to_cny_keeps_id_and_non_settlement_fields(live_ledger):
    ledger, context = live_ledger
    marker = "ebki-" + uuid.uuid4().hex[:16]
    payload = {
        "type": 3,
        "categoryId": context["categories"][0]["id"],
        "time": int(time.time()) - 60,
        "utcOffset": 480,
        "sourceAccountId": context["usd_account"]["id"],
        "sourceAmount": 1000,
        "comment": marker + " 合成月结",
        "clientSessionId": marker,
        "hideAmount": True,
        "tagIds": [],
        "pictureIds": [],
    }
    created = ledger.create(payload)
    current = ledger.get(created["id"])
    request = ledger.settlement_payload(current, 7200)
    request["sourceAccountId"] = context["accounts"][0]["id"]
    ledger.modify(request)
    actual = ledger.get(created["id"])
    assert actual["id"] == created["id"]
    assert actual["sourceAccountId"] == context["accounts"][0]["id"]
    assert actual["sourceAmount"] == 7200
    for field in ("type", "categoryId", "time", "utcOffset", "comment", "hideAmount", "tagIds"):
        assert actual[field] == current[field]
    assert len(ledger.search(payload["time"], payload["time"], marker)) == 1
