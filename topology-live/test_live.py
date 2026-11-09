"""Contract checks using isolated exports and a local HTTP fixture; never edits CAD."""
import copy
import http.client
import http.cookiejar
import json
from pathlib import Path
import shutil
import secrets
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import server

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT.parent / 'topology-visualization'


class FakeControl:
    def __init__(self):
        self.stage = 'before'
        self.revision = 1
        self.document = 'fixture-document'
        self.change_on_export = False
        self.body_count = 1
        self.commands = []

    def snapshot(self):
        return json.loads((FIXTURES / f'{self.stage}-capture.json').read_text(encoding='utf-8-sig'))

    def request(self, method, params=None, **_kwargs):
        self.commands.append(method)
        if method == 'document.revision':
            return {'document_id': self.document, 'revision': self.revision}
        if method == 'document.inspect':
            result = copy.deepcopy(self.snapshot()['2'])
            # Deliberately unrelated: this counter must never control consistency.
            result['revision'] = 999999
            if self.body_count > 1:
                result['bodies'] *= self.body_count
            return result
        if method == 'control.running':
            return {'running': []}
        raise AssertionError(method)

    def query(self, command, params):
        self.commands.append(command)
        assert params['body'] == 'Body1'
        return self.snapshot()['3' if command == 'model.faces' else '4']

    def export_mesh(self, destination):
        self.commands.append('file.export:3mf-tagged')
        shutil.copyfile(FIXTURES / f'{self.stage}.3mf', destination)
        if self.change_on_export:
            self.revision += 1
            self.change_on_export = False

    def close(self):
        pass


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp_path = (ROOT / ('test-' + secrets.token_hex(8))).resolve()
        self.assertTrue(self.temp_path.is_relative_to(ROOT.resolve()))
        self.temp_path.mkdir()
        def cleanup():
            (self.temp_path / 'snapshot.3mf').unlink(missing_ok=True)
            self.temp_path.rmdir()
        self.addCleanup(cleanup)
        self.state = server.ViewerState()
        self.control = FakeControl()
        self.collector = server.Collector(self.state, self.temp_path, client=self.control)

    def test_committed_rebuild_updates_names_and_mesh(self):
        self.collector.capture(('fixture-document', 1))
        old = self.state.read()
        self.assertEqual(len(old['data']['stages'][0]['faces']), 6)
        self.control.stage, self.control.revision = 'split', 2
        self.collector.capture(('fixture-document', 2))
        new = self.state.read()
        self.assertEqual(new['generation'], old['generation'] + 1)
        self.assertEqual(new['source']['revision'], 2)
        body = new['data']['stages'][0]
        self.assertEqual((len(body['faces']), len(body['edges']), len(body['points'])), (8, 18, 12))
        split_edges = [e for e in body['edges'] if e['name'].startswith('F2:side:l3|F2:top')]
        self.assertEqual(len(split_edges), 2)
        self.assertTrue(all('solid.' not in command and 'timeline.' not in command for command in self.control.commands))

    def test_mid_capture_revision_change_is_discarded(self):
        self.collector.capture(('fixture-document', 1))
        generation = self.state.generation
        self.control.stage, self.control.revision = 'split', 2
        self.control.change_on_export = True
        with self.assertRaises(server.SourceChanged):
            self.collector.capture(('fixture-document', 2))
        self.assertEqual(self.state.generation, generation)
        self.assertEqual(len(self.state.data['stages'][0]['faces']), 6)
        self.collector.capture(('fixture-document', 3))
        self.assertEqual(self.state.generation, generation + 1)

    def test_document_switch_has_new_identity(self):
        self.collector.capture(('fixture-document', 1))
        self.control.document = 'different-document'
        self.collector.capture(('different-document', 1))
        self.assertEqual(self.state.read()['source']['document_id'], 'different-document')

    def test_body_limit_keeps_last_good_snapshot(self):
        self.collector.capture(('fixture-document', 1))
        self.control.body_count = server.MAX_BODIES + 1
        with self.assertRaisesRegex(RuntimeError, '上限'):
            self.collector.capture(('fixture-document', 1))
        self.assertEqual(self.state.generation, 1)

    def test_http_generation_omits_unchanged_large_geometry(self):
        self.collector.capture(('fixture-document', 1))
        self.assertNotIn('data', self.state.read(after=1))
        self.assertIn('data', self.state.read(after=0))

    def test_cad_write_commands_cannot_pass_query_boundary(self):
        client = server.ControlClient(server.InstanceBinding(self.temp_path, 27878, 'a' * 64))
        with self.assertRaises(ValueError):
            client.query('solid.box', {})
        with self.assertRaises(ValueError):
            client.request('engine.execute', {'command': 'file.new'})
        with self.assertRaises(ValueError):
            client.export_mesh(ROOT / 'outside.3mf')

    def test_shutdown_detaches_connection_before_closing_resources(self):
        client = server.ControlClient(server.InstanceBinding(self.temp_path, 27878, 'a' * 64))
        calls = []
        class Socket:
            def shutdown(_self, _how):
                self.assertIsNone(client.sock)
                self.assertIsNone(client.reader)
                calls.append('shutdown')
            def close(_self):
                calls.append('socket.close')
        class Reader:
            def close(_self):
                calls.append('reader.close')
        client.sock, client.reader = Socket(), Reader()
        client.close()
        client.close()
        self.assertEqual(calls, ['shutdown', 'reader.close', 'socket.close'])

    def test_render_budget_rejects_before_publishing(self):
        self.collector.capture(('fixture-document', 1))
        oversized = copy.deepcopy(self.state.data['stages'][0])
        oversized['mesh']['triangles'] *= 1000
        oversized['edges'][0]['points'] *= 1000
        with patch('server.build_body_stage', return_value=oversized):
            with self.assertRaisesRegex(server.SnapshotLimitError, '渲染预算'):
                self.collector.capture(('fixture-document', 1))
        self.assertEqual(self.state.generation, 1)

    def test_adapter_name_mismatch_cannot_replace_last_good_snapshot(self):
        self.collector.capture(('fixture-document', 1))
        tampered = copy.deepcopy(self.state.data['stages'][0])
        tampered['faces'][0]['name'] = 'viewer-invented-name'
        with patch('server.build_body_stage', return_value=tampered):
            with self.assertRaisesRegex(RuntimeError, '原生|名称|校验'):
                self.collector.capture(('fixture-document', 1))
        self.assertEqual(self.state.generation, 1)

    def test_adapter_exact_mismatch_cannot_be_published(self):
        self.collector.capture(('fixture-document', 1))
        tampered = copy.deepcopy(self.state.data['stages'][0])
        tampered['faces'][0]['exact'] = not tampered['faces'][0]['exact']
        with patch('server.build_body_stage', return_value=tampered):
            with self.assertRaisesRegex(RuntimeError, '原生|名称|校验'):
                self.collector.capture(('fixture-document', 1))
        self.assertEqual(self.state.generation, 1)

    def test_worker_automatically_follows_committed_revision(self):
        self.collector.interval = .4
        self.collector.thread.start()
        self.addCleanup(self.collector.stop)
        def wait_for(predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(.02)
            self.fail('automatic collector did not publish in time: ' + str(self.state.read()))
        wait_for(lambda: self.state.generation == 1)
        self.control.stage = 'split'
        self.control.revision = 2
        wait_for(lambda: self.state.generation == 2)
        snapshot = self.state.read()
        self.assertEqual(snapshot['source']['revision'], 2)
        self.assertEqual(len(snapshot['data']['stages'][0]['faces']), 8)

    def test_manual_refresh_recaptures_even_when_revision_is_unchanged(self):
        self.collector.interval = .4
        self.collector.thread.start()
        self.addCleanup(self.collector.stop)
        def wait_for(predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(.02)
            self.fail('manual refresh did not finish')
        wait_for(lambda: self.state.generation == 1)
        request_id = self.state.request_refresh()
        self.assertEqual(self.state.request_refresh(), request_id, 'pending clicks must coalesce')
        wait_for(lambda: self.state.read()['refresh_completed'] >= request_id)
        snapshot = self.state.read()
        self.assertEqual(snapshot['generation'], 2)
        self.assertEqual(snapshot['source']['revision'], 1)
        self.assertEqual(self.control.commands.count('file.export:3mf-tagged'), 2)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = server.ViewerState()
        cls.state.publish({'meta': {'design': 'fixture'}, 'stages': []}, {'document_id': 'test', 'revision': 1})
        cls.http = server.LocalServer(('127.0.0.1', 0), None)
        cls.port = cls.http.server_port
        cls.http.RequestHandlerClass = server.make_handler(cls.state, 'private-test-session', b'<html>fixture</html>', cls.port)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.port}'

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join(2)

    def request(self, path, *, cookie=True, headers=None, method='GET'):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        request_headers = dict(headers or {})
        if cookie:
            request_headers['Cookie'] = f'{server.COOKIE}_{self.port}=private-test-session'
        client.request(method, path, headers=request_headers)
        response = client.getresponse()
        result = (response.status, response.read(), dict(response.getheaders()))
        client.close()
        return result

    def test_page_grants_private_same_site_session(self):
        status, _, headers = self.request('/', cookie=False)
        self.assertEqual(status, 200)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])

    def test_api_needs_session_and_rejects_foreign_origin_host(self):
        self.assertEqual(self.request('/api/state', cookie=False)[0], 403)
        self.assertEqual(self.request('/api/state', headers={'Origin': 'https://other.invalid'})[0], 403)
        self.assertEqual(self.request('/api/state', headers={'Host': f'other.invalid:{self.port}'})[0], 403)
        self.assertEqual(self.request('/api/state', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        status, payload, _ = self.request('/api/state')
        self.assertEqual(status, 200)
        self.assertNotIn(b'private-test-session', payload)

    def test_no_write_or_file_serving_endpoints(self):
        self.assertEqual(self.request('/api/state', method='POST')[0], 405)
        for path in ['/server.py', '/.runtime/snapshot.3mf', '/../server.py', '/api/execute?command=file.new']:
            self.assertEqual(self.request(path)[0], 404)

    def test_invalid_queries_are_bounded_errors(self):
        self.assertEqual(self.request('/api/state?after=nan')[0], 400)
        self.assertEqual(self.request('/api/state?a=1&b=1&c=1&d=1&e=1')[0], 400)
        self.assertEqual(self.request('/api/state?after=' + '9' * 25)[0], 400)
        self.assertEqual(self.request('/api/state?after=1')[0], 200)

    def test_manual_refresh_is_authenticated_and_coalesced(self):
        self.assertEqual(self.request('/api/refresh', method='POST', cookie=False)[0], 403)
        self.assertEqual(self.request('/api/refresh', method='POST', headers={'Origin': 'https://other.invalid'})[0], 403)
        status, payload, _ = self.request('/api/refresh', method='POST')
        self.assertEqual(status, 202)
        request_id = json.loads(payload)['request_id']
        self.assertEqual(json.loads(self.request('/api/refresh', method='POST')[1])['request_id'], request_id)


if __name__ == '__main__':
    unittest.main(verbosity=2)
