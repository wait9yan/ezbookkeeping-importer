"""正式默认入口的单进程、独立CLI及真实强杀恢复验收。"""
import json
from pathlib import Path
import time


WAIT_SECONDS = 45
FIXTURE = Path(__file__).resolve().parent / 'image-smoke'
ENVIRONMENT = {
    'EBKI_LEDGER_URL': 'http://unreachable.invalid',
    'EBKI_LEDGER_TOKEN': 'synthetic-ledger-token',
    'EBKI_IMAP_HOST': 'unreachable.invalid',
    'EBKI_IMAP_USERNAME': 'synthetic',
    'EBKI_IMAP_PASSWORD': 'synthetic-password',
    'PYTHONPATH': '/smoke-fixture',
    'PYTHONDONTWRITEBYTECODE': '1',
}
PROCESS_CHECK = '''
import json
from pathlib import Path
owners = []
children = []
for process in Path('/proc').glob('[0-9]*'):
    try:
        args = (process / 'cmdline').read_bytes().split(b'\\0')
    except FileNotFoundError:
        continue
    if any(arg.endswith(b'/ebki') for arg in args) and b'run' in args:
        owners.append(int(process.name))
    if any(arg.startswith(b'from multiprocessing.') for arg in args):
        children.append(int(process.name))
assert owners == [1], owners
status = (Path('/proc') / '1/status').read_text().splitlines()
assert next(line for line in status if line.startswith('Uid:')).split()[1:] == ['10001'] * 4
assert next(line for line in status if line.startswith('Gid:')).split()[1:] == ['10001'] * 4
assert children == [], children
print(json.dumps(owners))
'''
LOCK_CHECK = '''
import os
from ezbookkeeping_importer.adapters.persistence.postgres import PostgresStore
store = PostgresStore(os.environ['EBKI_DATABASE_URL'])
try:
    assert store.lock_worker() is EXPECTED
finally:
    store.close()
'''
STATUS_CHECK = '''
import json, os
from ezbookkeeping_importer.adapters.persistence.postgres import PostgresStore
store = PostgresStore(os.environ['EBKI_DATABASE_URL'])
try:
    print(json.dumps(store.all("SELECT id,import_status FROM bank_transactions ORDER BY id")))
finally:
    store.close()
'''


def until(predicate, description, seconds=WAIT_SECONDS):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.2)
    raise TimeoutError(f'镜像生命周期验证超时：{description}')


