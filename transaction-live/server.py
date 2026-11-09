"""SolidCraft operation observations and offline timeline reconstruction viewer."""
from __future__ import annotations

import argparse
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlsplit
import uuid
import webbrowser

from archive import Archive, ArchiveError, QuotaExceeded
from native import Recorder, available_instances, select_instance, safe_error
from rebuild import Rebuilder
from solidcraft_instances import default_control_dir

ROOT = Path(__file__).resolve().parent
COOKIE = 'solidcraft_trace_session'
SHA = re.compile(r'[a-f0-9]{64}\Z')
IDENT = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = True
    request_queue_size = 16

    def __init__(self, address, handler):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(10)
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_handler(archive, recorder, rebuilder, version, token, port):
    host = f'127.0.0.1:{port}'
    origin = 'http://' + host
    export_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = 'SolidCraftTrace/1'
        sys_version = ''

        def log_message(self, *args):
            pass

        def send_data(self, data, code=200, content_type='application/json; charset=utf-8', cookie=False, attachment=None):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'none'")
            if cookie:
                self.send_header('Set-Cookie', f'{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/')
            if attachment:
                self.send_header('Content-Disposition', f'attachment; filename="{attachment}"')
            self.end_headers()
            self.wfile.write(data)

        def reply(self, value, code=200):
            self.send_data(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'), code)

        def fail(self, code, message):
            self.reply({'error': message}, code)

        def allowed(self):
            if self.client_address[0] != '127.0.0.1' or self.headers.get('Host') != host:
                self.fail(403, '仅允许本机同源访问')
                return False
            supplied_origin = self.headers.get('Origin')
            if supplied_origin is not None and supplied_origin != origin:
                self.fail(403, '来源不匹配')
                return False
            return True

        def authenticated(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
                value = cookie.get(COOKIE)
                if value and secrets.compare_digest(value.value, token):
                    return True
            except Exception:
                pass
            self.fail(403, '请从查看器首页打开')
            return False

        def do_GET(self):
            if not self.allowed():
                return
            try:
                parsed = urlsplit(self.path)
                path = parsed.path
                query = parse_qs(parsed.query, max_num_fields=4, strict_parsing=True) if parsed.query else {}
                if path == '/' and not query:
                    self.send_data((ROOT / 'viewer.html').read_bytes(), content_type='text/html; charset=utf-8', cookie=True)
                    return
                if not self.authenticated():
                    return
                if path == '/geometry.html' and not query:
                    self.send_data((ROOT / 'geometry.html').read_bytes(), content_type='text/html; charset=utf-8')
                elif path == '/api/status' and not query:
                    state = recorder.status() if recorder else {'recording': False, 'status': 'offline', 'error': None, 'session_id': None, 'instance': None}
                    self.reply({'version': version, **state, 'cli_version': version, 'archive': archive.status()})
                elif path == '/api/sessions' and not query:
                    self.reply(archive.list_sessions())
                elif path == '/api/events':
                    if set(query) - {'after', 'limit', 'session_id'} or any(len(v) != 1 for v in query.values()):
                        raise ValueError('无效列表参数')
                    after, limit = int(query.get('after', ['0'])[0]), int(query.get('limit', ['100'])[0])
                    if not 0 <= after <= 2**63 - 1 or not 1 <= limit <= 500:
                        raise ValueError('列表范围无效')
                    session = query.get('session_id', [None])[0]
                    if session is not None and not IDENT.fullmatch(session):
                        raise ValueError('会话编号无效')
                    self.reply({'events': archive.list_events(after=after, limit=limit, session_id=session)})
                elif re.fullmatch(r'/api/observations/[1-9][0-9]{0,17}', path) and not query:
                    self.result(archive.snapshot(int(path.rsplit('/', 1)[1])))
                elif path.startswith('/api/stages/') and not query and IDENT.fullmatch(path.rsplit('/', 1)[1]):
                    self.result(archive.stage(path.rsplit('/', 1)[1]))
                elif path.startswith('/api/assets/') and not query and SHA.fullmatch(path.rsplit('/', 1)[1]):
                    data = archive.read_asset(path.rsplit('/', 1)[1])
                    # Objects may be JSON or ZIP/3MF. Never interpret them as HTML.
                    mime = 'application/json; charset=utf-8' if data.lstrip()[:1] in (b'{', b'[') else 'application/octet-stream'
                    self.send_data(data, content_type=mime)
                elif path == '/api/jobs' and not query:
                    self.reply({'jobs': archive.list_jobs()})
                elif path.startswith('/api/jobs/') and not query and IDENT.fullmatch(path.rsplit('/', 1)[1]):
                    self.result(archive.get_job(path.rsplit('/', 1)[1]))
                elif path == '/api/archive.zip' and not query:
                    self.export_archive()
                else:
                    self.fail(404, '接口不存在')
            except (BrokenPipeError, ConnectionResetError):
                return
            except (ValueError, KeyError, TypeError) as exc:
                self.fail(400, safe_error(exc))
            except FileNotFoundError:
                self.fail(404, '资源不存在')
            except QuotaExceeded as exc:
                self.fail(507, safe_error(exc))
            except (ArchiveError, RuntimeError, OSError) as exc:
                self.fail(409, safe_error(exc))

        def result(self, value):
            if value is None:
                self.fail(404, '记录不存在')
            else:
                self.reply(value)

        def do_POST(self):
            if not self.allowed() or not self.authenticated():
                return
            if self.headers.get('Origin') != origin:
                self.fail(403, '写请求需要同源 Origin')
                return
            try:
                if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                    raise ValueError('只接受有长度的 JSON 请求')
                length = int(self.headers.get('Content-Length', '-1'))
                if not 0 <= length <= 1024:
                    raise ValueError('请求体超过上限')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('请求体必须是对象')
                if self.path == '/api/capture' and body == {}:
                    if not recorder:
                        raise ValueError('离线档案未连接现场实例')
                    self.reply(recorder.request_capture(), 202)
                elif self.path == '/api/recording' and set(body) == {'paused'} and type(body['paused']) is bool:
                    if not recorder:
                        raise ValueError('离线档案未连接现场实例')
                    recorder.pause(body['paused'])
                    self.reply(recorder.status())
                elif self.path == '/api/rebuild' and set(body) == {'snapshot_id', 'position'}:
                    if not rebuilder:
                        raise ValueError('当前档案为只读或找不到独立 CLI')
                    if type(body['snapshot_id']) is not int or type(body['position']) is not int:
                        raise ValueError('观察编号和时间线前缀必须为整数')
                    self.reply(rebuilder.submit(body['snapshot_id'], body['position']), 202)
                elif re.fullmatch(r'/api/jobs/[A-Za-z0-9_-]{1,100}/cancel', self.path) and body == {}:
                    if not rebuilder:
                        raise ValueError('重建不可用')
                    job_id = self.path.split('/')[3]
                    rebuilder.cancel(job_id)
                    self.result(archive.get_job(job_id))
                else:
                    self.fail(400, '接口或字段不受支持')
            except (BrokenPipeError, ConnectionResetError):
                return
            except (ValueError, KeyError, TypeError) as exc:
                self.fail(400, safe_error(exc))
            except QuotaExceeded as exc:
                self.fail(507, safe_error(exc))
            except (ArchiveError, RuntimeError, OSError) as exc:
                self.fail(409, safe_error(exc))

        def export_archive(self):
            if archive.read_only:
                self.fail(409, '只读模式不在档案目录生成 ZIP；请使用 --offline 模式打包导出')
                return
            if not export_lock.acquire(False):
                self.fail(409, '已有导出任务正在进行')
                return
            folder = archive.root / '.work' / ('export-' + uuid.uuid4().hex)
            try:
                folder.mkdir(parents=True)
                path = archive.export_zip(folder / 'archive.zip')
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Length', str(path.stat().st_size))
                self.send_header('Content-Disposition', 'attachment; filename="solidcraft-trace.zip"')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                with path.open('rb') as source:
                    shutil.copyfileobj(source, self.wfile, 1024 * 1024)
            finally:
                shutil.rmtree(folder, ignore_errors=True)
                export_lock.release()

    return Handler


def create_server(archive, recorder=None, rebuilder=None, version='unknown', port=0):
    http = LocalServer(('127.0.0.1', port), BaseHTTPRequestHandler)
    http.RequestHandlerClass = make_handler(archive, recorder, rebuilder, version, secrets.token_urlsafe(32), http.server_port)
    return http


def installed_version(cli):
    try:
        opts = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        result = subprocess.run([str(cli), '--version'], capture_output=True, timeout=5, check=True, **opts)
        return result.stdout.decode('utf-8', 'replace').strip().removeprefix('solidcraft-cli ')
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


def options(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='独立记录目录；默认 records/本次时间')
    parser.add_argument('--offline', action='store_true', help='不连接 SolidCraft，浏览已有档案')
    parser.add_argument('--read-only', action='store_true', help='只读档案，不允许记录或重建')
    parser.add_argument('--limit-gib', type=float, default=2, help='包含数据库、对象及自有临时文件的容量预算')
    parser.add_argument('--control-dir', type=Path, default=default_control_dir())
    parser.add_argument('--control-port', type=int)
    parser.add_argument('--list-instances', action='store_true')
    parser.add_argument('--cli', type=Path, default=Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'SolidCraft' / 'solidcraft-cli.exe')
    parser.add_argument('--rebuild-timeout', type=int, default=120)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args(argv)
    if not .25 <= args.limit_gib <= 1024 or not 5 <= args.rebuild_timeout <= 3600:
        parser.error('预算需为 0.25..1024 GiB，超时需为 5..3600 秒')
    if not 0 <= args.port <= 65535 or (args.control_port is not None and not 1 <= args.control_port <= 65535):
        parser.error('端口无效')
    if (args.offline or args.read_only) and not args.archive:
        parser.error('离线/只读模式需指定 --archive')
    return args


def main(argv=None):
    args = options(argv)
    if args.list_instances:
        choices = available_instances(args.control_dir)
        for binding, name in choices:
            print(f'{name} | PID {binding.pid} | 控制端口 {binding.port}')
        if not choices:
            print('未找到当前账号可连接的 SolidCraft 实例。')
        return 0
    choice = None if args.offline or args.read_only else select_instance(args.control_dir, args.control_port)
    if not choice and not (args.offline or args.read_only):
        return 0
    version = installed_version(args.cli)
    root = args.archive or ROOT / 'records' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    archive = Archive(root, limit_bytes=int(args.limit_gib * 1024**3), read_only=args.read_only)
    recorder = rebuilder = http = None
    try:
        if choice:
            recorder = Recorder(archive, choice[0], version, choice[1])
        if args.cli.is_file() and not args.read_only:
            rebuilder = Rebuilder(archive, args.cli.resolve(), version, timeout=args.rebuild_timeout)
        http = create_server(archive, recorder, rebuilder, version, args.port)
        if recorder:
            recorder.start()
        url = f'http://127.0.0.1:{http.server_port}/'
        print(f'查看器：{url}\n档案：{archive.root}\n浏览器关闭后仍记录。关闭本控制台或按 Ctrl+C 停止。', flush=True)
        if args.open:
            webbrowser.open(url)
        http.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        print('\n正在停止采集并保存档案……', flush=True)
    finally:
        if recorder:
            recorder.stop()
        if rebuilder:
            rebuilder.stop()
        if http:
            http.server_close()
        archive.close()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, EOFError) as error:
        print('无法启动：' + safe_error(error), file=sys.stderr)
        raise SystemExit(1)
