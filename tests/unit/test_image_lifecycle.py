"""镜像必须运行真实无TTY单进程入口，不能只匹配一个子worker。"""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'image_lifecycle', Path(__file__).parents[2] / 'scripts' / 'image_lifecycle.py'
)
assert spec is not None and spec.loader is not None
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)


def process(root, pid, *args):
    target = root / str(pid)
    target.mkdir()
    (target / 'status').write_text('Uid:\t10001\t10001\t10001\t10001\nGid:\t10001\t10001\t10001\t10001\n')
    (target / 'cmdline').write_bytes(b'\0'.join(arg.encode() for arg in args))


def snapshot(root):
    exec(lifecycle.PROCESS_CHECK.replace("Path('/proc')", f'Path({str(root)!r})'))


def test_pid_one_without_multiprocessing(tmp_path, capsys):
    process(tmp_path, 1, '/app/.venv/bin/python', '/app/.venv/bin/ebki', 'run')
    process(tmp_path, 2, 'python', '-c', lifecycle.PROCESS_CHECK)
    snapshot(tmp_path)
    assert json.loads(capsys.readouterr().out) == [1]


@pytest.mark.parametrize('pid,child', [(2, False), (1, True)])
def test_non_pid_one_or_child_worker_rejected(tmp_path, pid, child):
    process(tmp_path, pid, '/app/.venv/bin/ebki', 'run')
    if child:
        process(tmp_path, 3, 'python', '-c', 'from multiprocessing.spawn import spawn_main; spawn_main()')
    with pytest.raises(AssertionError):
        snapshot(tmp_path)


@pytest.mark.parametrize('entrypoint,command', [(['python'], ['run']), (['ebki'], ['worker'])])
def test_wrong_default_entrypoint_rejected(entrypoint, command):
    calls = []

    def docker(*args):
        calls.append(args)
        return json.dumps([{'Config': {'Entrypoint': entrypoint, 'Cmd': command}}])

    with pytest.raises(AssertionError):
        lifecycle.verify_lifecycle(docker, 'image', [], 'net', [], 'synthetic', 'container')
    assert calls == [('image', 'inspect', 'image')]


def test_root_service_rejected(tmp_path):
    process(tmp_path, 1, '/app/.venv/bin/ebki', 'run')
    (tmp_path / '1/status').write_text('Uid: 0 0 0 0\nGid: 0 0 0 0\n')
    with pytest.raises(AssertionError):
        snapshot(tmp_path)
