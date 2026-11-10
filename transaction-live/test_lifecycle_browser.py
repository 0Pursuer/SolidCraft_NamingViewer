"""Lifecycle rendering and async ownership; no CAD command is executed."""
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from test_flow_browser import (ROOT, _browser_executable, make_flow_fixture,
                               readonly_server, sync_playwright)


@unittest.skipIf(sync_playwright is None, 'Python Playwright is unavailable')
class LifecycleBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lifecycle-browser-', dir=ROOT)
        self.root = Path(self.temp.name) / 'archive'
        self.ids = make_flow_fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def ready(self, page):
        page.wait_for_function('() => window.__lifecycle?.state.data?.frames?.length > 0')
        page.locator('#life-step-list .life-step').first.wait_for()

    def test_source_controls_show_four_objects_and_readonly_calls_with_real_source_links(self):
        with readonly_server(self.root) as (url, archive), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_browser_executable())
            try:
                page = browser.new_page(viewport={'width': 1460, 'height': 1100})
                writes, errors = [], []
                page.on('request', lambda request: writes.append(request.url)
                        if request.method not in {'GET', 'HEAD'} else None)
                page.on('pageerror', lambda error: errors.append(str(error)))
                counts = archive.status()['counts']
                page.goto(url + '/recompute')
                self.ready(page)
                for name in ('session', 'document', 'model', 'snapshot'):
                    self.assertEqual(page.locator('#life-diagram .life-object[data-object="' + name + '"]').count(), 1)
                    self.assertEqual(page.locator('#life-data .life-object-data[data-object="' + name + '"]').count(), 1)
                self.assertTrue(page.evaluate("() => window.__lifecycle.state.data.frames.every(f => f.provenance === 'source_explanation')"))
                self.assertIn('/blob/v0.30.1/', page.locator('#life-source').get_attribute('href'))
                page.locator('#life-next').click()
                self.assertEqual(page.evaluate('() => window.__lifecycle.state.frame'), 1)
                self.assertTrue(page.locator('#life-call').inner_text())
                page.locator('#life-prev').click()
                self.assertEqual(page.evaluate('() => window.__lifecycle.state.frame'), 0)
                page.locator('#life-speed').select_option(label='2×')
                page.locator('#life-play').click()
                page.wait_for_function('() => window.__lifecycle.state.frame > 0')
                page.locator('#life-play').click()
                page.locator('#life-step-list .life-step').last.click()
                self.assertEqual(page.evaluate('() => window.__lifecycle.state.frame'),
                                 page.evaluate('() => window.__lifecycle.state.data.frames.length - 1'))
                page.locator('#life-reset').click()
                self.assertEqual(page.evaluate('() => window.__lifecycle.state.frame'), 0)
                self.assertEqual(writes, [])
                self.assertEqual(errors, [])
                self.assertEqual(archive.status()['counts'], counts)
            finally:
                browser.close()

    def test_delayed_old_pair_lifecycle_cannot_overwrite_new_pair(self):
        with readonly_server(self.root) as (url, _), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_browser_executable())
            try:
                page = browser.new_page(viewport={'width': 1460, 'height': 1100})
                held, errors, writes, hold_a = [], [], [], True
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: writes.append(request.url)
                        if request.method not in {'GET', 'HEAD'} else None)

                def route_lifecycle(route):
                    query = parse_qs(urlsplit(route.request.url).query)
                    if hold_a and query.get('before_id') == [str(self.ids['height20'])]:
                        held.append(route)
                        return
                    response = route.fetch()
                    data = response.json()
                    for frame in data['frames']:
                        frame['title'] = 'CURRENT PAIR B'
                    route.fulfill(response=response, body=json.dumps(data), content_type='application/json; charset=utf-8')

                page.route('**/api/recompute/lifecycle?*', route_lifecycle)
                page.goto(url + '/recompute')
                page.wait_for_function('() => window.__recompute?.state.plan?.available === true')
                page.wait_for_function('() => window.__lifecycle?.state.requestEpoch >= 1')
                self.assertTrue(held)
                page.locator('#before').select_option(str(self.ids['height40']))
                page.locator('#after').select_option(str(self.ids['undo']))
                page.locator('#load-pair').click()
                self.ready(page)
                page.wait_for_function("() => document.getElementById('life-step-title').textContent === 'CURRENT PAIR B'")
                context = page.evaluate('() => window.__lifecycle.state.contextKey')
                for route in held:
                    response = route.fetch()
                    data = response.json()
                    for frame in data['frames']:
                        frame['title'] = 'STALE PAIR A'
                    route.fulfill(response=response, body=json.dumps(data), content_type='application/json; charset=utf-8')
                page.wait_for_timeout(150)
                self.assertEqual(page.locator('#life-step-title').inner_text(), 'CURRENT PAIR B')
                self.assertEqual(page.evaluate('() => window.__lifecycle.state.contextKey'), context)
                # A failed, older "use experiment" lookup must not write its
                # error into a different observation pair selected meanwhile.
                held_jobs, hold_a = [], False
                page.route('**/api/recompute/jobs', lambda route: held_jobs.append(route))
                page.locator('#life-use-experiment').click()
                self.assertEqual(len(held_jobs), 1)
                page.locator('#before').select_option(str(self.ids['height20']))
                page.locator('#after').select_option(str(self.ids['height40']))
                page.locator('#load-pair').click()
                page.wait_for_function('(old) => window.__lifecycle.state.data !== null && window.__lifecycle.state.contextKey !== old', arg=context)
                held_jobs.pop().fulfill(status=500, json={'error': 'STALE EXPERIMENT ERROR'})
                page.wait_for_timeout(150)
                self.assertNotIn('STALE EXPERIMENT ERROR', page.locator('#life-error').inner_text())
                self.assertEqual(writes, [])
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_delayed_poll_cannot_replace_reselected_same_job(self):
        with readonly_server(self.root) as (url, _), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_browser_executable())
            try:
                page = browser.new_page(viewport={'width': 1460, 'height': 1100})
                a, b = 'a' * 32, 'b' * 32
                jobs = {ident: {'id': ident, 'mode': 'full', 'status': 'completed',
                                'before_id': self.ids['height20'], 'after_id': self.ids['height40'],
                                'phases': [], 'version': '0.30.1', 'message': 'CURRENT SELECTION'}
                        for ident in (a, b)}
                held, a_reads = [], 0

                def route_jobs(route):
                    nonlocal a_reads
                    if route.request.url.endswith('/jobs'):
                        route.fulfill(json={'jobs': list(jobs.values())})
                    elif route.request.url.endswith('/' + a):
                        a_reads += 1
                        if a_reads == 1:
                            route.fulfill(json={**jobs[a], 'status': 'running'})
                        elif a_reads == 2:
                            held.append(route)  # polling the first selection of A
                        else:
                            route.fulfill(json=jobs[a])
                    elif route.request.url.endswith('/' + b):
                        route.fulfill(json=jobs[b])
                    else:
                        route.continue_()

                page.route('**/api/recompute/jobs**', route_jobs)
                page.goto(url + '/recompute')
                self.ready(page)
                page.locator('#jobs option[value="' + a + '"]').wait_for(state='attached')
                page.locator('#jobs').select_option(a)
                page.wait_for_function('() => window.__recompute.state.job?.status === "running"')
                self.assertEqual(len(held), 1)
                page.locator('#jobs').select_option(b)
                page.wait_for_function('(id) => window.__recompute.state.job?.id === id', arg=b)
                page.locator('#jobs').select_option(a)
                page.wait_for_function('(id) => window.__recompute.state.job?.id === id && window.__recompute.state.job.status === "completed"', arg=a)
                with page.expect_response(lambda response: response.url.endswith('/' + a)):
                    held.pop().fulfill(json={**jobs[a], 'message': 'STALE POLL'})
                page.wait_for_timeout(150)
                self.assertEqual(page.evaluate('() => window.__recompute.state.job.message'), 'CURRENT SELECTION')
                self.assertNotIn('STALE POLL', page.locator('#job-line').inner_text())
            finally:
                browser.close()

    def test_dynamic_text_is_not_html_and_mobile_data_does_not_overflow(self):
        with readonly_server(self.root) as (url, _), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_browser_executable())
            try:
                page = browser.new_page(viewport={'width': 390, 'height': 844})
                hostile = '<img src=x onerror="window.__lifeXss=1">'
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))

                def inject_text(route):
                    response = route.fetch()
                    data = response.json()
                    for frame in data['frames']:
                        frame['title'] = hostile
                        frame['description'] = hostile
                        frame['call'] = hostile
                        frame['data']['session']['fields'].append({
                            'name': 'hostile_value', 'label': hostile, 'value': hostile,
                            'provenance': 'source_explanation'})
                    route.fulfill(response=response, body=json.dumps(data), content_type='application/json; charset=utf-8')

                page.route('**/api/recompute/lifecycle?*', inject_text)
                page.goto(url + '/recompute')
                self.ready(page)
                self.assertIn(hostile, page.locator('#life-step-title').inner_text())
                self.assertIn(hostile, page.locator('#life-call').inner_text())
                self.assertIn(hostile, page.locator('#life-data').inner_text())
                self.assertEqual(page.locator('img[src="x"]').count(), 0)
                self.assertIsNone(page.evaluate('() => window.__lifeXss'))
                self.assertFalse(page.evaluate('() => document.documentElement.scrollWidth > window.innerWidth'))
                page.locator('#life-step-list .life-step').last.click()
                self.assertFalse(page.evaluate('() => document.documentElement.scrollWidth > window.innerWidth'))
                self.assertEqual(errors, [])
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
