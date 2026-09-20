from contextlib import contextmanager
from pathlib import Path
import re
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.conninfo import conninfo_to_dict

from ...domain.errors import DatabaseDiagnosticError
from psycopg.types.json import Jsonb

SCHEMA = Path(__file__).with_name("schema.sql")
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10

DIAGNOSTICS = {
    "authentication_failed": "密码认证失败；检查 EBKI_DATABASE_URL 中的用户名和密码，并向数据库管理员核对认证配置。",
    "authorization_failed": "连接身份未获授权；核对数据库角色、认证方式及 pg_hba.conf 规则。",
    "role_missing": "数据库角色不存在；核对 EBKI_DATABASE_URL 中的角色名，并由管理员确认角色已创建。",
    "missing_database": "目标数据库不存在；核对 EBKI_DATABASE_URL 中明确指定的数据库名，运行 migrate 可尝试创建该独立 importer 数据库；其他命令不会创建数据库。",
    "dns_failed": "数据库地址无法解析；检查 EBKI_DATABASE_URL 的主机名，以及当前进程或容器的 DNS 和网络环境。",
    "connection_refused": "数据库连接被拒绝；检查服务是否运行、监听端口、容器网络和端口映射。",
    "connection_timeout": "数据库连接超时；检查网络可达性、防火墙、监听端口及 connect_timeout 设置。",
    "ssl_failed": "TLS/SSL 连接校验失败或设置不兼容；检查 sslmode、证书路径、证书信任链及服务器 SSL 支持。",
    "permission_denied": "数据库权限不足；连接阶段核对 CONNECT 权限，迁移阶段核对目标 schema 的 USAGE/CREATE 权限。",
    "connection_lost": "数据库连接不可用或已中断；检查服务状态与网络，确认恢复后再执行命令。",
    "server_unavailable": "数据库当前无法接受连接；检查启动、恢复或停机状态。",
    "too_many_connections": "数据库连接数量已达限制；检查连接占用与服务器连接配额。",
    "operation_cancelled": "数据库操作被取消；核对 statement_timeout、锁等待和管理员取消记录。",
    "multiple_causes": "不同连接尝试返回了不同失败原因；核对多主机连接设置、网络和服务端日志，当前无法确定单一原因。",
    "unknown": "无法从可用错误码可靠确定原因；核对 EBKI_DATABASE_URL、网络及服务状态，并查看数据库服务端日志。",
}
SQLSTATE_CODES = {
    "28P01": "authentication_failed",
    "3D000": "missing_database",
    "42501": "permission_denied",
    "08001": "connection_lost",
    "08003": "connection_lost",
    "08006": "connection_lost",
    "57P01": "server_unavailable",
    "57P02": "server_unavailable",
    "57P03": "server_unavailable",
    "53300": "too_many_connections",
    "57014": "operation_cancelled",
}
# Startup/libpq errors often lack SQLSTATE. These known diagnostic forms are used only
# for classification, never copied into messages. Unrecognized/localized text remains unknown.
TEXT_CODES = {
    "authentication_failed": r"(?:^|FATAL:\s*|failed:\s*)password authentication failed for user\s+\"[^\"]*\"",
    "role_missing": r"(?:^|FATAL:\s*)role\s+\"[^\"]*\" does not exist",
    "missing_database": r"(?:^|FATAL:\s*)database\s+\"[^\"]*\" does not exist",
    "dns_failed": r"(?:^|failed:\s*)(?:could not translate host name\s+\"[^\"]*\" to address|failed to resolve host\b)",
    "connection_refused": r"(?:^|failed:\s*)Connection refused(?:\s|$)",
    "connection_timeout": r"(?:^|failed:\s*)(?:connection timeout expired|timeout expired|Connection timed out)(?:\s|$)",
    "ssl_failed": r"(?:^|failed:\s*)(?:SSL error:|server does not support SSL|root certificate file\b|could not read root certificate file\b|certificate verify failed\b)",
    "authorization_failed": r"(?:^|FATAL:\s*)no pg_hba.conf entry for host\b",
    "permission_denied": r"(?:^|FATAL:\s*|ERROR:\s*)permission denied for (?:database|schema)\b",
}


def database_diagnostic(error: psycopg.Error, stage: str) -> DatabaseDiagnosticError:
    raw_state = error.sqlstate
    sqlstate = (
        raw_state
        if isinstance(raw_state, str) and re.fullmatch(r"[A-Z0-9]{5}", raw_state)
        else None
    )
    code = SQLSTATE_CODES.get(sqlstate or "")
    if code is None or code == "connection_lost":
        matches = {
            name
            for name, pattern in TEXT_CODES.items()
            if re.search(pattern, str(error), re.IGNORECASE | re.MULTILINE)
        }
        if len(matches) > 1:
            code = "multiple_causes"
        elif matches:
            code = matches.pop()
        elif sqlstate == "28000":
            code = "authorization_failed"
    code = code or "unknown"
    reason = DIAGNOSTICS[code]
    if stage == "initialize" and code == "permission_denied":
        reason = "数据库初始化权限不足；当前角色需有维护库 CONNECT 和 CREATE DATABASE 权限，或由管理员预先创建目标数据库。"
    return DatabaseDiagnosticError(stage, code, reason, sqlstate)


