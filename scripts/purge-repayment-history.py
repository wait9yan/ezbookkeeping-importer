"""一次性删除 importer 还款历史；仅访问数据库和显式数据目录。"""

import argparse
from email import policy
from email.parser import BytesHeaderParser
from hashlib import sha256
import json
import os
import re
from pathlib import Path
import stat

from ezbookkeeping_importer.adapters.persistence.migrations import load_migrations
from ezbookkeeping_importer.adapters.persistence.postgres import PostgresStore
from ezbookkeeping_importer.domain.errors import Conflict
from ezbookkeeping_importer.domain.mail import normalize_subject

MANIFEST = '.repayment-purge.json'
SELECTION_END = '-- END REPAYMENT PURGE SELECTION\n'


def checked_path(root: Path, value: str, *, directory: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    relative = path.relative_to(root)
    if '..' in relative.parts:
        raise ValueError('清理路径不能越过数据目录')
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('清理路径不能包含符号链接')
    if path.exists() and not (path.is_dir() if directory else path.is_file()):
        raise ValueError('清理目标必须是普通文件')
    return path


def sync_directory(directory: Path):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes, *, preserve: bool = False):
    metadata = path.stat() if preserve else None
    temporary = path.with_name(path.name + '.repayment-purge.tmp')
    if temporary.is_symlink() or (temporary.exists() and temporary.stat().st_nlink != 1):
        raise ValueError('清理临时文件不能是链接')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            if metadata is not None:
                os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
                if (os.geteuid(), os.getegid()) != (metadata.st_uid, metadata.st_gid):
                    os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def database_identity(store):
    identity = store.one('SELECT current_database() AS database,current_schema() AS schema, '
                     '(SELECT oid FROM pg_database WHERE datname=current_database()) AS oid, '
                     'inet_server_addr()::text AS server_address,inet_server_port() AS server_port')
    # Unix sockets have no inet_server_* identity; retain only non-secret endpoint fields.
    return {**identity, 'connection_host': store.connection.info.host,
            'connection_port': store.connection.info.port}


def orphan_mail_paths(root: Path) -> set[str]:
    directory = checked_path(root, 'email', directory=True)
    result = set()
    for candidate in directory.glob('*.eml'):
        path = checked_path(root, str(candidate))
        headers = []
        with path.open('rb') as stream:
            for line in stream:
                headers.append(line)
                if line in (b'\n', b'\r\n'):
                    break
        message = BytesHeaderParser(policy=policy.default).parsebytes(b''.join(headers))
        if normalize_subject(str(message.get('Subject', '')))[0] == '自动还款扣款通知':
            result.add(str(path.relative_to(root)))
    return result


def collect_manifest(store, root: Path, report_dir: Path, log_dir: Path):
    migration = load_migrations()[2]
    if migration.script.count(SELECTION_END) != 1:
        raise ValueError('迁移缺少唯一清理定位段')
    # Selection is the exact reviewed migration prefix, so file and SQL targets cannot drift.
    with store.transaction():
        store.execute(migration.script.split(SELECTION_END)[0])
        emails = store.all('SELECT id,raw_path FROM email WHERE id IN (SELECT id FROM purge_repayment_emails)')
        reports = store.all('SELECT report_key FROM purge_repayment_reports UNION SELECT report_key FROM purge_repayment_statements WHERE (SELECT max(version) FROM schema_version)<3')
        transactions = store.all('SELECT id FROM purge_repayment_transactions')
        tasks = store.all('SELECT id FROM background_task WHERE bank_transaction_id IN (SELECT id FROM purge_repayment_transactions)')
        attempts = store.all('SELECT id FROM ledger_write_attempt WHERE task_id IN (SELECT id FROM background_task WHERE bank_transaction_id IN (SELECT id FROM purge_repayment_transactions))')
        sources = store.all('SELECT id FROM email_source_item WHERE email_id IN (SELECT id FROM purge_repayment_emails)')
    paths = orphan_mail_paths(root)
    for email in emails:
        # raw_path is stored relative to the worker working directory, not data_dir.
        raw = Path(email['raw_path'])
        if not raw.is_absolute():
            if not raw.parts or raw.parts[0] != root.name:
                raise ValueError('相对原件路径必须以数据目录名开头')
            raw = root.joinpath(*raw.parts[1:])
        path = raw
        if path.name != email['id'] + '.eml':
            raise ValueError('原件路径与邮件摘要身份不符')
        paths.add(str(checked_path(root, str(path)).relative_to(root)))
    for report in reports:
        name = sha256(report['report_key'].encode()).hexdigest()
        for extension in ('.json', '.tmp'):
            path = checked_path(root, str(report_dir / (name + extension)))
            paths.add(str(path.relative_to(root)))
    checked_path(root, str(report_dir), directory=True)
    checked_path(root, str(log_dir), directory=True)
    logs = [str(checked_path(root, str(path)).relative_to(root))
            for path in sorted(log_dir.glob('worker.jsonl*'))
            if re.fullmatch(r'worker\.jsonl(?:\.[0-9]+)?', path.name)]
    return {'format': 1, 'database': database_identity(store), 'root': str(root),
            'files': sorted(paths), 'logs': logs,
            'identities': {'email_id': sorted({row['id'] for row in emails} |
                                             {Path(path).stem for path in paths
                                              if re.fullmatch(r'[a-f0-9]{64}\.eml', Path(path).name)}),
                           'report_key': [row['report_key'] for row in reports],
                           'transaction_id': [row['id'] for row in transactions],
                           'task_id': [row['id'] for row in tasks],
                           'attempt_id': [row['id'] for row in attempts],
                           'source_item_id': [row['id'] for row in sources]}}


