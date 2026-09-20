"""Pure parsing of the independently researched CMB HTML templates."""

from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from hashlib import sha256
import json
import re
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from ezbookkeeping_importer.domain.models import MailKind, ParsedMail, ParseIssue, SourceRow
from ezbookkeeping_importer.domain.mail import BANK_SUBJECTS, normalize_subject

PARSER_VERSION = "cmb-1"
DAILY_ANCHOR = re.compile(r"^(\d{4}/\d{2}/\d{2})\s*您的消费明细如下[：:]$")
MONEY = re.compile(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?")
CONTROL_IDS = (
    "D1rmbLcurrBal",
    "D1rmbLbegBal",
    "D1rmbLpaymentAmt",
    "D1rmbLdebits",
    "D1rmbLcreditAmt",
    "D1rmbLinterest",
)


def _digest(value):
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def _money(value):
    value = re.sub(r"\s+", "", value).removeprefix("¥").removeprefix("￥").removesuffix("元")
    if not MONEY.fullmatch(value):
        raise ValueError("金额格式不合法或超过两位小数")
    return Decimal(value.replace(",", ""))


def _issue(code, locator, detail):
    return ParseIssue(code=code, locator=locator, detail=detail)


def _date(value):
    return datetime.strptime(value, "%Y/%m/%d").date()


def _month_day(value, start, end):
    if not re.fullmatch(r"\d{4}", value):
        raise ValueError("月账单日期格式错误")
    candidates = []
    for year in range(start.year, end.year + 1):
        try:
            candidate = date(year, int(value[:2]), int(value[2:]))
        except ValueError:
            continue
        if start <= candidate <= end:
            candidates.append(candidate)
    if len(candidates) != 1:
        raise ValueError("月账单日期不能唯一落入账期")
    return candidates[0]


def _transaction_day(value, posted):
    if not re.fullmatch(r"\d{4}", value):
        raise ValueError("月账单交易日期格式错误")
    for year in (posted.year, posted.year - 1):
        try:
            candidate = date(year, int(value[:2]), int(value[2:]))
        except ValueError:
            continue
        if candidate <= posted:
            return candidate
    raise ValueError("月账单交易日期无法还原")


class BankParser:
    def __init__(self, timezone="Asia/Shanghai", context="default"):
        self.timezone = ZoneInfo(timezone)
        self.context = context

    def parse(self, raw: bytes) -> ParsedMail:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        subject = str(message.get("Subject", ""))
        normalized_subject, forwarded = normalize_subject(subject)
        kind: MailKind = BANK_SUBJECTS.get(normalized_subject, "other")
        # Template detection is deliberately separate from source authentication.
        metadata = {
            "parser_version": PARSER_VERSION,
            "subject": subject,
            "normalized_subject": normalized_subject,
            "forwarded": forwarded,
            "message_id": str(message.get("Message-ID", "")),
            "from": str(message.get("From", "")),
            "authentication_results": [
                str(v) for v in message.get_all("Authentication-Results", [])
            ],
        }
        parts = [
            p
            for p in message.walk()
            if p.get_content_type() == "text/html" and p.get_content_disposition() != "attachment"
        ]
        issues = []
        rows = []
        report_date = None
        if kind == "other":
            if any(term in subject for term in ("账单", "信用管家", "还款", "消费提醒")):
                kind = "unknown"
                issues.append(_issue("unknown_template", "subject", "疑似账务邮件主题未支持"))
        elif len(parts) != 1:
            issues.append(_issue("mime_body", "mime", "已知模板需要唯一 HTML 正文"))
        else:
            try:
                html = parts[0].get_content(errors="strict")
                defects = [d for p in message.walk() for d in p.defects]
                if defects:
                    raise ValueError("MIME 结构或传输编码损坏")
                soup = BeautifulSoup(html, "html.parser")
                for node in soup(["script", "style"]):
                    node.decompose()
                if kind == "daily":
                    report_date, rows, issues, extra = self._daily(soup)
                elif kind == "repayment":
                    report_date, rows, issues, extra = self._repayment(soup)
                else:
                    report_date, rows, issues, extra = self._monthly(soup)
                metadata.update(extra)
            except (ValueError, UnicodeError, LookupError) as exc:
                issues.append(_issue("invalid_template", "body", str(exc)))
        report_key = (
            _digest(["cmb", self.context, kind, report_date, metadata.get("cycle_start")])
            if report_date
            else _digest(["cmb", raw.hex()])
        )
        counts: Counter[str] = Counter()
        identified = []
        for row in rows:
            fields = {key: val for key, val in row.items() if key not in ("evidence", "extra")}
            fingerprint = _digest(fields)
            counts[fingerprint] += 1
            identified.append(
                SourceRow(row_key=_digest([report_key, fingerprint, counts[fingerprint]]), **row)
            )
        return ParsedMail(
            kind=kind,
            report_key=report_key,
            report_date=report_date,
            rows=tuple(identified),
            issues=tuple(issues),
            metadata=metadata,
        )

    def _daily(self, soup):
        tokens = [" ".join(s.split()) for s in soup.stripped_strings]
        anchors = [
            (i, DAILY_ANCHOR.fullmatch(s))
            for i, s in enumerate(tokens)
            if DAILY_ANCHOR.fullmatch(s)
        ]
        if len(anchors) != 1:
            raise ValueError("日报需要唯一完整日期锚点")
        index, match = anchors[0]
        assert match is not None
        report_date = _date(match.group(1))
        tail = tokens[index + 1 :]
        rows, issues = [], []
        if not tail:
            issues.append(_issue("empty_daily", "body", "未观察到消费明细，不能推断零消费"))
        for offset in range(0, len(tail), 3):
            fields = tail[offset : offset + 3]
            locator = f"daily:{offset // 3 + 1}"
            try:
                if len(fields) != 3:
                    raise ValueError("日报行字段数量错误")
                clock, amount_text, detail_text = fields
                amount_match = re.fullmatch(r"([A-Z]{3})\s+(.+)", amount_text)
                detail = re.fullmatch(r"尾号(\d{4})\s+(\S+)\s+(.+)", detail_text)
                if not amount_match or not detail or not re.fullmatch(r"\d{2}:\d{2}:\d{2}", clock):
                    raise ValueError("日报行字段结构错误")
                event = {"消费": "expense", "邮购": "expense", "退货": "refund"}.get(
                    detail.group(2)
                )
                if event is None:
                    raise ValueError("不支持的日报交易类型")
                amount = _money(amount_match.group(2))
                if (event == "expense" and amount <= 0) or (event == "refund" and amount >= 0):
                    raise ValueError("日报类型与金额符号不一致")
                occurred_at = datetime.combine(
                    report_date, datetime.strptime(clock, "%H:%M:%S").time(), self.timezone
                )
                rows.append(
                    dict(
                        event_type=event,
                        occurred_date=report_date,
                        occurred_at=occurred_at,
                        time_precision="second",
                        card_reference=detail.group(1),
                        merchant_raw=detail.group(3),
                        original_amount=amount,
                        original_currency=amount_match.group(1),
                        evidence={"locator": locator, "fields": fields},
                        extra={"bank_event": detail.group(2)},
                    )
                )
            except ValueError as exc:
                issues.append(_issue("invalid_daily_row", locator, str(exc)))
        return report_date, rows, issues, {}

    def _repayment(self, soup):
        text = "".join(soup.stripped_strings)
        if "我行已从您关联的自动还款账户中扣款" not in text:
            return None, [], [_issue("repayment_state", "body", "未识别到已实际扣款的状态")], {}
        tokens = [" ".join(s.split()) for s in soup.stripped_strings]
        header = ["扣款日期", "扣款币种", "应扣金额", "实扣金额"]
        matches = [i for i in range(len(tokens)) if tokens[i : i + 4] == header]
        if len(matches) != 1:
            raise ValueError("还款四列表头缺失或不唯一")
        tail = tokens[matches[0] + 4 :]
        if sum(bool(re.fullmatch(r"\d{4}年\d{2}月\d{2}日", token)) for token in tail) != 1:
            raise ValueError("还款明细日期缺失或出现多个明细，需核实模板")
        fields = tail[:4]
        if len(fields) != 4:
            raise ValueError("还款明细字段缺失")
        day = datetime.strptime(fields[0], "%Y年%m月%d日").date()
        currency = {"人民币": "CNY"}.get(fields[1])
        if currency is None:
            raise ValueError("尚未支持的还款币种")
        due, actual = _money(fields[2]), _money(fields[3])
        if due < 0 or actual < 0:
            raise ValueError("还款金额不可为负")
        metadata = {
            "due_amount": str(due),
            "actual_amount": str(actual),
            "repayment_status": "zero" if actual == 0 else "partial" if actual < due else "paid",
        }
        if actual == 0:
            return day, [], [], metadata
        row = dict(
            event_type="repayment",
            occurred_date=day,
            occurred_at=None,
            time_precision="date",
            card_reference=None,
            merchant_raw="自动还款",
            original_amount=actual,
            original_currency=currency,
            evidence={"locator": "repayment:1", "fields": fields},
            extra=metadata,
        )
        return day, [row], [], metadata

    def _monthly(self, soup):
        cycle = soup.find(id="statementCycle")
        if cycle is None:
            raise ValueError("缺少账期锚点")
        period = re.fullmatch(
            r"(\d{4}/\d{2}/\d{2})\s*-\s*(\d{4}/\d{2}/\d{2})", cycle.get_text(strip=True)
        )
        if period is None:
            raise ValueError("账期格式错误")
        start, end = map(_date, period.groups())
        if not 0 < (end - start).days <= 62:
            raise ValueError("账期范围错误")
        group = None
        rows, issues = [], []
        for index, tr in enumerate(soup.find_all("tr")):
            cells = tr.find_all(["td", "th"], recursive=False)
            fields = [" ".join(cell.stripped_strings) for cell in cells]
            labels = [field for field in fields if field in ("还款", "退款", "消费")]
            if labels:
                group = labels[0]
            # Only leaf rows are business rows; outer layout tables aggregate them.
            if any(cell.find("tr") for cell in cells):
                continue
            candidate = len(fields) >= 5 and (
                re.fullmatch(r"\d{4}", fields[2])
                or (len(fields) == 8 and fields[4].startswith(("¥", "￥")))
            )
            if not candidate or any(cell.find(id=re.compile(r"^D1rmbL")) for cell in cells):
                continue
            locator = f"monthly:tr:{index}"
            try:
                if len(fields) != 8 or group is None:
                    raise ValueError("月账单明细列数或分组错误")
                posted = _month_day(fields[2], start, end)
                # The transaction can precede this cycle; only posting is bounded by it.
                occurred = _transaction_day(fields[1], posted) if fields[1] else posted
                amount, original = _money(fields[4]), _money(fields[7])
                if (group == "消费" and (amount <= 0 or original <= 0)) or (
                    group != "消费" and (amount >= 0 or original >= 0)
                ):
                    raise ValueError("月账单分组与金额符号不一致")
                if not re.fullmatch(r"\d{4}", fields[5]):
                    raise ValueError("月账单卡片字段错误")
                if group != "还款" and not fields[1]:
                    raise ValueError("消费或退款缺少交易日")
                rows.append(
                    dict(
                        event_type={"消费": "expense", "退款": "refund", "还款": "statement"}[
                            group
                        ],
                        occurred_date=occurred,
                        occurred_at=None,
                        time_precision="date",
                        posted_date=posted,
                        card_reference=fields[5],
                        merchant_raw=fields[3],
                        original_amount=original,
                        original_currency=None,
                        settlement_amount=amount,
                        settlement_currency="CNY",
                        evidence={"locator": locator, "fields": fields},
                        extra={
                            "statement_group": group,
                            "country": fields[6],
                            "occurred_date_missing": not bool(fields[1]),
                        },
                    )
                )
            except ValueError as exc:
                issues.append(_issue("invalid_monthly_row", locator, str(exc)))
        controls = {}
        for name in CONTROL_IDS:
            node = soup.find(id=name)
            if node is None:
                issues.append(_issue("missing_control", name, "缺少月账单控制汇总"))
            else:
                try:
                    controls[name] = _money(node.get_text(strip=True))
                except ValueError as exc:
                    issues.append(_issue("invalid_control", name, str(exc)))
        if len(controls) == len(CONTROL_IDS):
            current, previous, paid, spent, refunded, interest = [controls[k] for k in CONTROL_IDS]
            totals = {
                group: sum(
                    (
                        r["settlement_amount"]
                        for r in rows
                        if r["extra"]["statement_group"] == group
                    ),
                    Decimal(0),
                )
                for group in ("消费", "退款", "还款")
            }
            if (
                totals["消费"] != spent
                or -totals["退款"] != refunded
                or -totals["还款"] != paid
                or current != previous - paid + spent - refunded + interest
            ):
                issues.append(
                    _issue("control_mismatch", "monthly:controls", "明细或余额与控制汇总不符")
                )
        return (
            end,
            rows,
            issues,
            {
                "cycle_start": start.isoformat(),
                "cycle_end": end.isoformat(),
                "controls": {k: str(v) for k, v in controls.items()},
            },
        )
