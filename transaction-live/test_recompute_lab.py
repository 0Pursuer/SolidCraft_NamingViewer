"""Owned headless-CLI integration tests; never connect to a desktop session."""
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
from rebuild import RebuildError
from recompute_lab import RecomputeLab, MAX_PENDING, WORK_RESERVE_BYTES
from test_rebuild import CLI, create_sample


HERE = Path(__file__).resolve().parent


def wait_job(lab, job_id, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = lab.get(job_id)
        if job and job["status"] in {"completed", "failed", "cancelled", "timed_out", "interrupted"}:
            return job
        time.sleep(0.02)
    raise AssertionError("bounded isolated CLI job did not finish")


class ReadOnlySource:
    """Fail if the lab attempts any writer, job, or connection discovery API."""

    def __init__(self, archive):
        self.archive = archive

    def snapshot(self, source_id):
        return self.archive.snapshot(source_id)

    def asset_path(self, sha):
        return self.archive.asset_path(sha)


@unittest.skipUnless(CLI.is_file(), "installed SolidCraft CLI unavailable")
class RecomputeLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_folder = tempfile.TemporaryDirectory(prefix="lab-fixture-", dir=HERE)
        raw, cls.version = create_sample(CLI, Path(cls.fixture_folder.name))
        value = json.loads(raw)
        value["features"][1]["extent"]["distance"] = "20 mm"
        cls.before_raw = json.dumps(value, ensure_ascii=False).encode()
        value["features"][1]["extent"]["distance"] = "40 mm"
        cls.after_raw = json.dumps(value, ensure_ascii=False).encode()

    @classmethod
    def tearDownClass(cls):
        cls.fixture_folder.cleanup()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="test-recompute-lab-", dir=HERE)
        self.root = Path(self.folder.name).resolve()
        writer = Archive(self.root / "source")
        session = writer.new_session({"version": self.version})
        self.source_ids = []
        for i, raw in enumerate((self.before_raw, self.after_raw), 1):
            event = writer.add_event(session, "owned-test-document", i, "sample")
            snapshot = writer.commit_snapshot(event, {"document.json": raw}, {
                "document_id": "owned-test-document", "revision": i,
                "version": self.version, "source": "observed", "timeline": json.loads(raw)["features"],
                "document_hash": hashlib.sha256(raw).hexdigest(), "marker": None,
            })
            self.source_ids.append(snapshot["id"])
        writer.close()
        self.archive = Archive(self.root / "source", read_only=True)
        self.source = ReadOnlySource(self.archive)
        self.labs = []

    def tearDown(self):
        for lab in self.labs:
            lab.stop()
        self.archive.close()
        self.folder.cleanup()

    def lab(self, **kwargs):
        lab = RecomputeLab(self.source, CLI, self.root / "lab", self.version, **kwargs)
        self.labs.append(lab)
        return lab

    def submit(self, lab, mode="edit_undo"):
        return lab.submit(*self.source_ids, mode)

    def asset_json(self, lab, job, index, name):
        return json.loads(lab.asset_path(job["id"], index, name).read_bytes())

    def test_real_edit_20_to_40_then_undo_retains_native_evidence(self):
        lab = self.lab()
        done = wait_job(lab, self.submit(lab)["id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertRegex(done["id"], r"^[0-9a-f]{32}$")
        self.assertEqual([p["key"] for p in done["phases"]], ["baseline", "edited", "undone"])
        edited = done["phases"][1]
        self.assertEqual(edited["command_params"], {"feature": 2, "set": {"extent.distance": "40 mm"}})
        self.assertEqual(edited["command_result"], {"feature": 2, "recomputed": 1, "restored": 0})
        self.assertEqual(edited["counts"]["availability"], "native_command_result")
        self.assertEqual(done["phases"][2]["command_result"], {"undone": "Edit Feature"})
        self.assertIsNone(done["phases"][2]["counts"]["recomputed"])
        self.assertEqual(done["phases"][2]["counts"]["availability"], "not_returned")
        for index, height, undo, redo in [(0, "20 mm", 0, 0), (1, "40 mm", 1, 0), (2, "20 mm", 0, 1)]:
            with self.subTest(index=index):
                doc = self.asset_json(lab, done, index, "document.json")
                inspected = self.asset_json(lab, done, index, "inspect.json")
                geometry = self.asset_json(lab, done, index, "geometry.json")
                self.assertEqual(doc["features"][1]["extent"]["distance"], height)
                self.assertEqual(inspected["undo"], undo)
                self.assertEqual(inspected["redo"], redo)
                self.assertEqual(len(inspected["bodies"]), 1)
                self.assertEqual(len(geometry["stages"]), 1)
                self.assertTrue(done["phases"][index]["verification"]["definitions_match"])
                self.assertFalse(done["phases"][index]["summary"]["original_model_state"])
        self.assertEqual(self.archive.read_asset(done["source"]["before_hash"]), self.before_raw)
        self.assertEqual(self.archive.read_asset(done["source"]["after_hash"]), self.after_raw)
        self.assertFalse(list((lab.root / ".work").iterdir()))
        self.assertLess(lab._usage(), lab.limit_bytes)

    def test_real_complete_recompute_returns_only_actual_native_counters(self):
        lab = self.lab()
        done = wait_job(lab, self.submit(lab, "full")["id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(len(done["phases"]), 1)
        phase = done["phases"][0]
        self.assertEqual(phase["command"], "timeline.compute_all")
        self.assertEqual(phase["command_result"], {"features": 2, "errors": 0})
        self.assertIsNone(phase["counts"]["recomputed"])
        self.assertIsNone(phase["counts"]["restored"])
        self.assertEqual(self.asset_json(lab, done, 0, "document.json")["features"][1]["extent"]["distance"], "40 mm")

    def test_durable_jobs_reload_without_reexecuting_and_root_has_single_owner(self):
        lab = self.lab()
        done = wait_job(lab, self.submit(lab)["id"])
        self.assertEqual(done["status"], "completed", done)
        with self.assertRaisesRegex(RebuildError, "另一个"):
            self.lab()
        lab.stop()
        restarted = self.lab()
        self.assertEqual(restarted.get(done["id"]), done)
        self.assertEqual(len(restarted.list_jobs()), 1)
        self.assertTrue(restarted.asset_path(done["id"], 1, "geometry.json").is_file())

    def test_interrupted_durable_state_is_marked_without_resume(self):
        lab = self.lab()
        done = wait_job(lab, self.submit(lab, "full")["id"])
        lab.stop()
        manifest = lab.root / "jobs" / ("job_" + done["id"]) / "result.json"
        saved = json.loads(manifest.read_bytes())
        saved["status"] = "running"
        manifest.write_text(json.dumps(saved), encoding="utf-8")
        own_work = lab.root / ".work" / ("lab-" + "a" * 32)
        own_work.mkdir()
        (own_work / "unfinished-export.json").write_bytes(b"{}")
        restarted = self.lab()
        recovered = restarted.get(done["id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["phases"], done["phases"])
        self.assertFalse(own_work.exists())

    def test_cancel_and_bounded_queue_reap_owned_process(self):
        lab = self.lab()
        started = threading.Event()
        children = []

        def slow(client, *args, **kwargs):
            children.append(client.process)
            started.set()
            while True:
                client.request("document.revision")
                time.sleep(0.01)

        with patch("capture.collect_bundle", slow):
            active = self.submit(lab)
            self.assertTrue(started.wait(10))
            pending = [self.submit(lab) for _ in range(MAX_PENDING)]
            with self.assertRaisesRegex(RebuildError, "队列已满"):
                self.submit(lab)
            for job in pending:
                lab.cancel(job["id"])
            lab.cancel(active["id"])
            done = wait_job(lab, active["id"])
        self.assertEqual(done["status"], "cancelled", done)
        self.assertTrue(all(lab.get(job["id"])["status"] == "cancelled" for job in pending))
        self.assertIsNotNone(children[0].poll())
        self.assertNotIn("--connect", children[0].args)
        self.assertIn("native-reads.json", done["phases"][0]["assets"])
        self.assertFalse(list((lab.root / ".work").iterdir()))

    def test_timeout_reaps_child_and_preserves_partial_native_reads(self):
        lab = self.lab(timeout=1.2)
        children = []

        def slow(client, *args, **kwargs):
            children.append(client.process)
            while True:
                client.request("document.revision")
                time.sleep(0.01)

        with patch("capture.collect_bundle", slow):
            done = wait_job(lab, self.submit(lab)["id"])
        self.assertEqual(done["status"], "timed_out", done)
        self.assertIsNotNone(children[0].poll())
        reads = self.asset_json(lab, done, 0, "native-reads.json")
        self.assertTrue(reads["reads"])
        self.assertFalse(list((lab.root / ".work").iterdir()))

    def test_collector_failure_preserves_returned_edit_count_and_completed_baseline(self):
        from capture import collect_bundle
        lab = self.lab()
        count = 0
        children = []

        def fail_after_edit(client, *args, **kwargs):
            nonlocal count
            count += 1
            children.append(client.process)
            if count == 2:
                raise RebuildError("injected edited capture failure")
            return collect_bundle(client, *args, **kwargs)

        with patch("capture.collect_bundle", fail_after_edit):
            done = wait_job(lab, self.submit(lab)["id"])
        self.assertEqual(done["status"], "failed", done)
        self.assertEqual(done["phases"][0]["status"], "completed")
        self.assertEqual(done["phases"][1]["status"], "incomplete")
        self.assertEqual(done["phases"][1]["command_result"], {"feature": 2, "recomputed": 1, "restored": 0})
        self.assertIsNotNone(children[0].poll())
        self.assertNotIn("document.json", done["phases"][1]["assets"])

    def test_stop_waits_for_python_unwind_and_ends_pending_jobs(self):
        lab = self.lab()
        started, release = threading.Event(), threading.Event()
        children = []

        def waiting(client, *args, **kwargs):
            children.append(client.process)
            started.set()
            release.wait(5)
            client.request("document.revision")

        try:
            with patch("capture.collect_bundle", waiting):
                active = self.submit(lab)
                self.assertTrue(started.wait(10))
                pending = self.submit(lab)
                stopping = threading.Thread(target=lab.stop)
                stopping.start()
                time.sleep(0.05)
                self.assertTrue(stopping.is_alive())
                release.set()
                stopping.join(5)
                self.assertFalse(stopping.is_alive())
            self.assertEqual(lab.get(active["id"])["status"], "cancelled")
            self.assertEqual(lab.get(pending["id"])["status"], "cancelled")
            self.assertIsNotNone(children[0].poll())
        finally:
            release.set()

    def test_version_mismatch_is_failed_without_claiming_old_model(self):
        lab = self.lab()
        # The immutable source observation version is the effective boundary.
        original = self.source.snapshot

        def mismatch(source_id):
            value = original(source_id)
            value["summary"]["version"] = "0.0.0"
            return value

        with patch.object(self.source, "snapshot", mismatch):
            done = wait_job(lab, self.submit(lab, "full")["id"])
        self.assertEqual(done["status"], "failed", done)
        self.assertIn("版本不一致", done["message"])
        self.assertFalse(any(p.get("status") == "completed" for p in done["phases"]))

    def test_capacity_refusal_keeps_existing_files_and_sources(self):
        lab = self.lab(limit_bytes=WORK_RESERVE_BYTES - 1)
        preserved = lab.root / "existing-evidence.bin"
        preserved.write_bytes(b"keep-me")
        with self.assertRaisesRegex(RebuildError, "容量预算不足"):
            self.submit(lab)
        self.assertEqual(preserved.read_bytes(), b"keep-me")
        self.assertEqual(lab.list_jobs(), [])
        self.assertEqual(self.archive.read_asset(hashlib.sha256(self.before_raw).hexdigest()), self.before_raw)

    def test_api_paths_and_unknown_commands_are_rejected(self):
        lab = self.lab()
        with self.assertRaises(RebuildError):
            self.submit(lab, "file.export")
        done = wait_job(lab, self.submit(lab, "full")["id"])
        for args in [("../bad", 0, "document.json"), (done["id"], True, "document.json"),
                     (done["id"], 0, "../../secret"), (done["id"], 10, "document.json")]:
            with self.subTest(args=args), self.assertRaises(RebuildError):
                lab.asset_path(*args)
        self.assertIsNone(lab.get("../bad"))
        detached = lab.get(done["id"])
        detached["phases"].clear()
        self.assertTrue(lab.get(done["id"])["phases"])
        lab.cancel(done["id"])
        self.assertEqual(lab.get(done["id"])["status"], "completed")

    def test_same_size_asset_corruption_is_not_served_as_native_evidence(self):
        lab = self.lab()
        done = wait_job(lab, self.submit(lab, "full")["id"])
        self.assertEqual(done["status"], "completed", done)
        path = lab.asset_path(done["id"], 0, "geometry.json")
        raw = path.read_bytes()
        path.write_bytes(b"!" + raw[1:])
        with self.assertRaisesRegex(RebuildError, "哈希校验失败"):
            lab.asset_path(done["id"], 0, "geometry.json")

    def test_asset_reads_do_not_require_python311_file_digest(self):
        lab = self.lab()
        done = wait_job(lab, self.submit(lab, mode="full")["id"])
        self.assertEqual(done["status"], "completed", done)
        # Python 3.10 is supported by the launchers but lacks file_digest.
        with patch("hashlib.file_digest", create=True,
                   side_effect=AssertionError("Python 3.11-only API used")):
            document = self.asset_json(lab, done, 0, "document.json")
        self.assertEqual(document["features"][1]["extent"]["distance"], "40 mm")

    def test_cancel_after_third_completed_phase_does_not_break_recovery(self):
        lab = self.lab()
        capture = lab._capture

        def cancel_at_commit(job_id, phase, *args):
            value = capture(job_id, phase, *args)
            if phase["index"] == 2:
                lab._cancel[job_id].set()
            return value

        with patch.object(lab, "_capture", cancel_at_commit):
            done = wait_job(lab, self.submit(lab)["id"])
        self.assertEqual(done["status"], "cancelled", done)
        self.assertEqual([p["status"] for p in done["phases"][:3]], ["completed"] * 3)
        self.assertEqual(len(done["phases"]), 4)
        lab.stop()
        restarted = self.lab()
        self.assertEqual(restarted.get(done["id"])["status"], "cancelled")
        self.assertEqual(len(restarted.get(done["id"])["phases"]), 4)

    def test_after_observation_version_is_checked_separately(self):
        lab = self.lab()
        original = self.source.snapshot

        def mismatch(source_id):
            value = original(source_id)
            if source_id == self.source_ids[1]:
                value["summary"]["version"] = "0.0.0"
            return value

        with patch.object(self.source, "snapshot", mismatch):
            done = wait_job(lab, self.submit(lab)["id"])
        self.assertEqual(done["status"], "failed", done)
        self.assertIn("修改后采样版本不一致", done["message"])

    def test_crash_work_cleanup_precedes_quota_limited_recovery(self):
        work = self.root / "lab" / ".work" / ("lab-" + "b" * 32)
        work.mkdir(parents=True)
        with (work / "unfinished-native-export").open("wb") as stream:
            stream.truncate(5 * 1024 * 1024)
        lab = self.lab(limit_bytes=4 * 1024 * 1024)
        self.assertEqual(lab.list_jobs(), [])
        self.assertFalse(work.exists())
        self.assertLess(lab._usage(), lab.limit_bytes)

    def test_interrupted_asset_publish_does_not_create_missing_manifest_references(self):
        lab = self.lab()
        atomic = lab._atomic
        injected = False

        def fail_once(path, raw):
            nonlocal injected
            if Path(path).name == "topology.json" and not injected:
                injected = True
                raise OSError("injected disk write failure")
            return atomic(path, raw)

        with patch.object(lab, "_atomic", fail_once):
            done = wait_job(lab, self.submit(lab, "full")["id"])
        self.assertEqual(done["status"], "failed", done)
        self.assertTrue(injected)
        for phase in done["phases"]:
            for name in phase["assets"]:
                self.assertTrue(lab.asset_path(done["id"], phase["index"], name).is_file())
        lab.stop()
        restarted = self.lab()
        self.assertEqual(restarted.get(done["id"])["status"], "failed")


if __name__ == "__main__":
    unittest.main()
