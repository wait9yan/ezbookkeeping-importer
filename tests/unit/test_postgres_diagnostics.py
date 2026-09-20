"""Driver-boundary failures are synthetic; these tests never connect or migrate a database."""

from contextlib import contextmanager
from types import SimpleNamespace
import json
import traceback

import psycopg
import pytest

from ezbookkeeping_importer.adapters.persistence import postgres
from ezbookkeeping_importer.domain.errors import DatabaseDiagnosticError
from ezbookkeeping_importer.entrypoints import cli

DSN = "postgresql://private-user:private-password@private-host.test/private-database"
SECRET_TEXT = "private-user private-password private-host.test private-database"


@pytest.mark.parametrize(
    "error,code",
    [
        (psycopg.errors.InvalidPassword(SECRET_TEXT), "authentication_failed"),
        (psycopg.errors.InvalidCatalogName(SECRET_TEXT), "missing_database"),
        (psycopg.errors.InsufficientPrivilege(SECRET_TEXT), "permission_denied"),
        (psycopg.errors.InvalidAuthorizationSpecification(SECRET_TEXT), "authorization_failed"),
        (psycopg.errors.TooManyConnections(SECRET_TEXT), "too_many_connections"),
        (psycopg.errors.CannotConnectNow(SECRET_TEXT), "server_unavailable"),
        (psycopg.errors.ConnectionFailure(SECRET_TEXT), "connection_lost"),
        (psycopg.errors.QueryCanceled(SECRET_TEXT), "operation_cancelled"),
    ],
)
def test_sqlstate_is_preserved_without_server_text(error, code):
    diagnostic = postgres.database_diagnostic(error, "connect")
    assert diagnostic.code == code
    assert diagnostic.sqlstate == error.sqlstate
    assert error.sqlstate in str(diagnostic)
    for secret in SECRET_TEXT.split():
        assert secret not in str(diagnostic)
    assert DSN not in str(diagnostic)


@pytest.mark.parametrize(
    "message,code",
    [
        (
            'connection failed: FATAL:  password authentication failed for user "private-user"',
            "authentication_failed",
        ),
        ('FATAL: role "private-user" does not exist', "role_missing"),
        ('FATAL: database "private-database" does not exist', "missing_database"),
        (
            'could not translate host name "private-host.test" to address: nodename nor servname provided',
            "dns_failed",
        ),
        ("failed to resolve host 'private-host.test': a synthetic DNS error", "dns_failed"),
        (
            'connection to server at "private-host.test" failed: Connection refused\nIs the server running?',
            "connection_refused",
        ),
        (
            'connection to server at "private-host.test" failed: timeout expired',
            "connection_timeout",
        ),
        (
            'connection to server at "private-host.test" failed: SSL error: certificate verify failed',
            "ssl_failed",
        ),
        ("connection failed: server does not support SSL, but SSL was required", "ssl_failed"),
        (
            'FATAL: no pg_hba.conf entry for host "private-host.test", user "private-user"',
            "authorization_failed",
        ),
        ('FATAL: permission denied for database "private-database"', "permission_denied"),
        ("unrecognized driver problem " + SECRET_TEXT, "unknown"),
        (
            'FATAL: database "password authentication failed for user" does not exist',
            "missing_database",
        ),
        (
            "connection failed: Connection refused\nconnection failed: timeout expired",
            "multiple_causes",
        ),
    ],
)
def test_known_libpq_forms_classified_and_unknown_not_guessed(message, code):
    diagnostic = postgres.database_diagnostic(psycopg.OperationalError(message), "connect")
    assert diagnostic.code == code
    assert diagnostic.sqlstate is None
    assert message not in str(diagnostic)
    for secret in SECRET_TEXT.split():
        assert secret not in str(diagnostic)


def test_missing_database_diagnostic_identifies_explicit_migrate_initialization():
    diagnostic = postgres.database_diagnostic(
        psycopg.errors.InvalidCatalogName("private-database"), "connect"
    )
    assert "运行 migrate 可尝试创建" in str(diagnostic)
    assert "其他命令不会创建数据库" in str(diagnostic)
    assert "独立 importer 数据库" in str(diagnostic)


