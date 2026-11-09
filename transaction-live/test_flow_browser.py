"""Flow page integration uses a readonly archive; it never contacts a CAD process."""
from __future__ import annotations

from contextlib import contextmanager
import copy
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from archive import Archive
from server import create_server

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


ROOT = Path(__file__).resolve().parent
_real_archive = os.environ.get("TRACE_REAL_ARCHIVE")
REAL_ARCHIVE = Path(_real_archive).resolve() if _real_archive else None


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _sketch(empty=False):
    points = [{"id": "origin", "pos": [0, 0], "fixed": True}]
    if not empty:
        points += [{"id": "p" + str(i + 1), "pos": p, "fixed": False}
                   for i, p in enumerate([[0, 0], [40, 0], [40, 25], [0, 25]])]
    return {"id": 1, "name": "Sketch1", "type": "sketch", "suppressed": False,
            "plane": {"type": "origin", "name": "XY"},
            "sketch": {"points": points,
                       "curves": [] if empty else [{"id": "l" + str(i + 1), "type": "line",
                                                     "a": i + 1, "b": (i + 1) % 4 + 1, "construction": False}
                                                    for i in range(4)],
                       "constraints": [] if empty else [
                           {"id": "k1", "type": "horizontal", "l": 0},
                           {"id": "k2", "type": "horizontal", "l": 2},
                           {"id": "k3", "type": "vertical", "l": 1},
                           {"id": "k4", "type": "vertical", "l": 3},
                           {"id": "k5", "type": "coincident", "p": 0, "q": 1}],
                       "counters": {} if empty else {"p": 4, "l": 4, "k": 5}}}


def make_flow_fixture(root: Path):
    """An observed edit has a marker-only entry, one commit and one undo."""
    store = Archive(root)
    session = store.new_session({"name": "Flow test", "version": "0.30.1"})
    ids = {}

    def publish(doc_id, document, revision, undo, redo=0):
        event = store.add_event(session, doc_id, revision, "design.changed")
        marker = document.get("marker")
        timeline = [{"id": f["id"], "name": f["name"], "type": "Sketch" if f["type"] == "sketch" else "ExtrudeFeature",
                     "rolled_back": marker is not None and i >= marker, "suppressed": f.get("suppressed", False),
                     "error": None, "warning": None, "ms": 1.25}
                    for i, f in enumerate(document["features"])]
        ext = next((f for f in document["features"] if f["type"] == "extrude"), None)
        height = float(ext["extent"]["distance"].split()[0]) if ext and marker is None else 0
        bodies = ([{"name": "Body1", "bbox": {"min": [0, 0, 0], "max": [40, 25, height]},
                    "volume_mm3": 1000 * height, "faces": 6, "edges": 12,
                    "kernel": {"faces": 6, "edges": 12, "vertices": 8}}] if height else [])
        inspected = {"name": document["name"], "timeline": timeline, "bodies": bodies,
                     "undo": undo, "redo": redo, "marker": len(timeline) if marker is None else marker}
        return store.commit_snapshot(event, {"document.json": _bytes(document), "inspect.json": _bytes(inspected)},
            {"document_id": doc_id, "revision": revision, "name": document["name"], "undo": undo, "redo": redo,
             "marker": marker, "timeline": timeline, "source": "observed", "version": "0.30.1",
             "warnings": [], "completeness": "complete_public_api"})

    sample = {"format": "solidcraft/48", "name": 'Sample Plate <img src=x onerror="window.__flowXss=1">',
              "features": [{"id": 99, "name": "Old plate", "type": "extrude", "extent": {"distance": "8 mm"}}],
              "params": [], "marker": None}
    ids["sample"] = publish("sample-doc", sample, 1, 20)["id"]
    document = {"format": "solidcraft/48", "name": "Height experiment", "units": "mm", "params": [],
                "features": [], "marker": None}
    ids["empty"] = publish("experiment-doc", document, 2, 0)["id"]
    document["features"].append(_sketch(True))
    ids["sketch"] = publish("experiment-doc", document, 3, 1)["id"]
    document["features"] = [_sketch()]
    ids["rectangle"] = publish("experiment-doc", document, 4, 2)["id"]
    document["features"].append({"id": 2, "name": "Extrude1", "type": "extrude", "suppressed": False,
        "sketch": 1, "profiles": {"type": "curves", "loops": [["l1", "l2", "l3", "l4"]]},
        "extent": {"distance": "20 mm", "convention": "total_distance", "direction": "positive"},
        "operation": "new_body", "body_names": ["Body1"]})
    original = copy.deepcopy(document)
    ids["height20"] = publish("experiment-doc", document, 5, 3)["id"]
    document["marker"] = 1
    ids["editing"] = publish("experiment-doc", document, 6, 3)["id"]
    document["marker"] = None
    document["features"][-1]["extent"]["distance"] = "40 mm"
    ids["height40"] = publish("experiment-doc", document, 7, 4)["id"]
    ids["undo"] = publish("experiment-doc", original, 8, 3, 1)["id"]
    store.close()
    return ids