def related_event(event: dict, identities: dict) -> bool:
    if event.get('report_type') == 'repayment':
        return True
    if any(event.get(key) in values for key, values in identities.items()):
        return True
    aliases = {'job_id': 'task_id', 'source_row_id': 'transaction_id'}
    if any(event.get(alias) in identities.get(field, []) for alias, field in aliases.items()):
        return True
    entity_fields = {'email': 'email_id', 'transaction': 'transaction_id',
                     'report': 'report_key', 'task': 'task_id', 'source_item': 'source_item_id',
                     'bank_transactions': 'transaction_id', 'bank_report': 'report_key',
                     'background_task': 'task_id', 'email_source_item': 'source_item_id',
                     'ledger_write_attempt': 'attempt_id'}
    field = entity_fields.get(str(event.get('entity_type')))
    return field is not None and str(event.get('entity_id')) in {
        str(value) for value in identities.get(field, [])}


def clean_files(root: Path, manifest: dict):
    # Validate every path before deleting any; a failed filesystem stage remains resumable.
    paths = [checked_path(root, value) for value in manifest['files']]
    logs = [checked_path(root, value) for value in manifest['logs']]
    for path in paths:
        path.unlink(missing_ok=True)
        if path.parent.exists():
            sync_directory(path.parent)
    for path in logs:
        if not path.exists():
            continue
        lines = path.read_bytes().splitlines(keepends=True)
        retained = []
        for line in lines:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError('应用日志不是 JSON 对象；保留清理清单以供修复后续跑')
            if not related_event(event, manifest['identities']):
                retained.append(line)
        if len(retained) != len(lines):
            atomic_write(path, b''.join(retained), preserve=True)
        temporary = checked_path(root, str(path.with_name(path.name + '.repayment-purge.tmp')))
        temporary.unlink(missing_ok=True)
        sync_directory(path.parent)


def purge(store, data_dir: Path, *, report_dir: Path | None = None,
          log_dir: Path | None = None) -> dict:
    root = data_dir.absolute()
    if root != root.resolve() or not root.is_dir():
        raise ValueError('data-dir 必须是现存且不含符号链接的目录')
    if not store.lock_worker():
        raise Conflict('another worker is already running; stop it before cleanup')
    try:
        path = checked_path(root, MANIFEST)
        with store.transaction():
            version = store._schema_state(load_migrations())
        if path.exists():
            manifest = json.loads(path.read_text())
            if (manifest.get('format') != 1 or manifest.get('database') != database_identity(store)
                    or manifest.get('root') != str(root)):
                raise ValueError('清理清单与当前数据库或数据目录不一致')
            if version < 3:
                refreshed = collect_manifest(store, root, report_dir or root / 'reports',
                                             log_dir or root / 'logs')
                for key in ('files', 'logs'):
                    manifest[key] = sorted(set(manifest[key]) | set(refreshed[key]))
                for key, values in refreshed['identities'].items():
                    manifest['identities'][key] = sorted(set(manifest['identities'][key]) | set(values))
                atomic_write(path, (json.dumps(manifest, ensure_ascii=False) + '\n').encode())
        else:
            manifest = collect_manifest(store, root, report_dir or root / 'reports',
                                        log_dir or root / 'logs')
            atomic_write(path, (json.dumps(manifest, ensure_ascii=False) + '\n').encode())
        store.execute("SELECT set_config('ebki.repayment_purge_prepared','yes',false)")
        store.migrate(hold_worker=True)
        clean_files(root, manifest)
        path.unlink()
        sync_directory(root)
        return {'schema_version': store.check_schema(),
                'deleted': {key: len(values) for key, values in manifest['identities'].items()},
                'files': len(manifest['files'])}
    finally:
        store.execute("SELECT set_config('ebki.repayment_purge_prepared','',false)")
        store.unlock_worker()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--report-dir', type=Path, help='自定义报告目录，必须在 data-dir 内')
    parser.add_argument('--log-dir', type=Path, help='自定义日志目录，必须在 data-dir 内')
    args = parser.parse_args()
    dsn = os.environ.get('EBKI_DATABASE_URL')
    if not dsn:
        parser.error('需要 EBKI_DATABASE_URL 指向 importer 数据库')
    store = PostgresStore(dsn)
    try:
        print(json.dumps(purge(store, args.data_dir, report_dir=args.report_dir,
                               log_dir=args.log_dir), ensure_ascii=False))
    finally:
        store.close()


if __name__ == '__main__':
    main()
