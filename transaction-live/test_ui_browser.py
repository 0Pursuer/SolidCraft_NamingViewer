"""Browser integration fixture; never connects to SolidCraft.

Uses an already installed Python Playwright/browser. No production dependency.
Run: python -m unittest test_ui_browser -v
"""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import unittest
from urllib.parse import urlsplit

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parent


def cube():
    vertices = [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0],
                [0, 0, 10], [10, 0, 10], [10, 10, 10], [0, 10, 10]]
    faces = []
    for i in range(6):
        raw = {'index': i, 'name': 'F2:' + ['bottom', 'top', 'front', 'right', 'back', 'left'][i], 'exact': True}
        faces.append({**raw, 'nativeRecord': raw.copy(), 'nameSource': 'model.faces',
                      'displaySource': 'native-face-tag', 'area': 100, 'centroid': [5, 5, 5]})
    triangles = [[0, 2, 1, 0], [0, 3, 2, 0], [4, 5, 6, 1], [4, 6, 7, 1],
                 [0, 1, 5, 2], [0, 5, 4, 2], [1, 2, 6, 3], [1, 6, 5, 3],
                 [2, 3, 7, 4], [2, 7, 6, 4], [3, 0, 4, 5], [3, 4, 7, 5]]
    return {'meta': {'design': 'UI fixture', 'version': '0.30.1'}, 'stages': [{
        'id': 'body0', 'bodyName': 'Body1', 'title': 'Body1', 'faces': faces,
        'edges': [], 'points': [], 'mesh': {'vertices': vertices, 'triangles': triangles},
        'bounds': {'min': [0, 0, 0], 'max': [10, 10, 10]}, 'stats': {'volume': 1000},
        'features': [], 'warnings': [], 'visible': True}]}


def square():
    points = [{'id': 'p' + str(i), 'world': p} for i, p in enumerate(
        [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]])]
    return {'sketches': [{'points': points, 'curves': [
        {'id': 'l' + str(i), 'type': 'line', 'start': 'p' + str(i), 'end': 'p' + str((i + 1) % 4)} for i in range(4)]}], 'warnings': []}


@contextmanager
def fixture_server():
    assets = {}

    def asset(value):
        raw = json.dumps(value).encode()
        sha = hashlib.sha256(raw).hexdigest()
        assets[sha] = raw
        return {'sha256': sha, 'size': len(raw)}

    timeline = [{'id': 10, 'name': '矩形草图', 'type': 'sketch'},
                {'id': 20, 'name': '<img src=x onerror=window.__xss=1>', 'type': 'extrude'}]
    record = {'id': 2, 'label': 'O0002', 'summary': {
        'name': '高度 20 → 30', 'document_id': 'fixture-doc', 'revision': 5,
        'source': 'observed', 'undo': 2, 'redo': 0, 'marker': None, 'timeline': timeline,
        'completeness': 'complete_public_api', 'version': '0.30.1', 'warnings': []},
        'assets': {'geometry.json': asset(cube()), 'sketches.json': asset(square()),
                   'document.json': asset({'name': 'fixture', 'features': timeline})},
        'diff': [{'path': 'features[id=20].extent.distance', 'old': '20 mm', 'new': '30 mm'}],
        'inference': {'inferred': True, 'kind': 'observed_change', 'evidence': [{'undo': 2}]}}
    stage = {'id': 'stage_fixture', 'label': 'O0002/S1', 'summary': {
        **record['summary'], 'source': 'rebuilt', 'position': 1, 'marker': 1, 'undo': None, 'redo': None},
        'assets': {'geometry.json': asset({'meta': {}, 'stages': []}), 'sketches.json': asset(square())},
        'diff': [], 'inference': {'inferred': True, 'kind': 'reconstruction', 'evidence': []}}
    events = [{'id': 1, 'label': 'O0001', 'session_id': 'fixture-session', 'document_id': 'fixture-doc',
               'revision': 4, 'kind': 'document.changed', 'status': 'gap', 'message': '未取得该中间状态', 'snapshot_id': None},
              {'id': 2, 'label': 'O0002', 'session_id': 'fixture-session', 'document_id': 'fixture-doc',
               'revision': 5, 'kind': 'document.changed', 'status': 'captured', 'snapshot_id': 2}]
    jobs = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, payload, mime='application/json'):
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ('/', '/geometry.html'):
                return self.send((ROOT / ('viewer.html' if path == '/' else 'geometry.html')).read_bytes(), 'text/html; charset=utf-8')
            if path == '/api/status':
                return self.send({'recording': True, 'status': 'recording', 'version': '0.30.1', 'instance': {'pid': 123, 'control_port': 45678},
                                  'archive': {'used_bytes': 1024, 'limit_bytes': 2 * 1024**3}})
            if path == '/api/sessions':
                return self.send([{'id': 'fixture-session', 'started_at': '2026-11-09T00:00:00Z', 'info': {'selected_name': 'UI fixture'}}])
            if path == '/api/events':
                return self.send({'events': events})
            if path == '/api/jobs':
                return self.send({'jobs': jobs})
            if path == '/api/observations/2':
                return self.send(record)
            if path == '/api/stages/stage_fixture':
                return self.send(stage)
            if path.startswith('/api/assets/') and path.rsplit('/', 1)[1] in assets:
                return self.send(assets[path.rsplit('/', 1)[1]])
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            if self.path == '/api/rebuild':
                job = {'id': 'job_fixture', 'snapshot_id': request['snapshot_id'], 'position': request['position'],
                       'label': 'O0002/S' + str(request['position']), 'status': 'completed', 'stage_id': 'stage_fixture', 'version': '0.30.1'}
                jobs.append(job)
                return self.send(job)
            self.send_response(404)
            self.end_headers()

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    try:
        yield 'http://127.0.0.1:' + str(http.server_port)
    finally:
        http.shutdown()
        http.server_close()
        worker.join(timeout=3)