@contextmanager
def readonly_server(root: Path):
    archive = Archive(root, read_only=True)
    http = create_server(archive, version="0.30.1")
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", archive
    finally:
        http.shutdown()
        http.server_close()
        worker.join(timeout=3)
        archive.close()


def _browser_executable():
    configured = os.environ.get("TRACE_UI_BROWSER")
    if configured:
        return configured
    available = sorted((Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright").glob("chromium-*/chrome-win64/chrome.exe"))
    return str(available[-1]) if available else None


@unittest.skipIf(sync_playwright is None, "Python Playwright is unavailable")
class FlowBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flow-browser-", dir=ROOT)
        self.archive_root = Path(self.temp.name) / "archive"
        self.ids = make_flow_fixture(self.archive_root)

    def tearDown(self):
        self.temp.cleanup()

    def test_parallel_readonly_views_keep_distinct_browser_cookies(self):
        # Cookies belong to a host/path, not a TCP port. Distinct names keep a
        # flow reader from invalidating the recorder's other browser tab.
        with readonly_server(self.archive_root) as (first_url, _), readonly_server(self.archive_root) as (second_url, _):
            connections, cookies = [], []
            try:
                for url in (first_url, second_url):
                    connection = HTTPConnection("127.0.0.1", int(url.rsplit(":", 1)[1]), timeout=5)
                    connections.append(connection)
                    connection.request("GET", "/flow")
                    response = connection.getresponse()
                    response.read()
                    self.assertEqual(response.status, 200)
                    cookies.append(response.getheader("Set-Cookie").split(";", 1)[0])
                self.assertNotEqual(cookies[0].split("=", 1)[0], cookies[1].split("=", 1)[0])
                combined = "; ".join(cookies)
                for connection in connections:
                    connection.request("GET", "/api/flow?document_id=experiment-doc&limit=200", headers={"Cookie": combined})
                    response = connection.getresponse()
                    data = json.loads(response.read())
                    self.assertEqual(response.status, 200, data)
                    self.assertEqual(data["current"]["undo_depth"], 3)
                    self.assertEqual(data["current"]["redo_depth"], 1)
            finally:
                for connection in connections:
                    connection.close()

    def test_archive_link_opens_the_exact_observation(self):
        with readonly_server(self.archive_root) as (url, _), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_browser_executable())
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.goto(url + '/flow')
            page.locator('#flow-strip').get_by_text('40', exact=False).first.click()
            href = page.locator('#archive-link').get_attribute('href')
            self.assertEqual(href, '/archive#O' + str(self.ids['height40']))
            page.locator('#archive-link').click()
            page.locator('#detail-title').filter(has_text='O' + str(self.ids['height40']).zfill(4)).wait_for()
            page.get_by_role('tab', name='变化对比', exact=True).click()
            self.assertIn('40 mm', page.locator('#diff-table').inner_text())
            self.assertFalse(page.locator('#follow-latest').is_checked())
            browser.close()

    def test_readonly_flow_explains_committed_and_undone_height_without_cad_writes(self):
        with readonly_server(self.archive_root) as (url, archive), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_browser_executable())
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors, writes = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: writes.append(request.url) if request.method not in {"GET", "HEAD"} else None)
            page.goto(url + "/flow")
            page.locator("#document-select").select_option("experiment-doc")
            page.locator("#flow-strip").get_by_text("40", exact=False).first.wait_for()
            strip = page.locator("#flow-strip").inner_text()
            self.assertIn("草图", strip)
            self.assertIn("拉伸", strip)
            self.assertIn("20", strip)
            self.assertIn("40", strip)
            self.assertIn("当前", strip)
            # The operation that was undone remains inspectable on the redo side.
            page.locator("#flow-strip").get_by_text("40", exact=False).first.click()
            details = page.locator("#detail-tree").inner_text()
            self.assertIn("20", details)
            self.assertIn("40", details)
            self.assertIn("distance", details)
            self.assertEqual(page.locator("#undo-depth").inner_text(), "3")
            self.assertEqual(page.locator("#redo-depth").inner_text(), "1")
            page.screenshot(path=str(ROOT / "flow-desktop.png"), full_page=True)
            # Switching document cannot retain operations from the preceding doc.
            page.locator("#document-select").select_option("sample-doc")
            page.locator("#flow-strip").get_by_text("40", exact=False).wait_for(state="hidden")
            self.assertIn("Sample Plate", page.locator("#document-select").inner_text())
            self.assertEqual(page.locator('img[src="x"]').count(), 0)
            self.assertIsNone(page.evaluate("() => window.__flowXss"))
            page.locator("#document-select").select_option("experiment-doc")
            page.locator("#flow-strip").get_by_text("40", exact=False).first.wait_for()
            page.set_viewport_size({"width": 390, "height": 844})
            self.assertFalse(page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth"))
            page.screenshot(path=str(ROOT / "flow-mobile.png"), full_page=True)
            self.assertEqual(writes, [])
            self.assertEqual(errors, [])
            self.assertTrue(archive.read_only)
            self.assertEqual(archive.status()["counts"]["snapshots"], 8)
            browser.close()

    def test_polling_preserves_the_selected_before_view_and_expanded_evidence(self):
        with readonly_server(self.archive_root) as (url, _), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_browser_executable())
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(url + "/flow")
            page.locator("#flow-strip").get_by_text("40", exact=False).first.click()
            page.locator("#preview-before").click()
            page.locator("#collapse-all").click()
            page.locator("#evidence summary").click()
            self.assertIn("active", page.locator("#preview-before").get_attribute("class"))
            self.assertTrue(page.locator("#evidence").evaluate("element => element.open"))
            self.assertEqual(page.locator("#detail-tree details[open]").count(), 0)
            # The page polls every two seconds: ordinary refresh must not reset
            # local exploration when neither the observation nor operation changed.
            page.wait_for_timeout(2700)
            self.assertIn("active", page.locator("#preview-before").get_attribute("class"))
            self.assertTrue(page.locator("#evidence").evaluate("element => element.open"))
            self.assertEqual(page.locator("#detail-tree details[open]").count(), 0)
            self.assertIn("20", page.locator("#geometry-metrics").inner_text())
            browser.close()

    def test_delayed_observation_cannot_replace_a_new_operation_preview(self):
        with readonly_server(self.archive_root) as (url, archive), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_browser_executable())
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(url + "/flow")
            page.locator("#flow-strip").get_by_text("40", exact=False).first.wait_for()
            entered, release = threading.Event(), threading.Event()
            original_snapshot = archive.snapshot
            delayed_id = self.ids["sketch"]

            def delayed_snapshot(snapshot_id):
                if snapshot_id == delayed_id and not release.is_set():
                    entered.set()
                    release.wait(8)
                return original_snapshot(snapshot_id)

            try:
                with patch.object(archive, "snapshot", side_effect=delayed_snapshot):
                    page.locator("#event-strip button").filter(has_text=f'O{self.ids["rectangle"]:04d}').click()
                    self.assertTrue(entered.wait(3), "the old observation's before-request was not delayed")
                    page.locator("#flow-strip").get_by_text("40", exact=False).first.click()
                    self.assertIn("修改", page.locator("#selection-title").inner_text())
                    release.set()
                    page.wait_for_timeout(400)
                    page.locator("#preview-before").click()
                    # The selected height change began with one twenty-mm body;
                    # the delayed sketch observation had no bodies at all.
                    self.assertIn("20", page.locator("#geometry-metrics").inner_text())
                    self.assertIn("Body1", page.locator("#body-summary").inner_text())
                    self.assertIn("修改", page.locator("#selection-title").inner_text())
            finally:
                release.set()
                browser.close()

    @unittest.skipUnless(REAL_ARCHIVE is not None and (REAL_ARCHIVE / "archive.sqlite3").is_file(),
                         "set TRACE_REAL_ARCHIVE to opt into the matching read-only demo archive")
    def test_actual_demo_archive_displays_current_twenty_and_undone_forty(self):
        with readonly_server(REAL_ARCHIVE) as (url, archive), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_browser_executable())
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url + "/flow")
            page.locator("#document-select").select_option("6ce8581ab70c8cca-3")
            page.locator("#flow-strip").get_by_text("40", exact=False).first.wait_for()
            page.locator("#flow-strip").get_by_text("40", exact=False).first.click()
            tree = page.locator("#detail-tree").inner_text()
            self.assertIn("20", tree)
            self.assertIn("40", tree)
            self.assertIn("distance", tree)
            self.assertEqual(page.locator("#undo-depth").inner_text(), "3")
            self.assertEqual(page.locator("#redo-depth").inner_text(), "1")
            page.screenshot(path=str(ROOT / "flow-real-demo.png"), full_page=True)
            self.assertEqual(errors, [])
            self.assertEqual(archive.latest_snapshot("6ce8581ab70c8cca-3")["summary"]["undo"], 3)
            browser.close()


if __name__ == "__main__":
    unittest.main()
