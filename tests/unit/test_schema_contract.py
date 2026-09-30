"""结构签名不受集合查询顺序影响，但必须保留内容、数量与表内列顺序。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from ezbookkeeping_importer.adapters.persistence.schema_contract import schema_signature


@pytest.fixture
def catalog():
    return {
        'relations': [{'relname': name, 'relkind': 'r'} for name in ('z_table', 'a_table')],
        'types': [{'typname': name, 'typtype': 'e'} for name in ('z_type', 'a_type')],
        'routines': [
            {'proname': 'function', 'arguments': arguments}
            for arguments in ('value text', 'value integer')
        ],
        'columns': [
            {'relname': table, 'attname': column, 'type': 'text', 'attnotnull': False,
             'attgenerated': '', 'default_expression': None, 'table_comment': '业务表',
             'column_comment': '业务列'}
            for table in ('z_table', 'a_table') for column in ('z_column', 'a_column')
        ],
        'constraints': [
            {'relname': 'a_table', 'contype': 'c', 'definition': definition}
            for definition in ('CHECK (value > 2)', 'CHECK (value > 1)')
        ],
        'indexes': [
            {'tablename': 'a_table', 'indexname': name,
             'indexdef': f'CREATE INDEX {name} ON a_table USING btree (z_column)'}
            for name in ('z_index', 'a_index')
        ],
    }


def signature(catalog, namespace='synthetic'):
    results = iter(deepcopy(list(catalog.values())))

    def execute(query, parameters):
        assert parameters == (namespace,)
        if 'pg_attribute' in query:
            assert 'ORDER BY c.relname,a.attnum' in query
        rows = next(results)
        return SimpleNamespace(fetchall=lambda: rows)

    return schema_signature(SimpleNamespace(execute=execute), namespace)


def test_query_collection_order_does_not_change_signature(catalog):
    reordered = {section: list(reversed(rows)) for section, rows in catalog.items()}
    # Query ordering by attnum is meaningful; only rearrange entire table groups.
    reordered['columns'] = catalog['columns'][2:] + catalog['columns'][:2]
    assert signature(reordered) == signature(catalog)
    assert signature(signature(catalog)) == signature(catalog)


def test_namespace_is_normalized_before_constraints_are_sorted(catalog):
    namespace = 'z_namespace'
    catalog['constraints'] = [
        {'relname': 'a_table', 'contype': 'c', 'definition': definition}
        for definition in ('CHECK (value > m_limit())',
                           f'CHECK (value > {namespace}.a_limit())')
    ]
    normalized = deepcopy(catalog)
    normalized['constraints'][1]['definition'] = 'CHECK (value > a_limit())'
    result = signature(catalog, namespace)
    assert result == signature(normalized)
    assert [row['definition'] for row in result['constraints']] == [
        'CHECK (value > a_limit())', 'CHECK (value > m_limit())',
    ]


@pytest.mark.parametrize('change', [
    'duplicate_constraint', 'missing_constraint', 'constraint_definition', 'index_definition',
    'column_type', 'column_default', 'table_comment', 'column_comment', 'column_order',
])
def test_real_catalog_differences_remain_visible(catalog, change):
    changed = deepcopy(catalog)
    if change == 'duplicate_constraint':
        changed['constraints'].append(deepcopy(changed['constraints'][0]))
    elif change == 'missing_constraint':
        changed['constraints'].pop()
    elif change == 'constraint_definition':
        changed['constraints'][0]['definition'] = 'CHECK (value > 3)'
    elif change == 'index_definition':
        changed['indexes'][0]['indexdef'] += ' WHERE false'
    elif change == 'column_order':
        changed['columns'][0], changed['columns'][1] = changed['columns'][1], changed['columns'][0]
    else:
        field = {'column_type': 'type', 'column_default': 'default_expression',
                 'table_comment': 'table_comment', 'column_comment': 'column_comment'}[change]
        changed['columns'][0][field] = 'changed'
    assert signature(changed) != signature(catalog)
