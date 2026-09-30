"""Explicit migrate-only initialization, with mocked database connections and DDL."""

from contextlib import contextmanager
from urllib.parse import quote
from types import SimpleNamespace

import psycopg
from psycopg import sql
import pytest

from ezbookkeeping_importer.adapters.persistence import postgres
from ezbookkeeping_importer.config import command_capabilities
from ezbookkeeping_importer.domain.errors import DatabaseDiagnosticError

DSN = "postgresql://synthetic-user:synthetic-password@synthetic-host:15432/synthetic-db?sslmode=require&connect_timeout=7"


class Connection:
    def __init__(self, ddl_error=None):
        self.ddl_error = ddl_error
        self.statements = []
        self.closed = False
        self.transactions = []
        self.identifier_limit = "63"

    def execute(self, statement, params=()):
        self.statements.append(statement)
        if statement == "SHOW max_identifier_length":
            return SimpleNamespace(
                fetchone=lambda: {"max_identifier_length": self.identifier_limit}
            )
        if self.ddl_error:
            raise self.ddl_error
        return SimpleNamespace(fetchall=lambda: [])

    @contextmanager
    def transaction(self):
        self.transactions.append("begin")
        try:
            yield
        except Exception:
            self.transactions.append("rollback")
            raise
        else:
            self.transactions.append("commit")

    def close(self):
        self.closed = True


def connect_sequence(monkeypatch, results):
    calls = []

    def connect(dsn, **kwargs):
        calls.append((dsn, kwargs))
        result = results[len(calls) - 1]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(postgres.psycopg, "connect", connect)
    return calls


def missing():
    return psycopg.errors.InvalidCatalogName("synthetic-server-secret")


def test_missing_database_is_created_once_then_original_target_migrates(monkeypatch):
    maintenance, target = Connection(), Connection()
    calls = connect_sequence(monkeypatch, [missing(), maintenance, target])
    store = postgres.PostgresStore(DSN, create_database=True)
    assert len(calls) == 3
    assert all(dsn == DSN for dsn, _ in calls)
    assert calls[1][1]["dbname"] == "postgres"
    assert all(
        kwargs["autocommit"] is True and kwargs["connect_timeout"] == 7 for _, kwargs in calls
    )
    assert "dbname" not in calls[0][1] and "dbname" not in calls[2][1]
    assert len(maintenance.statements) == 2
    assert maintenance.statements[1].as_string() == 'CREATE DATABASE "synthetic-db"'
    assert maintenance.closed and maintenance.transactions == []
    assert target.statements == []  # Constructor never creates tables.
    store.migrate()
    assert target.transactions == ["begin", "commit"]
    assert any("CREATE TABLE schema_version" in str(s) for s in target.statements)
    store.close()
    assert target.closed


def test_existing_database_uses_single_connection_and_no_create(monkeypatch):
    target = Connection()
    calls = connect_sequence(monkeypatch, [target])
    store = postgres.PostgresStore(DSN, create_database=True)
    assert len(calls) == 1 and target.statements == []
    store.migrate()
    assert target.transactions == ["begin", "commit"]


@pytest.mark.parametrize(
    "error",
    [
        psycopg.errors.InvalidPassword("synthetic-server-secret"),
        psycopg.errors.InsufficientPrivilege("synthetic-server-secret"),
        psycopg.OperationalError(
            'could not translate host name "synthetic-host" to address: failed'
        ),
        psycopg.OperationalError("connection failed: Connection refused"),
        psycopg.OperationalError("connection failed: timeout expired"),
        psycopg.OperationalError("synthetic-unrecognized-secret"),
    ],
)
def test_non_missing_failure_never_attempts_maintenance(monkeypatch, error):
    calls = connect_sequence(monkeypatch, [error])
    with pytest.raises(DatabaseDiagnosticError):
        postgres.PostgresStore(DSN, create_database=True)
    assert len(calls) == 1


def test_default_store_does_not_create_missing_database(monkeypatch):
    calls = connect_sequence(monkeypatch, [missing()])
    with pytest.raises(DatabaseDiagnosticError, match="missing_database"):
        postgres.PostgresStore(DSN)
    assert len(calls) == 1


def test_no_explicit_dbname_cannot_be_guessed_for_initialization(monkeypatch):
    dsn = "postgresql://synthetic-user:synthetic-password@synthetic-host"
    calls = connect_sequence(monkeypatch, [missing()])
    with pytest.raises(DatabaseDiagnosticError) as caught:
        postgres.PostgresStore(dsn, create_database=True)
    assert caught.value.stage == "initialize"
    assert caught.value.code == "explicit_database_required"
    assert "dbname" in str(caught.value) and "synthetic" not in str(caught.value)
    assert len(calls) == 1


def test_existing_default_database_remains_compatible(monkeypatch):
    dsn = "postgresql://synthetic-user:synthetic-password@synthetic-host"
    calls = connect_sequence(monkeypatch, [Connection()])
    postgres.PostgresStore(dsn, create_database=True)
    assert len(calls) == 1


