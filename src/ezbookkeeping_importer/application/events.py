"""业务事件的唯一字段与展示契约；不创建日志文件，不参与业务决定。"""

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import logging
import hashlib
from pathlib import Path
import re
import time

from ..domain.errors import LogPersistenceError

EVENT_LABELS = {
    "database_preparing": "正在准备数据库",
    "database_migration_started": "开始升级数据库结构",
    "database_migration_completed": "数据库结构版本已提交",
    "database_ready": "数据库结构已就绪",
    "worker_starting": "服务启动中",
    "worker_started": "服务已就绪",
    "worker_stop_requested": "已收到停止请求",
    "worker_stopped": "服务已停止",
    "worker_failed": "服务异常退出",
    "worker_database_disconnected": "数据库连接已断开",
    "sync_started": "开始采集邮件",
    "sync_completed": "邮件采集完成",
    "sync_failed": "邮件采集未完成",
    "cycle_failed": "处理周期异常",
    "cycle_recovered": "处理周期已恢复",
    "mail_scan_started": "开始扫描邮箱",
    "mail_scan_completed": "邮箱扫描登记完成",
    "mail_scan_failed": "邮箱扫描失败",
    "collection_progress": "邮件采集进度",
    "collection_completed": "本轮采集汇总",
    "mail_batch_failed": "邮件批次处理失败",
    "mail_item_failed": "银行邮件采集失败",
    "parse_started": "开始解析",
    "parse_progress": "解析进度",
    "parse_completed": "本轮解析汇总",
    "report_accepted": "银行报告已接纳",
    "report_duplicate": "重复报告已关联",
    "report_blocked": "银行报告待处理",
    "parse_failed": "邮件解析失败",
    "classification_started": "开始分类",
    "classification_progress": "分类进度",
    "classification_completed": "本轮分类汇总",
    "transaction_blocked": "交易待处理",
    "classification_failed": "交易分类失败",
    "duplicate_check_failed": "账本查重查询未完成",
    "existing_marker_found": "发现既有来源标记",
    "write_tasks_queued": "写入任务已排队",
    "write_preflight_blocked": "写入预检未通过",
    "write_task_cancelled": "过期写入任务已取消",
    "write_attempt_registered": "写入尝试已登记",
    "write_response_received": "已收到账本响应",
    "write_rejected": "账本明确拒绝写入",
    "write_result_unknown": "写入结果待核实",
    "write_verified": "账本写入已核实",
    "existing_link_restored": "既有账本关联已恢复",
    "settlement_already_applied": "结算目标已达成",
    "write_interrupted_recovered": "中断写入转入核实",
    "write_verification_pending": "账本结果仍待核实",
    "reconciliation_started": "开始核对月账单",
    "reconciliation_published": "核对结果已发布",
    "reconciliation_queries_failed": "部分账本查询未成功",
    "reconciliation_stale": "本轮核对结果已过期",
    "reconciliation_failed": "本轮核对未完成",
    "report_exported": "核对文件已生成",
    "report_export_failed": "核对文件生成失败",
    "command_result": "命令处理结果",
}

TEXT_FIELDS = frozenset(
    "run_id stage task_type transaction_id ledger_transaction_id email_id report_key report_row_key source_id folder scan_mode reason_code error_code error_type next_action parser_version report_type completion_method trigger next_check_at mode reason command outcome entity_type entity_id response_status".split()
)
INTEGER_FIELDS = frozenset(
    "task_id source_item_id decision_version attempt_id duration_ms total processed collected skipped failed awaiting_acceptance accepted duplicate blocked ignored queued rule_matched ai_matched unmatched verification_pending duplicate_candidates returned_uid_count new_source_count affected_count row_count created_transaction_count candidate_count result_count settlement_queued_count failed_count repeat_count version".split()
)
COUNT_FIELDS = frozenset({"counts", "match_counts", "ledger_check_counts"})
BOOLEAN_FIELDS = frozenset({"publication_committed", "retry_scheduled"})
SAFE_FIELDS = TEXT_FIELDS | INTEGER_FIELDS | COUNT_FIELDS | BOOLEAN_FIELDS
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]+$")
_CONTEXT: ContextVar[dict] = ContextVar("event_context", default={})
_REPEAT_WINDOW = 300.0
_PROGRESS_INTERVAL = 5.0
_MAX_BLOCKED_KEYS = 4096
_blocked: OrderedDict[tuple, tuple[tuple, float]] = OrderedDict()


def event_title(event: str) -> str:
    return EVENT_LABELS.get(event, event if TOKEN.fullmatch(event) else "无法识别的事件")


