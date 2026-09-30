from contextlib import contextmanager
from pathlib import Path
import re
from threading import Event, Thread
import time
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.conninfo import conninfo_to_dict

from ...domain.errors import Conflict, DatabaseDiagnosticError, StartupInterrupted
from psycopg.types.json import Jsonb

from .migrations import apply_migration, load_migrations, schema_error
from .schema_contract import schema_signature

SCHEMA = Path(__file__).with_name("schema.sql")
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10

DIAGNOSTICS = {
    "authentication_failed": "密码认证失败；检查 EBKI_DATABASE_URL 中的用户名和密码，并向数据库管理员核对认证配置。",
    "authorization_failed": "连接身份未获授权；核对数据库角色、认证方式及 pg_hba.conf 规则。",
    "role_missing": "数据库角色不存在；核对 EBKI_DATABASE_URL 中的角色名，并由管理员确认角色已创建。",
    "missing_database": "目标数据库不存在；核对 EBKI_DATABASE_URL 中明确指定的数据库名，run 或 migrate 可尝试创建该独立 importer 数据库；查询与诊断命令不会创建数据库。",
    "dns_failed": "数据库地址无法解析；检查 EBKI_DATABASE_URL 的主机名，以及当前进程或容器的 DNS 和网络环境。",
    "connection_refused": "数据库连接被拒绝；检查服务是否运行、监听端口、容器网络和端口映射。",
    "connection_timeout": "数据库连接超时；检查网络可达性、防火墙、监听端口及 connect_timeout 设置。",
    "ssl_failed": "TLS/SSL 连接校验失败或设置不兼容；检查 sslmode、证书路径、证书信任链及服务器 SSL 支持。",
    "permission_denied": "数据库权限不足；连接阶段核对 CONNECT 权限，初始化核对 schema CREATE；升级通常需要对象所有者权限，可由管理员先执行 migrate。",
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
    def __init__(self, dsn: str, *, create_database: bool = False, stop_event=None):
        self._worker_locked = False
        timeout = connection_timeout(dsn)
        try:
            self.connection = self._connect(dsn, timeout)
        except DatabaseDiagnosticError as exc:
            if not create_database or exc.code != "missing_database":
                raise
            self._initialize_database(dsn, timeout, stop_event=stop_event)
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
    def _initialize_database(cls, dsn: str, timeout: int, *, stop_event=None):
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
                # Serialize creators in the maintenance database, including pg_database's
                # internal unique indexes. Do not treat unrelated UniqueViolation as success.
                deadline = time.monotonic() + min(timeout, DEFAULT_CONNECT_TIMEOUT_SECONDS)
                while True:
                    cls._check_stop(stop_event)
                    if maintenance.execute(
                        "SELECT pg_try_advisory_lock(780419) AS locked"
                    ).fetchone()["locked"]:
                        break
                    if time.monotonic() >= deadline:
                        raise DatabaseDiagnosticError(
                            "initialize", "initialization_busy",
                            "其他实例正在创建数据库；本次等待已到期，请稍后重启。"
                        )
                    time.sleep(0.05)
                existing = maintenance.execute(
                    "SELECT 1 FROM pg_database WHERE datname=%s", (target,)
                ).fetchone()
                if not existing:
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

    def _schema_signature(self, namespace: str) -> dict:
        return schema_signature(self.connection, namespace)

    def _schema_state(self, migrations) -> int:
        row = self.one("SELECT current_schema() AS namespace")
        namespace = row["namespace"]
        if not namespace:
            raise schema_error("missing_schema", "连接的 search_path 没有可用 schema；由管理员准备目标 schema。")
        actual = self._schema_signature(namespace)
        if not any(actual[key] for key in ("relations", "types", "routines")):
            return 0
        if not any(r["relname"] == "schema_version" and r["relkind"] == "r"
                   for r in actual["relations"]):
            raise schema_error("schema_drift", "schema contract differs；目标 schema 存在其他应用对象或缺少迁移历史。")
        versions = self.all("SELECT version FROM schema_version ORDER BY version")
        numbers = [item["version"] for item in versions]
        if not numbers or any(type(value) is not int or value <= 0 for value in numbers):
            raise schema_error("invalid_migration_history", "数据库迁移历史为空或版本非法；未修改数据。")
        current = max(numbers)
        if current > len(migrations):
            raise schema_error("schema_ahead", "数据库结构版本高于当前程序；使用兼容的新版本，不能自动降级。")
        if numbers != list(range(1, current + 1)):
            raise schema_error("invalid_migration_history", "数据库迁移历史缺项或顺序异常；未修改数据。")
        if actual != migrations[current - 1].signature:
            raise schema_error("schema_drift", "schema contract differs；数据库结构与已发布版本不一致，未推测修复。")
        if current > 1:
            history = self.all("SELECT version,script_sha256 FROM schema_version ORDER BY version")
            if any(item["script_sha256"] != migrations[item["version"] - 1].checksum
                   for item in history):
                raise schema_error("migration_checksum_mismatch", "已应用迁移的脚本摘要不一致；历史脚本不可改写。")
        return current

    def check_schema(self) -> int:
        """查询/诊断只读检查，既不建库，也不修改旧结构。"""
        migrations = load_migrations()
        try:
            with self.connection.transaction():
                # Consistent catalog/history read without a CREATE privilege requirement.
                self.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                current = self._schema_state(migrations)
                if current != len(migrations):
                    raise schema_error("schema_not_ready", "数据库结构未就绪；先停止旧 worker，再运行 migrate 或新版本 run。")
                return current
        except psycopg.Error as exc:
            raise database_diagnostic(exc, "schema") from None

    @staticmethod
    def _check_stop(stop_event):
        if stop_event is not None and stop_event.is_set():
            raise StartupInterrupted()

    @contextmanager
    def _migration_cancellation(self, stop_event):
        if stop_event is None:
            yield
            return
        finished = Event()
        errors = []

        def watch():
            while not finished.wait(0.05):
                if stop_event.is_set():
                    try:
                        self.connection.cancel_safe(timeout=1)
                    except psycopg.Error as exc:
                        errors.append(exc)
                    return

        monitor = Thread(target=watch, name="ebki-migration-cancel", daemon=True)
        monitor.start()
        try:
            yield
        finally:
            finished.set()
            monitor.join(2)
        if errors:
            self._check_stop(stop_event)
            raise database_diagnostic(errors[0], "migrate") from None

    def migrate(self, *, stop_event=None, hold_worker: bool = False, progress=None) -> int:
        """每个版本独立事务；持有 worker 锁阻止旧程序在 DDL 期间工作。"""
        migrations = load_migrations()
        self._check_stop(stop_event)
        acquired = not self._worker_locked
        try:
            if not self.lock_worker():
                raise Conflict("another worker is already running; stop it before schema migration")
            with self._migration_cancellation(stop_event):
                while True:
                    self._check_stop(stop_event)
                    with self.transaction():
                        self.execute("SELECT pg_advisory_xact_lock(780416)")
                        current = self._schema_state(migrations)
                        self._check_stop(stop_event)
                        if current == len(migrations):
                            return current
                        migration = migrations[current]
                        if progress is not None:
                            progress("database_migration_started", migration.version)
                        apply_migration(self.connection, migration, migrations[0].checksum)
                        if self._schema_state(migrations) != migration.version:
                            raise schema_error("invalid_migration", "迁移结果版本不符；当前版本已回滚。")
                        self._check_stop(stop_event)
                    if progress is not None:
                        progress("database_migration_completed", migration.version)
        except psycopg.Error as exc:
            self._check_stop(stop_event)
            raise database_diagnostic(exc, "migrate") from None
        finally:
            if acquired and not hold_worker:
                self.unlock_worker()

    @contextmanager
    def transaction(self):
        with self.connection.transaction():
            # Every short mutation transaction takes this before row locks. Network calls
            # live outside these transactions; reconciliation publication uses the same order.
            self.execute("SELECT pg_advisory_xact_lock(780418)")
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

    def is_connection_usable(self) -> bool:
        return not self.connection.closed and not self.connection.broken

    def lock_worker(self) -> bool:
        if self._worker_locked:
            return True
        self._worker_locked = bool(self.one("SELECT pg_try_advisory_lock(780417) AS locked")["locked"])
        return self._worker_locked

    def unlock_worker(self):
        if self._worker_locked and self.is_connection_usable():
            self.one("SELECT pg_advisory_unlock(780417) AS unlocked")
        self._worker_locked = False

    def close(self):
        self.connection.close()
