"""从权威迁移链在随机隔离 schema 生成契约；不读取 .env。"""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import uuid

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from ezbookkeeping_importer.adapters.persistence.migrations import (
    CONTRACT_FORMAT, Migration, apply_migration, digest, migration_sources,
)
from ezbookkeeping_importer.adapters.persistence.schema_contract import schema_signature

OUTPUT = Path('src/ezbookkeeping_importer/adapters/persistence/schema_contracts.json')


def generate(dsn: str) -> dict:
    sources = migration_sources(Path('migrations'))
    versions = []
    namespace = 'ebki_generate_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as connection:
        # All generated objects are transactional; failures and success both clean this schema.
        with connection.transaction(force_rollback=True):
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(namespace)))
            connection.execute("SELECT set_config('search_path',%s,true)", (namespace,))
            for version, name, script, checksum in sources:
                apply_migration(connection, Migration(version, name, script, checksum, {}),
                                sources[0][3])
                signature = schema_signature(connection, namespace)
                versions.append({'version': version, 'name': name, 'script_sha256': checksum,
                                 'signature_sha256': digest(signature), 'signature': signature})
    return {'format': CONTRACT_FORMAT, 'versions': versions}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--resources-only', action='store_true')
    args = parser.parse_args()
    if args.resources_only:
        from ezbookkeeping_importer.adapters.persistence.migrations import load_migrations
        migrations = load_migrations()
        for migration, source in zip(migrations, migration_sources(Path('migrations')), strict=True):
            if migration.checksum != source[3]:
                raise ValueError('打包迁移与权威 SQL 不一致')
        print('迁移资源和契约摘要一致')
        return 0
    dsn = os.environ.get('EBKI_TEST_DATABASE_URL')
    if not dsn:
        parser.error('需要显式 EBKI_TEST_DATABASE_URL 指向隔离测试实例')
    encoded = json.dumps(generate(dsn), ensure_ascii=False, indent=2) + '\n'
    if args.check:
        if not OUTPUT.exists() or sha256(OUTPUT.read_bytes()).digest() != sha256(encoded.encode()).digest():
            print('派生结构契约漂移；请重新生成并评审', file=sys.stderr)
            return 1
        print('真实 PostgreSQL 再生成结果一致')
    else:
        OUTPUT.write_text(encoded, encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