def verify_first_startup(docker, image, platform_args, network, mounts, database_url, name,
                         *, legacy=False):
    """独立数据卷及目标库直接默认run；真实分类HTTP与来源流水线完成后验证重启。"""
    ledger = name + '-ledger'
    fixture = ['--mount', f'type=bind,src={FIXTURE},dst=/smoke-fixture,readonly']
    environment = {
        **ENVIRONMENT, 'EBKI_DATABASE_URL': database_url,
        'EBKI_LEDGER_URL': f'http://{ledger}:8080', 'EBKI_IMAP_HOST': 'imap.qq.com',
        'EBKI_AI_URL': f'http://{ledger}:8080/v1', 'EBKI_AI_MODEL': 'synthetic-model',
        'EBKI_AI_TOKEN': 'synthetic-ai-token', 'EBKI_SMOKE_MAIL': 'single',
    }
    env_args = [arg for k, v in environment.items() for arg in ('-e', f'{k}={v}')]
    shared = [*platform_args, '--network', network, *mounts, *fixture, *env_args]
    created = []

    def python(code):
        return docker('run', '--rm', *shared, '--user', '10001:10001', '--entrypoint',
                      'python', image, '-c', code)

    def control():
        return json.loads(python('import urllib.request; '
                                 f'print(urllib.request.urlopen({environment["EBKI_LEDGER_URL"]!r}'
                                 '+"/control").read().decode())'))

    def logs():
        state = json.loads(docker('inspect', name))[0]['State']
        output = docker('logs', name)
        if not state['Running']:
            raise AssertionError(f'默认启动意外退出：{output}')
        return [json.loads(line) for line in output.splitlines() if line.strip()]

    try:
        docker('run', '-d', '--name', ledger, *platform_args, '--network', network,
               *fixture, '--user', '10001:10001', '--entrypoint', 'python', image,
               '/smoke-fixture/ledger_server.py')
        created.append(ledger)
        if legacy:
            # 只通过已发布001建立旧库；禁止调用新版migrate预先升级。
            python('import os; import psycopg; '
                   'from ezbookkeeping_importer.adapters.persistence.postgres import SCHEMA; '
                   'connection=psycopg.connect(os.environ["EBKI_DATABASE_URL"]); '
                   'connection.execute(SCHEMA.read_text()); '
                   'connection.execute("INSERT INTO email(id,raw_path,parse_status) "'
                   '"VALUES (%s,%s,%s)", ("c"*64,"synthetic-preserved","ignored")); '
                   'connection.commit(); connection.close()')
        # 不设置--config、不传命令、不预生成配置，不调用migrate。
        docker('run', '-d', '--name', name, *shared, image)
        created.append(name)
        until(lambda: any(e.get('event') == 'sync_completed' for e in logs()),
              '默认AI配置首次同步完成')
        until(lambda: control()['posts'] == 1, '合成邮件实际入账')
        assert control()['classifications'] == 1
        docker('exec', name, 'python', '-c', PROCESS_CHECK)
        python('import json, os; from pathlib import Path; from importlib.resources import files; '
               'from ezbookkeeping_importer.adapters.persistence.postgres import PostgresStore; '
               'assert Path("/app/data/config.toml").read_bytes() == '
               'files("ezbookkeeping_importer").joinpath("config.toml").read_bytes(); '
               'store=PostgresStore(os.environ["EBKI_DATABASE_URL"]); '
               'from ezbookkeeping_importer.adapters.persistence.migrations import load_migrations; '
               'migrations=load_migrations(); '
               'assert store.check_schema()==migrations[-1].version; '
               'history=store.all("SELECT version,script_sha256 FROM schema_version ORDER BY version"); '
               'assert [(r["version"],r["script_sha256"]) for r in history] == '
               '[(m.version,m.checksum) for m in migrations]; '
               'rows=store.all("SELECT * FROM bank_transactions"); '
               'assert len(rows)==1 and rows[0]["import_status"]=="booked", rows; '
               'assert store.one("SELECT * FROM email_source_item")["source_id"]=="qq-primary"; '
               'assert store.one("SELECT * FROM email_source_item")["source_status"]=="verified"; '
               'assert list(Path("/app/data/email").glob("*.eml")); '
               + ('assert store.one("SELECT raw_path FROM email WHERE id=%s", ("c"*64,))'
                  '["raw_path"]=="synthetic-preserved"; ' if legacy else '')
               + 'store.close()')
        initial_runs = sum(e.get('event') == 'worker_started' for e in logs())
        docker('stop', name, timeout=25)
        assert json.loads(docker('inspect', name))[0]['State']['ExitCode'] == 0
        docker('start', name)
        until(lambda: sum(e.get('event') == 'worker_started' for e in logs()) > initial_runs,
              '初始化后重启')
        until(lambda: sum(e.get('event') == 'sync_completed' for e in logs()) >= 2,
              '重启后同步完成')
        assert control()['posts'] == control()['classifications'] == 1
        docker('stop', name, timeout=25)
        assert json.loads(docker('inspect', name))[0]['State']['ExitCode'] == 0
        print(('旧v1数据升级' if legacy else '缺库全新默认启动')
              + '：内置AI配置、来源采集/解析/真实分类HTTP/入账、重启无重复通过', flush=True)
    except Exception:
        if name in created:
            print('首次启动失败诊断：' + docker('logs', name), flush=True)
            print('首次启动问题：' + docker('exec', name, 'ebki', 'issues'), flush=True)
        raise
    finally:
        for container in reversed(created):
            docker('rm', '-fv', container)


