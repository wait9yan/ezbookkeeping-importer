"""真实 Rich 渲染验证，不读取配置或连接任何数据库。"""

from argparse import Namespace
from datetime import date, datetime, timezone
from decimal import Decimal
from io import StringIO

import pytest
from rich.console import Console

from ezbookkeeping_importer.application.maintenance import issue_groups
from ezbookkeeping_importer.entrypoints import presentation


def render(command, result, *, width=100, args=None):
    stream = StringIO()
    display = Console(file=stream, width=width, color_system=None)
    presentation.render_result(display, command, result, args)
    return stream.getvalue()


def issue(code="duplicate_candidates", *, entity="bank_transactions", identity="tx-1",
          state="issue", detail=None, context=None):
    return {
        "entity_type": entity, "entity_id": identity, "code": code,
        "status": state, "version": 2, "detail": detail, "context": context or {},
    }


def status(items=()):
    return {
        "email_sync_checkpoint": [], "email_source_item": [], "email": [],
        "bank_transactions": [], "background_task": [], "issues": len(items),
        "issue_object_count": len({(item["entity_type"], item["entity_id"]) for item in items}),
        "issue_groups": issue_groups(items),
    }


def test_empty_and_missing_results_do_not_invent_success_or_zero_amount():
    value = render("status", status())
    assert "暂无记录" in value and "尚未建立记录" in value and "当前没有问题诊断" in value
    assert "已入账" not in value
    assert "未提供" in render("status", {})
    assert "当前没有问题诊断" in render("issues", [])
    assert "没有符合筛选条件" in render("issues", [], args=Namespace(entity_type="email", entity_id=None))


def test_status_covers_pipeline_history_and_problem_counts():
    data = status([issue(), issue("classification_failed")])
    data.update(
        email_source_item=[{"status": "collected", "count": 15}, {"status": "skipped", "count": 5004}],
        email=[{"parse_status": "parsed", "count": 13}],
        bank_transactions=[{"import_status": "issue", "count": 57}],
        background_task=[{"task_type": "create", "status": "unknown", "count": 1}],
        email_sync_checkpoint=[{"source_id": "mail", "folder": "收件箱", "registered_uid": 5005,
                                "initial_scan_upper_uid": 5000, "historical_complete": True,
                                "last_scanned_at": datetime(2026, 9, 29, 7, 19, tzinfo=timezone.utc)}],
    )
    value = render("status", data)
    for expected in ("已采集 15", "已跳过 5004", "已解析 13", "已暂停 57", "结果待核实",
                     "2026-09-29 07:19:00+00:00", "2 条诊断", "1 个业务对象", "重复交易候选", "recheck"):
        assert expected in value
    assert "不代表全部交易已入账" in value
    assert "5005" not in value and "5000" not in value and "source_id" not in value
    assert '"issues"' not in value


def test_many_duplicate_issues_are_grouped_without_dumping_objects():
    items = [issue(identity=f"full-transaction-{index}", detail={"candidate_ids": [f"remote-{index}"]})
             for index in range(57)]
    value = render("issues", items)
    assert "57 条诊断" in value and "57 个业务对象" in value and "57 / 57" in value
    assert value.count("重复交易候选") == 1
    assert "full-transaction-" not in value and "remote-" not in value
    assert "issues --entity-type bank_transactions" in value
    assert "当前可筛选对象类型" in value and "其他对象类型" not in value
    assert len(value.splitlines()) < 25


def test_mixed_issues_count_objects_separately_and_explain_recovery():
    items = [
        issue(), issue("duplicate_check_failed", identity="tx-2"),
        issue("unknown_template", entity="email", identity="mail", state="failed"),
        issue("invalid_template", entity="email", identity="mail", state="failed"),
        issue("source_acceptance", entity="email_source_item", identity="3", state="collected"),
        issue("write_unknown", entity="background_task", identity="4", state="unknown"),
        issue("reconciliation", entity="bank_statement_reconciliation", identity="5", state=None,
              detail={"match_status": "missing_source_transaction", "ledger_check_status": "not_checked"}),
    ]
    value = render("issues", items, width=140)
    for expected in ("7 条诊断", "6 个业务对象", "缺少日报来源交易", "资料缺口", "来源待接纳",
                     "不能重发", "未知邮件模板", "复查失败", "修复解析"):
        assert expected in value
    assert "自动复查" not in value


def test_pending_diagnostics_are_not_presented_as_awaiting_another_recheck():
    value = render("status", status([issue(state="pending")]), width=120)
    assert "正在处理，查看后续日志" in value
    assert "处理远端候选后" not in value


def test_duplicate_query_failure_requests_connection_repair_not_candidate_deletion():
    value = render("issues", [issue("duplicate_check_failed")], width=120)
    assert "检查账本连接，修正查询失败后执行 recheck" in value
    assert "处理远端候选" not in value


@pytest.mark.parametrize("state", ["unknown", "booked", "ignored"])
def test_ineligible_states_do_not_suggest_rechecking_a_stale_duplicate_diagnostic(state):
    value = render("status", status([issue(state=state)]), width=120)
    assert "recheck" not in value
    assert "不能重发" in value if state == "unknown" else "不安排重新创建" in value


