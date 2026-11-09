"""Flow routing and cache invalidation, against actual independent SQLite clients."""
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import unittest

from archive import Archive
from test_flow_browser import make_flow_fixture, readonly_server


class FlowApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'archive'
        make_flow_fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def open(self, url):
        connection = HTTPConnection('127.0.0.1', int(url.rsplit(':', 1)[1]), timeout=10)
        connection.request('GET', '/flow')
        response = connection.getresponse()
        response.read()
        return connection, response.getheader('Set-Cookie').split(';')[0]

    def get(self, connection, path, cookie):
        connection.request('GET', path, headers={'Cookie': cookie})
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def test_flow_arguments_and_auth_do_not_open_command_or_path_routes(self):
        with readonly_server(self.root) as (url, archive):
            conn, cookie = self.open(url)
            try:
                self.assertEqual(self.get(conn, '/api/flow', '')[0], 403)
                for path in ['/api/flow?path=C:/Windows/win.ini', '/api/flow?command=edit.undo',
                             '/api/flow?limit=201', '/api/flow?limit=0', '/api/flow?limit=no',
                             '/api/flow?document_id=../../escape', '/api/flow?limit=10&limit=20']:
                    self.assertEqual(self.get(conn, path, cookie)[0], 400, path)
                code, data = self.get(conn, '/api/flow?document_id=experiment-doc', cookie)
                self.assertEqual(code, 200)
                self.assertEqual(data['view_mode'], 'read_only')
                self.assertEqual((data['current']['undo_depth'], data['current']['redo_depth']), (3, 1))
                self.assertEqual(archive.status()['counts']['snapshots'], 8)
            finally:
                conn.close()

    def test_external_status_commit_invalidates_cache_without_a_new_event_id(self):
        writer = Archive(self.root)
        try:
            session = writer.list_sessions()[0]['id']
            event = writer.add_event(session, 'experiment-doc', 9, 'design.changed', status='pending')
            with readonly_server(self.root) as (url, reader):
                conn, cookie = self.open(url)
                try:
                    _, before = self.get(conn, '/api/flow?document_id=experiment-doc', cookie)
                    self.assertEqual(before['events'][-1]['id'], event)
                    self.assertEqual(before['events'][-1]['status'], 'pending')
                    writer.set_event_status(event, 'incomplete', 'injected collection failure')
                    _, after = self.get(conn, '/api/flow?document_id=experiment-doc', cookie)
                    self.assertEqual(after['events'][-1]['id'], event)
                    self.assertEqual(after['events'][-1]['status'], 'incomplete')
                    self.assertFalse(after['current']['is_latest_observation'])
                    self.assertEqual(reader.status()['counts']['events'], 9)
                finally:
                    conn.close()
        finally:
            writer.close()


if __name__ == '__main__':
    unittest.main()
