"""Read-only local SolidCraft topology viewer. Python 3.10+, no dependencies."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

from topology_geometry import GeometryError, GeometryLimitError, build_body_stage
from solidcraft_instances import (InstanceBinding, SelectedInstanceChanged,
    default_control_dir, discover_bindings, read_binding, verify_binding)

ROOT = Path(__file__).resolve().parent
MAX_REPLY = 16 * 1024 * 1024
MAX_DATA = 16 * 1024 * 1024
MAX_BODIES = 32
MAX_DISPLAY_WORK = 8_000_000
COOKIE = 'solidcraft_topology_session'
READ_COMMANDS = frozenset({'model.faces', 'model.edges'})


class SourceChanged(RuntimeError):
    """An uncommitted snapshot must not be published."""


class SnapshotLimitError(RuntimeError):
    """Wait for a different model instead of rebuilding an unsupported snapshot repeatedly."""


class NameIntegrityError(SnapshotLimitError):
    """A display adapter changed or lost records returned by the native queries."""


def _same_native_value(actual, expected):
    # Python's ordinary equality treats False == 0 and True == 1. The raw API
    # field types are evidence too, so a boolean cannot silently become a number.
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _same_native_value(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _same_native_value(a, b) for a, b in zip(actual, expected))
    return actual == expected


def verify_stage_names(stage, faces, edges, *, body_name=None):
    """Compare adapted entities to independently retained native query records.

    This checks current-snapshot extraction, not the lifetime stability of the
    native naming algorithm or the display geometry's correspondence.
    """
    def reject(reason):
        raise NameIntegrityError('原生名称校验失败：' + reason + '；保留上次快照。')

    def indexed(records, label):
        if not isinstance(records, list):
            reject(label + '记录不是列表')
        result = {}
        for record in records:
            if not isinstance(record, dict):
                reject(label + '记录无效')
            index = record.get('index')
            if type(index) is not int or index < 0 or index in result:
                reject(label + '索引无效或重复')
            result[index] = record
        return result

    if not isinstance(stage, dict):
        reject('显示快照无效')
    if body_name is not None and stage.get('bodyName') != body_name:
        reject('实体名称与原生查询不一致')
    checked = []
    for kind, original, command in (('faces', faces, 'model.faces'), ('edges', edges, 'model.edges')):
        source = indexed(original, command)
        output = indexed(stage.get(kind), kind)
        if source.keys() != output.keys():
            reject(command + '元素数量或索引集合与原生查询不一致')
        for index, native in source.items():
            entity = output[index]
            if not isinstance(native.get('name'), str) or not native['name']:
                reject(command + '未提供有效名称')
            if type(entity.get('name')) is not str or entity['name'] != native['name']:
                reject(command + '名称与原生查询不一致')
            if ('exact' in entity) != ('exact' in native):
                reject(command + '的 exact 字段被添加或遗漏')
            if 'exact' in native and not _same_native_value(entity['exact'], native['exact']):
                reject(command + '的 exact 字段与原生查询不一致')
            if entity.get('nameSource') != command:
                reject(command + '名称来源标记不一致')
            if not _same_native_value(entity.get('nativeRecord'), native):
                reject(command + '原始记录被修改或遗漏')
            checked.append(entity)
    # Stamp only after the whole stage passes, never partially approve a stage.
    for entity in checked:
        entity['nameAudit'] = 'native-records-match'


class ControlClient:
    """A single-worker, authenticated, bounded JSON-lines control connection."""

    def __init__(self, binding: InstanceBinding, *, runtime_dir: Path | None = None):
        self.binding = binding
        self.runtime_dir = runtime_dir.resolve() if runtime_dir is not None else None
        self.sock = None
        self.reader = None
        self.counter = 0
        self.connection_lock = threading.Lock()

    def close(self):
        with self.connection_lock:
            sock, reader = self.sock, self.reader
            self.sock = self.reader = None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if reader:
            try:
                reader.close()
            except OSError:
                pass
        if sock:
            sock.close()

    def connect(self, *, timeout=3):
        self.close()
        # This launch is selected once at startup. Never follow current.port or
        # adopt a freshly written token when the selected app disappears.
        verify_binding(self.binding)
        sock = socket.create_connection(('127.0.0.1', self.binding.port), timeout=timeout)
        sock.settimeout(5)
        reader = sock.makefile('rb')
        with self.connection_lock:
            self.sock, self.reader = sock, reader
        # The token never enters argv, HTTP responses or logs.
        try:
            revision_key(self._request('document.revision', {}, token=self.binding.token, timeout=timeout))
            verify_binding(self.binding)
        except Exception:
            self.close()
            raise

    def _request(self, method, params, *, token=None, timeout=5):
        self.counter += 1
        request = {'id': self.counter, 'method': method, 'params': params}
        if token:
            request['token'] = token
        with self.connection_lock:
            sock, reader = self.sock, self.reader
        if sock is None or reader is None:
            raise ConnectionError('SolidCraft 已断开连接')
        sock.settimeout(timeout)
        sock.sendall(json.dumps(request, separators=(',', ':')).encode('utf-8') + b'\n')
        try:
            for _ in range(128):
                line = reader.readline(MAX_REPLY + 1)
                if not line:
                    raise ConnectionError('SolidCraft 已断开连接')
                if len(line) > MAX_REPLY or not line.endswith(b'\n'):
                    raise RuntimeError('SolidCraft 返回的数据超过本工具的容量上限')
                reply = json.loads(line)
                if reply.get('id') != self.counter:
                    continue
                if not reply.get('ok'):
                    raise RuntimeError(str(reply.get('error', 'SolidCraft 请求失败')))
                return reply.get('result')
            raise RuntimeError('SolidCraft 返回了过多无关消息')
        except Exception:
            self.close()
            raise

    def request(self, method, params=None, *, timeout=5):
        if method not in {'document.revision', 'document.inspect', 'control.running'}:
            raise ValueError('Only read-only document methods are allowed')
        if not self.sock:
            self.connect()
        return self._request(method, params or {}, timeout=timeout)

    def query(self, command, params):
        if command not in READ_COMMANDS:
            raise ValueError('Only named face/edge queries are allowed')
        if not self.sock:
            self.connect()
        return self._request('engine.execute', {'command': command, 'params': params}, timeout=8)

    def export_mesh(self, destination: Path):
        # No path or command supplied by the browser can reach this method.
        destination = destination.resolve()
        if self.runtime_dir is None or destination != self.runtime_dir / 'snapshot.3mf':
            raise ValueError('Mesh export must use this viewer session\'s one owned file')
        if not self.sock:
            self.connect()
        return self._request('engine.execute', {'command': 'file.export', 'params': {'path': str(destination), 'format': '3mf-tagged'}}, timeout=20)


def revision_key(reply):
    if not isinstance(reply, dict) or not isinstance(reply.get('document_id'), str) or not isinstance(reply.get('revision'), int):
        raise RuntimeError('当前 SolidCraft 版本未返回有效的文档版本号')
    return reply['document_id'], reply['revision']


def safe_error(error):
    if isinstance(error, SelectedInstanceChanged):
        return '所选 SolidCraft 实例已退出或身份已变化；保留上次快照。请重新启动查看器并选择实例。'
    if isinstance(error, (FileNotFoundError, ConnectionError, ConnectionRefusedError)):
        return '等待 SolidCraft；请启动桌面程序并开启 AI 控制通道。'
    if isinstance(error, (TimeoutError, socket.timeout)):
        return 'SolidCraft 暂未响应；保留上次结果，等待后自动重连。'
    text = re.sub(r'[0-9a-fA-F]{32,}', '[redacted]', str(error))
    return text[:300] or type(error).__name__


class ViewerState:
    def __init__(self, instance=None):
        self.lock = threading.Lock()
        self.instance = instance
        self.status = 'waiting'
        self.error = None
        self.generation = 0
        self.updated_at = None
        self.source = None
        self.data = None
        self.refresh_requested = 0
        self.refresh_completed = 0
        self.last_client = time.monotonic()

    def transition(self, status, error=None):
        with self.lock:
            self.status, self.error = status, error

    def publish(self, data, source):
        # Validate response bytes before retaining the new snapshot.
        if len(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()) > MAX_DATA:
            raise SnapshotLimitError('模型可视化数据超过 16 MiB；请简化模型后重试。')
        with self.lock:
            self.data, self.source = data, source
            self.generation += 1
            self.updated_at = datetime.now(timezone.utc).isoformat()
            self.status, self.error = 'ready', None

    def read(self, after=-1, *, touch=True):
        with self.lock:
            if touch:
                self.last_client = time.monotonic()
            result = {'status': self.status, 'last_error': self.error, 'generation': self.generation,
                      'instance': self.instance,
                      'updated_at': self.updated_at, 'source': self.source,
                      'refresh_requested': self.refresh_requested, 'refresh_completed': self.refresh_completed}
            if self.data is not None and after != self.generation:
                result['data'] = self.data
            return result

    def request_refresh(self):
        with self.lock:
            self.last_client = time.monotonic()
            if self.refresh_requested <= self.refresh_completed:
                self.refresh_requested = self.refresh_completed + 1
            return self.refresh_requested

    def pending_refresh(self):
        with self.lock:
            return self.refresh_requested if self.refresh_requested > self.refresh_completed else 0

    def complete_refresh(self, request_id):
        with self.lock:
            self.refresh_completed = max(self.refresh_completed, request_id)

    def has_client(self):
        with self.lock:
            return time.monotonic() - self.last_client < 15


class Collector:
    def __init__(self, state, runtime_dir, *, client, interval=.7, version='0.30.1'):
        self.state = state
        self.runtime_dir = runtime_dir
        self.client = client
        self.interval = max(.4, interval)
        self.version = version
        self.stop_event = threading.Event()
        self.last_key = None
        self.pending_key = None
        self.pending_since = 0
        self.failed_key = None
        self.last_capture = 0
        self.thread = threading.Thread(target=self.run, name='solidcraft-read-only-collector', daemon=True)

    def capture(self, key):
        self.state.transition('capturing')
        # Only the dedicated document.revision counter defines snapshot consistency.
        inspected = self.client.request('document.inspect', {'measure': True, 'sketch_counts': True}, timeout=12)
        bodies = inspected.get('bodies', [])
        if len(bodies) > MAX_BODIES:
            raise SnapshotLimitError(f'当前有 {len(bodies)} 个实体，本工具上限为 {MAX_BODIES} 个。')
        queries = []
        for body in bodies:
            name = body.get('name')
            if not isinstance(name, str) or len(name) > 4096:
                raise RuntimeError('SolidCraft 返回的实体名称无效')
            faces = self.client.query('model.faces', {'body': name}).get('faces', [])
            edges = self.client.query('model.edges', {'body': name}).get('edges', [])
            queries.append((body, faces, edges))
        mesh_path = self.runtime_dir / 'snapshot.3mf'
        if queries:
            self.client.export_mesh(mesh_path)
        if revision_key(self.client.request('document.revision')) != key:
            raise SourceChanged('模型在采集期间更新，正在重新采集。')
        stages = []
        name_audits = []
        retained_bytes = 0
        all_triangles = all_segments = all_labels = 0
        for index, (body, faces, edges) in enumerate(queries):
            native_faces, native_edges = copy.deepcopy(faces), copy.deepcopy(edges)
            native_body_name = body['name']
            stage = build_body_stage(body, faces, edges, mesh_path, 'body' + str(index))
            verify_stage_names(stage, native_faces, native_edges, body_name=native_body_name)
            name_audits.append((stage, native_faces, native_edges, native_body_name))
            segments = sum(max(0, len(edge.get('points', [])) - 1) for edge in stage['edges'])
            display_work = len(stage['mesh']['triangles']) * (segments + len(stage['faces']) + len(stage['edges']) + len(stage['points']))
            if display_work > MAX_DISPLAY_WORK:
                raise SnapshotLimitError('该实体超出当前软件渲染预算；保留上次快照。请简化实体后重试。')
            stage['stats']['displayWork'] = display_work
            all_triangles += len(stage['mesh']['triangles'])
            all_segments += segments
            all_labels += len(stage['faces']) + len(stage['edges']) + len(stage['points'])
            stage['visible'] = body.get('visible', True)
            stage['features'] = inspected.get('timeline', [])
            stage['warning'] = '; '.join(str(f['warning']) for f in stage['features'] if f.get('warning')) or None
            retained_bytes += len(json.dumps(stage, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
            if retained_bytes > MAX_DATA:
                raise SnapshotLimitError('多个实体的显示数据超过 16 MiB；保留上次快照。')
            stages.append(stage)
        # Parsing can take time too: do not publish names from an already superseded design.
        if revision_key(self.client.request('document.revision')) != key:
            raise SourceChanged('模型在采集期间更新，正在重新采集。')
        for stage, native_faces, native_edges, native_body_name in name_audits:
            verify_stage_names(stage, native_faces, native_edges, body_name=native_body_name)
        all_view_work = all_triangles * (all_segments + all_labels)
        data = {'meta': {'version': self.version, 'design': inspected.get('name', 'Untitled'),
            'allViewWork': all_view_work, 'allViewAllowed': all_view_work <= MAX_DISPLAY_WORK,
            'vertexNote': 'P 是查看器对边端点坐标去重生成的编号，重合但拓扑不同的顶点可能合并。它不是原生顶点名；相邻边引用对象仅供说明，未写入 SolidCraft。',
            'geometryNote': '面、边名称与 SolidCraft 原始查询逐项核对；exact 是原生历史来源状态。面按原生面索引标签显示，边使用明确标注的网格重建；不能唯一对应的具名边不绘制。操作提交、重算完成后更新。',
            'sourceLinks': [{'title': '持久命名', 'url': 'https://github.com/tangency-labs/solidcraft/blob/v0.30.1/docs/book/src/dev/naming.md'},
                            {'title': '顶点通过相邻边名定位', 'url': 'https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/doc/src/construct.rs#L214-L242'}],
            'sketches': inspected.get('sketches', [])}, 'stages': stages}
        source = {'document_id': key[0], 'revision': key[1], 'name': inspected.get('name', 'Untitled'), 'version': self.version}
        self.state.publish(data, source)
        self.last_key = key
        self.failed_key = None
        self.last_capture = time.monotonic()

    def run(self):
        failures = 0
        try:
            while not self.stop_event.is_set():
                if not self.state.has_client():
                    self.stop_event.wait(1)
                    continue
                try:
                    refresh_id = self.state.pending_refresh()
                    key = revision_key(self.client.request('document.revision'))
                    if key == self.failed_key and not refresh_id:
                        self.stop_event.wait(self.interval)
                        continue
                    now = time.monotonic()
                    if key != self.last_key or refresh_id:
                        if key != self.pending_key:
                            self.pending_key, self.pending_since = key, now
                        elif now - self.pending_since >= .35 and now - self.last_capture >= 1:
                            try:
                                running = self.client.request('control.running').get('running', [])
                            except RuntimeError as error:
                                if 'unknown method' not in str(error):
                                    raise
                                running = []
                            if not running:
                                self.capture(key)
                                self.state.complete_refresh(refresh_id)
                    elif self.state.read(touch=False)['status'] in {'error', 'waiting', 'capturing'}:
                        self.state.transition('ready')
                    failures = 0
                    self.stop_event.wait(self.interval)
                except SourceChanged:
                    self.pending_key = None
                    self.stop_event.wait(self.interval)
                except (SnapshotLimitError, GeometryError) as error:
                    self.failed_key = key
                    self.state.transition('error', safe_error(error))
                    self.state.complete_refresh(refresh_id)
                    self.stop_event.wait(self.interval)
                except Exception as error:
                    self.client.close()
                    failures = min(failures + 1, 4)
                    self.state.transition('waiting' if isinstance(error, OSError) else 'error', safe_error(error))
                    self.state.complete_refresh(refresh_id)
                    self.stop_event.wait(min(10, 1.5 * 2 ** (failures - 1)))
        finally:
            self.client.close()

    def stop(self):
        self.stop_event.set()
        self.client.close()
        if self.thread.is_alive():
            self.thread.join(3)


class LocalServer(HTTPServer):
    request_queue_size = 8
    allow_reuse_address = False

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        return connection, address

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # Closing a tab or cancelling fetch is an ordinary local disconnect.
        super().handle_error(request, client_address)


def make_handler(state, session, html, port):
    expected_host = f'127.0.0.1:{port}'
    expected_origin = 'http://' + expected_host
    cookie_name = f'{COOKIE}_{port}'

    class Handler(BaseHTTPRequestHandler):
        server_version = 'SolidCraftTopology/1'

        def log_message(self, *_args):
            pass

        def send_bytes(self, status, payload=b'', content_type='application/json; charset=utf-8', *, cookie=False):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            if cookie:
                self.send_header('Set-Cookie', f'{cookie_name}={session}; HttpOnly; SameSite=Strict; Path=/')
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def allowed_request(self):
            if (len(self.path) > 2048 or self.headers.get('Host') != expected_host
                    or self.headers.get('Origin') not in (None, expected_origin)
                    or self.headers.get('Sec-Fetch-Site') not in (None, 'none', 'same-origin')):
                self.send_bytes(403, b'{"error":"forbidden"}')
                return False
            return True

        def authenticated(self):
            cookies = SimpleCookie()
            try:
                cookies.load(self.headers.get('Cookie', ''))
                supplied = cookies[cookie_name].value if cookie_name in cookies else ''
            except Exception:
                supplied = ''
            if not supplied.isascii() or len(supplied) > 128 or not secrets.compare_digest(supplied, session):
                self.send_bytes(403, b'{"error":"open the viewer page first"}')
                return False
            return True

        def do_GET(self):
            if not self.allowed_request():
                return
            try:
                parsed = urlsplit(self.path)
            except ValueError:
                self.send_bytes(400, b'{"error":"invalid URL"}')
                return
            if parsed.path in ('/', '/live-viewer.html'):
                state.read(touch=True)
                self.send_bytes(200, html, 'text/html; charset=utf-8', cookie=True)
                return
            if parsed.path == '/favicon.ico':
                self.send_bytes(204)
                return
            if not self.authenticated():
                return
            if parsed.path == '/api/state':
                try:
                    query = parse_qs(parsed.query, max_num_fields=4)
                except ValueError:
                    self.send_bytes(400, b'{"error":"too many query fields"}')
                    return
                text = query.get('after', ['-1'])[0]
                if not re.fullmatch(r'-?\d{1,18}', text):
                    self.send_bytes(400, b'{"error":"invalid generation"}')
                    return
                payload = json.dumps(state.read(int(text)), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                self.send_bytes(200, payload)
            else:
                self.send_bytes(404, b'{"error":"not found"}')

        def do_POST(self):
            if not self.allowed_request() or not self.authenticated():
                return
            if self.path != '/api/refresh':
                self.send_bytes(405, b'{"error":"read-only CAD access"}')
                return
            if self.headers.get('Content-Length', '0') != '0' or self.headers.get('Transfer-Encoding'):
                self.send_bytes(400, b'{"error":"refresh takes no body or command"}')
                return
            request_id = state.request_refresh()
            payload = json.dumps({'request_id': request_id, 'status': 'queued'}).encode('utf-8')
            self.send_bytes(202, payload)

    return Handler


def installed_version():
    cli = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'SolidCraft' / 'solidcraft-cli.exe'
    try:
        kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        result = subprocess.run([str(cli), '--version'], capture_output=True, text=True, timeout=5, check=True, **kwargs)
        return result.stdout.strip().removeprefix('solidcraft-cli ')
    except (OSError, subprocess.SubprocessError):
        return 'installed version'


def display_name(value):
    """Keep document titles readable without allowing terminal control sequences."""
    return ''.join(c if c.isprintable() else ' ' for c in str(value))[:120] or 'Untitled'


def probe_instance(binding, *, timeout=2):
    client = ControlClient(binding)
    try:
        client.connect(timeout=timeout)
        document = client.request('document.inspect', {'measure': False, 'sketch_counts': False}, timeout=timeout)
        if not isinstance(document, dict):
            raise RuntimeError('SolidCraft 文档信息无效')
        return binding, display_name(document.get('name', 'Untitled'))
    finally:
        client.close()


def available_instances(control_dir):
    bindings = discover_bindings(control_dir)
    if len(bindings) > 32:
        raise RuntimeError('发现的实例记录超过 32 个，请使用 --control-port 明确指定实例。')
    def probe(binding):
        try:
            return probe_instance(binding)
        except (OSError, RuntimeError, ValueError):
            return None  # Stale records are not offered as running instances.
    with ThreadPoolExecutor(max_workers=4) as executor:
        return [choice for choice in executor.map(probe, bindings) if choice is not None]


def print_instances(choices, *, output=print):
    for index, (binding, name) in enumerate(choices, 1):
        output(f'  {index}. {name}  | PID {binding.pid or "未知"} | SolidCraft 控制端口 {binding.port}')


def select_instance(control_dir, control_port=None, *, input_func=None, output=print):
    if control_port is not None:
        return probe_instance(read_binding(control_dir, control_port), timeout=3)
    while True:
        choices = available_instances(control_dir)
        output('当前账号可连接的 SolidCraft 实例：')
        print_instances(choices, output=output)
        if not choices:
            raise RuntimeError('未找到可连接的实例。请先启动 SolidCraft、开启 AI 控制通道，再运行查看器。')
        prompt = '选择实例编号' + (' [回车选 1]' if len(choices) == 1 else '') + '，R 重新扫描，Q 退出：'
        while True:
            try:
                answer = (input_func or input)(prompt).strip().lower()
            except EOFError:
                raise RuntimeError('需要选择 SolidCraft 实例；非交互启动请指定 --control-port。') from None
            if answer == 'q':
                return None
            if answer == 'r':
                break
            if not answer and len(choices) == 1:
                answer = '1'
            if re.fullmatch(r'\d{1,2}', answer) and 1 <= int(answer) <= len(choices):
                choice = choices[int(answer) - 1]
                verify_binding(choice[0])
                return choice
            output('请输入列表中的编号、R 或 Q。')


def create_http_server(state, html, port=0):
    # Bind port 0 directly: the OS allocates and reserves a free port atomically.
    # Probing a free port then closing/reopening it would race another user.
    http = LocalServer(('127.0.0.1', port), None)
    try:
        http.RequestHandlerClass = make_handler(state, secrets.token_urlsafe(32), html, http.server_port)
        return http
    except Exception:
        http.server_close()
        raise


def create_runtime_dir():
    runtime_root = Path(tempfile.gettempdir()) / 'SolidCraftTopologyViewer'
    # On Windows inherit the user's TEMP ACL. Python's special mode-0700 temp
    # directory creation can deny a restricted launcher's own subsequent access.
    mode = 0o777 if os.name == 'nt' else 0o700
    runtime_root.mkdir(mode=mode, exist_ok=True)
    runtime_dir = runtime_root / ('session-' + secrets.token_hex(12))
    runtime_dir.mkdir(mode=mode)
    return runtime_dir.resolve()


def parse_options(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0, help='Viewer HTTP port; default 0 lets the OS reserve a free port')
    parser.add_argument('--control-port', type=int, help='Select this SolidCraft control port in the current user control directory')
    parser.add_argument('--control-dir', type=Path, help='Override the current user SolidCraft control directory')
    parser.add_argument('--list-instances', action='store_true', help='List reachable current-user SolidCraft instances and exit')
    parser.add_argument('--interval', type=float, default=.7, help='Lightweight revision polling interval in seconds')
    parser.add_argument('--open', action='store_true', help='Open the viewer in the default browser')
    args = parser.parse_args(argv)
    if (args.port != 0 and not 1024 <= args.port <= 65535) or not .4 <= args.interval <= 30:
        parser.error('port must be 0 or 1024..65535 and interval 0.4..30')
    if args.control_port is not None and not 1 <= args.control_port <= 65535:
        parser.error('control-port must be 1..65535')
    return args


def main(argv=None):
    args = parse_options(argv)
    control_dir = args.control_dir or default_control_dir()
    if args.list_instances:
        choices = available_instances(control_dir)
        print_instances(choices)
        if not choices:
            print('未找到当前账号可连接的 SolidCraft 实例。')
        return 0
    choice = select_instance(control_dir, args.control_port)
    if choice is None:
        return 0
    binding, name = choice
    html = (ROOT / 'live-viewer.html').read_bytes()
    if len(html) > 1024 * 1024:
        raise RuntimeError('Viewer HTML is unexpectedly large')
    state = ViewerState({'control_port': binding.port, 'pid': binding.pid, 'selected_name': name})
    http = create_http_server(state, html, args.port)
    runtime_dir = None
    collector = None
    try:
        runtime_dir = create_runtime_dir()
        client = ControlClient(binding, runtime_dir=runtime_dir)
        collector = Collector(state, runtime_dir, client=client, interval=args.interval, version=installed_version())
        url = f'http://127.0.0.1:{http.server_port}/'
        print(f'已固定实例：{name} | PID {binding.pid or "未知"} | 控制端口 {binding.port}', flush=True)
        print(f'SolidCraft realtime topology viewer: {url}', flush=True)
        print('只读查看。断线仅重连所选实例；实例重启后请重新选择。按 Ctrl+C 停止。', flush=True)
        collector.thread.start()
        if args.open:
            webbrowser.open(url)
        http.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        pass
    finally:
        http.server_close()
        if collector is not None:
            collector.stop()
        # Delete only our known export file; never recursively delete a computed path.
        if runtime_dir is not None:
            try:
                (runtime_dir / 'snapshot.3mf').unlink(missing_ok=True)
                runtime_dir.rmdir()
            except OSError:
                pass  # A late native export can still own it. Preserve it rather than race it.
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except (OSError, RuntimeError, ValueError) as error:
        print('启动失败：' + safe_error(error), file=sys.stderr, flush=True)
        sys.exit(1)