def verify_lifecycle(docker, image, platform_args, network, mounts, database_url, name):
    metadata = json.loads(docker('image', 'inspect', image))[0]['Config']
    assert metadata['Entrypoint'] == ['ebki'], metadata['Entrypoint']
    assert metadata['Cmd'] == ['run'], metadata['Cmd']
    ledger = name + '-ledger'
    environment = {**ENVIRONMENT, 'EBKI_LEDGER_URL': f'http://{ledger}:8080',
                   'EBKI_DATABASE_URL': database_url}
    env_args = [arg for k, v in environment.items() for arg in ('-e', f'{k}={v}')]
    fixture = ['--mount', f'type=bind,src={FIXTURE},dst=/smoke-fixture,readonly']
    shared = [*platform_args, '--network', network, *mounts, *fixture, *env_args]
    created = []

    def inspect():
        return json.loads(docker('inspect', name))[0]

    def once_python(code):
        return docker('run', '--rm', *shared, '--user', '10001:10001', '--entrypoint', 'python', image, '-c', code)

    def statuses():
        return {r['id']: r['import_status'] for r in json.loads(once_python(STATUS_CHECK))}

    def control(body=None):
        code = (
            'import json,urllib.request; '
            f'url={environment["EBKI_LEDGER_URL"]!r}+"/control"; '
            + ('response=urllib.request.urlopen(url)' if body is None else
               f'response=urllib.request.urlopen(url,json.dumps({body!r}).encode())')
            + '; print(response.read().decode())'
        )
        return json.loads(once_python(code))

    def logs():
        return [json.loads(line) for line in docker('logs', name).splitlines() if line.strip()]

    def ready(previous=None):
        state = inspect()
        if not state['State']['Running']:
            return False
        starts = [e for e in logs() if e.get('event') == 'worker_started']
        return starts and (previous is None or starts[-1]['run_id'] != previous)

    def current_run():
        return [e['run_id'] for e in logs() if e.get('event') == 'worker_started'][-1]

    def assert_single():
        docker('exec', name, 'python', '-c', PROCESS_CHECK)
        docker('exec', name, 'python', '-c', LOCK_CHECK.replace('EXPECTED', 'False'))

    def default_stop(expected_exit):
        docker('stop', name, timeout=25)  # 不覆盖Docker默认十秒期限。
        state = inspect()
        assert not state['State']['Running'], state['State']
        assert state['State']['ExitCode'] == expected_exit, state['State']
        time.sleep(0.5)
        assert not inspect()['State']['Running']
        once_python(LOCK_CHECK.replace('EXPECTED', 'True'))

    try:
        docker('run', '-d', '--name', ledger, *platform_args, '--network', network,
               *fixture, '--user', '10001:10001', '--entrypoint', 'python', image, '/smoke-fixture/ledger_server.py')
        created.append(ledger)
        docker('run', '--rm', *shared, '--user', '0:0', '--entrypoint', 'python', image,
               '-c', "import os; from pathlib import Path; "
               "paths=[Path('/app/data/email/old.eml'), Path('/app/data/reports/old.tmp'), "
               "Path('/app/data/logs/old.jsonl')]; "
               "[(p.write_text('persistent'), os.chown(p,0,0), p.chmod(0o600)) for p in paths]; "
               "p=Path('/app/data/logs/worker.jsonl'); "
               "p.write_text('{\"event\":\"permission_fixture\"}\\n'); os.chown(p,0,0); p.chmod(0o600)")
        # 不加-i/-t/--init/停止期限，不覆盖CMD；运行真实发行包。
        docker('run', '-d', '--restart', 'unless-stopped', '--name', name, *shared, image)
        created.append(name)
        until(lambda: ready(), '无终端默认入口就绪')
        config = inspect()
        assert not config['Config']['Tty'] and not config['Config']['OpenStdin']
        assert not config['HostConfig'].get('Init')
        until(lambda: any(e.get('event') == 'sync_completed' for e in logs()), '空邮箱同步完成')
        assert_single()
        once_python("from pathlib import Path; "
                    "paths=[Path('/app/data/email/old.eml'), Path('/app/data/reports/old.tmp'), "
                    "Path('/app/data/logs/old.jsonl')]; "
                    "assert all(p.read_text() == 'persistent' for p in paths); "
                    "paths[1].open('a').close()")
        once_python("import json; from pathlib import Path; "
                    "lines=Path('/app/data/logs/worker.jsonl').read_text().splitlines(); "
                    "assert json.loads(lines[0]) == {'event': 'permission_fixture'}; "
                    "assert any(json.loads(line).get('event') == 'worker_started' for line in lines[1:])")
        docker('exec', name, 'ebki', 'issues', '--snapshot-out', '/app/data/maintenance-smoke.json')
        once_python("from pathlib import Path; assert Path('/app/data/maintenance-smoke.json').stat().st_uid == 10001")
        pid, run_id = config['State']['Pid'], current_run()
        for command in ('status', 'issues'):
            json.loads(docker('exec', name, 'ebki', command))
        assert (inspect()['State']['Pid'], current_run()) == (pid, run_id)
        print('无TTY/PID1单进程、独立JSON命令、worker独占锁通过', flush=True)
        default_stop(0)

        # 真实写前登记已提交，但HTTP尚未发送；默认stop在十秒后强杀。
        once_python("from pathlib import Path; Path('/app/data/pause-before-send').touch()")
        docker('run', '--rm', *shared, '--user', '10001:10001', '--entrypoint', 'python', image,
               '/smoke-fixture/seed.py', 'before')
        previous = current_run()
        docker('start', name)
        until(lambda: ready(previous), '写前中断场景启动')
        until(lambda: statuses().get('a' * 16) == 'dispatching', '写前登记已提交')
        assert control()['posts'] == 0
        default_stop(137)
        once_python("from pathlib import Path; Path('/app/data/pause-before-send').unlink()")
        previous = current_run()
        docker('start', name)
        until(lambda: ready(previous), '未发送请求场景恢复')
        until(lambda: statuses().get('a' * 16) == 'unknown', '未发送的未决记录保留UNKNOWN')
        assert control()['posts'] == 0
        default_stop(0)
        print('写前登记后强杀：重启保留UNKNOWN，未新增HTTP写入', flush=True)

        # HTTP服务先持久接受请求，挂起回复；本地被杀后核实而不再次POST。
        control({'hold_reply': True})
        docker('run', '--rm', *shared, '--user', '10001:10001', '--entrypoint', 'python', image,
               '/smoke-fixture/seed.py', 'after')
        previous = current_run()
        docker('start', name)
        until(lambda: ready(previous), '远端成功场景启动')
        until(lambda: control()['posts'] == 1, '远端已接受一次HTTP写入')
        default_stop(137)
        control({'hold_reply': False})
        previous = current_run()
        docker('start', name)
        until(lambda: ready(previous), '远端成功场景恢复')
        until(lambda: statuses().get('b' * 16) == 'booked', '重启回读确认已入账')
        assert control()['posts'] == 1
        assert statuses()['a' * 16] == 'unknown'
        assert_single()
        default_stop(0)
        print('远端成功后强杀：重启核实入账，HTTP写入总数仍为1；stop保持停止', flush=True)
    finally:
        for container in reversed(created):
            docker('rm', '-fv', container)
