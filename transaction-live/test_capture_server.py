from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from archive import Archive
from capture import collect_bundle, SourceChanged
from server import create_server


class FakeClient:
    def __init__(self, change=False):
        self.revisions = 0
        self.change = change
        self.exported = []

    def request(self, method, params=None, timeout=5):
        if method == 'document.revision':
            self.revisions += 1
            return {'document_id': 'd1', 'revision': 2 if self.change and self.revisions > 2 else 1}
        return {'name': '矩形', 'undo': 3, 'redo': 1, 'timeline': [{'id': 10, 'type': 'sketch'}],
                'sketches': [{'id': 10}], 'bodies': [], 'params': []}

    def export(self, path, format, timeout=30):
        self.exported.append((Path(path).name, format))
        Path(path).write_text(json.dumps({'features': [{'id': 10, 'type': 'sketch'}], 'marker': None}), encoding='utf-8')

    def query(self, command, params, timeout=12):
        return {'id': 10, 'points': [{'id': 'p1', 'world': [0, 0, 0]}], 'curves': [], 'profiles': [{'index': 0}]}


class CollectionTests(unittest.TestCase):
    def test_consistency_and_complete_raw_sketch(self):
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient()
            assets, summary = collect_bundle(client, Path(folder), '0.30.1', ('d1', 1))
            self.assertEqual(summary['undo'], 3)
            self.assertEqual(summary['redo'], 1)
            self.assertEqual(json.loads(assets['sketches.json'])['sketches'][0]['profiles'], [{'index': 0}])
            self.assertEqual(json.loads(assets['geometry.json'])['stages'], [])
            self.assertNotIn('model.3mf', assets)
            self.assertEqual(client.exported, [('document.json', 'json')])

    def test_revision_changed_during_collection_never_returns_mixed_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(SourceChanged):
                collect_bundle(FakeClient(True), Path(folder), 'test', ('d1', 1))

    def test_expected_notification_already_superseded(self):
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient()
            with self.assertRaises(SourceChanged):
                collect_bundle(client, Path(folder), 'test', ('d1', 0))
            self.assertEqual(client.exported, [])


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = Archive(Path(self.temp.name) / 'archive')
        self.http = create_server(self.archive)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.http.server_port
        self.host = f'127.0.0.1:{self.port}'
        self.conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        self.conn.request('GET', '/')
        response = self.conn.getresponse()
        response.read()
        self.cookie = response.getheader('Set-Cookie').split(';')[0]

    def tearDown(self):
        self.conn.close()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(2)
        self.archive.close()
        self.temp.cleanup()

    def request(self, method, path, data=None, **headers):
        headers = {'Cookie': self.cookie, 'Origin': 'http://' + self.host, **headers}
        if data is not None:
            data = json.dumps(data)
            headers['Content-Type'] = 'application/json'
        self.conn.request(method, path, data, headers)
        response = self.conn.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else None

    def test_archive_routes_and_no_arbitrary_native_or_file_access(self):
        self.assertEqual(self.request('GET', '/api/status')[1]['status'], 'offline')
        self.assertEqual(self.request('GET', '/api/sessions'), (200, []))
        self.assertEqual(self.request('GET', '/api/events?after=0&limit=20'), (200, {'events': []}))
        for path in ['/api/assets/../../server.py', '/api/raw?path=C:/Windows/win.ini', '/api/execute']:
            self.assertEqual(self.request('GET', path)[0], 404)
        self.assertEqual(self.request('POST', '/api/rebuild', {'snapshot_id': 1, 'position': True})[0], 400)
        self.assertEqual(self.request('POST', '/api/capture', {'command': 'undo'})[0], 400)

    def test_host_origin_and_cookie_boundaries(self):
        self.assertEqual(self.request('GET', '/api/status', Host='evil.test')[0], 403)
        self.assertEqual(self.request('GET', '/api/status', Origin='https://evil.test')[0], 403)
        self.assertEqual(self.request('GET', '/api/status', Cookie='')[0], 403)
        self.assertEqual(self.request('POST', '/api/capture', {}, Origin='null')[0], 403)


if __name__ == '__main__':
    unittest.main()
