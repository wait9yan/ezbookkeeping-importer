"""首次启动验收夹具保留真实来源、解析和AI HTTP边界。"""
import importlib.util
from pathlib import Path
from threading import Thread
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

from ezbookkeeping_importer.adapters.banks.cmb import BankParser
from ezbookkeeping_importer.adapters.llm.openai import AIClient
from ezbookkeeping_importer.application.collect import source_status


def fixture_module(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parents[2] / 'scripts' / 'image-smoke' / f'{name}.py'
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_startup_mail_is_verified_and_parses_one_real_expense():
    mailbox = fixture_module('synthetic_mail').SingleMailbox(None)
    headers = mailbox.fetch_headers_batch('INBOX', (1,))[1]
    settings = SimpleNamespace(mail=SimpleNamespace(host='imap.qq.com'))
    assert source_status(headers, settings, 'imap')[0] == 'verified'
    parsed = BankParser().parse(mailbox.fetch('INBOX', 1))
    assert not parsed.issues
    assert len(parsed.rows) == 1
    assert parsed.rows[0].original_amount == 10
    assert parsed.rows[0].original_currency == 'CNY'
    assert parsed.rows[0].card_reference == '1234'
    assert mailbox.scan('INBOX') == ('1', [1])
    assert mailbox.scan('INBOX', after_uid=1) == ('1', [])


def test_startup_mock_ai_uses_real_http_protocol():
    server_module = fixture_module('ledger_server')
    server = ThreadingHTTPServer(('127.0.0.1', 0), server_module.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = AIClient(f'http://127.0.0.1:{server.server_port}/v1', 'synthetic-token',
                      'synthetic-model')
    try:
        result = client.classify('synthetic-row', '合成商户', [{'id': '12', 'path': '其他杂项 → 合成用途'}])
        assert result['source_row_id'] == 'synthetic-row'
        assert result['classification_status'] == 'matched'
        assert result['category_id'] == '12'
        assert server_module.state.classifications == 1
        assert server_module.state.posts == 0
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
