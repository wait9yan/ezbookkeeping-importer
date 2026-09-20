"""Synchronous ezBookkeeping HTTP boundary; never retry mutations here."""

from copy import deepcopy
from typing import Any

import httpx

from ezbookkeeping_importer.domain.errors import LedgerError, LedgerRejected

__all__ = ["EzBookkeepingClient", "LedgerError", "LedgerRejected"]

PAGE_SIZE = 50
SEQUENCES_PER_SECOND = 1000
TRANSACTION_NOT_FOUND = 205001
REPEATED_REQUEST = 200019


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise LedgerError("Invalid ezBookkeeping object response")
    return value


def _objects(value: Any) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise LedgerError("Invalid ezBookkeeping list response")
    return value


def _flatten(
    items: list[dict], children_key: str, path: str = "", hidden: bool = False
) -> list[dict]:
    result = []
    for item in items:
        node = deepcopy(item)
        children = _objects(node.pop(children_key, []))
        node["hidden"] = hidden or node.get("hidden", False)
        if children_key == "subCategories":
            name = node.get("name")
            if not isinstance(name, str):
                raise LedgerError("Invalid category name")
            node["path"] = f"{path} → {name}" if path else name
        result.append(node)
        result.extend(_flatten(children, children_key, node.get("path", ""), node["hidden"]))
    return result


class EzBookkeepingClient:
    def __init__(
        self, base_url: str, token: str, timeout: float = 30, *, client: httpx.Client | None = None
    ):
        self._base = base_url.rstrip("/") + "/api/v1/"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "X-Timezone-Name": "Asia/Shanghai",
            "X-Timezone-Offset": "480",
        }
        self._timeout = timeout
        self._client = client if client is not None else httpx.Client()

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(
                method,
                self._base + path,
                headers=self._headers,
                timeout=self._timeout,
                follow_redirects=False,
                **kwargs,
            )
        except httpx.HTTPError:
            # Do not persist URLs, tokens or remote bodies in operational errors.
            raise LedgerError("ezBookkeeping transport failure; result unknown") from None
        if response.status_code >= 500:
            raise LedgerError("ezBookkeeping service failure; result unknown")
        try:
            body = _object(response.json())
        except ValueError:
            raise LedgerError("Invalid ezBookkeeping JSON response") from None
        if body.get("success") is False:
            code = body.get("errorCode")
            if (
                type(code) is int
                and 200000 <= code < 300000
                and code not in {200001, 200002, 200003, REPEATED_REQUEST}
                and response.status_code < 500
            ):
                raise LedgerRejected(code)
            raise LedgerError("ezBookkeeping failure; result unknown")
        if not response.is_success or body.get("success") is not True or "result" not in body:
            raise LedgerError("Invalid ezBookkeeping response envelope")
        return body["result"]

    def accounts(self) -> list[dict]:
        return _flatten(_objects(self._request("GET", "accounts/list.json")), "subAccounts")

    def categories(self) -> list[dict]:
        groups = _object(self._request("GET", "transaction/categories/list.json"))
        return _flatten(
            [item for group in groups.values() for item in _objects(group)], "subCategories"
        )

    def rates(self) -> dict:
        result = _object(self._request("GET", "exchange_rates/latest.json"))
        if not {"dataSource", "updateTime", "baseCurrency", "exchangeRates"} <= result.keys():
            raise LedgerError("Incomplete exchange rate response")
        _objects(result["exchangeRates"])
        return result

    @staticmethod
    def _transaction(value: Any) -> dict:
        result = _object(value)
        if not isinstance(result.get("id"), str) or not result["id"].isdigit():
            raise LedgerError("Transaction response lacks a valid id")
        return result

    def get(self, transaction_id: str) -> dict | None:
        try:
            return self._transaction(
                self._request(
                    "GET",
                    "transactions/get.json",
                    params={
                        "id": transaction_id,
                        "with_pictures": "true",
                    },
                )
            )
        except LedgerRejected as exc:
            if exc.code == TRANSACTION_NOT_FOUND:
                return None
            raise

    def search(self, start: int, end: int, keyword: str | None = None) -> list[dict]:
        """Return all transactions in the inclusive Unix-second interval."""
        if type(start) is not int or type(end) is not int or start < 0 or end < start:
            raise ValueError("Invalid transaction search interval")
        lower = start * SEQUENCES_PER_SECOND
        cursor = end * SEQUENCES_PER_SECOND + SEQUENCES_PER_SECOND - 1
        result: list[dict] = []
        while True:
            params: dict[str, str | int] = {
                "min_time": lower,
                "max_time": cursor,
                "count": PAGE_SIZE,
                "page": 1,
                "with_pictures": "true",
            }
            if keyword is not None:
                params["keyword"] = keyword
            page = _object(self._request("GET", "transactions/list.json", params=params))
            result.extend(self._transaction(item) for item in _objects(page.get("items")))
            if "nextTimeSequenceId" not in page:
                raise LedgerError("Missing transaction pagination cursor")
            next_cursor = page["nextTimeSequenceId"]
            if next_cursor is None:
                return result
            if not isinstance(next_cursor, str) or not next_cursor.isdigit():
                raise LedgerError("Invalid transaction pagination cursor")
            following = int(next_cursor)
            if not lower <= following < cursor:
                raise LedgerError("Transaction pagination did not advance")
            cursor = following

    def create(self, payload: dict) -> dict:
        return self._transaction(self._request("POST", "transactions/add.json", json=payload))

    def modify(self, payload: dict) -> dict:
        return self._transaction(self._request("POST", "transactions/modify.json", json=payload))

    @staticmethod
    def settlement_payload(current: dict, amount: int) -> dict:
        required = {
            "id",
            "type",
            "categoryId",
            "time",
            "utcOffset",
            "sourceAccountId",
            "sourceAmount",
            "hideAmount",
            "tagIds",
            "comment",
        }
        if not required <= current.keys() or current["type"] != 3:
            raise LedgerError("Settlement requires a complete expense response")
        if type(amount) is not int or not -999999999999999 <= amount <= 999999999999999:
            raise ValueError("Invalid settlement amount")
        fields = required | {"destinationAccountId", "destinationAmount", "geoLocation"}
        payload = {key: deepcopy(value) for key, value in current.items() if key in fields}
        pictures = _objects(current.get("pictures", []))
        if any(not isinstance(picture.get("pictureId"), str) for picture in pictures):
            raise LedgerError("Invalid transaction picture response")
        payload["pictureIds"] = [picture["pictureId"] for picture in pictures]
        payload["sourceAmount"] = amount
        return payload