def safe_event(value: dict) -> dict:
    """剔除任意请求、响应、异常文本和业务内容；嵌套结构只允许非负计数。"""
    event = value.get("event", "unrecognized_event")
    if not isinstance(event, str) or not TOKEN.fullmatch(event):
        event = "unrecognized_event"
    stamp = value.get("time")
    try:
        if (
            not isinstance(stamp, str)
            or not stamp.isprintable()
            or datetime.fromisoformat(stamp).tzinfo is None
        ):
            stamp = None
    except ValueError:
        stamp = None
    level = value.get("level")
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        level = "UNKNOWN"
    result = {"time": stamp, "level": level, "event": event}
    value = dict(value)
    for old, new in (
        ("job_id", "task_id"),
        ("source_row_id", "transaction_id"),
        ("target_id", "ledger_transaction_id"),
    ):
        if new not in value and old in value:
            value[new] = value[old]
    for key in SAFE_FIELDS:
        item = value.get(key)
        if key in INTEGER_FIELDS and type(item) is int and item >= 0:
            result[key] = item
        elif key in BOOLEAN_FIELDS and type(item) is bool:
            result[key] = item
        elif key in TEXT_FIELDS and isinstance(item, (str, int)) and not isinstance(item, bool):
            text = str(item)
            if key in {
                "error_code",
                "error_type",
                "reason_code",
                "next_action",
                "stage",
                "task_type",
                "completion_method",
                "trigger",
                "mode",
                "reason",
                "response_status",
            } and not TOKEN.fullmatch(text):
                continue
            result[key] = "".join(c for c in text[:256] if c.isprintable())
        elif key in COUNT_FIELDS and isinstance(item, dict):
            result[key] = {
                name: count
                for name, count in item.items()
                if isinstance(name, str)
                and TOKEN.fullmatch(name)
                and type(count) is int
                and count >= 0
            }
    frames = value.get("frames")
    if isinstance(frames, list):
        result["frames"] = [
            {"file": Path(frame["file"]).name, "function": frame["function"], "line": frame["line"]}
            for frame in frames
            if isinstance(frame, dict)
            and isinstance(frame.get("file"), str)
            and isinstance(frame.get("function"), str)
            and TOKEN.fullmatch(frame["function"])
            and type(frame.get("line")) is int
        ]
    return result


@contextmanager
def event_context(**fields):
    token = _CONTEXT.set({**_CONTEXT.get(), **fields})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def emit(event: str, *, level: int = logging.INFO, **fields):
    logger = logging.getLogger("ebki")
    # Direct library/CLI use neither creates a file nor invokes logging.lastResort.
    if not logger.handlers:
        return
    record = safe_event(
        {
            "event": event,
            "time": datetime.now(timezone.utc).isoformat(),
            "level": logging.getLevelName(level),
            **_CONTEXT.get(),
            **fields,
        }
    )
    try:
        logger.log(level, event, extra={"event_data": record})
    except LogPersistenceError:
        raise
    except Exception:
        raise LogPersistenceError("runtime log persistence failed") from None


def failure_fields(error: Exception, code: str, stage: str) -> dict:
    fields: dict = {
        "error_type": type(error).__name__,
        "error_code": str(getattr(error, "code", code)),
        "stage": stage,
    }
    frames = []
    trace = error.__traceback__
    while trace:
        frames.append(
            {
                "file": Path(trace.tb_frame.f_code.co_filename).name,
                "function": trace.tb_frame.f_code.co_name,
                "line": trace.tb_lineno,
            }
        )
        trace = trace.tb_next
    fields["frames"] = frames[-8:]
    return fields


def failure_identity(error: Exception) -> str:
    """仅供进程内降噪；不把异常原文或该指纹写入事件。"""
    return hashlib.sha256((type(error).__name__ + ":" + str(error)).encode()).hexdigest()


def blocked(event: str, *, identity: str, state: str | None = None, **fields):
    key = (event, identity, fields.get("decision_version"))
    signature = (
        state,
        fields.get("reason_code"),
        fields.get("error_code"),
        fields.get("error_type"),
    )
    now = time.monotonic()
    previous = _blocked.get(key)
    level = (
        logging.WARNING
        if previous is None or previous[0] != signature or now - previous[1] >= _REPEAT_WINDOW
        else logging.DEBUG
    )
    if level == logging.WARNING:
        _blocked[key] = (signature, now)
        _blocked.move_to_end(key)
        while len(_blocked) > _MAX_BLOCKED_KEYS:
            _blocked.popitem(last=False)
    emit(event, level=level, **fields)


class Progress:
    def __init__(self, stage: str, total: int = 0, **fields):
        self.stage = stage
        self.total = total
        self.fields = fields
        self.counts: dict[str, int] = {}
        self.processed = 0
        self.started = time.monotonic()
        self.last_emitted = self.started
        self.last_processed = 0

    def advance(self, count: int = 1, **counts: int):
        self.processed += count
        for key, value in counts.items():
            self.counts[key] = self.counts.get(key, 0) + value
        now = time.monotonic()
        if self.processed != self.last_processed and now - self.last_emitted >= _PROGRESS_INTERVAL:
            emit(self.stage + "_progress", **self.summary())
            self.last_emitted = now
            self.last_processed = self.processed

    def summary(self) -> dict:
        return {
            **self.fields,
            "stage": self.stage,
            "total": self.total,
            "processed": self.processed,
            "counts": dict(self.counts),
            "duration_ms": int((time.monotonic() - self.started) * 1000),
        }

    def finish(self):
        emit(self.stage + "_completed", **self.summary())


def format_event(value: dict) -> str:
    """纯文本中文摘要；Rich和交互控制台共享，输入始终先过安全投影。"""
    record = safe_event(value)
    parts = [event_title(record["event"])]
    labels = {
        "stage": "阶段",
        "task_id": "任务",
        "task_type": "任务类型",
        "transaction_id": "交易",
        "email_id": "邮件",
        "source_item_id": "来源项",
        "report_key": "报告",
        "folder": "文件夹",
        "error_code": "错误代码",
        "reason_code": "待处理原因",
        "next_action": "下一步",
        "completion_method": "完成方式",
        "affected_count": "影响数量",
    }
    for key, title in labels.items():
        if key in record:
            parts.append(f"{title}={record[key]}")
    if "processed" in record:
        parts.append(f"进度={record['processed']}/{record.get('total', '?')}")
    for key, title in (
        ("counts", "汇总"),
        ("match_counts", "银行匹配"),
        ("ledger_check_counts", "账本核对"),
    ):
        if record.get(key):
            parts.append(
                title + "=" + ", ".join(f"{name}:{count}" for name, count in record[key].items())
            )
    if "duration_ms" in record:
        parts.append(f"耗时={record['duration_ms'] / 1000:.1f}秒")
    return " | ".join(parts)
