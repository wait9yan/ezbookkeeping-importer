"""只读日志尾随；文件身份与读取位置仅属于控制台进程。"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import BinaryIO

from ..application.events import safe_event

READ_BYTES = 65_536
MAX_LINE_BYTES = 262_144
ANCHOR_BYTES = 64


@dataclass(frozen=True)
class LogNotice:
    message: str
    level: str = "WARNING"


class LogTailer:
    def __init__(self, path: Path, *, start_at_end: bool = True):
        self.path = path
        self._start_at_end = start_at_end
        self._file: BinaryIO | None = None
        self._identity: tuple[int, int] | None = None
        self._partial = b""
        self._anchor = b""
        self._discard_partial = False
        self._problem: str | None = None

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None

    def _notice(self, key: str, message: str) -> list:
        if self._problem == key:
            return []
        self._problem = key
        return [LogNotice(message)]

    def _open(self):
        self._file = self.path.open("rb")
        info = os.fstat(self._file.fileno())
        self._identity = (info.st_dev, info.st_ino)
        self._partial = b""
        self._discard_partial = False
        if self._start_at_end:
            self._file.seek(0, os.SEEK_END)
            offset = self._file.tell()
            if offset:
                self._file.seek(offset - 1)
                self._discard_partial = self._file.read(1) != b"\n"
        self._start_at_end = False
        self._remember_anchor()

    def _remember_anchor(self):
        assert self._file is not None
        position = self._file.tell()
        self._file.seek(max(0, position - ANCHOR_BYTES))
        self._anchor = self._file.read(position - self._file.tell())
        self._file.seek(position)

    def _was_truncated(self, size: int) -> bool:
        assert self._file is not None
        position = self._file.tell()
        if size < position:
            return True
        if not self._anchor:
            return False
        # 检测两次读取之间 truncate 后又增长到原 offset 以上的情况。
        self._file.seek(position - len(self._anchor))
        observed = self._file.read(len(self._anchor))
        self._file.seek(position)
        return observed != self._anchor

    def _read(self) -> tuple[list, bool]:
        assert self._file is not None
        chunk = self._file.read(READ_BYTES)
        self._remember_anchor()
        lines = (self._partial + chunk).split(b"\n")
        self._partial = lines.pop()
        output: list = []
        for line in lines:
            if self._discard_partial:
                self._discard_partial = False
                continue
            if len(line) > MAX_LINE_BYTES:
                output.append(LogNotice("日志行超过读取上限，该行未显示"))
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or not isinstance(record.get("event"), str):
                    raise ValueError("invalid event record")
            except (UnicodeDecodeError, ValueError):
                output.append(LogNotice("日志行格式无效，该行未显示；后续日志继续读取"))
            else:
                output.append(safe_event(record))
        if len(self._partial) > MAX_LINE_BYTES:
            if not self._discard_partial:
                output.append(LogNotice("日志行超过读取上限，该行未显示"))
            self._partial = b""
            self._discard_partial = True
        return output, len(chunk) < READ_BYTES

    def poll(self) -> list[dict | LogNotice]:
        output: list[dict | LogNotice] = []
        try:
            if self._file is None:
                self._open()
            assert self._file is not None
            try:
                info = self.path.stat()
            except FileNotFoundError:
                # rename 与新文件创建之间先读完旧文件，保持原来的文件描述符。
                records, _ = self._read()
                output.extend(records)
                output.extend(self._notice("missing", "日志文件尚不存在，等待 worker 创建"))
                return output
            if self._identity != (info.st_dev, info.st_ino):
                records, drained = self._read()
                output.extend(records)
                if not drained:
                    return output
                if self._partial and not self._discard_partial:
                    output.append(LogNotice("日志轮转时旧文件末行不完整，该行未显示"))
                self.close()
                self._open()
                output.append(LogNotice("日志文件已轮转，继续读取新文件", "INFO"))
            elif self._was_truncated(info.st_size):
                self._file.seek(0)
                self._partial = b""
                self._discard_partial = False
                output.append(LogNotice("日志文件已截断，从头继续读取", "INFO"))
            records, _ = self._read()
            output.extend(records)
            if self._problem is not None:
                output.insert(0, LogNotice("日志读取已恢复", "INFO"))
                self._problem = None
        except FileNotFoundError:
            # 控制台先启动时，之后新建的文件必须从第一条事件读取。
            self._start_at_end = False
            output.extend(self._notice("missing", "日志文件尚不存在，等待 worker 创建"))
        except OSError as exc:
            output.extend(
                self._notice(
                    type(exc).__name__, f"日志读取失败（{type(exc).__name__}），将继续重试"
                )
            )
        return output
