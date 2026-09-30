"""不可改写的有序 SQL 链和由真实 PostgreSQL 派生的版本契约。"""

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
import json
import re
from typing import Any

from ...domain.errors import DatabaseDiagnosticError

CONTRACT_FORMAT = 1
# Released v1 has no stored checksum; pin its published bytes before accepting that baseline.
PUBLISHED_BASELINE_SHA256 = "5b31feddebafe5a64bb2f997256a0b7480d218217088919ca0fc08ceacc19d2b"
RESOURCES = files(__package__)


def schema_error(code: str, reason: str) -> DatabaseDiagnosticError:
    return DatabaseDiagnosticError('schema', code, reason)


def digest(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    script: str
    checksum: str
    signature: dict[str, Any]


def migration_sources(root=None) -> list[tuple[int, str, str, str]]:
    directory = root or RESOURCES.joinpath('migration_sql')
    result: list[tuple[int, str, str, str]] = []
    for source in sorted(directory.iterdir(), key=lambda entry: entry.name):
        if not source.name.endswith('.sql'):
            continue
        match = re.fullmatch(r'(\d{3})_[a-z0-9_]+\.sql', source.name)
        if not match or int(match[1]) != len(result) + 1:
            raise schema_error('invalid_migration_chain', '迁移资源版本缺项或顺序错误；检查发行包。')
        raw = source.read_bytes()
        script = raw.decode('utf-8')
        # This is a reviewed SQL resource gate, not a parser for arbitrary user SQL.
        lines = re.sub(r'--[^\n]*', '', script)
        forbidden = r'(?im)^\s*(?:BEGIN|COMMIT|ROLLBACK|VACUUM|CREATE\s+DATABASE|ALTER\s+SYSTEM)\b|\bCONCURRENTLY\b'
        if re.search(forbidden, lines):
            raise schema_error('unsupported_migration', '迁移包含事务控制或非事务操作；需单独设计维护步骤。')
        result.append((int(match[1]), source.name, script, sha256(raw).hexdigest()))
    if not result:
        raise schema_error('missing_migrations', '发行包缺少数据库迁移资源。')
    if result[0][3] != PUBLISHED_BASELINE_SHA256:
        raise schema_error('published_migration_changed', '已发布的 001 迁移脚本被改写；恢复原文件并新增增量迁移。')
    return result


def load_migrations() -> tuple[Migration, ...]:
    try:
        manifest = json.loads(RESOURCES.joinpath('schema_contracts.json').read_text())
        sources = migration_sources()
        if manifest['format'] != CONTRACT_FORMAT or len(manifest['versions']) != len(sources):
            raise ValueError
        result = []
        for source, contract in zip(sources, manifest['versions'], strict=True):
            version, name, script, checksum = source
            if (contract['version'], contract['name'], contract['script_sha256']) != (
                version, name, checksum
            ) or digest(contract['signature']) != contract['signature_sha256']:
                raise ValueError
            result.append(Migration(version, name, script, checksum, contract['signature']))
        return tuple(result)
    except (OSError, ValueError, KeyError, TypeError):
        raise schema_error('invalid_schema_resources',
                           'SQL 链与派生结构契约缺失或不一致；重新生成契约并构建发行包。') from None


def apply_migration(connection, migration: Migration, baseline_checksum: str):
    connection.execute("SELECT set_config('ebki.migration_baseline_sha256',%s,true)",
                       (baseline_checksum,))
    connection.execute(migration.script)
    # Published 001 owns the original version row. Subsequent history is engine-owned.
    if migration.version > 1:
        connection.execute('INSERT INTO schema_version(version,script_sha256) VALUES (%s,%s)',
                           (migration.version, migration.checksum))
