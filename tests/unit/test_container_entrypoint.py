import errno
import os

import pytest

from ezbookkeeping_importer.entrypoints import container


@pytest.fixture
def local_identity(monkeypatch):
    monkeypatch.setattr(container, 'APPLICATION_UID', os.getuid())
    monkeypatch.setattr(container, 'APPLICATION_GID', os.getgid())


def test_prepares_managed_tree_preserves_content(tmp_path, local_identity):
    config = tmp_path / 'config.toml'
    config.write_text('existing')
    config.chmod(0o400)
    for name in ('email', 'reports', 'logs'):
        folder = tmp_path / name
        folder.mkdir()
        (folder / 'old').write_text('persistent')
        (folder / 'old').chmod(0o400)
    untouched = tmp_path / 'other'
    untouched.write_text('untouched')
    untouched.chmod(0o400)
    container.prepare_data(tmp_path, worker=True, default_config=True)
    assert config.read_text() == 'existing'
    for name in ('email', 'reports', 'logs'):
        assert (tmp_path / name / 'old').read_text() == 'persistent'
        assert (tmp_path / name / 'old').stat().st_mode & 0o777 == 0o400
    assert untouched.stat().st_mode & 0o777 == 0o400


def test_repeated_and_readonly_maintenance_dont_modify(tmp_path, local_identity, monkeypatch):
    (tmp_path / 'config.toml').write_text('existing')
    (tmp_path / 'config.toml').chmod(0o444)
    tmp_path.chmod(0o555)
    def unexpected(*args):
        pytest.fail('already accessible objects must not be modified')
    monkeypatch.setattr(os, 'fchown', unexpected)
    monkeypatch.setattr(os, 'fchmod', unexpected)
    for _ in range(2):
        container.prepare_data(tmp_path, worker=False, default_config=True)
    assert not (tmp_path / 'email').exists()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink'])
def test_readable_config_links_preserved_without_modifying_target(tmp_path, local_identity, kind):
    outside = tmp_path / 'outside'
    outside.write_text('private')
    outside.chmod(0o400)
    data = tmp_path / 'data'
    data.mkdir()
    if kind == 'symlink':
        (data / 'config.toml').symlink_to(outside)
    else:
        os.link(outside, data / 'config.toml')
    container.prepare_data(data, worker=False, default_config=True)
    assert outside.stat().st_mode & 0o777 == 0o400
    assert outside.read_text() == 'private'


def test_directory_symlink_rejected(tmp_path, local_identity):
    outside = tmp_path / 'outside'
    outside.mkdir()
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'email').symlink_to(outside)
    with pytest.raises(OSError):
        container.prepare_data(data, worker=True, default_config=True)
    assert list(outside.iterdir()) == []


def test_help_does_not_prepare_data(monkeypatch):
    monkeypatch.setattr(os, 'getuid', lambda: 0)
    monkeypatch.setattr('sys.argv', ['ebki', '--help'])
    monkeypatch.setattr(container, 'prepare_data', lambda *a, **k: pytest.fail('writes on help'))
    with pytest.raises(SystemExit) as exc:
        container.main()
    assert exc.value.code == 0


class Executed(BaseException):
    pass


@pytest.mark.parametrize('uid', [0, 12345])
def test_exec_preserves_arguments_and_drops_root(monkeypatch, uid):
    monkeypatch.setattr(os, 'getuid', lambda: uid)
    arguments = ['recheck', '--snapshot', '/not-readable-by-root']
    monkeypatch.setattr('sys.argv', ['ebki', *arguments])
    preparations = []
    monkeypatch.setattr(container, 'prepare_data', lambda *a, **k: preparations.append(k))
    def execute(path, args):
        assert args[-3:] == arguments
        assert path == ('/usr/sbin/gosu' if uid == 0 else container.CLI_PATH)
        raise Executed()
    monkeypatch.setattr(os, 'execv', execute)
    with pytest.raises(Executed):
        container.main()
    assert len(preparations) == (1 if uid == 0 else 0)


def test_permission_error_is_safe(monkeypatch, capsys):
    monkeypatch.setattr(os, 'getuid', lambda: 0)
    monkeypatch.setattr('sys.argv', ['ebki', 'run'])
    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, 'secret-path')
    monkeypatch.setattr(container, 'prepare_data', denied)
    with pytest.raises(SystemExit) as exc:
        container.main()
    assert exc.value.code == 1
    stderr = capsys.readouterr().err
    assert 'EACCES' in stderr and 'secret-path' not in stderr


def test_initialization_honors_sigterm(monkeypatch):
    import signal
    monkeypatch.setattr(os, 'getuid', lambda: 0)
    monkeypatch.setattr('sys.argv', ['ebki', 'run'])
    def interrupt(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
    monkeypatch.setattr(container, 'prepare_data', interrupt)
    with pytest.raises(SystemExit) as exc:
        container.main()
    assert exc.value.code == 128 + signal.SIGTERM


def test_only_active_outputs_gain_write_permission(tmp_path, local_identity):
    for name, filename in [('reports', 'old.tmp'), ('logs', 'worker.jsonl')]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / filename).write_text('persistent')
        (folder / filename).chmod(0o400)
    container.prepare_data(tmp_path, worker=True, default_config=True)
    assert (tmp_path / 'reports/old.tmp').stat().st_mode & 0o600 == 0o600
    assert (tmp_path / 'logs/worker.jsonl').stat().st_mode & 0o600 == 0o600


def test_hardlinked_file_needing_permission_change_rejected(tmp_path, local_identity):
    outside = tmp_path / 'outside'
    outside.write_text('private')
    outside.chmod(0o400)
    logs = tmp_path / 'logs'
    logs.mkdir()
    os.link(outside, logs / 'worker.jsonl')
    with pytest.raises(OSError, match='multiply linked'):
        container.prepare_data(tmp_path, worker=True, default_config=True)
    assert outside.stat().st_mode & 0o777 == 0o400


@pytest.mark.parametrize('existing', [False, True])
def test_readonly_maintenance_defers_to_actual_cli_identity(tmp_path, monkeypatch, existing):
    from types import SimpleNamespace
    if existing:
        (tmp_path / 'config.toml').write_text('preserved')
        (tmp_path / 'config.toml').chmod(0o600)
    monkeypatch.setattr(os, 'fstatvfs', lambda fd: SimpleNamespace(f_flag=os.ST_RDONLY))
    def unexpected(*args):
        pytest.fail('read-only maintenance must not infer access from root ownership view')
    monkeypatch.setattr(container, '_prepare', unexpected)
    container.prepare_data(tmp_path, worker=False, default_config=True)
    assert (tmp_path / 'config.toml').exists() is existing


def test_readonly_worker_still_requires_permission_preparation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(os, 'fstatvfs', lambda fd: SimpleNamespace(f_flag=os.ST_RDONLY))
    def readonly(*args):
        raise OSError(errno.EROFS, 'read-only fixture')
    monkeypatch.setattr(container, '_prepare', readonly)
    with pytest.raises(OSError) as exc:
        container.prepare_data(tmp_path, worker=True, default_config=True)
    assert exc.value.errno == errno.EROFS
