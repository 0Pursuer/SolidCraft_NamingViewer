"""Lifecycle UI over an owned headless CLI experiment; no desktop connection."""
import hashlib
import json
from contextlib import ExitStack, closing
from pathlib import Path
import tempfile
import threading
import unittest

from archive import Archive
from recompute_lab import RecomputeLab
from server import create_server
from test_flow_browser import ROOT, _browser_executable, sync_playwright
from test_rebuild import CLI, create_sample
from test_recompute_lab import wait_job


@unittest.skipUnless(CLI.is_file() and sync_playwright, 'installed CLI or Playwright unavailable')
class LifecycleRealEvidenceTests(unittest.TestCase):
    def test_real_20_40_undo_evidence_stays_separate_from_source_frames(self):
        with tempfile.TemporaryDirectory(prefix='test-lifecycle-real-', dir=ROOT) as folder, ExitStack() as cleanup:
            work = Path(folder)
            raw, version = create_sample(CLI, work / 'fixture')
            document = json.loads(raw)
            with closing(Archive(work / 'archive')) as writer:
                session = writer.new_session({'version': version})
                ids = []
                for revision, height in enumerate(('20 mm', '40 mm'), 1):
                    document['features'][1]['extent']['distance'] = height
                    data = json.dumps(document, ensure_ascii=False).encode('utf-8')
                    event = writer.add_event(session, 'owned-lifecycle-test', revision, 'sample')
                    record = writer.commit_snapshot(event, {'document.json': data}, {
                        'document_id': 'owned-lifecycle-test', 'revision': revision,
                        'version': version, 'source': 'observed', 'marker': None,
                        'timeline': document['features'],
                        'document_hash': hashlib.sha256(data).hexdigest()})
                    ids.append(record['id'])
            archive = Archive(work / 'archive', read_only=True)
            before = archive.status()['counts']
            cleanup.callback(archive.close)
            lab = RecomputeLab(archive, CLI, work / 'lab', version)
            cleanup.callback(lab.stop)
            job = wait_job(lab, lab.submit(*ids, 'edit_undo')['id'])
            self.assertEqual(job['status'], 'completed', job)
            http = create_server(archive, version=version, recompute_lab=lab)
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            url = f'http://127.0.0.1:{http.server_port}'
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True, executable_path=_browser_executable())
                    page = browser.new_page(viewport={'width': 1540, 'height': 1250})
                    errors, posts = [], []
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    page.on('request', lambda r: posts.append(r.url) if r.method == 'POST' else None)
                    page.goto(url + f'/recompute#before={ids[0]}&after={ids[1]}')
                    page.wait_for_function('() => window.__lifecycle?.state.data?.frames?.length === 13')
                    page.locator('#life-use-experiment').click()
                    page.wait_for_function('(id) => window.__lifecycle?.state.jobId === id', arg=job['id'])
                    frames = page.evaluate('() => window.__lifecycle.state.data.frames')
                    actual = [f for f in frames if f['provenance'] == 'native_phase_observation']
                    self.assertEqual([f['id'] for f in actual], ['baseline', 'edited', 'undone'])
                    volumes = [next(v['value'] for v in f['data']['model']['fields'] if v['name'] == 'volume_mm3') for f in actual]
                    self.assertEqual(volumes, [4000, 8000, 4000])
                    for frame_id, height, undo, redo in [('baseline', '20 mm', 0, 0), ('edited', '40 mm', 1, 0), ('undone', '20 mm', 0, 1)]:
                        page.locator(f'#life-stages button[data-frame="{frame_id}"]').click()
                        page.wait_for_function('(id) => window.__lifecycle.state.data.frames[window.__lifecycle.state.frame].id === id', arg=frame_id)
                        self.assertIn(height, page.locator('#life-data [data-object="document"]').inner_text())
                        session = page.locator('#life-diagram [data-object="session"] .life-value').inner_text()
                        self.assertIn(f'Undo {undo} / Redo {redo}', session)
                        self.assertIn('实际采集', page.locator('#life-origin').inner_text())
                    page.locator('#life-stages button[data-frame="edited"]').click()
                    page.evaluate('window.scrollTo(0,0)')
                    page.locator('#lifecycle').screenshot(path=str(ROOT / 'lifecycle-edit.png'))
                    page.locator('#life-stages button[data-frame="undone"]').click()
                    page.evaluate('window.scrollTo(0,0)')
                    page.locator('#lifecycle').screenshot(path=str(ROOT / 'lifecycle-undo.png'))
                    self.assertEqual(errors, [])
                    self.assertEqual(posts, [])
                    browser.close()
                self.assertEqual(archive.status()['counts'], before)
                self.assertEqual(len(lab.list_jobs()), 1)
            finally:
                http.shutdown()
                thread.join(5)
                http.server_close()


if __name__ == '__main__':
    unittest.main()
