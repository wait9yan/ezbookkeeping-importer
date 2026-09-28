import json

import httpx
import pytest

from ezbookkeeping_importer.adapters.ezbookkeeping.client import (
    EzBookkeepingClient,
    LedgerError,
    LedgerRejected,
)


def client(handler):
    return EzBookkeepingClient(
        "https://ledger.test/prefix",
        "test-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def ok(result):
    return httpx.Response(200, json={"success": True, "result": result})


def test_search_sequence_cursor_preserves_interval_and_all_pages():
    requests = []

    def handle(request):
        requests.append(request)
        assert request.url.path == "/prefix/api/v1/transactions/list.json"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["X-Timezone-Name"] == "Asia/Shanghai"
        assert request.headers["X-Timezone-Offset"] == "480"
        assert request.url.params["min_time"] == "100000"
        assert request.url.params["keyword"] == "ebki-marker"
        assert request.url.params["with_pictures"] == "true"
        if len(requests) == 1:
            assert request.url.params["max_time"] == "200999"
            return ok(
                {"items": [{"id": str(i)} for i in range(1, 51)], "nextTimeSequenceId": "150032"}
            )
        assert request.url.params["max_time"] == "150032"
        return ok({"items": [{"id": "51"}, {"id": "52"}], "nextTimeSequenceId": None})

    assert len(client(handle).search(100, 200, "ebki-marker")) == 52


def test_repeated_cursor_is_failure_not_partial_success():
    ledger = client(lambda _: ok({"items": [], "nextTimeSequenceId": "200999"}))
    with pytest.raises(LedgerError, match="advance"):
        ledger.search(100, 200)


def test_only_explicit_transaction_not_found_returns_none():
    def absent(request):
        assert request.url.params["with_pictures"] == "true"
        return httpx.Response(400, json={"success": False, "errorCode": 205001})

    assert client(absent).get("1") is None
    with pytest.raises(LedgerError):
        client(lambda _: httpx.Response(404, text="proxy not found")).get("1")
    with pytest.raises(LedgerRejected):
        client(lambda _: httpx.Response(401, json={"success": False, "errorCode": 202001})).get("1")


@pytest.mark.parametrize(
    "status,body,rejected",
    [
        (400, {"success": False, "errorCode": 205017}, True),
        (500, {"success": False, "errorCode": 205017}, False),
        (400, {"success": False, "errorCode": 200019}, False),
        (200, {"success": True, "result": {}}, False),
        (200, {"success": False, "errorCode": 100001}, False),
    ],
)
def test_create_failure_classification(status, body, rejected):
    with pytest.raises(LedgerError) as caught:
        client(lambda _: httpx.Response(status, json=body)).create({"sourceAmount": 10})
    assert isinstance(caught.value, LedgerRejected) is rejected


def test_timeout_is_unknown_and_does_not_retry_or_expose_secret():
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("contains secret", request=request)

    with pytest.raises(LedgerError) as caught:
        client(timeout).create({})
    assert not isinstance(caught.value, LedgerRejected)
    assert "secret" not in str(caught.value)
    assert len(calls) == 1


def test_settlement_roundtrip_preserves_all_non_amount_fields():
    current = {
        "id": "11",
        "type": 3,
        "categoryId": "2",
        "time": 123,
        "utcOffset": 480,
        "sourceAccountId": "3",
        "sourceAmount": 1234,
        "hideAmount": True,
        "tagIds": ["4"],
        "comment": "edited comment",
        "pictures": [{"pictureId": "5", "originalUrl": "https://image.test"}],
        "geoLocation": {"latitude": 1.25, "longitude": 2.5},
        "editable": True,
    }
    payload = EzBookkeepingClient.settlement_payload(current, 1250)
    assert current["sourceAmount"] == 1234
    assert payload == {
        **{
            key: value
            for key, value in current.items()
            if key not in {"pictures", "editable", "sourceAmount"}
        },
        "pictureIds": ["5"],
        "sourceAmount": 1250,
    }

    def handle(request):
        assert request.url.path.endswith("transactions/modify.json")
        assert json.loads(request.content) == payload
        return ok({**current, "sourceAmount": 1250})

    assert client(handle).modify(payload)["sourceAmount"] == 1250


def test_tree_actual_wire_shapes():
    def handle(request):
        if request.url.path.endswith("categories/list.json"):
            return ok(
                {
                    "2": [
                        {
                            "id": "1",
                            "name": "其他杂项",
                            "parentId": "0",
                            "type": 2,
                            "hidden": True,
                            "subCategories": [
                                {
                                    "id": "2",
                                    "name": "待分类",
                                    "parentId": "1",
                                    "type": 2,
                                    "hidden": False,
                                }
                            ],
                        }
                    ]
                }
            )
        if request.url.path.endswith("accounts/list.json"):
            return ok([{"id": "1", "subAccounts": [{"id": "2"}]}])
        raise AssertionError("unexpected endpoint")

    ledger = client(handle)
    assert ledger.categories()[1]["path"] == "其他杂项 → 待分类"
    assert ledger.categories()[1]["hidden"] is True
    assert [account["id"] for account in ledger.accounts()] == ["1", "2"]


def test_create_preserves_payload_and_returns_complete_remote_record():
    payload = {
        "type": 3,
        "sourceAmount": -123,
        "categoryId": "2",
        "sourceAccountId": "3",
        "time": 123,
        "utcOffset": 480,
        "clientSessionId": "ebki-test",
    }
    remote = {**payload, "id": "99", "tagIds": [], "hideAmount": False, "comment": ""}

    def handle(request):
        assert request.url.path.endswith("transactions/add.json")
        assert request.method == "POST"
        assert json.loads(request.content) == payload
        return ok(remote)

    assert client(handle).create(payload) == remote
