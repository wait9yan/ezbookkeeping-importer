"""仅镜像使用的权限准备入口；业务配置仍由普通 CLI 初始化。"""
import errno
import json
import os
import stat
import sys
from pathlib import Path

from .cli import CommandInterrupted, build_parser, maintenance_signals

APPLICATION_UID = APPLICATION_GID = 10001
DATA_PATH = Path('/app/data')
CLI_PATH = '/app/.venv/bin/ebki'


def _allowed(info: os.stat_result, required: int) -> bool:
    shift = 6 if info.st_uid == APPLICATION_UID else 3 if info.st_gid == APPLICATION_GID else 0
    return ((stat.S_IMODE(info.st_mode) >> shift) & required) == required


def _prepare(fd: int, required: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
        raise OSError(errno.EINVAL, 'unsupported data entry')
    if _allowed(info, required):
        return
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise OSError(errno.EINVAL, 'cannot modify multiply linked data entry')
    if (info.st_uid, info.st_gid) != (APPLICATION_UID, APPLICATION_GID):
        os.fchown(fd, APPLICATION_UID, APPLICATION_GID)
    mode = stat.S_IMODE(os.fstat(fd).st_mode)
    if (mode >> 6) & required != required:
        os.fchmod(fd, mode | (required << 6))


def _open(name: str | Path, parent: int | None = None, *, directory: bool = False) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if directory:
        flags |= os.O_DIRECTORY
    return os.open(name, flags, dir_fd=parent)


def _tree(parent: int, name: str, kind: str, *, top: bool = False) -> None:
    fd = _open(name, parent, directory=top)
    try:
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        writable = (kind == 'reports' and name.endswith('.tmp')) or (kind == 'logs' and name == 'worker.jsonl')
        _prepare(fd, 7 if directory else 6 if writable else 4)
        if directory:
            for child in os.listdir(fd):
                _tree(fd, child, kind)
    finally:
        os.close(fd)


def prepare_data(path: Path, *, worker: bool, default_config: bool) -> None:
    if not worker and not default_config:
        return
    path.mkdir(exist_ok=True)
    fd = _open(path, directory=True)
    try:
        # A read-only maintenance mount must be judged by the actual dropped identity.
        # Shared filesystems may present different ownership to root and that identity.
        if not worker and os.fstatvfs(fd).f_flag & os.ST_RDONLY:
            return
        missing = False
        if default_config:
            try:
                config = _open('config.toml', fd)
            except FileNotFoundError:
                missing = True
            except OSError as exc:
                if exc.errno != errno.ELOOP:
                    raise
                # Existing config links remain the unprivileged CLI's responsibility.
            else:
                try:
                    _prepare(config, 4)
                finally:
                    os.close(config)
        _prepare(fd, 7 if worker or missing else 1)
        if worker:
            for name in ('email', 'reports', 'logs'):
                try:
                    os.mkdir(name, dir_fd=fd)
                except FileExistsError:
                    pass
                _tree(fd, name, name, top=True)
    finally:
        os.close(fd)


def main() -> None:
    arguments = sys.argv[1:]
    if os.getuid() != 0:
        os.execv(CLI_PATH, [CLI_PATH, *arguments])
    # argparse handles help/errors without touching data or reading snapshot files.
    args = build_parser().parse_args(arguments)
    try:
        with maintenance_signals():
            prepare_data(DATA_PATH, worker=args.command == 'run', default_config=not hasattr(args, 'config'))
    except CommandInterrupted as exc:
        raise SystemExit(128 + exc.signum) from None
    except OSError as exc:
        reason = errno.errorcode.get(exc.errno or 0, 'IO_ERROR')
        print(json.dumps({'error_type': 'ConfigurationError', 'message':
              f'container data permission initialization failed ({reason}); check data mount permissions and storage'}),
              file=sys.stderr)
        raise SystemExit(1) from None
    os.execv('/usr/sbin/gosu', ['gosu', f'{APPLICATION_UID}:{APPLICATION_GID}', CLI_PATH, *arguments])
