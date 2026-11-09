"""镜像必须运行真实无TTY单进程入口，不能只匹配一个子worker。"""
import importlib.util
import json
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
from datetime import datetime
from decimal import Decimal
import ast
import re

import pytest

spec = importlib.util.spec_from_file_location(
    'image_lifecycle', Path(__file__).parents[2] / 'scripts' / 'image_lifecycle.py'
)
assert spec is not None and spec.loader is not None
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)


def process(root, pid, *args):
    target = root / str(pid)
    target.mkdir()
    (target / 'status').write_text('Uid:\t10001\t10001\t10001\t10001\nGid:\t10001\t10001\t10001\t10001\n')
    (target / 'cmdline').write_bytes(b'\0'.join(arg.encode() for arg in args))


def snapshot(root):
    exec(lifecycle.PROCESS_CHECK.replace("Path('/proc')", f'Path({str(root)!r})'))


def test_pid_one_without_multiprocessing(tmp_path, capsys):
    process(tmp_path, 1, '/app/.venv/bin/python', '/app/.venv/bin/ebki', 'run')
    process(tmp_path, 2, 'python', '-c', lifecycle.PROCESS_CHECK)
    snapshot(tmp_path)
    assert json.loads(capsys.readouterr().out) == [1]


@pytest.mark.parametrize('pid,child', [(2, False), (1, True)])
def test_non_pid_one_or_child_worker_rejected(tmp_path, pid, child):
    process(tmp_path, pid, '/app/.venv/bin/ebki', 'run')
    if child:
        process(tmp_path, 3, 'python', '-c', 'from multiprocessing.spawn import spawn_main; spawn_main()')
    with pytest.raises(AssertionError):
        snapshot(tmp_path)


@pytest.mark.parametrize('entrypoint,command', [(['python'], ['run']), (['ebki'], ['worker'])])
def test_wrong_default_entrypoint_rejected(entrypoint, command):
    calls = []

    def docker(*args):
        calls.append(args)
        return json.dumps([{'Config': {'Entrypoint': entrypoint, 'Cmd': command}}])

    with pytest.raises(AssertionError):
        lifecycle.verify_lifecycle(docker, 'image', [], 'net', [], 'synthetic', 'container')
    assert calls == [('image', 'inspect', 'image')]


def test_root_service_rejected(tmp_path):
    process(tmp_path, 1, '/app/.venv/bin/ebki', 'run')
    (tmp_path / '1/status').write_text('Uid: 0 0 0 0\nGid: 0 0 0 0\n')
    with pytest.raises(AssertionError):
        snapshot(tmp_path)


@pytest.mark.parametrize('phase', ['before', 'after'])
def test_seeded_transaction_has_report_facts_required_by_real_create_preflight(monkeypatch, phase):
    """捕获合成seed的实际INSERT参数；真实SQL和强杀路径由镜像验收执行。"""
    seed_spec = importlib.util.spec_from_file_location('image_seed', lifecycle.FIXTURE / 'seed.py')
    assert seed_spec is not None and seed_spec.loader is not None
    seed = importlib.util.module_from_spec(seed_spec)
    seed_spec.loader.exec_module(seed)
    calls = []
    store = SimpleNamespace(transaction=nullcontext, close=lambda: None,
                            execute=lambda query, params=(): calls.append((query, params)))
    monkeypatch.setattr(seed, 'PostgresStore', lambda dsn: store)
    monkeypatch.setenv('EBKI_DATABASE_URL', 'postgresql://synthetic@database.invalid/isolated')
    seed.seed(phase)

    query, params = next(call for call in calls if 'INSERT INTO bank_transactions' in call[0])
    columns, values = re.search(r'bank_transactions\s*\((.*?)\)\s*VALUES\s*\((.*?)\)',
                                query, re.DOTALL).groups()
    parameters = iter(params)
    # Model SQL defaults for the nullable source fields, then decode the actual
    # bound values. This checks seed wiring, not a second application projection.
    transaction = dict(card_reference=None, occurred_at=None, posted_date=None,
                       bank_settlement_amount=None, bank_settlement_currency=None, source_details={})
    transaction.update({column.strip(): next(parameters) if value.strip() == '%s' else ast.literal_eval(value.strip())
                        for column, value in zip(columns.split(','), values.split(','), strict=True)})
    report = next(params[-1] for query, params in calls if 'INSERT INTO bank_report' in query)
    from ezbookkeeping_importer.application.records import source_row
    from ezbookkeeping_importer.application.statement_candidates import query_candidates
    facts = source_row(transaction)
    ledger = SimpleNamespace(
        accounts=lambda: [{'id': '1', 'type': 1, 'currency': 'CNY', 'comment': '6222000000001234'}],
        search=lambda start, end: [])
    assert query_candidates(ledger, facts)[0] == []
    for field in ('card_reference', 'occurred_at', 'occurred_date', 'original_currency'):
        assert facts[field] == report['rows'][0][field]
    assert Decimal(facts['original_amount']) == Decimal(report['rows'][0]['original_amount'])
    assert int(datetime.fromisoformat(facts['occurred_at']).timestamp()) == transaction['import_decision']['payload']['time']
