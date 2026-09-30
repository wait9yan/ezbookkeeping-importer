"""无交互参数边界、快照往返、依赖选择与进程中断。"""
import json
import signal
from io import StringIO
from types import SimpleNamespace

import pytest

from ezbookkeeping_importer.entrypoints import cli


def snapshot():
    return {"snapshot_version": 1, "items": [{
        "issue": {"entity_type": "bank_transactions", "entity_id": "tx", "code": "duplicate_candidates",
                  "version": 2, "status": "issue", "detail": {}, "context": {}},
        "state": {"row": {}, "related": {}}, "view": {"actions": ["link"]},
    }]}


def parse(values):
    return cli.parse_command(cli.build_parser(), values)


@pytest.mark.parametrize("tail", [
    ["resolve", "--snapshot", "-", "--action", "link", "--reason", "核实"],
    ["resolve", "--snapshot", "-", "--action", "ignore", "--reason", " "],
    ["resolve", "--snapshot", "-", "--action", "ignore", "--reason", "核实", "--target-id", "t"],
    ["resolve", "--snapshot", "-", "--action", "link", "--reason", "核实", "--target-id", "t", "--account-id", "a"],
    ["--entity-id", "tx", "candidates", "--snapshot", "-"],
    ["--status", "issue", "show", "--entity-type", "email", "--entity-id", "x"],
])
def test_incompatible_parameters_fail_before_config_or_stdin(monkeypatch, tail):
    class NoInput:
        def read(self):
            pytest.fail("invalid parameters read stdin")
    monkeypatch.setattr("sys.stdin", NoInput())
    with pytest.raises(SystemExit) as error:
        parse(["issues", *tail])
    assert error.value.code == 2


@pytest.mark.parametrize("content", ["not-json", '{"snapshot_version":1,"snapshot_version":1,"items":[]}',
                                     '{"snapshot_version":NaN,"items":[]}',
                                     '{"snapshot_version":1,"items":[]}'])
def test_invalid_and_empty_single_snapshot_is_parameter_error(monkeypatch, content):
    monkeypatch.setattr("sys.stdin", StringIO(content))
    with pytest.raises(SystemExit) as error:
        parse(["issues", "candidates", "--snapshot", "-"])
    assert error.value.code == 2


def test_show_and_export_share_envelope_and_snapshot_file_is_reusable(tmp_path, monkeypatch):
    result = snapshot()
    monkeypatch.setattr(cli.issue_snapshot, "snapshot_issues", lambda *a: result)
    runtime = SimpleNamespace(store=object())
    show = cli._execute(parse(["issues", "show", "--entity-type", "bank_transactions", "--entity-id", "tx"]), runtime)
    path = tmp_path / "selected.json"
    listed = cli._execute(parse(["issues", "--snapshot-out", str(path)]), runtime)
    assert listed == [result["items"][0]["issue"]]
    assert json.loads(path.read_text()) == show
    args = parse(["recheck", "--snapshot", str(path)])
    assert args.snapshot_document == show


@pytest.mark.parametrize("action,target,account,needs_ledger", [
    ("retry", None, None, False), ("ignore", None, None, False),
    ("accept-source", None, None, False), ("confirm-new", None, None, False),
    ("link", "remote", None, True), ("retry", None, "account", True),
])
def test_action_dispatch_reuses_snapshot_and_dependency_contract(monkeypatch, action, target, account, needs_ledger):
    from ezbookkeeping_importer.config import command_capabilities
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(snapshot())))
    values = ["issues", "resolve", "--snapshot", "-", "--action", action, "--reason", "已人工核实"]
    if target:
        values += ["--target-id", target]
    if account:
        values += ["--account-id", account]
    args = parse(values)
    options = []
    monkeypatch.setattr(cli, "load_settings", lambda path, **kw: options.append(kw))
    closed = []
    runtime = SimpleNamespace(store=object(), optional_ledger=object(), close=lambda: closed.append(True))
    monkeypatch.setattr(cli, "Runtime", lambda *a, **kw: runtime)
    calls = []
    monkeypatch.setattr(cli.issue_snapshot, "resolve_snapshot", lambda *a: calls.append(a) or {"action": action})
    assert cli.execute_command(args) == {"action": action}
    assert calls[0][2] == snapshot()
    assert calls[0][3:] == (action, "已人工核实", target, account)
    assert ("ledger" in command_capabilities("resolve", "rules_only", action=action, account_id=account)) == needs_ledger
    assert closed == [True]


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_maintenance_signal_closes_runtime_and_warns_of_commits(monkeypatch, capsys, signum):
    monkeypatch.setattr("sys.argv", ["ebki", "restore-audit"])
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: None)
    closed = []
    runtime = SimpleNamespace(store=object(), close=lambda: closed.append(True))
    monkeypatch.setattr(cli, "Runtime", lambda *a, **k: runtime)
    def operation(*args):
        signal.raise_signal(signum)
    monkeypatch.setattr(cli, "_execute", operation)
    previous = signal.getsignal(signum)
    assert cli.main() == 128 + signum
    assert signal.getsignal(signum) is previous
    assert closed == [True]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "可能已提交" in json.loads(captured.err)["message"]


