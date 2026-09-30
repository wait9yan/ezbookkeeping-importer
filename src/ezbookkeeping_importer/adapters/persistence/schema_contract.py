"""生成器和运行时共享的 PostgreSQL 只读结构提取器。"""

from typing import Any
from operator import itemgetter
import re


def normalize_namespace(value: str, namespace: str) -> str:
    quoted = '"' + namespace.replace('"', '""') + '"'
    qualifier = re.compile(r'(?<![\w"])' + '(?:' + re.escape(quoted) + '|' +
                           re.escape(namespace) + r')\.')
    # SQL string literals are data, including namespace-looking text and E-string escapes.
    pieces = re.split(r"((?:[eE])?'(?:[^'\\]|\\.|'')*')", value)
    return ''.join(piece if index % 2 else qualifier.sub('', piece)
                   for index, piece in enumerate(pieces))


def schema_signature(connection, namespace: str) -> dict[str, Any]:
    def rows(query: str):
        return connection.execute(query, (namespace,)).fetchall()

    relations = rows("""SELECT c.relname,c.relkind FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s
        AND c.relkind IN ('r','p','v','m','S','f') ORDER BY c.relname""")
    types = rows('''SELECT t.typname,t.typtype FROM pg_type t
        JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname=%s
        AND t.typrelid=0 AND t.typelem=0 ORDER BY t.typname''')
    routines = rows('''SELECT p.proname,pg_get_function_identity_arguments(p.oid) AS arguments
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname=%s ORDER BY p.proname,arguments''')
    columns = rows("""SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod) AS type,
        a.attnotnull,a.attgenerated,pg_get_expr(d.adbin,d.adrelid) AS default_expression,
        obj_description(c.oid) AS table_comment,col_description(c.oid,a.attnum) AS column_comment
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        WHERE n.nspname=%s AND c.relkind IN ('r','p') ORDER BY c.relname,a.attnum""")
    # PG18 also exposes NOT NULL as contype=n; attnotnull above is canonical on both versions.
    constraints = rows("""SELECT c.relname,k.contype,pg_get_constraintdef(k.oid) AS definition
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND k.contype<>'n'
        ORDER BY c.relname,k.contype,pg_get_constraintdef(k.oid)""")
    indexes = rows("""SELECT tablename,indexname,indexdef FROM pg_indexes
        WHERE schemaname=%s ORDER BY tablename,indexname""")
    # Only expression fields can contain a namespace qualifier; never rewrite business comments.
    for items, fields in ((columns, ('type', 'default_expression')),
                          (constraints, ('definition',)), (indexes, ('indexdef',))):
        for item in items:
            for field in fields:
                value = item.get(field)
                if isinstance(value, str):
                    item[field] = normalize_namespace(value, namespace)
    # SQL ordering depends on the database collation; sort normalized values in Python.
    # The column query retains attnum order within each table, including dropped-column gaps.
    return {
        'relations': sorted(relations, key=itemgetter('relname', 'relkind')),
        'types': sorted(types, key=itemgetter('typname', 'typtype')),
        'routines': sorted(routines, key=itemgetter('proname', 'arguments')),
        'columns': sorted(columns, key=itemgetter('relname')),
        'constraints': sorted(constraints, key=itemgetter('relname', 'contype', 'definition')),
        'indexes': sorted(indexes, key=itemgetter('tablename', 'indexname', 'indexdef')),
    }
