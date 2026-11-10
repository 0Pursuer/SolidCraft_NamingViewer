"""Read-only lifecycle endpoint boundaries using private archives and an inert lab."""
from contextlib import closing
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import unittest

from test_flow_browser import make_flow_fixture, readonly_server
from test_recompute_api import FakeLab, fake_lab_server
from archive import Archive


class LifecycleApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lifecycle-api-')
        self.root = Path(self.temp.name) / 'archive'
        self.ids = make_flow_fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def request(self, url, path, method='GET', cookie='', body=None, headers=None):
        connection = HTTPConnection('127.0.0.1', int(url.rsplit(':', 1)[1]), timeout=10)
        supplied = {'Cookie': cookie}
        if method == 'POST':
            supplied.update({'Origin': url, 'Content-Type': 'application/json'})
        supplied.update(headers or {})
        payload = json.dumps(body).encode('utf-8') if body is not None else None
        try:
            connection.request(method, path, body=payload, headers=supplied)
            reply = connection.getresponse()
            content, response_headers = reply.read(), dict(reply.getheaders())
            if response_headers.get('Content-Type', '').startswith('application/json'):
                content = json.loads(content)
            return reply.status, content, response_headers
        finally:
            connection.close()

    def bootstrap(self, url):
        status, _, headers = self.request(url, '/recompute')
        self.assertEqual(status, 200)
        return headers['Set-Cookie'].split(';', 1)[0]

    def query(self, **extra):
        params = {'before_id': self.ids['height20'], 'after_id': self.ids['height40'], **extra}
        return '/api/recompute/lifecycle?' + '&'.join(f'{key}={value}' for key, value in params.items())

    def test_source_only_without_lab_is_authenticated_readonly_and_has_no_native_claim(self):
        with readonly_server(self.root) as (url, archive):
            counts = archive.status()['counts']
            cookie = self.bootstrap(url)
            self.assertEqual(self.request(url, self.query())[0], 403)
            status, lifecycle, _ = self.request(url, self.query(), cookie=cookie)
            self.assertEqual(status, 200)
            self.assertEqual(lifecycle['source_version'], '0.30.1')
            self.assertTrue(lifecycle['frames'])
            self.assertTrue(lifecycle['objects'])
            self.assertTrue(lifecycle['relations'])
            self.assertTrue(all(frame['provenance'] == 'source_explanation'
                                for frame in lifecycle['frames']))
            self.assertTrue(all('/blob/v0.30.1/' in frame['source_url']
                                for frame in lifecycle['frames']))
            self.assertEqual(archive.status()['counts'], counts)

    def test_parameters_cannot_select_commands_paths_duplicate_keys_or_bad_ids(self):
        with readonly_server(self.root) as (url, _):
            cookie = self.bootstrap(url)
            valid = f'before_id={self.ids["height20"]}&after_id={self.ids["height40"]}'
            queries = (
                'before_id=0&after_id=7', 'before_id=-1&after_id=7',
                'before_id=&after_id=', 'before_id=5', 'after_id=7',
                'before_id=True&after_id=7', 'before_id=9223372036854775808&after_id=7',
                'before_id=5&before_id=6&after_id=7',
                'before_id=5&before_id=&after_id=7',
                valid + '&job_id=../../archive.sqlite3', valid + '&job_id=',
                valid + '&job_id=' + 'a' * 32 + '&job_id=' + 'b' * 32,
                valid + '&command=edit.undo', valid + '&path=C:/Windows/win.ini',
            )
            for query in queries:
                with self.subTest(query=query):
                    self.assertEqual(self.request(url, '/api/recompute/lifecycle?' + query, cookie=cookie)[0], 400)

    def test_job_pair_is_validated_and_get_does_not_submit_cancel_or_mutate(self):
        lab = FakeLab(Path(self.temp.name))
        lab.job = {'id': lab.ident, 'before_id': self.ids['height20'],
                   'after_id': self.ids['height40'], 'mode': 'edit_undo',
                   'status': 'completed', 'phases': [], 'cli_version': '0.30.1',
                   'spec': {'feature_id': 2, 'before_distance': '20 mm', 'after_distance': '40 mm'}}
        with fake_lab_server(self.root, lab) as (url, archive):
            cookie = self.bootstrap(url)
            counts = archive.status()['counts']
            status, lifecycle, _ = self.request(url, self.query(job_id=lab.ident), cookie=cookie)
            self.assertEqual(status, 200)
            self.assertTrue(lifecycle['frames'])
            self.assertFalse(any(frame['provenance'] == 'native_phase_observation'
                                 for frame in lifecycle['frames']))
            other = self.query(after_id=self.ids['undo'], job_id=lab.ident)
            self.assertIn(self.request(url, other, cookie=cookie)[0], (400, 409))
            missing = self.query(job_id='b' * 32)
            self.assertIn(self.request(url, missing, cookie=cookie)[0], (400, 404, 409))
            self.assertEqual(lab.calls, [])
            self.assertEqual(archive.status()['counts'], counts)

    def test_no_lab_or_foreign_origin_cannot_supply_a_native_job(self):
        with readonly_server(self.root) as (url, archive):
            cookie = self.bootstrap(url)
            counts = archive.status()['counts']
            self.assertIn(self.request(url, self.query(job_id='a' * 32), cookie=cookie)[0], (400, 404, 409))
            self.assertEqual(self.request(url, self.query(), cookie=cookie,
                                         headers={'Origin': 'https://foreign.invalid'})[0], 403)
            self.assertEqual(self.request(url, self.query(), cookie=cookie,
                                         headers={'Host': 'foreign.invalid'})[0], 403)
            self.assertEqual(archive.status()['counts'], counts)

    def test_completed_phase_reads_are_allowlisted_hashed_and_show_exact_exported_values(self):
        lab = FakeLab(Path(self.temp.name))
        assets, phases = {}, []
        with closing(Archive(self.root, read_only=True)) as source:
            for index, (key, ident) in enumerate((('baseline', self.ids['height20']),
                                                   ('edited', self.ids['height40']),
                                                   ('undone', self.ids['undo']))):
                record = source.snapshot(ident)
                references = {}
                for name in ('document.json', 'inspect.json'):
                    ref = record['assets'][name]
                    path = Path(self.temp.name) / (str(index) + '-' + name)
                    path.write_bytes(source.read_asset(ref['sha256']))
                    references[name] = ref
                    assets[(index, name)] = path
                phases.append({'index': index, 'key': key, 'label': key,
                               'status': 'completed', 'assets': references,
                               'command': 'timeline.edit' if key == 'edited' else 'edit.undo' if key == 'undone' else None,
                               'command_params': {'feature': 2, 'set': {'extent.distance': '40 mm'}} if key == 'edited' else {},
                               'command_result': {'feature': 2, 'recomputed': 1, 'restored': 0}
                                                 if key == 'edited' else {'undone': 'Edit Feature'} if key == 'undone' else None})
        lab.job = {'id': lab.ident, 'before_id': self.ids['height20'],
                   'after_id': self.ids['height40'], 'mode': 'edit_undo',
                   'status': 'completed', 'phases': phases, 'cli_version': '0.30.1',
                   'spec': {'feature_id': 2, 'before_distance': '20 mm', 'after_distance': '40 mm'}}

        def fixed_asset(ident, index, name):
            self.assertEqual(ident, lab.ident)
            lab.calls.append(('asset', ident, index, name))
            return assets[(index, name)]

        lab.asset_path = fixed_asset
        with fake_lab_server(self.root, lab) as (url, archive):
            cookie = self.bootstrap(url)
            counts = archive.status()['counts']
            status, lifecycle, _ = self.request(url, self.query(job_id=lab.ident), cookie=cookie)
            self.assertEqual(status, 200)
            observed = [frame for frame in lifecycle['frames'] if frame['provenance'] == 'native_phase_observation']
            self.assertEqual([frame['id'] for frame in observed], ['baseline', 'edited', 'undone'])
            field = lambda frame, object_name, name: next(item['value'] for item in frame['data'][object_name]['fields'] if item['name'] == name)
            self.assertEqual([field(frame, 'document', 'height') for frame in observed], ['20 mm', '40 mm', '20 mm'])
            self.assertEqual([field(frame, 'session', 'undo_depth') for frame in observed], [3, 4, 3])
            self.assertEqual([field(frame, 'session', 'redo_depth') for frame in observed], [0, 0, 1])
            self.assertEqual(field(observed[1], 'model', 'recomputed'), 1)
            self.assertIsNone(field(observed[2], 'model', 'recomputed'))
            self.assertEqual(len(lab.calls), 6)
            self.assertEqual({call[-1] for call in lab.calls}, {'document.json', 'inspect.json'})
            self.assertEqual(archive.status()['counts'], counts)

    def test_lifecycle_has_no_post_command_entry(self):
        lab = FakeLab(Path(self.temp.name))
        with fake_lab_server(self.root, lab) as (url, archive):
            cookie = self.bootstrap(url)
            counts = archive.status()['counts']
            for path in ('/api/recompute/lifecycle', self.query()):
                self.assertIn(self.request(url, path, 'POST', cookie,
                                          {'command': 'edit.undo'})[0], (400, 404))
            self.assertEqual(lab.calls, [])
            self.assertEqual(archive.status()['counts'], counts)


if __name__ == '__main__':
    unittest.main()