def connection_timeout(dsn: str) -> int:
    try:
        options = conninfo_to_dict(dsn)
    except psycopg.ProgrammingError:
        raise DatabaseDiagnosticError(
            "connect",
            "invalid_connection_string",
            "EBKI_DATABASE_URL 不是合法的 PostgreSQL 连接串；检查连接参数名称与格式。",
        ) from None
    configured = options.get("connect_timeout")
    if configured is None:
        return DEFAULT_CONNECT_TIMEOUT_SECONDS
    try:
        timeout = int(configured)
    except ValueError:
        timeout = 0
    if timeout <= 0:
        raise DatabaseDiagnosticError(
            "connect",
            "invalid_connect_timeout",
            "EBKI_DATABASE_URL 中的 connect_timeout 必须是正整数秒；省略时使用 10 秒。",
        ) from None
    return timeout


class PostgresStore:
    def __init__(self, dsn: str, *, create_database: bool = False):
        timeout = connection_timeout(dsn)
        try:
            self.connection = self._connect(dsn, timeout)
        except DatabaseDiagnosticError as exc:
            if not create_database or exc.code != "missing_database":
                raise
            self._initialize_database(dsn, timeout)
            # A successful CREATE (including a concurrent creator) is not enough: reconnect
            # to the original, exact target before the caller can execute migrations.
            self.connection = self._connect(dsn, timeout)

    @staticmethod
    def _connect(dsn: str, timeout: int, *, maintenance: bool = False):
        options: dict[str, Any] = {"dbname": "postgres"} if maintenance else {}
        try:
            return psycopg.connect(
                dsn, autocommit=True, row_factory=dict_row, connect_timeout=timeout, **options
            )
        except (
            psycopg.OperationalError,
            psycopg.errors.InvalidCatalogName,
            psycopg.errors.InsufficientPrivilege,
        ) as exc:
            raise database_diagnostic(exc, "initialize" if maintenance else "connect") from None

    @classmethod
    def _initialize_database(cls, dsn: str, timeout: int):
        target = conninfo_to_dict(dsn).get("dbname")
        if not isinstance(target, str) or not target:
            raise DatabaseDiagnosticError(
                "initialize",
                "explicit_database_required",
                "自动创建数据库需要在 EBKI_DATABASE_URL 中明确填写 dbname；不能使用默认用户名推测数据库名。",
            ) from None
        maintenance = cls._connect(dsn, timeout, maintenance=True)
        try:
            try:
                cls._validate_database_name(maintenance, target)
                maintenance.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
            except psycopg.errors.DuplicateDatabase:
                # Another migrate process created the same explicitly requested database.
                # The caller still reconnects and runs its normal transactional migration.
                pass
            except psycopg.Error as exc:
                raise database_diagnostic(exc, "initialize") from None
        finally:
            maintenance.close()

    @staticmethod
    def _validate_database_name(maintenance, target: str):
        row = maintenance.execute("SHOW max_identifier_length").fetchone()
        try:
            limit = int(row["max_identifier_length"])
            if limit <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise DatabaseDiagnosticError(
                "initialize",
                "invalid_identifier_limit",
                "无法读取服务器有效的 max_identifier_length；未尝试创建数据库，请检查服务器配置。",
            ) from None
        if len(target.encode("utf-8")) > limit:
            raise DatabaseDiagnosticError(
                "initialize",
                "database_name_too_long",
                "EBKI_DATABASE_URL 中的 dbname 超过服务器 max_identifier_length 字节限制；未创建截断名称的数据库，请明确配置更短的目标名称。",
            ) from None

    def migrate(self):
        try:
            with self.transaction():
                self.execute("SELECT pg_advisory_xact_lock(780416)")
                self.execute(SCHEMA.read_text())
        except (psycopg.OperationalError, psycopg.errors.InsufficientPrivilege) as exc:
            raise database_diagnostic(exc, "migrate") from None

    @contextmanager
    def transaction(self):
        with self.connection.transaction():
            yield self

    def execute(self, sql: str, params: tuple = ()):
        return self.connection.execute(
            sql,
            tuple(Jsonb(value) if isinstance(value, (dict, list)) else value for value in params),
        )

    def one(self, sql: str, params: tuple = ()):
        return self.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()):
        return self.execute(sql, params).fetchall()

    def audit(self, event: str, entity_id: str, data: dict):
        self.execute(
            "INSERT INTO audit_events(event,entity_id,data) VALUES (%s,%s,%s)",
            (event, entity_id, Jsonb(data)),
        )

    def issue(self, code: str, entity_id: str, data: dict):
        self.execute(
            """INSERT INTO issues(code,entity_id,data) VALUES (%s,%s,%s)
            ON CONFLICT(code,entity_id) DO UPDATE SET data=excluded.data, resolved=false,
            updated_at=now()""",
            (code, entity_id, Jsonb(data)),
        )

    def lock_worker(self) -> bool:
        return self.one("SELECT pg_try_advisory_lock(780417) AS locked")["locked"]

    def close(self):
        self.connection.close()
