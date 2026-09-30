"""迁移字节、派生资源完整性和 SQL qualifier 规范化契约。"""

from hashlib import sha256
import json
from pathlib import Path

import pytest

from ezbookkeeping_importer.adapters.persistence import migrations
from ezbookkeeping_importer.adapters.persistence.schema_contract import normalize_namespace
from ezbookkeeping_importer.domain.errors import DatabaseDiagnosticError


def test_full_chain_matches_authoritative_sql_and_versioned_contracts():
    chain = migrations.load_migrations()
    assert [step.version for step in chain] == [1, 2]
    for step in chain:
        assert sha256(Path('migrations', step.name).read_bytes()).hexdigest() == step.checksum
        assert set(step.signature) == {'relations', 'types', 'routines', 'columns', 'constraints', 'indexes'}
    assert Path('migrations/001_initial.sql').read_bytes() == migrations.RESOURCES.joinpath('schema.sql').read_bytes()


@pytest.mark.parametrize('change', ['script', 'signature', 'version', 'missing'])
def test_resource_drift_is_rejected(tmp_path, monkeypatch, change):
    directory = tmp_path / 'migration_sql'
    directory.mkdir()
    for file in Path('migrations').glob('*.sql'):
        (directory / file.name).write_bytes(file.read_bytes())
    document = json.loads(migrations.RESOURCES.joinpath('schema_contracts.json').read_text())
    if change == 'script':
        (directory / '002_migration_history.sql').write_text('SELECT 1;')
    elif change == 'signature':
        document['versions'][1]['signature']['columns'].pop()
    elif change == 'version':
        document['versions'][1]['version'] = 3
    else:
        document['versions'].pop()
    (tmp_path / 'schema_contracts.json').write_text(json.dumps(document))
    monkeypatch.setattr(migrations, 'RESOURCES', tmp_path)
    with pytest.raises(DatabaseDiagnosticError):
        migrations.load_migrations()


def test_rewritten_published_baseline_is_rejected_even_when_only_comment_changed(tmp_path):
    (tmp_path / '001_initial.sql').write_bytes(Path('migrations/001_initial.sql').read_bytes() + b'\n-- rewritten\n')
    with pytest.raises(DatabaseDiagnosticError, match='published_migration_changed'):
        migrations.migration_sources(tmp_path)


@pytest.mark.parametrize('statement', ['BEGIN;', 'COMMIT;', 'VACUUM;', 'CREATE INDEX CONCURRENTLY x ON email(id);'])
def test_nontransactional_script_resources_are_rejected(tmp_path, statement):
    (tmp_path / '001_initial.sql').write_bytes(Path('migrations/001_initial.sql').read_bytes())
    (tmp_path / '002_invalid.sql').write_text(statement)
    with pytest.raises(DatabaseDiagnosticError, match='unsupported_migration'):
        migrations.migration_sources(tmp_path)


@pytest.mark.parametrize('namespace', ['public', 'quoted"name'])
def test_namespace_normalization_preserves_literal_business_values(namespace):
    quoted = '"' + namespace.replace('"', '""') + '"'
    query = f"CHECK (x = '{namespace}.foo' OR x = E'escaped\\\\text') ON {quoted}.email"
    assert normalize_namespace(query, namespace) == f"CHECK (x = '{namespace}.foo' OR x = E'escaped\\\\text') ON email"