def test_filtered_issues_preserve_default_json(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ebki", "issues", "--code", "duplicate_candidates", "--status", "issue"])
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: None)
    runtime = SimpleNamespace(store=object(), close=lambda: None)
    monkeypatch.setattr(cli, "Runtime", lambda *a, **k: runtime)
    item = snapshot()["items"][0]["issue"]
    monkeypatch.setattr(cli.maintenance, "issues", lambda *a: [item, {**item, "status": "pending"}])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out) == [item]


def test_unknown_and_booked_actions_do_not_offer_creation():
    from ezbookkeeping_importer.application.issue_interaction import resolution_actions
    row = {"ledger_transaction_id": None, "import_decision": {"payload": {"type": 3}}}
    assert resolution_actions("bank_transactions", row, active=True) == ["retry"]
    assert resolution_actions("bank_transactions", {**row, "ledger_transaction_id": "1"}) == []
    assert resolution_actions("bank_statement_reconciliation", {}) == []


def test_scoped_recheck_passes_only_observed_snapshot(monkeypatch):
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(snapshot())))
    args = parse(["recheck", "--snapshot", "-"])
    runtime = SimpleNamespace(store=object())
    calls = []
    monkeypatch.setattr(cli, "request_snapshot_recheck", lambda *a: calls.append(a) or {"scheduled": 1})
    monkeypatch.setattr(cli, "request_recheck", lambda *a: pytest.fail("expanded to global recheck"))
    assert cli._execute(args, runtime) == {"scheduled": 1}
    assert calls == [(runtime.store, snapshot())]


def test_conflict_returns_business_failure_without_refresh_or_replay(monkeypatch, capsys):
    from ezbookkeeping_importer.domain.errors import Conflict
    monkeypatch.setattr("sys.argv", ["ebki", "issues", "resolve", "--snapshot", "-", "--action", "retry", "--reason", "核实"])
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(snapshot())))
    calls = []
    def execute(args):
        calls.append(args.snapshot_document)
        raise Conflict("问题状态已经变化")
    monkeypatch.setattr(cli, "execute_command", execute)
    assert cli.main() == 1
    assert calls == [snapshot()]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error_type"] == "Conflict"


@pytest.mark.parametrize("arguments", [
    ["issues", "--format", "text", "show", "--entity-type", "email", "--entity-id", "mail"],
    ["issues", "show", "--entity-type", "email", "--entity-id", "mail", "--format", "text"],
])
def test_explicit_output_format_survives_subcommand_parsing(arguments):
    assert parse(arguments).format == "text"


@pytest.mark.parametrize("option", ["--target-id", "--account-id"])
def test_empty_action_identifiers_are_rejected_before_reading_snapshot(monkeypatch, option):
    monkeypatch.setattr("sys.stdin", StringIO(""))
    with pytest.raises(SystemExit) as error:
        parse(["issues", "resolve", "--snapshot", "-", "--action", "retry", "--reason", "核实", option, " "])
    assert error.value.code == 2


def test_plain_text_resolve_feedback_does_not_claim_completed_write(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ebki", "issues", "resolve", "--snapshot", "-", "--action", "retry", "--reason", "核实", "--format", "text"])
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(snapshot())))
    monkeypatch.setattr(cli, "execute_command", lambda args: {"result": "decision saved", "action": "retry"})
    assert cli.main() == 0
    captured = capsys.readouterr()
    assert "处理决定已保存" in captured.out and "不表示账本写入完成" in captured.out
    assert captured.err == ""
