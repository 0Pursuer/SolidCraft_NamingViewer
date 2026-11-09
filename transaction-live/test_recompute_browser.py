"""Browser tests of source playback; no installed SolidCraft instance is used."""
import json
from pathlib import Path
import tempfile
import unittest

from test_flow_browser import (ROOT, _browser_executable, make_flow_fixture,
                               readonly_server, sync_playwright)


@unittest.skipIf(sync_playwright is None, 'Python Playwright is unavailable')
class RecomputeBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='recompute-browser-', dir=ROOT)
        self.root = Path(self.temp.name) / 'archive'
        self.ids = make_flow_fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def ready(self, page):
        page.wait_for_function('() => window.__recompute?.state.plan?.available === true')
        page.locator('#graph .feature').nth(1).wait_for()

    def test_source_playback_is_readonly_and_shows_real_edit_and_explicit_reference(self):
        with readonly_server(self.root) as (url, archive), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_browser_executable())
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                writes, errors = [], []
                page.on('request', lambda request: writes.append((request.method, request.url)) if request.method not in {'GET', 'HEAD'} else None)
                page.on('pageerror', lambda error: errors.append(str(error)))
                counts = archive.status()['counts']
                page.goto(url + '/recompute')
                self.ready(page)
                self.assertEqual(page.locator('#before').input_value(), str(self.ids['height20']))
                self.assertEqual(page.locator('#after').input_value(), str(self.ids['height40']))
                self.assertEqual(page.locator('#graph .feature').count(), 2)
                self.assertEqual(page.locator('#graph .stage').count(), 3)
                self.assertIn('F1', page.locator('#graph .feature').nth(0).inner_text())
                self.assertIn('F2', page.locator('#graph .feature').nth(1).inner_text())
                self.assertIn('sketch = 1', page.locator('#graph .ref-line').inner_text())
                details = page.locator('#feature-detail').inner_text()
                self.assertIn('extent.distance', details)
                self.assertIn('20 mm', details)
                self.assertIn('40 mm', details)
                self.assertTrue(page.locator('#run-edit').is_disabled())
                self.assertTrue(page.locator('#run-full').is_disabled())
                # Step controls and the timer advance a source explanation only.
                page.locator('#speed').select_option('900')
                page.locator('#play').click()
                page.wait_for_function('() => window.__recompute.state.step > 0')
                page.locator('#play').click()
                page.locator('#next').click()
                step = page.evaluate('() => window.__recompute.state.step')
                self.assertGreaterEqual(step, 2)
                page.locator('#step-list .step').last.click()
                self.assertIn('/blob/v0.30.1/', page.locator('#step-source').get_attribute('href'))
                self.assertTrue(page.evaluate("() => window.__recompute.state.plan.features.every(f => f.runtime_cache_branch === 'unavailable')"))
                self.assertIn('不代表现场', page.locator('.explain-banner').inner_text())
                self.assertIn('接口未提供', page.locator('#feature-detail').inner_text())
                page.locator('#reset').click()
                self.assertEqual(page.evaluate('() => window.__recompute.state.step'), 0)
                self.assertEqual(writes, [])
                self.assertEqual(errors, [])
                self.assertEqual(archive.status()['counts'], counts)
            finally:
                browser.close()

    def test_explicit_observation_pair_and_feature_selection_are_not_confused_with_undo(self):
        with readonly_server(self.root) as (url, _), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_browser_executable())
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.goto(url + '/recompute')
                self.ready(page)
                page.locator('#before').select_option(str(self.ids['height40']))
                page.locator('#after').select_option(str(self.ids['undo']))
                page.locator('#load-pair').click()
                page.wait_for_function('(id) => window.__recompute.state.plan.after.id === id', arg=self.ids['undo'])
                self.assertEqual(page.locator('#before').input_value(), str(self.ids['height40']))
                self.assertEqual(page.locator('#after').input_value(), str(self.ids['undo']))
                changes = page.evaluate('() => window.__recompute.state.plan.features.find(f => f.id === 2).changes')
                self.assertEqual([(c['old'], c['new']) for c in changes], [('40 mm', '20 mm')])
                self.assertIn('#before=' + str(self.ids['height40']) + '&after=' + str(self.ids['undo']), page.url)
                page.locator('#graph .feature').first.click()
                self.assertIn('F1', page.locator('#feature-title').inner_text())
                self.assertEqual(page.locator('#feature-detail .field-name').count(), 0)
                page.locator('#graph .feature').nth(1).click()
                self.assertIn('extent.distance', page.locator('#feature-detail').inner_text())
            finally:
                browser.close()

    def test_names_and_field_values_render_as_text_and_mobile_has_no_page_overflow(self):
        with readonly_server(self.root) as (url, _), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_browser_executable())
            try:
                page = browser.new_page(viewport={'width': 390, 'height': 844})
                errors, writes = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: writes.append(request.url) if request.method not in {'GET', 'HEAD'} else None)
                hostile = '<img src=x onerror="window.__recomputeXss=1">'

                def insert_hostile_text(route):
                    response = route.fetch()
                    data = response.json()
                    if isinstance(data.get('features'), list):
                        data['features'][1]['name'] = hostile
                        data['features'][1]['changes'][0]['old'] = hostile
                        data['features'][1]['changes'][0]['new'] = hostile
                    route.fulfill(response=response, body=json.dumps(data, ensure_ascii=False), content_type='application/json; charset=utf-8')

                page.route('**/api/recompute*', insert_hostile_text)
                page.goto(url + '/recompute')
                self.ready(page)
                self.assertIn(hostile, page.locator('#feature-title').inner_text())
                self.assertEqual(page.locator('img[src="x"]').count(), 0)
                self.assertIsNone(page.evaluate('() => window.__recomputeXss'))
                self.assertFalse(page.evaluate('() => document.documentElement.scrollWidth > window.innerWidth'))
                page.locator('#step-list .step').last.click()
                self.assertFalse(page.evaluate('() => document.documentElement.scrollWidth > window.innerWidth'))
                self.assertEqual(errors, [])
                self.assertEqual(writes, [])
            finally:
                browser.close()

    def test_slow_job_a_cannot_replace_job_b_selected_after_it(self):
        with readonly_server(self.root) as (url, _), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_browser_executable())
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                a, b = 'a' * 32, 'b' * 32
                jobs = {ident: {'id': ident, 'mode': 'full', 'status': 'completed',
                                'before_id': self.ids['height20'], 'after_id': self.ids['height40'],
                                'phases': [], 'version': '0.30.1'} for ident in (a, b)}
                held, errors, writes = [], [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: writes.append(request.url) if request.method not in {'GET', 'HEAD'} else None)

                def route_jobs(route):
                    if route.request.url.endswith('/jobs'):
                        route.fulfill(json={'jobs': list(jobs.values())})
                    elif route.request.url.endswith('/' + a):
                        # Return immediately without answering: the browser can
                        # select B while A's fetch remains genuinely pending.
                        held.append(route)
                    elif route.request.url.endswith('/' + b):
                        route.fulfill(json=jobs[b])
                    else:
                        route.continue_()

                page.route('**/api/recompute/jobs**', route_jobs)
                page.goto(url + '/recompute')
                self.ready(page)
                page.locator('#jobs option[value="' + a + '"]').wait_for(state='attached')
                page.locator('#jobs').select_option(a)
                page.wait_for_function('() => window.__recompute.state.jobEpoch >= 1')
                self.assertEqual(len(held), 1)
                page.locator('#jobs').select_option(b)
                page.wait_for_function('(id) => window.__recompute.state.job?.id === id', arg=b)
                with page.expect_response(lambda response: response.url.endswith('/' + a)) as delivered:
                    held.pop().fulfill(json=jobs[a])
                self.assertEqual(delivered.value.status, 200)
                page.wait_for_timeout(150)  # let the delayed fetch's continuation run
                self.assertEqual(page.evaluate('() => window.__recompute.state.job.id'), b)
                self.assertEqual(page.locator('#jobs').input_value(), b)
                self.assertIn(b[:8], page.locator('#job-line').inner_text())
                self.assertEqual(errors, [])
                self.assertEqual(writes, [])
            finally:
                browser.close()

    def test_slow_cancel_cannot_replace_a_newly_selected_experiment(self):
        with readonly_server(self.root) as (url, _), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_browser_executable())
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                a, b = 'a' * 32, 'b' * 32
                jobs = {ident: {'id': ident, 'mode': 'full',
                                'status': 'running' if ident == a else 'completed',
                                'before_id': self.ids['height20'], 'after_id': self.ids['height40'],
                                'phases': [], 'version': '0.30.1'} for ident in (a, b)}
                held, errors = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                def route_jobs(route):
                    if route.request.url.endswith('/' + a + '/cancel'):
                        held.append(route)
                    elif route.request.url.endswith('/jobs'):
                        route.fulfill(json={'jobs': list(jobs.values())})
                    elif route.request.url.endswith('/' + a):
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
                page.wait_for_function('(id) => window.__recompute.state.job?.id === id', arg=a)
                page.locator('#cancel').click()
                page.wait_for_timeout(50)
                self.assertEqual(len(held), 1)
                page.locator('#jobs').select_option(b)
                page.wait_for_function('(id) => window.__recompute.state.job?.id === id', arg=b)
                with page.expect_response(lambda response: response.url.endswith('/' + a + '/cancel')):
                    held.pop().fulfill(json={**jobs[a], 'status': 'cancelled'})
                page.wait_for_timeout(150)
                self.assertEqual(page.evaluate('() => window.__recompute.state.job.id'), b)
                self.assertEqual(page.locator('#jobs').input_value(), b)
                self.assertIn(b[:8], page.locator('#job-line').inner_text())
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_removed_feature_is_visible_only_as_a_difference_not_a_current_stage(self):
        with readonly_server(self.root) as (url, _), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_browser_executable())
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})

                def removed_feature(route):
                    response = route.fetch()
                    data = response.json()
                    if isinstance(data.get('features'), list):
                        data['features'].append({'id': 3, 'name': 'RemovedFeature', 'type': 'extrude',
                            'position': 2, 'position_before': 2, 'position_after': None,
                            'removed': True, 'added': False, 'definition_changed': True,
                            'suppressed': False, 'rolled_back': False, 'references': [],
                            'changes': [{'path': '$', 'kind': 'removed', 'old': {'id': 3}, 'new': None}],
                            'runtime_cache_branch': 'unavailable'})
                    route.fulfill(response=response, body=json.dumps(data), content_type='application/json; charset=utf-8')

                page.route('**/api/recompute*', removed_feature)
                page.goto(url + '/recompute')
                self.ready(page)
                self.assertEqual(page.locator('#graph .feature').count(), 2)
                self.assertEqual(page.locator('#graph .stage').count(), 3)
                self.assertFalse(any('RemovedFeature' in text for text in page.locator('#graph .feature').all_inner_texts()))
                removed = page.locator('#graph').get_by_role('button', name='已删除 · RemovedFeature / F3', exact=True)
                removed.click()
                self.assertIn('RemovedFeature', page.locator('#feature-title').inner_text())
                self.assertIn('不在修改后的求值链', page.locator('#feature-detail').inner_text())
                self.assertEqual(page.locator('#graph .feature.selected').count(), 0)
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
