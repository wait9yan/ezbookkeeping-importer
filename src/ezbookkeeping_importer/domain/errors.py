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
        label = {"connect": "连接", "initialize": "数据库初始化", "migrate": "迁移", "schema": "结构检查"}[stage]
        state = f", SQLSTATE={sqlstate}" if sqlstate else ""
        super().__init__(f"PostgreSQL {label}失败 [{code}{state}]：{reason}")


class LogPersistenceError(OSError):
    """运行日志失败；必须穿过业务异常处理边界，不改变已提交业务状态。"""


class StartupInterrupted(BaseException):
    """启动迁移收到停止请求；回滚当前版本并由 run 正常退出。"""
