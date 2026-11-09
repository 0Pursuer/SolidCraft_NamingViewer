"""Protocol simulator exercises lifecycle and consistency without any user document."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from archive import Archive, QuotaExceeded
from native import Recorder, ControlClient
from solidcraft_instances import read_binding


def wait_for(fn, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(.03)
    raise AssertionError('simulated condition timed out')


class SimulatedCad:
    def __init__(self, folder):
        self.key = {'document_id': 'doc-one', 'revision': 1}
        self.value = 20
        self.undo, self.redo = 3, 0
        self.token = 'a' * 64
        self.connections = []
        self.subscribers = []
        self.commands = []
        self.lock = threading.RLock()
        self.change_during_export = False
        self.block_export = None
        self.export_entered = threading.Event()
        cad = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                send_lock = threading.Lock()
                cad.connections.append(self.request)
                authenticated = False
                try:
                    while True:
                        line = self.rfile.readline(17 * 1024 * 1024)
                        if not line:
                            return
                        req = json.loads(line)
                        if not authenticated:
                            if req.get('token') != cad.token:
                                return
                            authenticated = True
                        method = req['method']
                        with cad.lock:
                            cad.commands.append((method, req.get('params')))
                            result = dict(cad.key)
                            if method == 'document.inspect':
                                result = {'name': 'Simulated', 'undo': cad.undo, 'redo': cad.redo,
                                    'timeline': [{'id': 1, 'type': 'extrude'}], 'bodies': [], 'sketches': []}
                            elif method == 'engine.execute':
                                params = req['params']
                                if params['command'] != 'file.export':
                                    raise AssertionError('observer sent a mutation')
                                cad.export_entered.set()
                                if cad.block_export:
                                    cad.block_export.wait(4)
                                Path(params['params']['path']).write_text(json.dumps({'features': [{'id': 1, 'distance': cad.value}]}), encoding='utf-8')
                                if cad.change_during_export:
                                    cad.change_during_export = False
                                    cad.change(cad.value + 1)
                                result = {'bytes': 100}
                            with send_lock:
                                self.request.sendall(json.dumps({'id': req['id'], 'ok': True, 'result': result}).encode() + b'\n')
                            if method == 'control.subscribe':
                                cad.subscribers.append((self.request, send_lock))
                except (OSError, ConnectionError):
                    pass

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True
        self.server = Server(('127.0.0.1', 0), Handler)
        port = self.server.server_address[1]
        self.folder = Path(folder)
        self.folder.mkdir()
        (self.folder / f'{port}.token').write_text(self.token, encoding='ascii')
        (self.folder / f'{port}.pid').write_text(str(os.getpid()), encoding='ascii')
        self.binding = read_binding(self.folder, port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def change(self, value, document=None, undo=None, redo=None):
        with self.lock:
            self.value = value
            self.key['revision'] += 1
            if document:
                self.key['document_id'] = document
            if undo is not None:
                self.undo = undo
            if redo is not None:
                self.redo = redo
            payload = json.dumps({'event': 'design.changed', 'params': self.key}).encode() + b'\n'
            for sock, lock in list(self.subscribers):
                try:
                    with lock:
                        sock.sendall(payload)
                except OSError:
                    pass

    def disconnect(self):
        for sock in list(self.connections):
            try:
                sock.shutdown(socket.SHUT_RDWR)
                sock.close()
            except OSError:
                pass
        self.subscribers.clear()

    def close(self):
        self.disconnect()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.archive = Archive(root / 'archive')
        self.cad = SimulatedCad(root / 'control')
        self.recorder = Recorder(self.archive, self.cad.binding, 'test')

    def tearDown(self):
        self.recorder.stop()
        self.cad.close()
        self.archive.close()
        self.temp.cleanup()

    def snapshot_at(self, revision):
        snap = self.archive.latest_snapshot()
        return snap if snap and snap['summary']['revision'] == revision else None

    def start(self):
        self.recorder.start()
        return wait_for(lambda: self.snapshot_at(1))

    def test_notifications_and_consistent_snapshots_count_separately_without_browser(self):
        self.start()
        for value in range(21, 31):
            self.cad.change(value)
        final = wait_for(lambda: self.snapshot_at(11))
        events = self.archive.list_events(limit=100)
        self.assertEqual(len(events), 11, {'events': events, 'recorder': self.recorder.status()})
        self.assertTrue(any(x['status'] == 'superseded' for x in events))
        self.assertEqual(final['summary']['document_id'], 'doc-one')
        document = json.loads(self.archive.read_asset(final['assets']['document.json']['sha256']))
        self.assertEqual(document['features'][0]['distance'], 30)
        commands = [x[1]['command'] for x in self.cad.commands if x[0] == 'engine.execute']
        self.assertEqual(set(commands), {'file.export'})

    def test_mid_capture_change_never_attaches_later_document_to_old_event(self):
        self.cad.change_during_export = True
        self.recorder.start()
        wait_for(lambda: self.snapshot_at(2))
        events = self.archive.list_events(limit=100)
        self.assertEqual(events[0]['status'], 'superseded')
        self.assertIsNone(self.archive.snapshot(events[0]['id']))

    def test_disconnect_gap_reconnect_and_document_switch(self):
        self.start()
        self.cad.disconnect()
        wait_for(lambda: any(x['kind'] == 'gap' for x in self.archive.list_events()))
        wait_for(lambda: len([x for x in self.cad.commands if x[0] == 'control.subscribe']) >= 2)
        self.cad.change(5, document='doc-two')
        snap = wait_for(lambda: self.snapshot_at(2))
        self.assertEqual(snap['summary']['document_id'], 'doc-two')

    def test_same_port_replaced_identity_stops_permanently(self):
        self.start()
        path = self.cad.folder / f'{self.cad.binding.port}.token'
        path.write_text('b' * 64, encoding='ascii')
        wait_for(lambda: self.recorder.status()['status'] == 'instance_stopped')
        count = len(self.cad.commands)
        self.cad.change(25)
        time.sleep(.3)
        self.assertEqual(len(self.cad.commands), count)
        self.assertFalse(self.recorder.status()['recording'])

    def test_quota_failure_pauses_even_if_event_status_write_also_fails(self):
        self.start()
        original = self.archive.reserve
        def fail_reserve(*args, **kwargs):
            raise QuotaExceeded('injected low disk')
        with patch.object(self.archive, 'reserve', fail_reserve), patch.object(self.archive, 'set_event_status', side_effect=QuotaExceeded('injected full disk')):
            self.cad.change(25)
            wait_for(lambda: self.recorder.paused)
            self.assertFalse(self.recorder.status()['recording'])
            self.assertTrue(self.recorder.threads[1].is_alive())
        self.assertEqual(self.archive.reserve, original)

    def test_pause_resume_shows_gap_and_latest_state(self):
        self.start()
        self.recorder.pause(True)
        self.cad.change(25)
        time.sleep(.2)
        self.recorder.pause(False)
        wait_for(lambda: self.snapshot_at(2))
        events = self.archive.list_events(limit=100)
        self.assertTrue(any(x['kind'] == 'reconnect' and '不可补回' in str(x['raw']) for x in events))


if __name__ == '__main__':
    unittest.main()
