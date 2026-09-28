"""Account identity comes from ledger comments; only safe matching evidence is retained."""

import re

from .errors import ImporterError


class AccountMatchError(ImporterError):
    def __init__(self, code: str, candidate_ids: list[str]):
        self.code = code
        self.candidate_ids = candidate_ids
        reason = (
            "no account matches the card and original currency"
            if code == "account_not_found"
            else "multiple accounts match the card and original currency"
        )
        super().__init__(reason)


def card_numbers(comment: str) -> set[str]:
    numbers = set()
    # A maximal numeric run is either a complete number or short space/hyphen groups.
    # Two already-complete numbers are considered separately, never concatenated.
    for match in re.finditer(r"(?<![0-9])[0-9]+(?:[ \t-]+[0-9]+)*(?![0-9])", comment):
        groups = re.split(r"[ \t-]+", match.group())
        full = [group for group in groups if 12 <= len(group) <= 19]
        if full:
            numbers.update(full)
        elif all(1 <= len(group) <= 6 for group in groups):
            joined = "".join(groups)
            if 12 <= len(joined) <= 19:
                numbers.add(joined)
    return numbers


def recordable(account: dict) -> bool:
    return not account.get("hidden", False) and account.get("type") == 1


def match_account(accounts: list[dict], reference: str | None, currency: str) -> tuple[str, dict]:
    if not reference or not re.fullmatch(r"(?:[0-9]{4}|[0-9]{12,19})", reference):
        raise ImporterError("card reference must be four trailing digits or a complete card number")
    matched = {}
    for account in accounts:
        if not recordable(account):
            continue
        numbers = card_numbers(account.get("comment") or "")
        same_card = any(
            number.endswith(reference) if len(reference) == 4 else number == reference
            for number in numbers
        )
        if same_card and account.get("currency") == currency:
            matched[str(account["id"])] = account
    if len(matched) != 1:
        raise AccountMatchError(
            "account_not_found" if not matched else "account_ambiguous", sorted(matched)
        )
    account_id = next(iter(matched))
    return account_id, {
        "account_id": account_id,
        "card_last4": reference[-4:],
        "match_method": "suffix4" if len(reference) == 4 else "full_number",
        "currency": currency,
    }


def decision_currency(transaction: dict) -> str:
    decision = transaction.get("decision") or {}
    currency = decision.get("target_currency")
    if currency is not None:
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ImporterError("stored target currency is invalid")
        return currency
    # Before original-currency imports existed, every persisted request targeted CNY.
    # This compatibility branch is only for an existing decision, never a new mapping.
    if decision.get("payload"):
        return "CNY"
    raise ImporterError("a persisted target currency decision is required")


def legacy_cny_estimate(transaction: dict) -> bool:
    decision = transaction.get("decision") or {}
    return (
        bool(decision.get("rate_snapshot"))
        and decision_currency(transaction) == "CNY"
        and transaction["facts"]["event_type"] == "expense"
        and transaction["facts"]["original_currency"] != "CNY"
    )
