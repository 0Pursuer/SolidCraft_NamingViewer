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
import time
from urllib.parse import parse_qs, urlsplit
import uuid
import webbrowser

from archive import Archive, ArchiveError, QuotaExceeded
from native import Recorder, available_instances, select_instance, safe_error
from rebuild import Rebuilder
from flow_model import build_flow
from recompute_model import build_recompute
from lifecycle_model import build_lifecycle
from solidcraft_instances import default_control_dir

ROOT = Path(__file__).resolve().parent
COOKIE = 'solidcraft_trace_session'
SHA = re.compile(r'[a-f0-9]{64}\Z')
IDENT = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')


def unique_fields(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('JSON 不接受重复字段')
        value[key] = item
    return value


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


def make_handler(archive, recorder, rebuilder, version, token, port, recompute_lab=None):
    host = f'127.0.0.1:{port}'
    origin = 'http://' + host
    export_lock = threading.Lock()
    cookie_name = f'{COOKIE}_{port}'
    flow_lock = threading.Lock()
    flow_cache = {}

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
                self.send_header('Set-Cookie', f'{cookie_name}={token}; HttpOnly; SameSite=Strict; Path=/')
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
                value = cookie.get(cookie_name)
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
                query = parse_qs(parsed.query, max_num_fields=4, strict_parsing=True, keep_blank_values=True) if parsed.query else {}
                if path in {'/', '/flow', '/flow.html', '/archive', '/recompute', '/recompute.html'} and not query:
                    name = 'viewer.html' if path == '/archive' else 'recompute.html' if path.startswith('/recompute') else 'flow.html'
                    self.send_data((ROOT / name).read_bytes(), content_type='text/html; charset=utf-8', cookie=True)
                    return
                if not self.authenticated():
                    return
                if path in {'/lifecycle.js', '/lifecycle.css'} and not query:
                    mime = 'text/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8'
                    self.send_data((ROOT / path[1:]).read_bytes(), content_type=mime)
                elif path == '/api/recompute/lifecycle':
                    if set(query) - {'before_id', 'after_id', 'job_id'} or any(len(v) != 1 for v in query.values()):
                        raise ValueError('对象关系视图只接受观察对和实验编号')
                    ids = {key: int(query[key][0]) for key in ('before_id', 'after_id') if key in query}
                    if any(not 1 <= value <= 2**63 - 1 for value in ids.values()) or bool('before_id' in ids) != bool('after_id' in ids):
                        raise ValueError('请选择完整的一对观察编号')
                    job_id = query.get('job_id', [None])[0]
                    if job_id is not None and not re.fullmatch(r'[a-f0-9]{32}', job_id):
                        raise ValueError('实验编号无效')
                    job = recompute_lab.get(job_id) if recompute_lab and job_id else None
                    if job_id and not job:
                        raise FileNotFoundError()
                    def read_phase_asset(index, name):
                        if (type(index) is not int or index < 0 or name not in {'document.json', 'inspect.json'}
                                or not job or index >= len(job.get('phases', []))):
                            raise ValueError('对象关系仅允许读取已保存阶段的定义和观察')
                        ref = job['phases'][index].get('assets', {}).get(name)
                        if not isinstance(ref, dict) or type(ref.get('size')) is not int or not 0 <= ref['size'] <= 2 * 1024 * 1024:
                            raise ValueError('阶段资源缺失或超过2MiB读取限制')
                        asset = recompute_lab.asset_path(job_id, index, name)
                        if asset.stat().st_size > 2 * 1024 * 1024:
                            raise ValueError('阶段资源超过2MiB读取限制')
                        with asset.open('rb') as source:
                            raw = source.read(2 * 1024 * 1024 + 1)
                        if len(raw) != ref['size']:
                            raise ValueError('阶段资源长度变化')
                        return raw
                    with flow_lock:
                        plan = build_recompute(archive, **ids)
                        value = build_lifecycle(plan, job, read_phase_asset if job else None)
                    data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
                    if len(data) > 2 * 1024 * 1024:
                        raise ValueError('对象关系视图超过2MiB响应预算')
                    self.send_data(data)
                elif path == '/api/recompute':
                    if set(query) - {'before_id', 'after_id'} or any(len(v) != 1 for v in query.values()):
                        raise ValueError('重算视图只接受观察编号')
                    ids = {k: int(v[0]) for k, v in query.items()}
                    if any(not 1 <= n <= 2**63 - 1 for n in ids.values()) or bool('before_id' in ids) != bool('after_id' in ids):
                        raise ValueError('请选择完整的一对观察编号')
                    with flow_lock:
                        value = build_recompute(archive, **ids)
                    value['execution_enabled'] = recompute_lab is not None
                    data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
                    if len(data) > 4 * 1024 * 1024:
                        raise ValueError('重算视图超过4MiB预算')
                    self.send_data(data)
                elif path == '/api/recompute/jobs' and not query:
                    self.reply({'jobs': recompute_lab.list_jobs() if recompute_lab else []})
                elif re.fullmatch(r'/api/recompute/jobs/[a-f0-9]{32}', path) and not query:
                    self.result(recompute_lab.get(path.rsplit('/', 1)[1]) if recompute_lab else None)
                elif re.fullmatch(r'/api/recompute/jobs/[a-f0-9]{32}/phases/[0-9]{1,2}/assets/[A-Za-z0-9_.-]{1,80}', path) and not query:
                    if not recompute_lab:
                        raise FileNotFoundError()
                    parts = path.split('/')
                    asset = recompute_lab.asset_path(parts[4], int(parts[6]), parts[8])
                    if asset.stat().st_size > 64 * 1024 * 1024:
                        raise ValueError('单个资源超过64MiB读取预算')
                    data = asset.read_bytes()
                    mime = 'application/json; charset=utf-8' if asset.suffix == '.json' else 'application/octet-stream'
                    self.send_data(data, content_type=mime)
                elif path == '/api/flow':
                    if set(query) - {'document_id', 'limit'} or any(len(v) != 1 for v in query.values()):
                        raise ValueError('流程参数不受支持')
                    document_id = query.get('document_id', [None])[0]
                    if document_id is not None and (len(document_id) > 200 or not re.fullmatch(r'[A-Za-z0-9_-]+', document_id)):
                        raise ValueError('文档编号无效')
                    limit = int(query.get('limit', ['200'])[0])
                    if not 1 <= limit <= 200:
                        raise ValueError('流程窗口须为1到200条观察')
                    with flow_lock:
                        with archive._lock:
                            epoch = (archive._db.execute('PRAGMA data_version').fetchone()[0], archive._db.total_changes)
                        key = (document_id, limit, epoch)
                        cached = flow_cache.get(key)
                        if cached and time.monotonic() - cached[0] < 2:
                            data = cached[1]
                        else:
                            value = build_flow(archive, document_id=document_id, limit=limit)
                            value['view_mode'] = 'recorder' if recorder else ('read_only' if archive.read_only else 'offline')
                            data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
                            if len(data) > 4 * 1024 * 1024:
                                raise ValueError('流程数据超过4MiB，请缩小观察窗口')
                            if len(flow_cache) >= 4:
                                flow_cache.clear()
                            flow_cache[key] = (time.monotonic(), data)
                    self.send_data(data)
                elif path == '/geometry.html' and not query:
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
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
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
                body = json.loads(self.rfile.read(length), object_pairs_hook=unique_fields)
                if not isinstance(body, dict):
                    raise ValueError('请求体必须是对象')
                if self.path == '/api/recompute/jobs' and set(body) == {'before_id', 'after_id', 'mode'}:
                    if not recompute_lab:
                        raise ValueError('当前服务未启用隔离CLI实验')
                    if type(body['before_id']) is not int or type(body['after_id']) is not int or body['mode'] not in ('edit_undo', 'full'):
                        raise ValueError('实验参数无效')
                    self.reply(recompute_lab.submit(body['before_id'], body['after_id'], body['mode']), 202)
                elif re.fullmatch(r'/api/recompute/jobs/[a-f0-9]{32}/cancel', self.path) and body == {}:
                    if not recompute_lab:
                        raise ValueError('隔离CLI实验未启用')
                    job_id = self.path.split('/')[4]
                    recompute_lab.cancel(job_id)
                    self.result(recompute_lab.get(job_id))
                elif self.path == '/api/capture' and body == {}:
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


def create_server(archive, recorder=None, rebuilder=None, version='unknown', port=0, recompute_lab=None):
    http = LocalServer(('127.0.0.1', port), BaseHTTPRequestHandler)
    http.RequestHandlerClass = make_handler(archive, recorder, rebuilder, version, secrets.token_urlsafe(32), http.server_port, recompute_lab)
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
    parser.add_argument('--recompute-lab', action='store_true', help='在独立CLI中演示重算，仅读取原始档案')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args(argv)
    if not .25 <= args.limit_gib <= 1024 or not 5 <= args.rebuild_timeout <= 3600:
        parser.error('预算需为 0.25..1024 GiB，超时需为 5..3600 秒')
    if not 0 <= args.port <= 65535 or (args.control_port is not None and not 1 <= args.control_port <= 65535):
        parser.error('端口无效')
    if (args.offline or args.read_only) and not args.archive:
        parser.error('离线/只读模式需指定 --archive')
    if args.recompute_lab and not args.read_only:
        parser.error('--recompute-lab 需要 --read-only，独立实验不与记录服务的阶段重建并发')
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
    recorder = rebuilder = http = lab = None
    try:
        if choice:
            recorder = Recorder(archive, choice[0], version, choice[1])
        if args.cli.is_file() and not args.read_only:
            rebuilder = Rebuilder(archive, args.cli.resolve(), version, timeout=args.rebuild_timeout)
        if args.recompute_lab and args.cli.is_file():
            from recompute_lab import RecomputeLab
            import hashlib
            archive_key = hashlib.sha256(str(archive.root.resolve()).encode('utf-8')).hexdigest()[:20]
            lab = RecomputeLab(archive, args.cli.resolve(), ROOT / 'recompute-runs' / archive_key, version, timeout=min(args.rebuild_timeout, 600))
        http = create_server(archive, recorder, rebuilder, version, args.port, recompute_lab=lab)
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
        if lab:
            lab.stop()
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
