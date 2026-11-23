"""HTTP 服务：REST API + 静态班报页面（仅依赖标准库）。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .auth import Authenticator

STATIC_DIR = __file__.rsplit('/', 1)[0] + '/static'


class AppState:
    def __init__(self, service, authenticator: Authenticator):
        self.service = service
        self.auth = authenticator


def json_response(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    handler.wfile.write(body)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = 'Rejudgment/1.0'

    # 方便子类注入
    @property
    def state(self) -> AppState:
        return self.server.app_state

    def log_message(self, fmt, *args):  # 安静日志，交给审计表
        return

    # ------------------------------------------------------------ helpers
    def _body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        ctype = self.headers.get('Content-Type', '')
        if 'application/json' in ctype:
            try:
                return json.loads(raw.decode('utf-8'))
            except json.JSONDecodeError:
                raise ValueError('请求体不是有效 JSON')
        if 'application/x-www-form-urlencoded' in ctype:
            parsed = parse_qs(raw.decode('utf-8'))
            return {k: v[0] for k, v in parsed.items()}
        return {'raw': raw.decode('utf-8', errors='replace')}

    def _user(self, require_role=None):
        token = self.headers.get('Authorization', '').removeprefix('Bearer ').strip()
        user = self.state.auth.verify(token)
        if user is None:
            raise PermissionError('未登录或登录已过期')
        if require_role and user['role'] not in require_role:
            raise PermissionError(f'当前角色 {user["role"]} 无权执行该操作')
        return user

    def _csv_payload(self, body):
        """支持 JSON {"filename","content"} 或表单 raw 文本。"""
        if isinstance(body, dict) and 'content' in body:
            content = body['content']
            filename = body.get('filename') or 'upload.csv'
        else:
            content = body.get('raw', '') if isinstance(body, dict) else ''
            filename = 'upload.csv'
        if not content.strip():
            raise ValueError('CSV 内容为空')
        return filename, content

    # ------------------------------------------------------------ routing
    def do_GET(self):
        try:
            url = urlparse(self.path)
            path = url.path
            if path == '/healthz':
                return json_response(self, {'ok': True})
            if path.startswith('/api/'):
                return self._route_get(path)
            return self._serve_static(path)
        except PermissionError as exc:
            json_response(self, {'error': str(exc)}, 401)
        except FileNotFoundError as exc:
            json_response(self, {'error': str(exc)}, 404)
        except ValueError as exc:
            json_response(self, {'error': str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            json_response(self, {'error': f'服务器错误：{exc}'}, 500)

    def do_POST(self):
        try:
            return self._route_post(urlparse(self.path).path)
        except PermissionError as exc:
            json_response(self, {'error': str(exc)}, 401)
        except ValueError as exc:
            json_response(self, {'error': str(exc)}, 400)
        except FileNotFoundError as exc:
            json_response(self, {'error': str(exc)}, 404)
        except Exception as exc:  # noqa: BLE001
            json_response(self, {'error': f'服务器错误：{exc}'}, 500)

    # ------------------------------------------------------------ GET API
    def _route_get(self, path):
        svc = self.state.service
        if path == '/api/me':
            user = self._user()
            return json_response(self, {'user': {k: user[k] for k in ('username', 'role', 'name')}})
        if path == '/api/batches':
            self._user()
            return json_response(self, {'batches': svc.list_batches()})
        if path.startswith('/api/batches/') and path.endswith('/detail'):
            self._user()
            batch_id = int(path.split('/')[3])
            return json_response(self, svc.get_batch_detail(batch_id))
        if path == '/api/runs':
            self._user()
            return json_response(self, {'runs': svc.list_runs()})
        if path == '/api/corrections':
            self._user()
            return json_response(self, {'corrections': svc.list_corrections()})
        if path == '/api/audit':
            self._user(require_role=('admin',))
            return json_response(self, {'events': svc.list_audit()})
        raise FileNotFoundError(f'接口 {path} 不存在')

    # ----------------------------------------------------------- POST API
    def _route_post(self, path):
        svc = self.state.service
        body = self._body()

        if path == '/api/login':
            username = body.get('username')
            token, account = self.state.auth.login(username, body.get('password'))
            return json_response(self, {
                'token': token,
                'user': {'username': username,
                         'role': account['role'], 'name': account['name']},
            })
        if path == '/api/logout':
            self.state.auth.logout(self.headers.get('Authorization', '').removeprefix('Bearer ').strip())
            return json_response(self, {'ok': True})

        if path == '/api/import/readings':
            user = self._user(require_role=('admin', 'analyst'))
            filename, content = self._csv_payload(body)
            batch_id = svc.import_readings(content, filename, user['name'])
            return json_response(self, {'ok': True, 'batch_id': batch_id}, 201)

        if path == '/api/import/corrections':
            user = self._user(require_role=('admin', 'analyst'))
            filename, content = self._csv_payload(body)
            outcome = svc.import_corrections(content, filename, user['name'])
            return json_response(self, {'ok': True, **outcome})

        if path.startswith('/api/batches/') and path.endswith('/recompute'):
            user = self._user(require_role=('admin', 'analyst'))
            batch_id = int(path.split('/')[3])
            run_id = svc.recompute_batch(batch_id, user['name'])
            return json_response(self, {'ok': True, 'run_id': run_id})

        raise FileNotFoundError(f'接口 {path} 不存在')

    # ------------------------------------------------------------ static
    def _serve_static(self, path):
        if path in ('', '/'):
            path = '/index.html'
        root = Path(STATIC_DIR).resolve()
        full = (root / path.lstrip('/')).resolve()
        if root not in full.parents and full != root:
            raise FileNotFoundError('非法路径')
        if not full.is_file():
            raise FileNotFoundError(f'{path} 不存在')
        with open(full, 'rb') as fh:
            data = fh.read()
        suffix = full.suffix
        ctype = ('text/html; charset=utf-8' if suffix == '.html' else
                 'text/css; charset=utf-8' if suffix == '.css' else
                 'application/javascript; charset=utf-8' if suffix == '.js' else
                 'application/octet-stream')
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def build_server(host, port, state: AppState):
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.app_state = state
    return server
