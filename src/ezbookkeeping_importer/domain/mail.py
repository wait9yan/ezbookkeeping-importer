"""Bank mail identification is distinct from source authentication."""

import re
from .models import MailKind

BANK_SUBJECTS: dict[str, MailKind] = {
    "每日信用管家": "daily",
    "自动还款扣款通知": "repayment",
    "招商银行信用卡电子账单": "monthly",
}
FORWARD_PREFIX = re.compile(r"^(?:fw|fwd|转发)\s*[:：]\s*", re.IGNORECASE)


def normalize_subject(subject: str) -> tuple[str, bool]:
    normalized = subject.strip()
    forwarded = False
    while match := FORWARD_PREFIX.match(normalized):
        forwarded = True
        normalized = normalized[match.end() :].strip()
    return normalized, forwarded


def bank_candidate(sender: str, subject: str) -> bool:
    normalized, _ = normalize_subject(subject)
    return sender.lower().endswith("@message.cmbchina.com") or normalized in BANK_SUBJECTS
