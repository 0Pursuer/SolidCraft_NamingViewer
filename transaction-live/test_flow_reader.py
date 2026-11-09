"""Archive discovery uses SQLite write activity, never a live CAD connection."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flow_reader import latest_archive


class ArchiveSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='test-flow-reader-', dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.records = Path(self.temp.name).resolve()
        self.active = self.records / 'active'
        self.closed = self.records / 'closed'
        for directory, seconds in ((self.active, 1000), (self.closed, 2000)):
            directory.mkdir()
            database = directory / 'archive.sqlite3'
            database.write_bytes(b'fixture')
            os.utime(database, (seconds, seconds))

    def test_live_wal_updates_are_newer_than_a_closed_database(self):
        wal = self.active / 'archive.sqlite3-wal'
        wal.write_bytes(b'new commit')
        os.utime(wal, (3000, 3000))
        self.assertEqual(latest_archive(self.records), self.active)

    def test_shared_memory_activity_is_not_a_document_update(self):
        shared = self.active / 'archive.sqlite3-shm'
        shared.write_bytes(b'new reader')
        os.utime(shared, (4000, 4000))
        self.assertEqual(latest_archive(self.records), self.closed)

    def test_checkpoint_removing_wal_during_scan_is_normal(self):
        wal = self.active / 'archive.sqlite3-wal'
        wal.write_bytes(b'checkpointed')
        original = Path.stat
        def stat(path, *args, **kwargs):
            if path == wal:
                raise FileNotFoundError(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'stat', stat):
            self.assertEqual(latest_archive(self.records), self.closed)


if __name__ == '__main__':
    unittest.main()