@pytest.mark.parametrize("width", [32, 80, 160])
def test_filtered_details_keep_full_identifiers_and_render_markup_as_data(width):
    identity = "long-id-" + "a1b2" * 20
    merchant = "[bold red]中文商户[/]" + "长文本" * 30
    items = [issue(identity=identity, detail={"candidate_ids": ["remote-123456789"],
                   "token": "SECRET", "request": {"body": "SECRET"}},
                   context={"transaction_date": date(2026, 9, 1), "merchant": merchant,
                            "original_amount": Decimal("0.00"), "original_currency": "CNY"})]
    value = render("issues", items, width=width, args=Namespace(entity_type="bank_transactions", entity_id=None))
    compact = "".join(value.split())
    for expected in (identity, merchant, "remote-123456789", "0.00CNY", "候选数"):
        assert "".join(expected.split()) in compact
    assert "SECRET" not in value and "candidate_ids" not in value and "{" not in value


def test_reconciliation_details_distinguish_missing_amounts_and_nested_candidates():
    items = [issue("reconciliation", entity="bank_statement_reconciliation", state=None, detail={
        "statement_report_key": "statement:2026-09", "statement_row_key": "row-17",
        "bank_transaction_id": "tx-1", "match_status": "ambiguous", "ledger_check_status": "not_checked",
        "expected_amount": Decimal("12.34"), "expected_currency": "USD", "actual_amount": None,
        "actual_currency": None, "details": {
            "candidate_ids": ["candidate-A", "candidate-B"], "raw_body": "SECRET",
            "differences": {"sourceAccountId": {"expected": "account-A", "actual": "account-B"}},
        },
    })]
    value = render("issues", items, args=Namespace(entity_type=None, entity_id="tx-1"))
    for expected in ("尚未核验", "12.34 USD", "未提供", "statement:2026-09", "candidate-B",
                     "账本账户差异", "预期 account-A / 实际 account-B"):
        assert expected in value
    assert "0.00" not in value and "SECRET" not in value


def test_terminal_control_characters_are_not_emitted():
    items = [issue(context={"merchant": "商户\x1b[31m危险\r\x07"})]
    value = render("issues", items, args=Namespace(entity_type=None, entity_id="tx-1"))
    assert "\x1b" not in value and "\r" not in value and "\x07" not in value
    assert "商户�[31m危险��" in value


def test_repayment_reconciliation_details_explain_destination_differences():
    items = [issue("reconciliation", entity="bank_statement_reconciliation", state=None, detail={
        "match_status": "matched", "ledger_check_status": "mismatched", "details": {
            "differences": {
                "destinationAccountId": {"expected": "card-account", "actual": "other-account"},
                "destinationAmount": {"expected": 1234, "actual": 4321},
                "destination_currency": "missing, hidden or incompatible destination account",
            },
        },
    })]
    value = render("issues", items, args=Namespace(entity_type=None, entity_id="tx-1"))
    for expected in ("转入账户差异", "预期 card-account / 实际 other-account",
                     "转入金额（分）差异", "预期 1234 / 实际 4321",
                     "转入账户缺失、已隐藏或币种不符"):
        assert "".join(expected.split()) in "".join(value.split())


@pytest.mark.parametrize("queued,expected", [(True, "已排队"), (False, "已合并")])
def test_sync_reports_scheduling_and_optional_range(queued, expected):
    value = render("sync", {"queued": queued, "since": date(2026, 9, 1), "until": None})
    assert expected in value and "2026-09-01" in value and "不设上界" in value
    assert "尚不代表采集或入账完成" in value
    assert "false" not in value and '"queued"' not in value


def test_recheck_feedback_is_a_one_time_schedule_not_a_write_success():
    value = render("recheck", {"scheduled": 57, "already_pending": 2, "skipped": 1})
    for expected in ("已安排", "一次复查", "57", "已在处理", "已跳过", "复查尚未完成", "可能入账", "不会自动再次复查"):
        assert expected in value
    assert "入账成功" not in value and "scheduled" not in value


def test_empty_recheck_does_not_claim_work_will_start():
    value = render("recheck", {"scheduled": 0, "already_pending": 0, "skipped": 0})
    assert "本次没有新增复查安排" in value
    assert "worker 将" not in value and "复查尚未完成" not in value


def test_resolve_reports_decision_and_unknown_intent_separately():
    args = Namespace(entity_type="bank_transactions", entity_id="full-ID")
    value = render("resolve", {"result": "decision saved", "action": "link"}, args=args)
    assert "处理决定已保存" in value and "没有新建账单" in value and "full-ID" in value
    unknown = render("resolve", {"result": "intent recorded; external outcome must be verified", "status": "unknown"})
    assert "处理意图已保存" in unknown and "仍待核实" in unknown and "不会重新发送" in unknown
    assert "处理决定已保存" not in unknown


def test_help_covers_minimal_commands_and_side_effects_without_argparse_dump():
    stream = StringIO()
    display = Console(file=stream, width=100, color_system=None)
    for command in (None, "recheck", "resolve", "issues", "exit"):
        presentation.render_help(display, command)
    value = stream.getvalue()
    for expected in ("recheck", "一次", "可能入账", "不", "--entity-id ID", "--version VERSION",
                     "confirm-new", "Ctrl+C", "console 和 worker"):
        assert expected in value
    assert "usage:" not in value and "options:" not in value and "quit" not in value
