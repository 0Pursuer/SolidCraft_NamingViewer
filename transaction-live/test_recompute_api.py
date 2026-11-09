"""Recompute HTTP contract tests using only private archives and an inert lab."""
from contextlib import contextmanager
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from archive import Archive
from server import create_server
from test_flow_browser import make_flow_fixture, readonly_server


class FakeLab:
    """No process or CAD access: just the public job/asset route contract."""
    ident = 'a' * 32

    def __init__(self, root):
        self.root = root
        self.calls = []
        self.job = None
        self.asset = root / 'inspect.json'
        self.asset.write_text('{"timeline":[],"bodies":[]}', encoding='utf-8')

    def list_jobs(self):
        return [self.job] if self.job else []

    def submit(self, before, after, mode):
        if type(before) is not int or type(after) is not int or not 1 <= before <= 2**63 - 1 or not 1 <= after <= 2**63 - 1:
            raise ValueError('invalid observation id')
        self.calls.append(('submit', before, after, mode))
        self.job = {'id': self.ident, 'before_id': before, 'after_id': after, 'mode': mode,
                    'status': 'queued', 'phases': []}
        return self.job

    def get(self, ident):
        return self.job if ident == self.ident else None

    def cancel(self, ident):
        if self.get(ident) is None:
            raise ValueError('unknown job')
        self.calls.append(('cancel', ident))
        self.job = {**self.job, 'status': 'cancelled'}

    def asset_path(self, ident, phase, name):
        self.calls.append(('asset', ident, phase, name))
        if ident != self.ident or phase != 0 or name != 'inspect.json':
            raise FileNotFoundError('unknown phase asset')
        return self.asset


@contextmanager
def fake_lab_server(root, lab):
    archive = Archive(root, read_only=True)
    http = create_server(archive, version='0.30.1', recompute_lab=lab)
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{http.server_port}', archive
    finally:
        http.shutdown()
        http.server_close()
        worker.join(timeout=3)
        archive.close()


class RecomputeApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='recompute-api-')
        self.root = Path(self.temp.name) / 'archive'
        self.ids = make_flow_fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def request(self, url, path, method='GET', cookie='', body=None, headers=None, raw=False):
        conn = HTTPConnection('127.0.0.1', int(url.rsplit(':', 1)[1]), timeout=10)
        supplied = {'Cookie': cookie}
        if method == 'POST':
            supplied.update({'Origin': url, 'Content-Type': 'application/json'})
        supplied.update(headers or {})
        payload = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
        try:
            conn.request(method, path, body=payload, headers=supplied)
            reply = conn.getresponse()
            content, response_headers = reply.read(), dict(reply.getheaders())
            return reply.status, content if raw else json.loads(content), response_headers
        finally:
            conn.close()

    def bootstrap(self, url):
        status, page, headers = self.request(url, '/recompute', raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b'/api/recompute', page)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        return headers['Set-Cookie'].split(';', 1)[0]

    def test_bootstrap_auth_and_plan_use_exact_original_height_edit(self):
        with readonly_server(self.root) as (url, archive):
            counts = archive.status()['counts']
            cookie = self.bootstrap(url)
            self.assertEqual(self.request(url, '/api/recompute')[0], 403)
            status, data, _ = self.request(url, '/api/recompute', cookie=cookie)
            self.assertEqual(status, 200)
            self.assertFalse(data['execution_enabled'])
            self.assertEqual((data['before']['id'], data['after']['id']), (self.ids['height20'], self.ids['height40']))
            self.assertEqual(data['related_operation']['status'], 'undone')
            self.assertEqual(data['features'][1]['references'][0]['target_feature_id'], 1)
            self.assertEqual(data['features'][1]['runtime_cache_branch'], 'unavailable')
            self.assertEqual(data['source_version'], '0.30.1')
            self.assertEqual(archive.status()['counts'], counts)

    def test_plan_arguments_are_strict_and_cannot_address_commands_or_files(self):
        with readonly_server(self.root) as (url, _):
            cookie = self.bootstrap(url)
            for query in (
                'before_id=5', 'after_id=7', 'before_id=0&after_id=7',
                'before_id=-1&after_id=7', 'before_id=True&after_id=7',
                'before_id=9223372036854775808&after_id=7',
                'before_id=5&before_id=6&after_id=7',
                'before_id=&after_id=', 'before_id=5&before_id=&after_id=7',
                'path=C:/Windows/win.ini', 'command=edit.undo', 'limit=201',
                'before_id=5&after_id=7&extra=1',
            ):
                with self.subTest(query=query):
                    self.assertEqual(self.request(url, '/api/recompute?' + query, cookie=cookie)[0], 400)
            status, data, _ = self.request(url, '/api/recompute?before_id=7&after_id=8', cookie=cookie)
            self.assertEqual(status, 200)
            self.assertEqual((data['before']['id'], data['after']['id']), (7, 8))
            self.assertEqual(data['features'][1]['changes'][0]['new'], '20 mm')

    def test_disabled_lab_and_foreign_origins_cannot_submit(self):
        with readonly_server(self.root) as (url, archive):
            cookie = self.bootstrap(url)
            body = {'before_id': 5, 'after_id': 7, 'mode': 'edit_undo'}
            self.assertEqual(self.request(url, '/api/recompute/jobs', 'POST', body=body)[0], 403)
            self.assertEqual(self.request(url, '/api/recompute/jobs', 'POST', cookie, body)[0], 400)
            self.assertEqual(self.request(url, '/api/recompute/jobs', 'POST', cookie, body, {'Origin': 'https://foreign.invalid'})[0], 403)
            self.assertEqual(self.request(url, '/api/recompute', cookie=cookie, headers={'Host': 'foreign.invalid'})[0], 403)
            self.assertEqual(self.request(url, '/api/recompute', cookie=cookie, headers={'Origin': 'https://foreign.invalid'})[0], 403)
            status, data, _ = self.request(url, '/api/recompute/jobs', cookie=cookie)
            self.assertEqual((status, data), (200, {'jobs': []}))
            self.assertEqual(archive.status()['counts']['snapshots'], 8)

    def test_enabled_job_cancel_and_allowlisted_asset_routes_round_trip(self):
        lab = FakeLab(Path(self.temp.name))
        with fake_lab_server(self.root, lab) as (url, archive):
            cookie = self.bootstrap(url)
            status, data, _ = self.request(url, '/api/recompute/jobs', 'POST', cookie,
                                         {'before_id': 5, 'after_id': 7, 'mode': 'edit_undo'})
            self.assertEqual(status, 202)
            self.assertEqual(data['id'], lab.ident)
            status, listing, _ = self.request(url, '/api/recompute/jobs', cookie=cookie)
            self.assertEqual((status, listing['jobs'][0]['status']), (200, 'queued'))
            path = '/api/recompute/jobs/' + lab.ident
            self.assertEqual(self.request(url, path, cookie=cookie)[1]['before_id'], 5)
            status, asset, headers = self.request(url, path + '/phases/0/assets/inspect.json', cookie=cookie)
            self.assertEqual((status, asset), (200, {'timeline': [], 'bodies': []}))
            self.assertIn('nosniff', headers['X-Content-Type-Options'])
            status, cancelled, _ = self.request(url, path + '/cancel', 'POST', cookie, {})
            self.assertEqual((status, cancelled['status']), (200, 'cancelled'))
            self.assertEqual(lab.calls, [('submit', 5, 7, 'edit_undo'), ('asset', lab.ident, 0, 'inspect.json'), ('cancel', lab.ident)])
            self.assertTrue(archive.read_only)
            self.assertEqual(archive.status()['counts']['snapshots'], 8)

    def test_bad_job_inputs_are_refused_before_an_inert_lab_is_called(self):
        lab = FakeLab(Path(self.temp.name))
        with fake_lab_server(self.root, lab) as (url, _):
            cookie = self.bootstrap(url)
            for body in (
                {'before_id': True, 'after_id': 7, 'mode': 'edit_undo'},
                {'before_id': '5', 'after_id': 7, 'mode': 'edit_undo'},
                {'before_id': 5, 'after_id': 7, 'mode': 'edit.undo'},
                {'before_id': 5, 'after_id': 7, 'mode': 'full', 'path': 'C:/Windows/win.ini'},
                {'command': 'timeline.compute_all'},
                b'{"before_id":5,"before_id":6,"after_id":7,"mode":"full"}',
            ):
                with self.subTest(body=body):
                    self.assertIn(self.request(url, '/api/recompute/jobs', 'POST', cookie, body)[0], (400, 404))
            self.assertEqual(lab.calls, [])
            for suffix in (
                '/phases/0/assets/../../archive.sqlite3',
                '/phases/0/assets/%2E%2E%2Farchive.sqlite3',
                '/phases/0/assets/C:%5CWindows%5Cwin.ini',
                '/phases/0/assets/inspect.json?path=C:/Windows/win.ini',
            ):
                self.assertIn(self.request(url, '/api/recompute/jobs/' + lab.ident + suffix, cookie=cookie)[0], (400, 404))
            self.assertEqual(lab.calls, [])


if __name__ == '__main__':
    unittest.main()
