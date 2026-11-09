"""Check the actual HTTP server and a previously captured native archive."""
import argparse
import os
from pathlib import Path
import threading

from playwright.sync_api import sync_playwright

from archive import Archive
from server import create_server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', type=Path)
    args = parser.parse_args()
    archive = Archive(args.archive, read_only=True)
    http = create_server(archive, version='0.30.1')
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            available = sorted((Path(os.environ.get('LOCALAPPDATA', '')) / 'ms-playwright').glob('chromium-*/chrome-win64/chrome.exe'))
            browser = pw.chromium.launch(headless=True, executable_path=os.environ.get('TRACE_UI_BROWSER') or str(available[-1]))
            page = browser.new_page(viewport={'width': 1536, 'height': 1100})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{http.server_port}/')
            page.wait_for_function('() => window.__traceViewer?.state.record')
            page.locator('#event-jump').fill('5')
            page.locator('#event-jump-button').click()
            page.wait_for_function('() => window.__traceViewer.state.record?.id===5')
            page.wait_for_function("() => document.querySelector('iframe').contentWindow.__traceGeometry?.data.stages.length===1")
            assert page.locator('#stat-undo').inner_text() == '4'
            page.get_by_role('tab', name='变化对比').click()
            assert 'features[id=2].extent.distance' in page.locator('#diff-table').inner_text()
            assert '30' in page.locator('#diff-table').inner_text()
            page.get_by_role('tab', name='历史几何').click()
            page.screenshot(path=str(Path(__file__).with_name('ui-real-observation.png')), full_page=True)
            page.locator('#event-jump').fill('7')
            page.locator('#event-jump-button').click()
            page.wait_for_function('() => window.__traceViewer.state.record?.id===7')
            page.get_by_role('tab', name='特征时间线').click()
            page.get_by_role('button', name='查看阶段', exact=True).nth(1).click()
            page.wait_for_function('() => window.__traceViewer.state.stage!==null')
            page.wait_for_function("() => document.querySelector('iframe').contentWindow.__traceGeometry?.data.stages.length===0")
            assert page.locator('#stat-undo').inner_text() == '不可获取'
            assert '4 条原生草图线段' in page.frame_locator('#geometry-frame').locator('#status-extra').inner_text()
            page.get_by_role('tab', name='历史几何').click()
            page.screenshot(path=str(Path(__file__).with_name('ui-real-sketch-stage.png')), full_page=True)
            assert not errors, errors
            browser.close()
        print('实际HTTP服务 + 原生测试档案 + 3D实体/离线草图阶段：通过')
    finally:
        http.shutdown()
        http.server_close()
        thread.join(3)
        archive.close()


if __name__ == '__main__':
    main()
