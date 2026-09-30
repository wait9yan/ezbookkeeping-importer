"""单进程运行入口：信号先于依赖初始化，退出时关闭 Runtime。"""

from ..bootstrap import Runtime
from ..config import load_settings
from ..domain.errors import StartupInterrupted
from . import worker


def run_service(config_path: str) -> int:
    with worker.StopSignals() as stopping:
        settings = load_settings(config_path, command="run")
        if stopping.is_set():
            return 0
        try:
            runtime = Runtime(settings, command="run", stop_event=stopping)
        except StartupInterrupted:
            return 0
        try:
            worker.run(runtime, stop_event=stopping)
        finally:
            runtime.close()
    return 0
