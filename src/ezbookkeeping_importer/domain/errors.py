class ImporterError(Exception):
    """可持久化并对用户呈现的明确业务错误。"""


class Conflict(ImporterError):
    pass


class LedgerError(ImporterError):
    """The remote result cannot be established (including transport failures)."""


class LedgerRejected(LedgerError):
    """The API explicitly rejected the operation before a successful write."""

    def __init__(self, code: int):
        self.code = code
        super().__init__(f"ezBookkeeping rejected request: code={code}")


class DatabaseDiagnosticError(ImporterError):
    """Sanitized database boundary failure; never stores the driver exception or DSN."""

    def __init__(self, stage: str, code: str, reason: str, sqlstate: str | None = None):
        self.stage = stage
        self.code = code
        self.sqlstate = sqlstate
        label = {"connect": "连接", "initialize": "数据库初始化", "migrate": "迁移"}[stage]
        state = f", SQLSTATE={sqlstate}" if sqlstate else ""
        super().__init__(f"PostgreSQL {label}失败 [{code}{state}]：{reason}")
