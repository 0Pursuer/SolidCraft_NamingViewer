import concurrent.futures
import errno
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

import archive
from archive import Archive, ArchiveError, QuotaExceeded, document_diff


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        # Archive canonicalizes its root; compare the same paths when Windows
        # supplies an 8.3 TEMP alias such as ADMINI~1.
        self.directory = Path(self.temporary.name).resolve()
        self.root = self.directory / "history"
        self.store = Archive(self.root)
        self.session = self.store.new_session({"pid": 123, "version": "0.30.1"})

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def capture(self, document, revision=1, undo=0, redo=0, document_id="design-a", assets=None):
        event = self.store.add_event(self.session, document_id, revision, "revision")
        content = {"document.json": encoded(document)}
        content.update(assets or {})
        result = self.store.commit_snapshot(event, content, {"name": "Part", "undo": undo, "redo": redo,
                                                           "source": "observed", "completeness": "complete"})
        return event, result

    def test_snapshot_bytes_are_immutable_deduplicated_and_reopenable(self):
        document = {"features": [{"id": 7, "name": "Base", "kind": {"type": "sketch"}}]}
        first, one = self.capture(document)
        second, two = self.capture(document, revision=2)
        self.assertEqual(one["assets"], two["assets"])
        self.assertEqual(self.store.status()["counts"]["assets"], 1)
        digest = hashlib.sha256(encoded(document)).hexdigest()
        self.assertEqual(one["summary"]["document_hash"], digest)
        self.assertEqual(self.store.read_asset(digest), encoded(document))
        self.assertEqual(one["label"], "O0001")
        self.assertEqual(self.store.latest_snapshot("design-a")["id"], second)
        # Repeating a commit cannot replace the already published observation.
        repeated = self.store.commit_snapshot(first, {"document.json": b"{}"}, {"name": "replacement"})
        self.assertEqual(repeated, one)
        self.store.close()
        self.store = Archive(self.root)
        self.assertEqual(self.store.snapshot(first), one)
        self.assertEqual(self.store.snapshot(second), two)
        self.assertEqual(self.store.read_asset(digest), encoded(document))

    def test_quota_failure_keeps_older_records_and_removes_partial_objects(self):
        first, old = self.capture({"features": []}, assets={"model.3mf": b"old-model"})
        event = self.store.add_event(self.session, "design-a", 2, "revision")
        before = self.store.status()["used_bytes"]
        self.store.limit_bytes = before + 64 * 1024 + 40 * 1024
        with self.assertRaises(QuotaExceeded):
            # A first object fits, then the second does not: neither can be published.
            self.store.commit_snapshot(event, {"a.json": b"a" * 1024, "z.bin": b"z" * 100_000}, {"name": "too large"})
        self.assertEqual(self.store.snapshot(first), old)
        self.assertIsNone(self.store.snapshot(event))
        self.assertEqual(self.store.read_asset(old["assets"]["model.3mf"]["sha256"]), b"old-model")
        self.assertEqual(self.store.status()["counts"]["snapshots"], 1)
        self.assertEqual(self.store.status()["counts"]["assets"], 2)
        self.assertFalse(self.store.status()["recording_allowed"])
        self.assertTrue(self.store.status()["quota_exceeded"])
        self.assertEqual(list((self.root / "tmp").iterdir()), [])
        objects = [p for p in (self.root / "objects").glob("*/*") if p.is_file()]
        self.assertEqual(len(objects), 2)
        self.assertLessEqual(self.store.status()["used_bytes"], self.store.limit_bytes)

    def test_metadata_quota_failure_does_not_consume_event_or_delete_old_data(self):
        event, original = self.capture({"features": []})
        count = len(self.store.list_events())
        self.store.limit_bytes = self.store.status()["used_bytes"] + 64 * 1024 + 1000
        with self.assertRaises(QuotaExceeded):
            self.store.add_event(self.session, "design-a", 2, "revision", raw={"payload": "a" * 5000})
        self.assertEqual(len(self.store.list_events()), count)
        self.assertEqual(self.store.snapshot(event), original)

    def test_enospc_during_object_staging_rolls_back_and_preserves_prior_snapshot(self):
        old_id, old = self.capture({'features': []})
        event = self.store.add_event(self.session, 'design-a', 2, 'revision')
        before = self.store.status()['counts'].copy()
        with mock.patch.object(archive.tempfile, 'mkstemp', side_effect=OSError(errno.ENOSPC, 'injected full disk')):
            with self.assertRaises(QuotaExceeded):
                self.store.commit_snapshot(event, {'document.json': b'{"features":[{"id":1}]}'}, {})
        self.assertIsNone(self.store.snapshot(event))
        self.assertEqual(self.store.snapshot(old_id), old)
        self.assertEqual(self.store.status()['counts'], before)
        self.assertEqual(list((self.root / 'tmp').iterdir()), [])

    def test_export_over_budget_does_not_replace_previous_download_or_history(self):
        ident, old = self.capture({'features': [{'id': 1}]})
        destination = self.directory / 'download.zip'
        destination.write_bytes(b'previous-download')
        self.store.limit_bytes = self.store.status()['used_bytes'] + 1024 * 1024
        with self.assertRaises(QuotaExceeded):
            self.store.export_zip(destination)
        self.assertEqual(destination.read_bytes(), b'previous-download')
        self.assertEqual(self.store.snapshot(ident), old)
        self.assertEqual(self.store.status()['reserved_bytes'], 0)

    def test_files_are_published_before_database_references(self):
        event = self.store.add_event(self.session, "design-a", 1, "revision")
        original_replace = archive.os.replace
        observed = []

        def replace(source, target):
            original_replace(source, target)
            if Path(target).parent.parent.name == "objects":
                self.assertTrue(Path(target).is_file())
                observer = sqlite3.connect(self.root / "archive.sqlite3")
                try:
                    observed.append(observer.execute("SELECT count(*) FROM snapshots").fetchone()[0])
                    self.assertEqual(observer.execute("SELECT status FROM events WHERE id=?", (event,)).fetchone()[0], "pending")
                finally:
                    observer.close()

        with mock.patch.object(archive.os, "replace", side_effect=replace):
            result = self.store.commit_snapshot(event, {"document.json": b"{}", "geometry.json": b"{}"}, {})
        self.assertEqual(observed, [0])  # The two identical objects are stored once.
        self.assertEqual(self.store.list_events()[0]["status"], "committed")
        for reference in result["assets"].values():
            self.assertTrue(self.store.asset_path(reference["sha256"]).is_file())

    def test_database_failure_rolls_back_every_reference_and_new_file(self):
        old_id, old = self.capture({"features": [{"id": 1}]})
        event = self.store.add_event(self.session, "design-a", 2, "revision")
        self.store._db.execute("CREATE TRIGGER reject_snapshot BEFORE INSERT ON snapshots BEGIN SELECT RAISE(ABORT,'injected storage failure'); END")
        with self.assertRaises(ArchiveError) as raised:
            self.store.commit_snapshot(event, {"document.json": b'{"features":[{"id":2}]}', "model.3mf": b"new mesh"}, {})
        self.assertIsInstance(raised.exception.__cause__, sqlite3.IntegrityError)
        self.assertIsNone(self.store.snapshot(event))
        self.assertEqual(self.store.snapshot(old_id), old)
        self.assertEqual(self.store.status()["counts"]["assets"], 1)
        self.assertEqual(len(list((self.root / "objects").glob("*/*"))), 1)
        self.assertEqual(self.store.list_events()[-1]["status"], "pending")

    def test_restart_marks_pending_capture_and_job_interrupted_preserving_diagnostics(self):
        _, complete = self.capture({"features": []})
        pending = self.store.add_event(self.session, "design-a", 2, "revision", raw={"revision": 2})
        self.store.set_event_status(pending, "capturing")
        job = self.store.add_job(complete["id"], 0, "0.30.1")
        self.store.update_job(job, "running", diagnostics={"stderr": "native diagnostic"}, progress=0.5)
        abandoned = self.root / "tmp" / ".pending-interrupted"
        abandoned.write_bytes(b"partial")
        unrelated = self.root / "tmp" / "leave-me.txt"
        unrelated.write_text("not owned", encoding="utf-8")
        orphan_sha = hashlib.sha256(b"published before crash").hexdigest()
        orphan = self.root / "objects" / orphan_sha[:2] / orphan_sha
        orphan.parent.mkdir(exist_ok=True)
        orphan.write_bytes(b"published before crash")
        self.store.close()
        self.store = Archive(self.root)
        event = self.store.list_events(after=complete["id"])[0]
        self.assertEqual(event["id"], pending)
        self.assertEqual(event["status"], "interrupted")
        self.assertEqual(event["raw"], {"revision": 2})
        self.assertIn("interrupted", event["message"].lower())
        recovered_job = self.store.get_job(job)
        self.assertEqual(recovered_job["status"], "interrupted")
        self.assertEqual(recovered_job["diagnostics"], {"stderr": "native diagnostic"})
        self.assertEqual(recovered_job["progress"], 0.5)
        self.assertFalse(abandoned.exists())
        self.assertFalse(orphan.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(self.store.status()["recovery"], {"events": 1, "jobs": 1})
        self.assertEqual(self.store.snapshot(complete["id"]), complete)

    def test_event_gaps_remain_visible_without_fabricated_snapshots(self):
        first, _ = self.capture({"features": []}, revision=1)
        gap = self.store.add_event(self.session, "design-a", 4, "revision_gap", raw={"last_revision": 1, "observed_revision": 4},
                                   status="gap", message="Revisions 2 and 3 were not observed.")
        last, _ = self.capture({"features": [{"id": 12}]}, revision=4)
        events = self.store.list_events(after=first)
        self.assertEqual([event["id"] for event in events], [gap, last])
        self.assertIsNone(events[0]["snapshot_id"])
        self.assertEqual(events[0]["status"], "gap")
        self.assertEqual(events[0]["raw"]["observed_revision"], 4)
        self.assertIsNone(self.store.snapshot(gap))
        self.assertEqual(events[1]["snapshot_id"], last)

    def test_diff_tracks_stable_feature_ids_across_reorder_insert_and_parameter_edit(self):
        before = {"features": [{"id": 10, "kind": {"type": "sketch"}}, {"id": 20, "kind": {"type": "extrude", "distance": "5"}}],
                  "params": [{"name": "width", "expression": "10 mm"}]}
        after = {"features": [{"id": 20, "kind": {"type": "extrude", "distance": "8"}}, {"id": 30, "kind": {"type": "fillet"}},
                              {"id": 10, "kind": {"type": "sketch"}}], "params": [{"name": "width", "expression": "12 mm"}]}
        self.capture(before)
        _, changed = self.capture(after, revision=2)
        paths = {item["path"]: item for item in changed["diff"]}
        self.assertIn("features[id=20].kind.distance", paths)
        self.assertEqual(paths["features[id=20].kind.distance"]["old"], "5")
        self.assertEqual(paths["features[id=20].kind.distance"]["new"], "8")
        self.assertEqual(paths["features[id=30]"]["kind"], "added")
        self.assertEqual(paths["features.order"]["kind"], "reordered")
        self.assertIn('params[name="width"].expression', paths)
        self.assertFalse(any("features[0]" in path for path in paths))
        removed = document_diff(after, {"features": [], "params": []})
        self.assertIn("features[id=20]", [item["path"] for item in removed])
        added = document_diff({"features": []}, {"features": [{"id": 99}]})
        self.assertEqual(added[0]["path"], "features[id=99]")

    def test_diff_bound_and_large_value_preview(self):
        changes = document_diff({"features": [{"id": 2, "payload": "a" * 5000}]}, {"features": [{"id": 2, "payload": "b" * 5000}]})
        self.assertTrue(changes[0]["old"]["truncated"])
        self.assertLess(len(changes[0]["old"]["preview"]), 1025)
        many = document_diff({str(i): 0 for i in range(1000)}, {str(i): 1 for i in range(1000)})
        self.assertEqual(len(many), 128)
        self.assertEqual(many[-1]["kind"], "truncated")

    def test_undo_redo_links_are_only_inferred_and_document_scoped(self):
        one, _ = self.capture({"features": []}, undo=0, redo=0)
        two, _ = self.capture({"features": [{"id": 4}]}, revision=2, undo=1, redo=0)
        _, undone = self.capture({"features": []}, revision=3, undo=0, redo=1)
        self.assertTrue(undone["inference"]["inferred"])
        self.assertEqual(undone["inference"]["kind"], "possible_undo")
        self.assertEqual(undone["inference"]["linked_snapshot_id"], one)
        _, redone = self.capture({"features": [{"id": 4}]}, revision=4, undo=1, redo=0)
        self.assertEqual(redone["inference"]["kind"], "possible_redo")
        self.assertEqual(redone["inference"]["linked_snapshot_id"], two)
        _, independent = self.capture({"features": []}, document_id="another-document", undo=0, redo=1)
        self.assertIsNone(independent["inference"]["linked_snapshot_id"])
        self.assertNotEqual(independent["inference"]["kind"], "possible_undo")

    def test_stage_records_source_hash_version_prefix_and_no_observed_stack_depths(self):
        source_id, source = self.capture({"features": [{"id": 7}, {"id": 200}]}, undo=9, redo=3)
        job = self.store.add_job(source_id, 1, "0.30.1")
        self.store.update_job(job, "running", diagnostics={"command": "isolated CLI"})
        stage = self.store.commit_stage(job, {"document.json": b'{"features":[{"id":7}],"marker":1}', "geometry.json": b'{"stages":[]}'},
                                        {"undo": 9, "redo": 3, "source": "observed", "warnings": ["External links not available."], "completeness": "partial"})
        self.assertEqual(stage["summary"]["source"], "rebuilt")
        self.assertEqual(stage["summary"]["input_document_hash"], source["summary"]["document_hash"])
        self.assertEqual(stage["summary"]["version"], "0.30.1")
        self.assertEqual(stage["summary"]["position"], 1)
        self.assertEqual(stage["label"], f"O{source_id:04d}/S1")
        self.assertNotIn("undo", stage["summary"])
        self.assertNotIn("redo", stage["summary"])
        self.assertTrue(stage["inference"]["inferred"])
        self.assertEqual(stage["inference"]["kind"], "reconstruction")
        self.assertEqual(self.store.get_job(job)["status"], "completed")
        self.assertEqual(self.store.get_job(job)["stage_id"], stage["id"])
        self.assertEqual(self.store.stage(stage["id"]), stage)
        self.assertEqual(self.store.commit_stage(job, {}, {}), stage)
        self.assertEqual(self.store.snapshot(source_id), source)

    def test_consistent_zip_contains_only_owned_records_and_referenced_assets(self):
        ident, original = self.capture({"features": [{"id": 2}]}, assets={"model.3mf": b"mesh"})
        (self.root / "not-archive.txt").write_text("unrelated-private-file", encoding="utf-8")
        orphan_data = b"orphan"
        orphan_sha = hashlib.sha256(orphan_data).hexdigest()
        orphan = self.root / "objects" / orphan_sha[:2] / orphan_sha
        orphan.parent.mkdir(exist_ok=True)
        orphan.write_bytes(orphan_data)
        destination = self.directory / "download.zip"
        self.assertEqual(self.store.export_zip(destination), destination)
        with zipfile.ZipFile(destination) as zipped:
            names = zipped.namelist()
            self.assertIn("archive.sqlite3", names)
            self.assertIn("manifest.json", names)
            self.assertNotIn("not-archive.txt", names)
            self.assertNotIn(f"objects/{orphan_sha[:2]}/{orphan_sha}", names)
            expected = {"archive.sqlite3", "manifest.json"}
            for ref in original["assets"].values():
                expected.add(f"objects/{ref['sha256'][:2]}/{ref['sha256']}")
            self.assertEqual(set(names), expected)
            reopened_root = self.directory / "unpacked"
            zipped.extractall(reopened_root)
        reader = Archive(reopened_root, read_only=True)
        try:
            self.assertEqual(reader.snapshot(ident), original)
            for name, ref in original["assets"].items():
                self.assertEqual(reader.read_asset(ref["sha256"]), self.store.read_asset(ref["sha256"]))
        finally:
            reader.close()

    def test_missing_or_invalid_assets_and_read_only_mutations_are_refused(self):
        self.assertIsNone(self.store.snapshot(999))
        self.assertIsNone(self.store.stage("stage_missing"))
        self.assertIsNone(self.store.get_job("job_missing"))
        with self.assertRaises(ArchiveError):
            self.store.asset_path("../archive.sqlite3")
        with self.assertRaises(ArchiveError):
            self.store.asset_path("0" * 64)
        ident, original = self.capture({"features": []})
        reader = Archive(self.root, read_only=True)
        try:
            self.assertEqual(reader.snapshot(ident), original)
            with self.assertRaises(ArchiveError):
                reader.new_session({})
            with self.assertRaises(ArchiveError):
                reader.commit_snapshot(ident, {}, {})
        finally:
            reader.close()
        with self.assertRaises(ArchiveError):
            self.store.add_job(ident, -1, "v")
        with self.assertRaises(ArchiveError):
            self.store.add_job(999, 1, "v")

    def test_metadata_credentials_are_redacted_without_changing_native_asset_bytes(self):
        session = self.store.new_session({"pid": 5, "access_token": "sensitive-one", "authorization": "Bearer sensitive-two", "port": 42})
        event = self.store.add_event(session, "design-a", 3, "revision", raw={"password": "sensitive-three"}, message="https://local/?token=sensitive-four")
        rows = encoded(self.store.list_sessions()) + encoded(self.store.list_events(session_id=session))
        for value in (b"sensitive-one", b"sensitive-two", b"sensitive-three", b"sensitive-four"):
            self.assertNotIn(value, rows)
        self.assertIn(b"[redacted]", rows)
        self.store.commit_snapshot(event, {"document.json": b'{"name":"native unchanged"}'}, {})

    def test_out_of_order_publication_never_links_to_a_future_event(self):
        first = self.store.add_event(self.session, "design-a", 1, "revision")
        second = self.store.add_event(self.session, "design-a", 2, "revision")
        self.store.commit_snapshot(second, {"document.json": b'{"features":[{"id":2}]}'}, {"undo": 1, "redo": 0})
        original = self.store.commit_snapshot(first, {"document.json": b'{"features":[]}'}, {"undo": 0, "redo": 1})
        self.assertIsNone(original["inference"]["linked_snapshot_id"])
        self.assertEqual(original["inference"]["kind"], "unavailable")
        self.assertEqual(original["diff"], [])

    def test_corrupt_asset_refuses_read_and_export_without_replacing_old_download(self):
        _, original = self.capture({"features": []})
        sha = original["assets"]["document.json"]["sha256"]
        self.store.asset_path(sha).write_bytes(b"corrupt")
        with self.assertRaises(ArchiveError):
            self.store.read_asset(sha)
        destination = self.directory / "previous-download.zip"
        destination.write_bytes(b"previous valid download")
        with self.assertRaises(ArchiveError):
            self.store.export_zip(destination)
        self.assertEqual(destination.read_bytes(), b"previous valid download")
        self.assertEqual(list(self.directory.glob(".archive-*")), [])

    def test_threaded_event_and_snapshot_writes_remain_complete(self):
        def capture(index):
            event = self.store.add_event(self.session, "design-a", index, "revision")
            self.store.commit_snapshot(event, {"document.json": encoded({"features": [{"id": index}]})}, {})
            return event
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(capture, range(1, 17)))
        self.assertEqual(len(set(ids)), 16)
        self.assertEqual(self.store.status()["counts"]["snapshots"], 16)
        for event in self.store.list_events():
            self.assertEqual(event["status"], "committed")
            record = self.store.snapshot(event["id"])
            self.assertEqual(json.loads(self.store.read_asset(record["assets"]["document.json"]["sha256"])), {"features": [{"id": event["revision"]}]})

    def test_exclusive_writer_lock_prevents_another_process_recovering_live_work(self):
        pending = self.store.add_event(self.session, "design-a", 1, "revision")
        code = """import sys
from pathlib import Path
from archive import Archive, ArchiveError
try:
    other = Archive(Path(sys.argv[1]))
except ArchiveError:
    sys.exit(0)
other.close()
sys.exit(1)
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.root)], cwd=Path(__file__).parent,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(self.store.list_events()[0]["status"], "pending")
        reader = Archive(self.root, read_only=True)
        try:
            self.assertEqual(reader.list_events()[0]["status"], "pending")
        finally:
            reader.close()
        self.store.close()
        self.store = Archive(self.root)
        self.assertEqual(self.store.list_events()[0]["id"], pending)
        self.assertEqual(self.store.list_events()[0]["status"], "interrupted")

    def test_shared_reservation_credits_actual_work_and_cas_without_double_counting(self):
        event = self.store.add_event(self.session, "design-a", 1, "revision")
        baseline = self.store.status()["used_bytes"]
        self.store.limit_bytes = baseline + 64 * 1024 + 1024 * 1024
        workdir = self.root / ".work" / "capture-budget-test"
        with self.store.reserve(800 * 1024, workdir=workdir):
            workdir.mkdir(parents=True)
            work = workdir / "native-export.bin"
            work.write_bytes(b"a" * (400 * 1024))
            status = self.store.status()
            self.assertEqual(status["used_bytes"], baseline + 400 * 1024)
            self.assertEqual(status["reserved_bytes"], 400 * 1024)
            self.assertEqual(status["budget_used_bytes"], baseline + 800 * 1024)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                def compete():
                    with self.store.reserve(400 * 1024, workdir=self.root / ".work" / "rebuild-competing"):
                        self.fail("two workers exceeded their shared byte budget")
                with self.assertRaises(QuotaExceeded):
                    pool.submit(compete).result(timeout=5)
            result = self.store.commit_snapshot(event, {"model.3mf": b"b" * (300 * 1024)}, {})
            self.assertEqual(result["assets"]["model.3mf"]["size"], 300 * 1024)
            self.assertLessEqual(self.store.status()["budget_used_bytes"], baseline + 800 * 1024 + 8192)
            work.unlink()
            workdir.rmdir()
        self.assertEqual(self.store.status()["reserved_bytes"], 0)

    def test_owned_temporary_files_and_journal_are_counted_but_unknown_layout_is_not_walked(self):
        before = self.store.status()["used_bytes"]
        pending = self.root / "tmp" / ".pending-count-test"
        pending.write_bytes(b"a" * 2048)
        workdir = self.root / ".work" / "rebuild-count-test"
        (workdir / "nested").mkdir(parents=True)
        (workdir / "nested" / "derived.json").write_bytes(b"b" * 4096)
        unknown = self.root / ".work" / "unrelated-directory"
        unknown.mkdir()
        (unknown / "private").write_bytes(b"c" * 50000)
        journal = self.root / "archive.sqlite3-journal"
        self.store._db.execute("BEGIN IMMEDIATE")
        try:
            self.store._db.execute("UPDATE sessions SET info=? WHERE id=?", ('{"test":true}', self.session))
            journal_bytes = journal.stat().st_size
            self.assertGreater(journal_bytes, 0)
            self.assertEqual(self.store.status()["used_bytes"], before + 2048 + 4096 + journal_bytes)
        finally:
            self.store._db.rollback()
        with self.assertRaises(ArchiveError):
            with self.store.reserve(100, workdir=self.directory / "outside"):
                pass
        with self.assertRaises(ArchiveError):
            with self.store.reserve(100, workdir=unknown):
                pass

    def test_export_in_owned_workdir_uses_its_own_reservation_and_releases_it(self):
        ident, _ = self.capture({"features": [{"id": 13}]})
        workdir = self.root / ".work" / "export-download-test"
        workdir.mkdir(parents=True)
        destination = workdir / "archive.zip"
        self.store.export_zip(destination)
        self.assertTrue(destination.is_file())
        self.assertEqual(self.store.status()["reserved_bytes"], 0)
        self.assertEqual(list(workdir.iterdir()), [destination])
        with zipfile.ZipFile(destination) as content:
            self.assertIn("archive.sqlite3", content.namelist())
        destination.unlink()
        self.assertEqual(self.store.snapshot(ident)["id"], ident)

    def test_asset_size_is_bounded_before_hashing_or_reading_a_large_input(self):
        event = self.store.add_event(self.session, "design-a", 1, "revision")
        source = self.directory / "large-native-output.bin"
        with source.open("wb") as handle:
            handle.truncate(64 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ArchiveError, "64 MiB"):
            self.store.commit_snapshot(event, {"model.3mf": source}, {})
        self.assertIsNone(self.store.snapshot(event))
        self.assertEqual(self.store.status()["counts"]["assets"], 0)

    def test_directory_disappearing_between_discovery_and_scandir_is_normal_cleanup(self):
        workdir = self.root / ".work" / "capture-disappearing"
        workdir.mkdir(parents=True)
        original = archive.os.scandir
        removed = False

        def scandir(path):
            nonlocal removed
            if Path(path) == workdir and not removed:
                removed = True
                workdir.rmdir()
                raise FileNotFoundError(3, "owned capture was cleaned up", str(workdir))
            return original(path)

        with mock.patch.object(archive.os, "scandir", side_effect=scandir):
            self.store.add_event(self.session, "design-a", 1, "revision")
        self.assertTrue(removed)
        self.assertEqual(len(self.store.list_events()), 1)

    def test_parallel_temporary_cleanup_does_not_drop_events_or_status_updates(self):
        workdir = self.root / ".work" / "capture-cleanup-race"
        ready = threading.Event()
        stop = threading.Event()

        def clean():
            ready.set()
            for _ in range(150):
                (workdir / "nested").mkdir(parents=True, exist_ok=True)
                (workdir / "nested" / "document.json").write_bytes(b"x" * 100)
                shutil.rmtree(workdir)
                if stop.is_set():
                    break

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            cleaner = pool.submit(clean)
            self.assertTrue(ready.wait(2))
            try:
                ids = []
                for i in range(60):
                    event = self.store.add_event(self.session, "design-a", i, "revision")
                    self.store.set_event_status(event, "superseded", "fixture")
                    self.store.status()
                    ids.append(event)
            finally:
                stop.set()
            cleaner.result(timeout=5)
        self.assertEqual(len(ids), 60)
        self.assertEqual([row["id"] for row in self.store.list_events()], ids)
        self.assertTrue(all(row["status"] == "superseded" for row in self.store.list_events()))

    def test_zip_compression_does_not_block_event_persistence_and_backup_stays_consistent(self):
        first, _ = self.capture({"features": [{"id": 10}]})
        entered, proceed = threading.Event(), threading.Event()
        original = zipfile.ZipFile.write

        def blocked(output, filename, arcname=None, *args, **kwargs):
            if arcname == "archive.sqlite3":
                entered.set()
                if not proceed.wait(5):
                    raise AssertionError("export fixture was never released")
            return original(output, filename, arcname, *args, **kwargs)

        destination = self.directory / "concurrent-export.zip"
        with mock.patch.object(zipfile.ZipFile, "write", new=blocked):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                export = pool.submit(self.store.export_zip, destination)
                self.assertTrue(entered.wait(3))
                try:
                    added = pool.submit(self.store.add_event, self.session, "design-a", 2, "revision")
                    event = added.result(timeout=2)
                    self.assertGreater(event, first)
                finally:
                    proceed.set()
                export.result(timeout=5)
        with zipfile.ZipFile(destination) as content:
            unpacked = self.directory / "concurrent-unpacked"
            content.extractall(unpacked)
        reader = Archive(unpacked, read_only=True)
        try:
            self.assertEqual([row["id"] for row in reader.list_events()], [first])
            self.assertEqual([row["id"] for row in self.store.list_events()], [first, event])
        finally:
            reader.close()

    def test_failed_job_evidence_is_atomic_immutable_and_never_a_stage(self):
        source_id, source = self.capture({"features": [{"id": 12}]})
        job = self.store.add_job(source_id, 1, "0.30.1")
        self.store.update_job(job, "running", diagnostics={"stderr": "useful native error"})
        result = self.store.commit_job_partial(job, {"derived.json": b'{"features":[{"id":12}]}', "inspect.json": b'{"partial":true}'},
                                               {"error": "CLI failed after inspection", "native": True}, "failed")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["diagnostic"]["error"], "CLI failed after inspection")
        self.assertIn("no reconstructed Stage", result["message"])
        self.assertEqual(self.store.status()["counts"]["stages"], 0)
        self.assertEqual(self.store.snapshot(source_id), source)
        self.assertEqual(self.store.read_asset(result["partial_assets"]["inspect.json"]["sha256"]), b'{"partial":true}')
        self.assertEqual(self.store.commit_job_partial(job, {"derived.json": b"replacement"}, "changed", "timed_out"), result)
        with self.assertRaises(ArchiveError):
            self.store.commit_job_partial(job, {}, {}, "completed")

    def test_failed_job_partial_database_failure_keeps_job_and_old_assets(self):
        source_id, source = self.capture({"features": []})
        job = self.store.add_job(source_id, 0, "0.30.1")
        self.store.update_job(job, "running")
        self.store._db.execute("CREATE TRIGGER reject_partial BEFORE UPDATE ON jobs BEGIN SELECT RAISE(ABORT,'injected diagnostic failure'); END")
        with self.assertRaises(ArchiveError):
            self.store.commit_job_partial(job, {"inspect.json": b'{"diagnostic":true}'}, "partial", "failed")
        self.assertEqual(self.store.get_job(job)["status"], "running")
        self.assertNotIn("partial_assets", self.store.get_job(job))
        self.assertEqual(self.store.status()["counts"]["assets"], 1)
        self.assertEqual(self.store.status()["counts"]["stages"], 0)
        self.assertEqual(self.store.snapshot(source_id), source)

    def test_snapshot_rejects_a_later_revision_or_another_document_for_an_old_event(self):
        event = self.store.add_event(self.session, "design-a", 4, "revision")
        for summary in ({"document_id": "design-a", "revision": 5}, {"document_id": "design-b", "revision": 4}, {"revision": True}):
            with self.assertRaisesRegex(ArchiveError, "does not match"):
                self.store.commit_snapshot(event, {"document.json": b'{"features":[{"id":20}]}'}, summary)
            self.assertIsNone(self.store.snapshot(event))
            self.assertEqual(self.store.status()["counts"]["assets"], 0)
            self.assertEqual(self.store.list_events()[0]["status"], "pending")
        valid = self.store.commit_snapshot(event, {"document.json": b'{"features":[]}'}, {"document_id": "design-a", "revision": 4})
        self.assertEqual(valid["summary"]["revision"], 4)

    def test_stage_preserves_actual_cli_version_and_records_the_requested_version_separately(self):
        source_id, _ = self.capture({"features": []})
        job = self.store.add_job(source_id, 0, "requested-0.30.1")
        stage = self.store.commit_stage(job, {"document.json": b'{"features":[]}'},
                                       {"version": "reported-fallback", "cli_version": "actual-0.30.2"})
        self.assertEqual(stage["summary"]["version"], "actual-0.30.2")
        self.assertEqual(stage["summary"]["cli_version"], "actual-0.30.2")
        self.assertEqual(stage["summary"]["requested_version"], "requested-0.30.1")

    def test_session_and_job_lists_are_bounded_without_losing_id_lookup(self):
        ident, _ = self.capture({"features": []})
        with self.store._transaction():
            self.store._db.executemany("INSERT INTO sessions VALUES(?,?,?)", [(f"test-session-{i}", f"{i:06d}", "{}") for i in range(1010)])
            self.store._db.executemany("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                                      [(f"test-job-{i}", ident, i, "test", "completed", f"{i:06d}", f"{i:06d}", "{}") for i in range(1010)])
        self.assertEqual(len(self.store.list_sessions()), 1000)
        self.assertEqual(len(self.store.list_jobs()), 1000)
        self.assertEqual(self.store.status()["counts"]["jobs"], 1010)
        self.assertEqual(self.store.get_job("test-job-0")["position"], 0)


if __name__ == "__main__":
    unittest.main()