@pytest.mark.parametrize("suffix,timeout", [("", 10), ("?connect_timeout=7", 7)])
def test_connection_timeout_default_and_dsn_override(monkeypatch, suffix, timeout):
    called: dict = {}
    connection = SimpleNamespace(close=lambda: None)

    def connect(dsn, **kwargs):
        called.update(dsn=dsn, **kwargs)
        return connection

    monkeypatch.setattr(postgres.psycopg, "connect", connect)
    store = postgres.PostgresStore(DSN + suffix)
    assert called["dsn"] == DSN + suffix
    assert called["connect_timeout"] == timeout
    assert called["autocommit"] is True
    assert store.connection is connection


@pytest.mark.parametrize("timeout", ["0", "-1", "not-an-integer"])
def test_invalid_timeout_is_rejected_before_connect_without_echo(monkeypatch, timeout):
    monkeypatch.setattr(
        postgres.psycopg, "connect", lambda *a, **k: pytest.fail("must not connect")
    )
    with pytest.raises(DatabaseDiagnosticError) as caught:
        postgres.PostgresStore(DSN + "?connect_timeout=" + timeout)
    assert caught.value.code == "invalid_connect_timeout"
    assert "正整数秒" in str(caught.value)
    assert "private-" not in str(caught.value)


def test_invalid_dsn_is_actionable_without_echo(monkeypatch):
    monkeypatch.setattr(
        postgres.psycopg, "connect", lambda *a, **k: pytest.fail("must not connect")
    )
    with pytest.raises(DatabaseDiagnosticError) as caught:
        postgres.PostgresStore("not-a-valid-DSN " + SECRET_TEXT)
    assert caught.value.code == "invalid_connection_string"
    assert "EBKI_DATABASE_URL" in str(caught.value)
    assert "private-" not in str(caught.value)


def test_connect_error_has_safe_cli_and_traceback(monkeypatch, capsys):
    original = psycopg.OperationalError(
        'FATAL: database "private-database" does not exist ' + SECRET_TEXT
    )

    def fail_connect(*args, **kwargs):
        raise original

    monkeypatch.setattr(postgres.psycopg, "connect", fail_connect)
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: None)
    monkeypatch.setattr(cli, "Runtime", lambda *a, **k: postgres.PostgresStore(DSN))
    monkeypatch.setattr("sys.argv", ["ebki", "migrate"])
    assert cli.main() == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error_type"] == "DatabaseDiagnosticError"
    assert "missing_database" in error["message"]
    assert "private-" not in error["message"]
    try:
        postgres.PostgresStore(DSN)
    except DatabaseDiagnosticError as diagnostic:
        rendered = "".join(
            traceback.format_exception(type(diagnostic), diagnostic, diagnostic.__traceback__)
        )
        assert "private-database" not in rendered and "private-password" not in rendered


@pytest.mark.parametrize(
    "error,code",
    [
        (psycopg.errors.InsufficientPrivilege(SECRET_TEXT), "permission_denied"),
        (psycopg.errors.ConnectionFailure(SECRET_TEXT), "connection_lost"),
    ],
)
def test_migration_error_is_safe_and_transaction_rolls_back(monkeypatch, error, code):
    events = []

    class Connection:
        @contextmanager
        def transaction(self):
            events.append("begin")
            try:
                yield
            except Exception:
                events.append("rollback")
                raise

        def execute(self, *args):
            raise error

    monkeypatch.setattr(postgres.psycopg, "connect", lambda *a, **k: Connection())
    store = postgres.PostgresStore(DSN)
    with pytest.raises(DatabaseDiagnosticError) as caught:
        store.migrate()
    assert caught.value.stage == "migrate" and caught.value.code == code
    assert "private-" not in str(caught.value)
    assert events == ["begin", "rollback"]


def test_unrelated_programming_error_is_not_mislabeled(monkeypatch):
    class Connection:
        @contextmanager
        def transaction(self):
            yield

        def execute(self, *args):
            raise psycopg.errors.SyntaxError("synthetic bad SQL")

    monkeypatch.setattr(postgres.psycopg, "connect", lambda *a, **k: Connection())
    store = postgres.PostgresStore(DSN)
    with pytest.raises(psycopg.errors.SyntaxError):
        store.migrate()
