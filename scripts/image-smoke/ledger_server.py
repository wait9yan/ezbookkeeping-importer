"""内部测试网络上的合成账本，记录真实HTTP写入次数及响应中断。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.records = {}
        self.posts = 0
        self.hold_reply = False
        self.release = threading.Event()
        self.release.set()


state = State()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, body, status=200):
        data = json.dumps(body).encode()
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # 测试明确模拟客户端强杀，远端记录仍已提交。
            return

    def do_GET(self):
        result: object
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == '/control':
            with state.lock:
                result = {'posts': state.posts, 'records': list(state.records.values())}
            self.reply(result)
            return
        if url.path == '/api/v1/accounts/list.json':
            result = [{'id': '1', 'type': 1, 'currency': 'CNY', 'name': '测试账户'}]
        elif url.path == '/api/v1/transaction/categories/list.json':
            result = {'expense': [{'id': '10', 'type': 2, 'name': '其他杂项', 'subCategories': [
                {'id': '11', 'type': 2, 'parentId': '10', 'name': '待分类'}]}]}
        elif url.path == '/api/v1/transactions/list.json':
            keyword = query.get('keyword', [''])[0]
            with state.lock:
                items = [r for r in state.records.values() if keyword in r['comment']]
            result = {'items': items, 'nextTimeSequenceId': None}
        elif url.path == '/api/v1/transactions/get.json':
            with state.lock:
                result = state.records.get(query['id'][0])
            if result is None:
                self.reply({'success': False, 'errorCode': 205001})
                return
        else:
            self.reply({'error': 'unsupported path'}, 404)
            return
        self.reply({'success': True, 'result': result})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/control':
            with state.lock:
                state.hold_reply = body['hold_reply']
                if state.hold_reply:
                    state.release.clear()
                else:
                    state.release.set()
            self.reply({'ok': True})
            return
        if self.path != '/api/v1/transactions/add.json':
            self.reply({'error': 'unsupported mutation'}, 404)
            return
        with state.lock:
            state.posts += 1
            record = {**body, 'id': str(state.posts)}
            state.records[record['id']] = record
            hold = state.hold_reply
        if hold:
            state.release.wait(60)
        self.reply({'success': True, 'result': record})


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
