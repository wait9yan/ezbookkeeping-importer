"""Synthetic fixtures only; no personal sample content is copied here."""

from datetime import date
from decimal import Decimal
from email.message import EmailMessage

from ezbookkeeping_importer.adapters.banks.cmb.parser import BankParser, CONTROL_IDS


def mail(subject, body, alternative=False, charset="utf-8"):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "synthetic@example.test"
    if alternative:
        msg.set_content("Synthetic plain-text alternative")
        msg.add_alternative(body, subtype="html", charset=charset)
    else:
        msg.set_content(body, subtype="html", charset=charset)
    return msg.as_bytes()


def daily(rows, day="2026/01/01"):
    return mail(
        "每日信用管家",
        "<p>"
        + day
        + " 您的消费明细如下：</p>"
        + "".join(
            f"<div><span>{clock}</span><b>{amount}</b><p>{detail}</p></div>"
            for clock, amount, detail in rows
        ),
        True,
    )


def test_daily_types_identity_multiplicity_and_alternative():
    row = ("12:34:56", "CNY 10.25", "尾号1234 消费 合成商户")
    refund = ("13:00:00", "CNY -2.00", "尾号1234 退货 合成商户")
    result = BankParser().parse(daily([row, row, refund]))
    reordered = BankParser().parse(daily([refund, row, row]))
    assert not result.issues
    assert len(result.rows) == len({r.row_key for r in result.rows}) == 3
    assert {r.row_key for r in result.rows} == {r.row_key for r in reordered.rows}
    assert result.rows[0].occurred_at is not None
    assert result.rows[0].occurred_at.isoformat() == "2026-01-01T12:34:56+08:00"
    assert result.rows[-1].original_amount == Decimal("-2.00")
    assert result.rows[-1].event_type == "refund"


def test_daily_invalid_row_preserved_and_sign_not_guessed():
    result = BankParser().parse(
        daily(
            [
                ("12:00:00", "USD 1.99", "尾号1234 邮购 合成商户"),
                ("12:00:01", "CNY -2.00", "尾号1234 消费 合成商户"),
                ("12:00:02", "CNY 3.00", "尾号1234 分期 合成商户"),
                ("12:00:03", "CNY 1.234", "尾号1234 消费 合成商户"),
            ]
        )
    )
    assert len(result.rows) == 1
    assert len(result.issues) == 3
    assert result.rows[0].original_currency == "USD"


def repayment(actual="8.00", state="我行已从您关联的自动还款账户中扣款"):
    return mail(
        "自动还款扣款通知",
        f"<p>{state}</p><table><tr>"
        + "".join(f"<td>{v}</td>" for v in ["扣款日期", "扣款币种", "应扣金额", "实扣金额"])
        + "</tr><tr>"
        + "".join(f"<td>{v}</td>" for v in ["2026年01月05日", "人民币", "10.00 元", actual + " 元"])
        + "</tr></table>",
    )


def test_repayment_actual_partial_zero_and_unknown_state():
    result = BankParser().parse(repayment())
    assert not result.issues
    assert result.rows[0].original_amount == Decimal("8.00")
    assert result.rows[0].occurred_at is None
    assert result.rows[0].card_reference is None
    assert result.metadata["repayment_status"] == "partial"
    zero = BankParser().parse(repayment("0.00"))
    assert not zero.rows and not zero.issues
    assert zero.metadata["repayment_status"] == "zero"
    assert BankParser().parse(repayment("-1.00")).issues
    assert BankParser().parse(repayment(state="将扣款")).issues


def monthly(cells=None, controls=None):
    cells = cells or ["", "1231", "0101", "合成商户", "¥ 72.00", "1234", "US", "10.00"]
    controls = controls or ["72", "0", "0", "72", "0", "0"]
    return mail(
        "招商银行信用卡电子账单",
        '<p id="statementCycle">2025/12/18-2026/01/17</p><table><tr><td>消费</td></tr><tr>'
        + "".join(f"<td>{v}</td>" for v in cells)
        + "</tr></table>"
        + "".join(
            f'<p id="{key}">¥ {val}</p>' for key, val in zip(CONTROL_IDS, controls, strict=True)
        ),
    )


def test_monthly_cross_year_no_currency_inference_controls():
    result = BankParser().parse(monthly())
    assert not result.issues
    row = result.rows[0]
    assert row.occurred_date == date(2025, 12, 31)
    assert row.posted_date == date(2026, 1, 1)
    assert row.original_currency is None
    assert row.original_amount == Decimal("10")
    assert row.settlement_amount == Decimal("72")
    assert BankParser().parse(monthly(controls=["73", "0", "0", "73", "0", "0"])).issues


def test_malformed_unknown_and_nontransaction_mail():
    assert BankParser().parse(mail("每日信用管家", "<p>损坏模板</p>")).issues
    assert BankParser().parse(mail("新信用卡账单", "<p>未知</p>")).kind == "unknown"
    assert BankParser().parse(mail("满意度调研", "<p>调查</p>")).kind == "other"
    result = BankParser().parse(
        mail("每日信用管家", "<p>2026/01/01 您的消费明细如下：</p>", charset="gb18030")
    )
    assert result.issues[0].code == "empty_daily"


def test_monthly_transaction_before_cycle_is_preserved():
    result = BankParser().parse(
        monthly(cells=["", "1217", "1218", "合成商户", "¥ 72.00", "1234", "US", "10.00"])
    )
    assert not result.issues
    assert result.rows[0].occurred_date == date(2025, 12, 17)
    assert result.rows[0].posted_date == date(2025, 12, 18)


def test_same_day_posting_uses_current_year():
    result = BankParser().parse(
        monthly(cells=["", "0101", "0101", "合成商户", "¥ 72.00", "1234", "US", "10.00"])
    )
    assert not result.issues
    assert result.rows[0].occurred_date == date(2026, 1, 1)


def test_forward_prefix_is_normalized_but_original_subject_preserved():
    from email.message import EmailMessage
    from ezbookkeeping_importer.application.collect import source_status
    from types import SimpleNamespace

    message = EmailMessage()
    message["Subject"] = "Fwd: 转发：每日信用管家"
    message["From"] = "ccsvc@message.cmbchina.com"
    message["Received"] = "from bank by mx.qq.com; Thu, 1 Jan 2026 12:00:00 +0800"
    message["Authentication-Results"] = (
        "mx.qq.com; spf=pass; dkim=pass; dmarc=pass header.from=message.cmbchina.com"
    )
    message.set_content(
        "<p>2026/01/01 您的消费明细如下：</p><b>12:00:00</b><b>CNY 10.00</b><b>尾号1234 消费 合成商户</b>",
        subtype="html",
    )
    raw = message.as_bytes()
    parsed = BankParser().parse(raw)
    assert parsed.kind == "daily" and len(parsed.rows) == 1 and not parsed.issues
    assert parsed.metadata["subject"] == "Fwd: 转发：每日信用管家"
    assert parsed.metadata["forwarded"] is True
    settings = SimpleNamespace(
        source_policy="qq_authentication_results", trusted_authserv_id="mx.qq.com"
    )
    assert source_status(raw, settings, "imap")[0] == "requires_acceptance"


def test_explicit_full_card_field_is_preserved_for_exact_account_matching():
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "每日信用管家"
    message.set_content(
        "<p>2026/01/01 您的消费明细如下：</p><b>12:00:00</b><b>USD 10.00</b><b>卡号4444333322221234 消费 合成商户</b>",
        subtype="html",
    )
    parsed = BankParser().parse(message.as_bytes())
    assert not parsed.issues and parsed.rows[0].card_reference == "4444333322221234"
