"""Real loopback fixtures for selecting and retaining one native app launch."""
import http.client
import json
from pathlib import Path
import secrets
import socketserver
import threading
import unittest

import server


class NativeFixture:
    def __init__(self, directory, document, token):
        self.document, self.token = document, token
        self.requests = 0
        self.after_auth = None
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                authenticated = False
                for line in self.rfile:
                    request = json.loads(line)
                    owner.requests += 1
                    authenticated = authenticated or request.get('token') == owner.token
                    result = {'document_id': owner.document, 'revision': 1}
                    if request['method'] == 'document.inspect':
                        result = {'name': owner.document, 'bodies': []}
                    reply = {'id': request['id'], 'ok': authenticated}
                    reply['result' if authenticated else 'error'] = result if authenticated else 'authentication rejected'
                    if authenticated and 'token' in request and owner.after_auth is not None:
                        owner.after_auth()
                    self.wfile.write(json.dumps(reply).encode() + b'\n')
                    self.wfile.flush()
                    if not authenticated:
                        return

        class TCPServer(socketserver.ThreadingTCPServer):
            daemon_threads = True

        self.http = TCPServer(('127.0.0.1', 0), Handler)
        self.port = self.http.server_address[1]
        self.directory = directory
        (directory / f'{self.port}.token').write_text(token, encoding='ascii')
        (directory / f'{self.port}.pid').write_text('12345', encoding='ascii')
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(2)


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parent / ('startup-test-' + secrets.token_hex(8))
        self.directory.mkdir()
        def cleanup():
            for child in self.directory.iterdir():
                child.unlink()
            self.directory.rmdir()
        self.addCleanup(cleanup)
        self.a = NativeFixture(self.directory, 'document-A', 'a' * 64)
        self.b = NativeFixture(self.directory, 'document-B', 'b' * 64)
        self.addCleanup(self.b.close)
        self.addCleanup(self.a.close)
        (self.directory / 'current.port').write_text(str(self.a.port), encoding='ascii')

    def selected_client(self):
        client = server.ControlClient(server.read_binding(self.directory, self.a.port))
        self.addCleanup(client.close)
        return client

    def test_reconnect_ignores_latest_started_other_instance(self):
        client = self.selected_client()
        self.assertEqual(client.request('document.revision')['document_id'], 'document-A')
        client.close()
        (self.directory / 'current.port').write_text(str(self.b.port), encoding='ascii')
        self.assertEqual(client.request('document.revision')['document_id'], 'document-A')
        self.assertEqual(self.b.requests, 0)

    def test_same_port_with_replaced_launch_is_not_adopted(self):
        client = self.selected_client()
        client.request('document.revision')
        client.close()
        requests = self.a.requests
        self.a.token, self.a.document = 'c' * 64, 'replacement'
        (self.directory / f'{self.a.port}.token').write_text(self.a.token, encoding='ascii')
        with self.assertRaisesRegex(RuntimeError, '所选|实例|身份|凭据'):
            client.request('document.revision')
        self.assertEqual(self.a.requests, requests)

    def test_identity_replacement_during_authentication_closes_connection(self):
        client = self.selected_client()
        def replace_identity():
            (self.directory / f'{self.a.port}.token').write_text('c' * 64, encoding='ascii')
        self.a.after_auth = replace_identity
        with self.assertRaises(server.SelectedInstanceChanged):
            client.request('document.inspect')
        self.assertIsNone(client.sock)
        self.assertEqual(self.a.requests, 1, 'only authentication may run, never document inspection')

    def test_lost_selected_instance_retains_snapshot_and_never_queries_other_app(self):
        import time
        client = self.selected_client()
        state = server.ViewerState()
        collector = server.Collector(state, self.directory, client=client, interval=.4)
        self.addCleanup(collector.stop)
        collector.capture(('document-A', 1))
        previous = state.read()
        client.close()
        (self.directory / f'{self.a.port}.token').write_text('c' * 64, encoding='ascii')
        (self.directory / 'current.port').write_text(str(self.b.port), encoding='ascii')
        state.request_refresh()
        collector.thread.start()
        deadline = time.monotonic() + 3
        while state.read()['status'] != 'error' and time.monotonic() < deadline:
            time.sleep(.02)
        result = state.read()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['generation'], previous['generation'])
        self.assertEqual(result['source'], previous['source'])
        self.assertEqual(result['data'], previous['data'])
        self.assertIn('重新启动', result['last_error'])
        self.assertEqual(self.b.requests, 0)

    def test_two_default_http_listeners_reserve_distinct_ports(self):
        self.assertEqual(server.parse_options([]).port, 0)
        first = server.create_http_server(server.ViewerState(), b'first')
        self.addCleanup(first.server_close)
        second = server.create_http_server(server.ViewerState(), b'second')
        self.addCleanup(second.server_close)
        self.assertNotEqual(first.server_port, second.server_port)
        with self.assertRaises(OSError):
            server.create_http_server(server.ViewerState(), b'collision', first.server_port)
        thread = threading.Thread(target=second.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (second.shutdown(), thread.join(2)))
        conn = http.client.HTTPConnection('127.0.0.1', second.server_port, timeout=3)
        self.addCleanup(conn.close)
        conn.request('GET', '/')
        response = conn.getresponse()
        self.assertEqual(response.read(), b'second')
        self.assertIn(str(second.server_port), response.getheader('Set-Cookie'))

    def test_selection_is_explicit_and_uses_title_pid_port(self):
        output = []
        selected = server.select_instance(self.directory, input_func=lambda _: '2', output=output.append)
        choices = sorted([self.a, self.b], key=lambda f: f.port)
        self.assertEqual(selected[0].port, choices[1].port)
        text = '\n'.join(output)
        self.assertIn('document-A', text)
        self.assertIn('document-B', text)
        self.assertIn('12345', text)
        self.assertNotIn(self.a.token, text)

    def test_explicit_control_port_ignores_discovery_pointer(self):
        (self.directory / 'current.port').write_text(str(self.b.port), encoding='ascii')
        binding, name = server.select_instance(self.directory, self.a.port)
        self.assertEqual((binding.port, name), (self.a.port, 'document-A'))

    def test_runtime_is_in_launching_users_temp_and_export_is_session_scoped(self):
        from unittest.mock import patch
        with patch('server.tempfile.gettempdir', return_value=str(self.directory)):
            runtime = server.create_runtime_dir()
        self.addCleanup(lambda: runtime.parent.rmdir())
        self.addCleanup(runtime.rmdir)
        self.assertTrue(runtime.is_relative_to(self.directory / 'SolidCraftTopologyViewer'))
        client = server.ControlClient(server.read_binding(self.directory, self.a.port), runtime_dir=runtime)
        self.addCleanup(client.close)
        with self.assertRaises(ValueError):
            client.export_mesh(runtime.parent / 'other-session.3mf')
        with self.assertRaises(ValueError):
            client.export_mesh(runtime / 'other.3mf')
        client.connect()
        with patch.object(client, '_request', return_value={}) as request:
            client.export_mesh(runtime / 'snapshot.3mf')
        self.assertEqual(request.call_args.args[0], 'engine.execute')

    def test_api_reports_binding_without_credentials(self):
        state = server.ViewerState({'control_port': self.a.port, 'pid': 12345, 'selected_name': 'document-A'})
        payload = state.read()
        self.assertEqual(payload['instance']['control_port'], self.a.port)
        self.assertNotIn(self.a.token, json.dumps(payload))


if __name__ == '__main__':
    unittest.main(verbosity=2)
