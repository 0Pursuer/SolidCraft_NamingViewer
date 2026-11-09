"""Fixed-launch, read-only control client and browser-independent recorder."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import shutil
import socket
import threading
import time
import uuid

from archive import ArchiveError, QuotaExceeded
from capture import collect_bundle, revision_key, SourceChanged, CaptureLimit
from solidcraft_instances import (verify_binding, discover_bindings, read_binding,
                                 SelectedInstanceChanged)

MAX_REPLY = 16 * 1024 * 1024


def safe_error(exc):
    text = re.sub(r'[0-9a-fA-F]{32,}', '[redacted]', str(exc))
    return ''.join(c if c.isprintable() else ' ' for c in text)[:500] or type(exc).__name__


class ControlClient:
    """One calling thread per connection. close() may interrupt it from another."""
    def __init__(self, binding, runtime_dir=None):
        self.binding = binding
        self.runtime_dir = Path(runtime_dir).resolve() if runtime_dir else None
        self.sock = None
        self.counter = 0
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.cancel_event = None

    def close(self):
        with self.lock:
            sock, self.sock = self.sock, None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def connect(self, timeout=3):
        self.close()
        if self.cancel_event and self.cancel_event.is_set():
            raise ConnectionError('记录器已停止')
        verify_binding(self.binding)
        sock = socket.create_connection(('127.0.0.1', self.binding.port), timeout)
        with self.lock:
            self.sock = sock
        self.buffer.clear()
        try:
            key = revision_key(self._request('document.revision', {}, timeout, self.binding.token))
            verify_binding(self.binding)
            return key
        except Exception:
            self.close()
            raise

    def receive(self, timeout=2):
        deadline = time.monotonic() + timeout
        while True:
            pos = self.buffer.find(b'\n')
            if pos >= 0:
                line = bytes(self.buffer[:pos])
                del self.buffer[:pos + 1]
                if len(line) > MAX_REPLY:
                    raise CaptureLimit('原生消息超过 16 MiB')
                return json.loads(line)
            if len(self.buffer) > MAX_REPLY:
                raise CaptureLimit('原生消息超过 16 MiB')
            sock = self.sock
            if not sock:
                raise ConnectionError('SolidCraft 连接已关闭')
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError('SolidCraft 请求超时')
            sock.settimeout(left)
            part = sock.recv(min(65536, MAX_REPLY + 1 - len(self.buffer)))
            if not part:
                raise ConnectionError('SolidCraft 已断线')
            self.buffer.extend(part)

    def _request(self, method, params, timeout=5, token=None):
        if self.cancel_event and self.cancel_event.is_set():
            raise ConnectionError('记录器已停止')
        self.counter += 1
        request = {'id': self.counter, 'method': method, 'params': params}
        if token:
            request['token'] = token
        sock = self.sock
        if not sock:
            raise ConnectionError('SolidCraft 未连接')
        sock.settimeout(timeout)
        sock.sendall(json.dumps(request).encode() + b'\n')
        deadline = time.monotonic() + timeout
        for _ in range(128):
            reply = self.receive(max(.01, deadline - time.monotonic()))
            if reply.get('id') != self.counter:
                continue
            if not reply.get('ok'):
                raise RuntimeError(safe_error(reply.get('error', '原生请求失败')))
            return reply.get('result')
        raise RuntimeError('原生接口返回过多无关消息')

    def request(self, method, params=None, timeout=5):
        if method not in {'document.revision', 'document.inspect', 'control.running', 'control.subscribe', 'ui.help'}:
            raise ValueError('不允许该原生方法')
        if method == 'ui.help' and params:
            raise ValueError('ui.help 只允许固定空参数读取版本')
        if not self.sock:
            self.connect()
        verify_binding(self.binding)
        try:
            return self._request(method, params or {}, timeout)
        except Exception:
            self.close()
            raise

    def query(self, command, params, timeout=12):
        if command not in {'sketch.inspect', 'model.faces', 'model.edges'}:
            raise ValueError('不允许该 CAD 命令')
        if not self.sock:
            self.connect()
        verify_binding(self.binding)
        try:
            return self._request('engine.execute', {'command': command, 'params': params}, timeout)
        except Exception:
            self.close()
            raise

    def export(self, destination, format, timeout=30):
        destination = Path(destination).resolve()
        expected = {'json': 'document.json', '3mf-tagged': 'model.3mf'}
        if (format not in expected or not self.runtime_dir or
                destination.parent != self.runtime_dir or destination.name != expected[format]):
            raise ValueError('只允许导出到采集任务自有文件')
        if not self.sock:
            self.connect()
        verify_binding(self.binding)
        try:
            return self._request('engine.execute', {'command': 'file.export',
                'params': {'path': str(destination), 'format': format}}, timeout)
        except Exception:
            self.close()
            raise


def probe_instance(binding, timeout=2):
    client = ControlClient(binding)
    try:
        client.connect(timeout)
        data = client.request('document.inspect', {}, timeout)
        name = ''.join(c if c.isprintable() else ' ' for c in str(data.get('name', 'Untitled')))[:120]
        return binding, name
    finally:
        client.close()


def available_instances(control_dir):
    bindings = discover_bindings(control_dir)
    if len(bindings) > 32:
        raise ValueError('实例记录超过 32 个，请指定 --control-port')
    def probe(binding):
        try:
            return probe_instance(binding)
        except (OSError, RuntimeError, ValueError):
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        return [x for x in pool.map(probe, bindings) if x is not None]


def select_instance(control_dir, control_port=None):
    if control_port:
        return probe_instance(read_binding(control_dir, control_port), 3)
    while True:
        choices = available_instances(control_dir)
        print('当前账号可连接的 SolidCraft 实例：')
        for i, (binding, name) in enumerate(choices, 1):
            print(f'  {i}. {name} | PID {binding.pid or "未知"} | 控制端口 {binding.port}')
        if not choices:
            raise RuntimeError('没有可连接实例。请开启 SolidCraft AI 控制通道；或使用 --offline 浏览归档。')
        answer = input('选择编号（单实例回车选1），R重新扫描，Q退出：').strip().lower()
        if answer == 'q':
            return None
        if answer == 'r':
            continue
        if not answer and len(choices) == 1:
            answer = '1'
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            result = choices[int(answer) - 1]
            verify_binding(result[0])
            return result


class Recorder:
    def __init__(self, archive, binding, version, name='Untitled', client_factory=ControlClient):
        self.archive, self.binding = archive, binding
        self.events_client, self.capture_client = client_factory(binding), client_factory(binding)
        try:
            info = self.capture_client.request('ui.help', {}, timeout=3)
            self.version = str(info.get('version', 'unknown'))[:200]
            self.build = {key: str(info.get(key, 'not_reported'))[:200] for key in ('commit', 'built')}
        except Exception:
            self.version, self.build = 'unknown', {'commit': 'not_reported', 'built': 'not_reported'}
            self.capture_client.close()
        self.session_id = archive.new_session({'pid': binding.pid, 'port': binding.port, 'version': self.version, **self.build, 'name': name})
        self.stop_event = threading.Event()
        self.events_client.cancel_event = self.stop_event
        self.capture_client.cancel_event = self.stop_event
        self.condition = threading.Condition()
        self.pending = None
        self.paused = False
        self.permanent = False
        self.state, self.error = 'connecting', None
        self.threads = []

    def start(self):
        for fn, name in [(self._subscribe, 'trace-events'), (self._capture, 'trace-capture')]:
            thread = threading.Thread(target=fn, name=name, daemon=True)
            self.threads.append(thread)
            thread.start()

    def status(self):
        with self.condition:
            return {'recording': not self.paused and not self.permanent and not self.stop_event.is_set(),
                'status': self.state, 'error': self.error, 'session_id': self.session_id,
                'version': self.version, 'build': self.build,
                'instance': {'pid': self.binding.pid, 'control_port': self.binding.port}}

    def _state(self, state, error=None):
        with self.condition:
            self.state, self.error = state, error

    def pause(self, paused):
        with self.condition:
            if self.permanent:
                raise RuntimeError('实例已退出或身份变化，请重新启动查看器选择实例')
            if not paused and not self.archive.status().get('recording_allowed', True):
                raise QuotaExceeded('归档空间不足；请提高预算后重启')
            self.paused = paused
            self.state = 'paused' if paused else 'connecting'
            self.condition.notify_all()
        # Reconnect on resume to obtain the actual latest revision and an explicit gap.
        self.events_client.close()

    def request_capture(self):
        with self.condition:
            if self.paused or self.permanent:
                raise RuntimeError('记录已暂停或实例已停止')
            self.condition.notify_all()
        # subscriber owns its socket; reconnect supplies a fresh revision safely
        self.events_client.close()
        return {'status': 'queued'}

    def _event(self, key, kind, raw=None):
        event = self.archive.add_event(self.session_id, key[0], key[1], kind, raw=raw)
        with self.condition:
            if self.pending:
                self.archive.set_event_status(self.pending[0], 'superseded', '被后续版本越过，未取得完整快照')
            self.pending = (event, key)
            self.condition.notify_all()
        return event

    def _subscribe(self):
        connected_before, failures = False, 0
        while not self.stop_event.is_set() and not self.permanent:
            if self.paused:
                self.stop_event.wait(.3)
                continue
            try:
                key = self.events_client.connect()
                # Native notifications are not transactions and may coalesce changes.
                key = revision_key(self.events_client.request('control.subscribe', {}))
                self._event(key, 'reconnect' if connected_before else 'initial',
                    {'note': '断线/暂停间隔内的操作不可补回' if connected_before else '开始记录后的首份观察'})
                connected_before, failures = True, 0
                self._state('recording')
                while not self.stop_event.is_set() and not self.paused:
                    verify_binding(self.binding)
                    try:
                        message = self.events_client.receive(1)
                    except (TimeoutError, socket.timeout):
                        continue
                    if message.get('event') == 'design.changed':
                        params = message.get('params', {})
                        key = revision_key(params)
                        self._event(key, 'design.changed', {'event': 'design.changed', 'params': params})
            except SelectedInstanceChanged as exc:
                self.permanent = True
                self._state('instance_stopped', safe_error(exc))
                self._gap('实例已退出或身份改变；不会连接同端口的新实例')
            except QuotaExceeded as exc:
                self.paused = True
                self._state('quota_paused', safe_error(exc))
            except ArchiveError as exc:
                self.paused = True
                self._state('storage_paused', safe_error(exc))
            except Exception as exc:
                if not self.stop_event.is_set() and not self.paused:
                    self._state('disconnected', safe_error(exc))
                    if failures == 0:
                        self._gap('连接中断；此后的未观察操作无法补回。' + safe_error(exc))
                    failures += 1
                    self.stop_event.wait(min(5, .5 * 2 ** min(failures, 4)))
            finally:
                self.events_client.close()

    def _gap(self, message):
        try:
            self.archive.add_event(self.session_id, None, None, 'gap', status='gap', message=message)
        except Exception:
            pass  # A full archive cannot grow indefinitely to report that it is full.

    def _event_status(self, event_id, status, message):
        try:
            self.archive.set_event_status(event_id, status, message)
        except Exception as exc:
            self.paused = True
            self._state('storage_paused', safe_error(exc))

    def _capture(self):
        retry_key, retry_count = None, 0
        while not self.stop_event.is_set():
            with self.condition:
                self.condition.wait_for(lambda: self.stop_event.is_set() or (self.pending and not self.paused), timeout=.5)
                if self.stop_event.is_set():
                    break
                if not self.pending or self.paused:
                    continue
                self.condition.wait(.2)
                if self.paused or self.stop_event.is_set():
                    continue
                event_id, key = self.pending
                self.pending = None
                if key != retry_key:
                    retry_key, retry_count = key, 0
            workdir = self.archive.root / '.work' / ('capture-' + uuid.uuid4().hex)
            try:
                if self.archive.status().get('quota_exceeded'):
                    raise QuotaExceeded('归档空间预留已不足；请提高预算后重启')
                with self.archive.reserve(192 * 1024 * 1024, workdir=workdir):
                    try:
                        workdir.mkdir(parents=True)
                        self.capture_client.runtime_dir = workdir.resolve()
                        assets, summary = collect_bundle(self.capture_client, workdir, self.version, expected_key=key)
                        summary['native_build'] = dict(self.build)
                        if self.version == 'unknown':
                            summary['warnings'].append('现场程序版本接口不可用，版本未知；不能确认与离线 CLI 版本一致')
                        if self.stop_event.is_set() or self.paused or self.permanent:
                            raise ConnectionError('停止或暂停前未完成的采集不发布')
                        self.archive.commit_snapshot(event_id, assets, summary)
                    finally:
                        shutil.rmtree(workdir, ignore_errors=True)
                if not self.paused:
                    self._state('recording')
                retry_count = 0
            except SourceChanged as exc:
                self._event_status(event_id, 'superseded', safe_error(exc))
            except SelectedInstanceChanged as exc:
                self.permanent = True
                self._state('instance_stopped', safe_error(exc))
                self.events_client.close()
                self._event_status(event_id, 'gap', safe_error(exc))
            except QuotaExceeded as exc:
                self.paused = True
                self._state('quota_paused', safe_error(exc))
                self.events_client.close()
                self._event_status(event_id, 'quota_paused', safe_error(exc))
            except Exception as exc:
                if not self.paused and not self.permanent:
                    self._state('capture_failed', safe_error(exc))
                self.capture_client.close()
                self._event_status(event_id, 'incomplete', safe_error(exc))
                if isinstance(exc, (OSError, ConnectionError)) and retry_count < 2:
                    with self.condition:
                        if not self.pending and not self.paused and not self.permanent and not self.stop_event.is_set():
                            retry_count += 1
                            try:
                                self._event(key, 'capture_retry', {'retry_of': event_id, 'attempt': retry_count})
                            except Exception as storage_error:
                                self.paused = True
                                self._state('storage_paused', safe_error(storage_error))
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

    def stop(self):
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        self.events_client.close()
        self.capture_client.close()
        for thread in self.threads:
            while thread.is_alive():
                thread.join(1)
        with self.condition:
            if self.pending:
                self._event_status(self.pending[0], 'gap', '查看器停止前尚未采集')
                self.pending = None
