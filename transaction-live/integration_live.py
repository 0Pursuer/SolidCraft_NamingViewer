"""Explicit installed-desktop integration test; mutates only its own launched PID.

Not discovered by unittest. Run: py -3 integration_live.py
The retained test archive is useful as an offline demonstration.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import time

from archive import Archive
from native import ControlClient, Recorder
from rebuild import Rebuilder
from server import installed_version
from solidcraft_instances import default_control_dir, read_binding, InvalidBinding
from test_rebuild import wait_job

ROOT = Path(__file__).resolve().parent


def wait_until(fn, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(.1)
    raise AssertionError('独立测试步骤等待超时')


def main():
    executable = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'SolidCraft' / 'solidcraft.exe'
    cli = executable.with_name('solidcraft-cli.exe')
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    archive = Archive(ROOT / 'verification-records' / time.strftime('%Y%m%d-%H%M%S'))
    process = subprocess.Popen([str(executable), '--control', str(port)], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    recorder = client = worker = None
    try:
        def binding_ready():
            if process.poll() is not None:
                raise RuntimeError('独立测试程序提前退出')
            try:
                binding = read_binding(default_control_dir(), port)
                if binding.pid != process.pid:
                    raise RuntimeError('控制端口 PID 不属于本测试启动实例，拒绝写操作')
                return binding
            except (FileNotFoundError, InvalidBinding):
                return None
        binding = wait_until(binding_ready, 35)
        client = ControlClient(binding)
        wait_until(lambda: connect(client), 20)
        recorder = Recorder(archive, binding, '0.30.1', 'Owned integration test')
        recorder.start()
        labels = []

        def latest_after(revision):
            snapshot = archive.latest_snapshot()
            return snapshot if snapshot and snapshot['summary']['revision'] == revision else None

        def run(command, params):
            # This private test connection is never exposed by the server.
            client._request('engine.execute', {'command': command, 'params': params}, timeout=30)
            revision = client.request('document.revision')['revision']
            snapshot = wait_until(lambda: latest_after(revision))
            labels.append({'command': command, 'observation': snapshot['label'],
                'undo': snapshot['summary'].get('undo'), 'redo': snapshot['summary'].get('redo'),
                'diff': snapshot.get('diff'), 'inference': snapshot.get('inference')})
            return snapshot

        wait_until(lambda: archive.latest_snapshot())
        run('sketch.create', {'plane': 'XY', 'name': 'TraceRectangle'})
        run('sketch.rectangle.two_point', {'p0': [0, 0], 'p1': [20, 10]})
        run('sketch.finish', {})
        pad = run('solid.extrude', {'distance': 20, 'operation': 'new', 'name': 'TracePad'})
        changed = run('parameters.change', {'name': 'd1', 'expression': '30'})
        undone = run('edit.undo', {})
        redone = run('edit.redo', {})
        assert changed['summary']['undo'] == pad['summary']['undo'] + 1
        assert undone['summary']['redo'] > changed['summary']['redo']
        assert redone['summary']['redo'] == changed['summary']['redo']
        assert any('30' in str(x.get('new')) for x in changed['diff']), changed['diff']
        before = client.request('document.inspect')
        key = client.request('document.revision')
        worker = Rebuilder(archive, cli, installed_version(cli), timeout=45)
        rebuilt = []
        for prefix in (0, 1, 2):
            job = wait_job(archive, worker.submit(redone['id'], prefix)['id'])
            assert job['status'] == 'completed', job
            rebuilt.append({'position': prefix, 'stage_id': job['stage_id']})
        after = client.request('document.inspect')
        assert before == after, '离线重建改变现场 inspect'
        assert key == client.request('document.revision'), '离线重建改变现场 revision'
        # Fast consecutive edits are intentionally not waited on. Notifications
        # and consistent captures can differ; never manufacture intermediate O.
        for value in range(31, 41):
            client._request('engine.execute', {'command': 'parameters.change', 'params': {'name': 'd1', 'expression': str(value)}}, timeout=30)
        final_revision = client.request('document.revision')['revision']
        wait_until(lambda: latest_after(final_revision))
        # Switch to a fresh test document, then verify the observer follows document identity.
        original_document = key['document_id']
        run('file.new', {})
        assert client.request('document.revision')['document_id'] != original_document
        # No browser has ever been opened during this test: recording is independent.
        result = {'result': 'passed', 'version': recorder.version, 'owned_pid': process.pid,
            'archive': str(archive.root), 'steps': labels, 'reconstructed': rebuilt,
            'events': archive.list_events(limit=500), 'status': archive.status(),
            'native_unchanged_by_rebuild': True, 'browser_opened': False}
        report = ROOT / 'verification-records' / 'live-integration.json'
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'result': 'passed', 'archive': str(archive.root), 'steps': len(labels), 'report': str(report)}, ensure_ascii=False))
    finally:
        if worker:
            worker.stop()
        if recorder:
            recorder.stop()
        if client:
            client.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(5)
        archive.close()


def connect(client):
    try:
        client.connect()
        return True
    except OSError:
        return False


if __name__ == '__main__':
    main()
