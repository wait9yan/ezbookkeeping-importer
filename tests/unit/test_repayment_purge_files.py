"""一次性清理的文件边界与日志身份回归。"""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('purge_repayment_files', Path('scripts/purge-repayment-history.py'))
purge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(purge)


def test_database_identity_distinguishes_unix_socket_endpoints_without_secrets():
    database = {'database': 'importer', 'schema': 'public', 'oid': 16384,
                'server_address': None, 'server_port': None}
    def store(host):
        return SimpleNamespace(
            one=lambda query: database,
            connection=SimpleNamespace(info=SimpleNamespace(
                host=host, port=5432, password='must-not-be-persisted')),
        )
    first = purge.database_identity(store('/tmp/cluster-a'))
    second = purge.database_identity(store('/tmp/cluster-b'))
    assert first != second
    assert first['connection_host'] == '/tmp/cluster-a'
    assert first['connection_port'] == 5432
    assert 'must-not-be-persisted' not in json.dumps(first)


@pytest.mark.parametrize('entity,field,value', [
    ('email', 'email_id', 'a'*64), ('bank_transactions', 'transaction_id', 'A'*16),
    ('bank_report', 'report_key', 'synthetic-report'), ('background_task', 'task_id', 5),
    ('email_source_item', 'source_item_id', 6), ('ledger_write_attempt', 'attempt_id', 7),
])
def test_logs_match_real_entity_names(entity, field, value):
    identities = {field: [value]}
    assert purge.related_event({'entity_type': entity, 'entity_id': str(value)}, identities)
    assert not purge.related_event({'entity_type': entity, 'entity_id': 'unrelated'}, identities)


@pytest.mark.parametrize('field,target', [('job_id', 'task_id'), ('source_row_id', 'transaction_id')])
def test_old_log_field_aliases(field, target):
    assert purge.related_event({field: 'synthetic'}, {target: ['synthetic']})


def test_external_symlink_and_parent_paths_rejected(tmp_path):
    root = tmp_path.resolve()
    (root / 'link').symlink_to('/tmp')
    for value in ('../outside', 'link/file', '/etc/passwd'):
        with pytest.raises(ValueError):
            purge.checked_path(root, value)


def test_log_rewrite_preserves_other_bytes_permissions_and_removes_stale_temp(tmp_path):
    root = tmp_path.resolve()
    path = root / 'worker.jsonl'
    original = b'{ "event": "worker_started" }\n'
    path.write_bytes(b'{"task_id":5}\n' + original)
    os.chmod(path, 0o640)
    temp = root / 'worker.jsonl.repayment-purge.tmp'
    temp.write_text('old interrupted rewrite')
    purge.clean_files(root, {'files': [], 'logs': [path.name], 'identities': {'task_id': [5]}})
    assert path.read_bytes() == original
    assert path.stat().st_mode & 0o777 == 0o640
    assert not temp.exists()


def test_invalid_log_explicitly_fails_without_replacing_it(tmp_path):
    root = tmp_path.resolve()
    path = root / 'worker.jsonl'
    path.write_text('not JSON\n')
    with pytest.raises(json.JSONDecodeError):
        purge.clean_files(root, {'files': [], 'logs': [path.name], 'identities': {}})
    assert path.read_text() == 'not JSON\n'


def test_orphan_mail_scan_only_recognizes_normalized_repayment_headers(tmp_path):
    from email.message import EmailMessage
    root = tmp_path.resolve()
    directory = root / 'email'
    directory.mkdir()
    for name, subject in (('repayment', 'Fwd: 转发： 自动还款扣款通知'), ('daily', '每日信用管家')):
        message = EmailMessage()
        message['Subject'] = subject
        message.set_content('synthetic body')
        (directory / (name+'.eml')).write_bytes(message.as_bytes())
    assert purge.orphan_mail_paths(root) == {'email/repayment.eml'}
