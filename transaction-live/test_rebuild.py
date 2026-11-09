"""Rebuilder tests use owned temporary folders and headless CLI processes only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from archive import Archive
from rebuild import (OfflineClient, Rebuilder, RebuildError, derive_document,
                     MAX_PENDING_JOBS, _safe_error, _same_version)


HERE = Path(__file__).resolve().parent
CLI = Path(r"C:\Program Files\SolidCraft\solidcraft-cli.exe")


def create_sample(cli: Path, workdir: Path):
    """Return a real native JSON sample: F1 sketch -> F2 five-mm extrusion."""
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    with OfflineClient(cli, None, workdir, threading.Event(), time.monotonic() + 45) as client:
        client.query("sketch.create", {"plane": "XY", "name": "Base"})
        client.query("sketch.rectangle.two_point", {"p0": [0, 0], "p1": [20, 10]})
        client.query("sketch.finish", {})
        client.query("solid.extrude", {"distance": 5, "operation": "new", "name": "Pad"})
        target = workdir / "fixture.json"
        client.export(target, "json")
        return target.read_bytes(), client.version


def wait_job(archive, job_id, timeout=40):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        job = archive.get_job(job_id)
        if job and job["status"] in {"completed", "failed", "cancelled", "timed_out"}:
            return job
        time.sleep(0.03)
    raise AssertionError("owned test job did not finish within its bounded wait")


class DocumentTests(unittest.TestCase):
    def test_marker_is_derived_without_altering_original(self):
        raw = json.dumps({"format": "solidcraft/51", "features": [{"id": 10}, {"id": 20}],
                          "marker": None, "name": "keep", "extra": {"a": 3}}).encode()
        original = raw[:]
        derived, ids = derive_document(raw, 1)
        value = json.loads(derived)
        self.assertEqual(value["marker"], 1)
        self.assertEqual(ids, [10])
        self.assertEqual(value["extra"], {"a": 3})
        self.assertEqual(raw, original)
        self.assertIsNone(json.loads(derive_document(raw, 2)[0])["marker"])

    def test_bounds_and_duplicate_fields_are_refused(self):
        raw = b'{"format":"solidcraft/51","features":[]}'
        for value in (True, -1, 1, "0"):
            with self.subTest(value=value), self.assertRaises(RebuildError):
                derive_document(raw, value)
        with self.assertRaises(RebuildError):
            derive_document(b'{"format":"solidcraft/51","features":[],"features":[]}', 0)

    def test_external_links_are_never_resolved_in_capture_folder(self):
        raw = json.dumps({"format": "solidcraft/51", "features": [],
                          "links": [{"path": "../original/part.sc"}]}).encode()
        with self.assertRaisesRegex(RebuildError, "external_dependencies"):
            derive_document(raw, 0)

    def test_diagnostics_hide_token_like_strings(self):
        text = _safe_error("token=never-record-me password:private " + "a" * 64)
        self.assertNotIn("never-record-me", text)
        self.assertNotIn("private", text)
        self.assertNotIn("a" * 64, text)

    def test_native_document_cannot_be_reinterpreted_as_a_script(self):
        for value in [
            {"format": "script", "features": []},
            {"format": "solidcraft/51", "features": [], "commands": []},
        ]:
            with self.assertRaises(RebuildError):
                derive_document(json.dumps(value).encode(), 0)

    def test_release_version_prefix_does_not_create_a_false_mismatch(self):
        self.assertTrue(_same_version("0.30.1", "v0.30.1"))
        self.assertFalse(_same_version("0.30.1", "0.30.2"))


@unittest.skipUnless(CLI.is_file(), "installed SolidCraft CLI is unavailable")
class InstalledCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_folder = tempfile.TemporaryDirectory(prefix="test-rebuild-fixture-", dir=HERE)
        cls.fixture, cls.version = create_sample(CLI, Path(cls.fixture_folder.name))

    @classmethod
    def tearDownClass(cls):
        cls.fixture_folder.cleanup()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="test-rebuild-", dir=HERE)
        self.root = Path(self.folder.name).resolve()
        self.archive = Archive(self.root / "archive")
        self.session = self.archive.new_session({"version": self.version, "test": "headless-only"})
        self.workers = []

    def tearDown(self):
        for worker in self.workers:
            worker.stop()
        self.archive.close()
        self.folder.cleanup()

    def observation(self, raw=None, version=None):
        raw = self.fixture if raw is None else raw
        event = self.archive.add_event(self.session, "test-document", 1, "sample")
        return self.archive.commit_snapshot(event, {"document.json": raw}, {
            "document_hash": hashlib.sha256(raw).hexdigest(), "document_id": "test-document",
            "revision": 1, "version": version or self.version, "source": "observed",
            "undo": 17, "redo": 2, "name": "fixture", "timeline": json.loads(raw)["features"],
            "warnings": [], "completeness": "complete_public_api",
        })

    def worker(self, timeout=45):
        worker = Rebuilder(self.archive, CLI, self.version, timeout=timeout)
        self.workers.append(worker)
        return worker

    def test_real_zero_sketch_and_extrude_prefixes(self):
        source = self.observation()
        original_asset = source["assets"]["document.json"]["sha256"]
        worker = self.worker()
        for position, bodies, sketches in [(0, 0, 0), (1, 0, 1), (2, 1, 1)]:
            with self.subTest(position=position):
                job = worker.submit(source["id"], position)
                done = wait_job(self.archive, job["id"])
                self.assertEqual(done["status"], "completed", done)
                stage = self.archive.stage(done["stage_id"])
                summary = stage["summary"]
                self.assertEqual(summary["source"], "rebuilt")
                self.assertEqual(summary["position"], position)
                self.assertEqual(summary["input_document_hash"], original_asset)
                self.assertNotIn("undo", summary)
                self.assertNotIn("redo", summary)
                self.assertFalse(summary["native_model_state_available"])
                inspected = json.loads(self.archive.read_asset(stage["assets"]["inspect.json"]["sha256"]))
                self.assertEqual(len(inspected["bodies"]), bodies)
                self.assertEqual(len(inspected["sketches"]), sketches)
                geometry = json.loads(self.archive.read_asset(stage["assets"]["geometry.json"]["sha256"]))
                self.assertEqual(len(geometry["stages"]), bodies)
                self.assertEqual("model.3mf" in stage["assets"], bool(bodies))
        self.assertEqual(self.archive.read_asset(original_asset), self.fixture)
        self.assertFalse(list((self.archive.root / ".work").glob("rebuild-*")))

    def test_version_mismatch_is_explicit(self):
        source = self.observation(version="0.0.0-test")
        done = wait_job(self.archive, self.worker().submit(source["id"], 0)["id"])
        self.assertEqual(done["status"], "completed", done)
        summary = self.archive.stage(done["stage_id"])["summary"]
        self.assertEqual(summary["input_version"], "0.0.0-test")
        self.assertEqual(summary["cli_version"], self.version)
        self.assertTrue(any("version differs" in warning for warning in summary["warnings"]))

    def test_native_failed_feature_is_not_reported_as_a_complete_model(self):
        value = json.loads(self.fixture)
        # The serialized Feature flattens kind fields into the feature record.
        extrusion = value["features"][1]
        extrusion["extent"]["distance"] = "missing_parameter_for_rebuild_test"
        source = self.observation(json.dumps(value).encode())
        done = wait_job(self.archive, self.worker().submit(source["id"], 2)["id"])
        self.assertEqual(done["status"], "completed", done)
        summary = self.archive.stage(done["stage_id"])["summary"]
        self.assertEqual(summary["completeness"], "partial_reconstruction")
        self.assertTrue(any(item.get("error") for item in summary["timeline"]))

    def test_suppressed_extrusion_keeps_sketch_and_source_unchanged(self):
        value = json.loads(self.fixture)
        value["features"][1]["suppressed"] = True
        raw = json.dumps(value).encode()
        source = self.observation(raw)
        source_hash = source["assets"]["document.json"]["sha256"]
        done = wait_job(self.archive, self.worker().submit(source["id"], 2)["id"])
        self.assertEqual(done["status"], "completed", done)
        stage = self.archive.stage(done["stage_id"])
        inspected = json.loads(self.archive.read_asset(stage["assets"]["inspect.json"]["sha256"]))
        self.assertEqual(len(inspected["bodies"]), 0)
        self.assertEqual(len(inspected["sketches"]), 1)
        self.assertTrue(inspected["timeline"][1]["suppressed"])
        self.assertEqual(stage["summary"]["input_document_hash"], source_hash)
        self.assertEqual(self.archive.read_asset(source_hash), raw)

    def test_external_dependencies_fail_durably(self):
        value = json.loads(self.fixture)
        value["links"] = [{"path": "../unfrozen/part.sc"}]
        source = self.observation(json.dumps(value).encode())
        done = wait_job(self.archive, self.worker().submit(source["id"], 1)["id"])
        self.assertEqual(done["status"], "failed", done)
        self.assertIn("external_dependencies", done.get("message", ""))
        self.assertIsNone(done.get("stage_id"))

    def test_job_cannot_export_without_shared_work_budget(self):
        source = self.observation()
        # Reserve metadata room but less than the CLI job's 256 MiB workspace.
        self.archive.limit_bytes = 160 * 1024 * 1024
        done = wait_job(self.archive, self.worker().submit(source["id"], 0)["id"])
        self.assertEqual(done["status"], "failed", done)
        self.assertIsNone(done.get("stage_id"))
        self.assertEqual(self.archive.status().get("reserved_bytes", 0), 0)
        self.assertFalse(list((self.archive.root / ".work").glob("rebuild-*")))
        self.assertEqual(self.archive.read_asset(source["assets"]["document.json"]["sha256"]), self.fixture)

    def test_collector_failure_reaps_cli_and_preserves_diagnostic(self):
        source = self.observation()
        children = []

        def broken(client, *args, **kwargs):
            children.append(client.process)
            client.request("document.inspect", {})
            client.export(client.workdir / "document.json", "json")
            raise RebuildError("isolated collector failure")

        with patch("rebuild._collect_bundle", broken):
            done = wait_job(self.archive, self.worker().submit(source["id"], 0)["id"])
        self.assertEqual(done["status"], "failed", done)
        self.assertIn("isolated collector failure", done.get("diagnostic", done.get("message", "")))
        self.assertTrue(children)
        self.assertIsNotNone(children[0].poll())
        self.assertIn("document.json", done["partial_assets"])
        self.assertIn("derived.json", done["partial_assets"])
        reads = json.loads(self.archive.read_asset(done["partial_assets"]["native-reads.json"]["sha256"]))
        self.assertTrue(any(item["command"] == "document.inspect" for item in reads["reads"]))
        self.assertIsNone(done.get("stage_id"))

    def test_cancel_and_queue_bound_reap_only_owned_cli(self):
        source = self.observation()
        started = threading.Event()
        children = []

        def slow(client, *args, **kwargs):
            children.append(client.process)
            started.set()
            while True:
                client.request("document.revision")
                time.sleep(0.02)

        worker = self.worker()
        with patch("rebuild._collect_bundle", slow):
            active = worker.submit(source["id"], 0)
            self.assertTrue(started.wait(15))
            pending = [worker.submit(source["id"], 0) for _ in range(MAX_PENDING_JOBS)]
            with self.assertRaisesRegex(RebuildError, "queue is full"):
                worker.submit(source["id"], 0)
            for job in pending:
                worker.cancel(job["id"])
            worker.cancel(active["id"])
            done = wait_job(self.archive, active["id"])
        self.assertEqual(done["status"], "cancelled", done)
        self.assertIn("derived.json", done["partial_assets"])
        self.assertTrue(all(self.archive.get_job(job["id"])["status"] == "cancelled" for job in pending))
        self.assertIsNotNone(children[0].poll())

    def test_timeout_reaps_owned_cli(self):
        source = self.observation()
        children = []

        def slow(client, *args, **kwargs):
            children.append(client.process)
            while True:
                client.request("document.revision")
                time.sleep(0.02)

        with patch("rebuild._collect_bundle", slow):
            done = wait_job(self.archive, self.worker(timeout=2).submit(source["id"], 0)["id"])
        self.assertEqual(done["status"], "timed_out", done)
        if children:
            self.assertIn("derived.json", done["partial_assets"])
        if children:
            self.assertIsNotNone(children[0].poll())

    def test_stop_cancels_active_and_queued_jobs_before_archive_close(self):
        source = self.observation()
        started = threading.Event()
        children = []

        def slow(client, *args, **kwargs):
            children.append(client.process)
            started.set()
            while True:
                client.request("document.revision")
                time.sleep(0.02)

        worker = self.worker()
        with patch("rebuild._collect_bundle", slow):
            active = worker.submit(source["id"], 0)
            self.assertTrue(started.wait(15))
            queued = worker.submit(source["id"], 1)
            worker.stop()
            self.assertFalse(worker._thread.is_alive())
            self.assertEqual(self.archive.get_job(active["id"])["status"], "cancelled")
            self.assertEqual(self.archive.get_job(queued["id"])["status"], "cancelled")
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(self.archive.status().get("reserved_bytes", 0), 0)

    def test_stop_waits_for_python_collection_to_unwind(self):
        source = self.observation()
        started, release = threading.Event(), threading.Event()

        def delayed(client, *args, **kwargs):
            started.set()
            release.wait(5)
            return {}, {"timeline": [], "warnings": []}

        worker = self.worker()
        try:
            with patch("rebuild._collect_bundle", delayed):
                job = worker.submit(source["id"], 0)
                self.assertTrue(started.wait(15))
                stopping = threading.Thread(target=worker.stop)
                stopping.start()
                time.sleep(0.1)
                self.assertTrue(stopping.is_alive(), "stop must keep archive owners alive until collection exits")
                release.set()
                stopping.join(5)
                self.assertFalse(stopping.is_alive())
                self.assertFalse(worker._thread.is_alive())
                self.assertEqual(self.archive.get_job(job["id"])["status"], "cancelled")
        finally:
            release.set()

    def test_offline_client_never_allows_exports_outside_job(self):
        with OfflineClient(CLI, None, self.root, threading.Event(), time.monotonic() + 30) as client:
            with self.assertRaisesRegex(RebuildError, "outside"):
                client.export(self.root.parent / "forbidden.json", "json")
            self.assertEqual(client.process.args[1:3], ["mcp", "--headless"])
            self.assertNotIn("--connect", client.process.args)


if __name__ == "__main__":
    unittest.main()