def test_create_permission_failure_is_safe_and_closes_maintenance(monkeypatch):
    maintenance = Connection(psycopg.errors.InsufficientPrivilege("synthetic-server-secret"))
    calls = connect_sequence(monkeypatch, [missing(), maintenance])
    with pytest.raises(DatabaseDiagnosticError) as caught:
        postgres.PostgresStore(DSN, create_database=True)
    assert caught.value.stage == "initialize" and caught.value.code == "permission_denied"
    assert "CREATE DATABASE" in str(caught.value)
    assert "synthetic" not in str(caught.value)
    assert maintenance.closed and len(calls) == 2


def test_maintenance_connection_failure_is_distinguished(monkeypatch):
    calls = connect_sequence(
        monkeypatch, [missing(), psycopg.errors.InvalidPassword("synthetic-server-secret")]
    )
    with pytest.raises(DatabaseDiagnosticError) as caught:
        postgres.PostgresStore(DSN, create_database=True)
    assert caught.value.stage == "initialize" and caught.value.code == "authentication_failed"
    assert "数据库初始化" in str(caught.value)
    assert "synthetic" not in str(caught.value)
    assert len(calls) == 2


def test_identifier_is_quoted_and_never_interpolated(monkeypatch):
    name = 'synthetic"; DROP DATABASE other; --'
    dsn = "postgresql://synthetic-user:synthetic-password@synthetic-host/" + quote(name, safe="")
    maintenance = Connection()
    connect_sequence(monkeypatch, [missing(), maintenance, Connection()])
    postgres.PostgresStore(dsn, create_database=True)
    query = maintenance.statements[1]
    assert isinstance(query, sql.Composed)
    assert query.as_string() == 'CREATE DATABASE "synthetic""; DROP DATABASE other; --"'


def test_concurrent_creation_still_reconnects_and_migrates(monkeypatch):
    maintenance = Connection(psycopg.errors.DuplicateDatabase("synthetic-server-secret"))
    target = Connection()
    calls = connect_sequence(monkeypatch, [missing(), maintenance, target])
    store = postgres.PostgresStore(DSN, create_database=True)
    store.migrate()
    assert len(calls) == 3 and maintenance.closed
    assert target.transactions == ["begin", "commit"]


def test_other_ddl_errors_remain_failures_and_are_not_auth_errors(monkeypatch):
    maintenance = Connection(psycopg.errors.SyntaxError("synthetic-server-secret"))
    calls = connect_sequence(monkeypatch, [missing(), maintenance])
    with pytest.raises(DatabaseDiagnosticError) as caught:
        postgres.PostgresStore(DSN, create_database=True)
    assert caught.value.code == "unknown" and caught.value.sqlstate == "42601"
    assert "synthetic" not in str(caught.value)
    assert maintenance.closed and len(calls) == 2


def test_reconnect_failure_does_not_fake_success(monkeypatch):
    maintenance = Connection()
    calls = connect_sequence(
        monkeypatch,
        [missing(), maintenance, psycopg.OperationalError("connection failed: timeout expired")],
    )
    with pytest.raises(DatabaseDiagnosticError) as caught:
        postgres.PostgresStore(DSN, create_database=True)
    assert caught.value.stage == "connect" and caught.value.code == "connection_timeout"
    assert maintenance.closed and len(calls) == 3


@pytest.mark.parametrize(
    "command",
    [
        "migrate",
        "status",
        "issues",
        "sync",
        "resolve",
        "restore-audit",
        "run",
        "doctor",
    ],
)
def test_only_migrate_has_database_creation_capability(command):
    assert ("create_database" in command_capabilities(command, "rules_only")) is (
        command == "migrate"
    )


@pytest.mark.parametrize(
    "name,limit", [("synthetic" * 10, "63"), ("测试" * 11, "63"), ("boundedx", "7")]
)
def test_server_identifier_byte_limit_prevents_truncated_database_creation(
    monkeypatch, name, limit
):
    dsn = "postgresql://synthetic-user:synthetic-password@synthetic-host/" + quote(name, safe="")
    maintenance = Connection()
    maintenance.identifier_limit = limit
    calls = connect_sequence(monkeypatch, [missing(), maintenance])
    with pytest.raises(DatabaseDiagnosticError) as caught:
        postgres.PostgresStore(dsn, create_database=True)
    assert caught.value.code == "database_name_too_long"
    assert maintenance.statements == ["SHOW max_identifier_length"]
    assert maintenance.closed and len(calls) == 2
    assert name not in str(caught.value) and "synthetic-password" not in str(caught.value)


def test_server_identifier_limit_allows_exact_byte_boundary(monkeypatch):
    name = "测试" * 10 + "abc"
    assert len(name.encode("utf-8")) == 63
    maintenance = Connection()
    connect_sequence(monkeypatch, [missing(), maintenance, Connection()])
    postgres.PostgresStore(
        "postgresql://synthetic-host/" + quote(name, safe=""), create_database=True
    )
    assert isinstance(maintenance.statements[1], sql.Composed)


def test_invalid_server_identifier_limit_is_explicit_and_does_not_create(monkeypatch):
    maintenance = Connection()
    maintenance.identifier_limit = "synthetic-invalid-value"
    connect_sequence(monkeypatch, [missing(), maintenance])
    with pytest.raises(DatabaseDiagnosticError) as caught:
        postgres.PostgresStore(DSN, create_database=True)
    assert caught.value.code == "invalid_identifier_limit"
    assert maintenance.statements == ["SHOW max_identifier_length"] and maintenance.closed
    assert "synthetic-invalid-value" not in str(caught.value)