@unittest.skipIf(sync_playwright is None, 'Python Playwright is not installed')
class BrowserTests(unittest.TestCase):
    def test_observation_stage_and_sketch_without_native_connection(self):
        with fixture_server() as url, sync_playwright() as pw:
            configured = os.environ.get('TRACE_UI_BROWSER')
            available = sorted((Path(os.environ.get('LOCALAPPDATA', '')) / 'ms-playwright').glob('chromium-*/chrome-win64/chrome.exe'))
            executable = configured or (str(available[-1]) if available else None)
            browser = pw.chromium.launch(headless=True, executable_path=executable)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url)
            page.wait_for_function("window.__traceViewer?.state.record?.id===2")
            page.wait_for_function("document.querySelector('iframe').contentWindow.__traceGeometry?.data.stages.length===1")
            self.assertEqual(page.locator('#stat-undo').inner_text(), '2')
            self.assertEqual(page.locator('#stat-marker').inner_text(), '2')
            self.assertFalse(page.locator('#back-observation').is_visible())
            page.screenshot(path=str(ROOT / 'ui-observation-verified.png'), full_page=True)
            page.get_by_role('tab', name='变化对比').click()
            self.assertIn('30 mm', page.locator('#diff-table').inner_text())
            page.get_by_role('tab', name='特征时间线').click()
            self.assertEqual(page.locator('img[src="x"]').count(), 0)
            page.get_by_role('button', name='重建 S1', exact=True).click()
            page.get_by_role('button', name='查看阶段', exact=True).click()
            page.wait_for_function("window.__traceViewer.state.stage==='stage_fixture'")
            page.wait_for_function("document.querySelector('iframe').contentWindow.__traceGeometry?.data.stages.length===0")
            self.assertEqual(page.locator('#stat-undo').inner_text(), '不可获取')
            self.assertEqual(page.locator('#stat-marker').inner_text(), '1')
            frame = page.frame_locator('#geometry-frame')
            self.assertIn('4 条原生草图线段', frame.locator('#status-extra').inner_text())
            page.screenshot(path=str(ROOT / 'ui-stage-verified.png'), full_page=True)
            page.locator('#back-observation').click()
            self.assertEqual(page.locator('#stat-undo').inner_text(), '2')
            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path=str(ROOT / 'ui-mobile-verified.png'), full_page=True)
            self.assertFalse(page.evaluate('document.documentElement.scrollWidth>window.innerWidth'))
            self.assertIsNone(page.evaluate('window.__xss'))
            self.assertEqual(errors, [])
            browser.close()


if __name__ == '__main__':
    unittest.main()
