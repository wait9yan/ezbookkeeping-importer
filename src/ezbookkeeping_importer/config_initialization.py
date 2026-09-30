"""首次 CLI 运行发布包内默认业务配置，不覆盖已有文件。"""

import errno
import os
import shutil
import tempfile
from pathlib import Path
from importlib.resources import files

from .config import ConfigurationError


DEFAULT_CONFIG_RESOURCE = files("ezbookkeeping_importer").joinpath("config.toml")


def initialize_default_config(path: Path) -> None:
    try:
        try:
            path.lstat()
            return
        except FileNotFoundError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".config-", suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            try:
                with DEFAULT_CONFIG_RESOURCE.open("rb") as source:
                    shutil.copyfileobj(source, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
                # 同一文件系统的硬链接仅发布完整文件，且不会覆盖并发创建的目标。
                try:
                    os.link(temporary_path, path)
                except FileExistsError:
                    pass
            finally:
                temporary_path.unlink()
    except OSError as exc:
        code = errno.errorcode.get(exc.errno or 0, "IO_ERROR")
        raise ConfigurationError(
            f"business configuration initialization failed ({code}); "
            "check data directory permissions and storage"
        ) from None

