"""Instance-discovery and launch-identity contracts; no real CAD connections."""
import os
from pathlib import Path
import secrets
import shutil
import unittest
from unittest.mock import patch

import solidcraft_instances as instances


ROOT = Path(__file__).resolve().parent


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = (ROOT / ('test-instances-' + secrets.token_hex(8))).resolve()
        self.assertTrue(self.directory.is_relative_to(ROOT))
        self.directory.mkdir()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.assertTrue(self.directory.is_relative_to(ROOT))
        shutil.rmtree(self.directory)

    def token(self, port, value='a' * 64, pid='101\n'):
        (self.directory / f'{port}.token').write_text(value + '\n', encoding='ascii')
        if pid is not None:
            (self.directory / f'{port}.pid').write_text(pid, encoding='ascii')

    def test_discovers_all_user_instances_not_only_current(self):
        self.token(27879)
        self.token(27878, 'b' * 64)
        (self.directory / 'current.port').write_text('27879', encoding='ascii')
        found = instances.discover_bindings(self.directory)
        self.assertEqual([x.port for x in found], [27878, 27879])
        self.assertEqual([x.pid for x in found], [101, 101])

    def test_current_port_change_does_not_change_selected_binding(self):
        self.token(27878)
        selected = instances.read_binding(self.directory, 27878)
        self.token(27879, 'b' * 64)
        (self.directory / 'current.port').write_text('27879', encoding='ascii')
        instances.verify_binding(selected)
        self.assertEqual(selected.port, 27878)
        self.assertEqual(selected.token, 'a' * 64)

    def test_replaced_token_rejects_original_identity(self):
        self.token(27878)
        selected = instances.read_binding(self.directory, 27878)
        self.token(27878, 'b' * 64)
        with self.assertRaises(instances.SelectedInstanceChanged) as error:
            instances.verify_binding(selected)
        self.assertNotIn(selected.token, str(error.exception))
        self.assertEqual(selected.token, 'a' * 64)

    def test_replaced_pid_with_reused_token_rejects_original_identity(self):
        self.token(27878)
        selected = instances.read_binding(self.directory, 27878)
        self.token(27878, pid='202\n')
        with self.assertRaises(instances.SelectedInstanceChanged):
            instances.verify_binding(selected)

    def test_rewritten_identity_with_reused_token_and_pid_is_rejected(self):
        self.token(27878)
        selected = instances.read_binding(self.directory, 27878)
        path = self.directory / '27878.token'
        path.unlink()
        path.write_text('a' * 64 + '\n', encoding='ascii')
        # Explicitly advance metadata so coarse filesystems also exercise the
        # launch record change even when the numeric PID/token were reused.
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
        with self.assertRaises(instances.SelectedInstanceChanged):
            instances.verify_binding(selected)

    def test_disappeared_pid_is_rejected_if_originally_known(self):
        self.token(27878)
        selected = instances.read_binding(self.directory, 27878)
        (self.directory / '27878.pid').unlink()
        with self.assertRaises(instances.SelectedInstanceChanged):
            instances.verify_binding(selected)

    def test_deleted_token_does_not_fall_back_to_current(self):
        self.token(27878)
        selected = instances.read_binding(self.directory, 27878)
        (self.directory / '27878.token').unlink()
        self.token(27879, 'b' * 64)
        (self.directory / 'current.port').write_text('27879', encoding='ascii')
        with self.assertRaises(instances.SelectedInstanceChanged):
            instances.verify_binding(selected)

    def test_only_the_selected_profile_directory_is_scanned(self):
        first, second = self.directory / 'alice', self.directory / 'bob'
        first.mkdir()
        second.mkdir()
        (first / '27878.token').write_text('a' * 64, encoding='ascii')
        (second / '27879.token').write_text('b' * 64, encoding='ascii')
        self.assertEqual([x.port for x in instances.discover_bindings(first)], [27878])

    def test_default_directory_uses_current_process_profile_or_explicit_override(self):
        with patch.dict(os.environ, {'LOCALAPPDATA': str(self.directory)}, clear=True):
            self.assertEqual(instances.default_control_dir(), self.directory / 'SolidCraft' / 'control')
        with patch.dict(os.environ, {'LOCALAPPDATA': str(self.directory),
                                    'SOLIDCRAFT_CONTROL_DIR': str(self.directory / 'explicit')}, clear=True):
            self.assertEqual(instances.default_control_dir(), self.directory / 'explicit')

    def test_invalid_tokens_ports_and_filenames_are_not_discovered(self):
        self.token(27878)
        for name, data in [('0.token', 'a' * 64), ('65536.token', 'a' * 64),
                           ('027879.token', 'a' * 64), ('current.token', 'a' * 64),
                           ('27879.token', 'not-a-token'), ('27880.token', 'a' * 257)]:
            (self.directory / name).write_text(data, encoding='ascii')
        self.assertEqual([x.port for x in instances.discover_bindings(self.directory)], [27878])

    def test_invalid_port_arguments_cannot_be_paths_or_aliases(self):
        for port in (0, 65536, -1, True, '27878', '../27878'):
            with self.subTest(port=port), self.assertRaises(ValueError):
                instances.read_binding(self.directory, port)

    def test_repr_and_errors_do_not_expose_credentials(self):
        self.token(27878)
        selected = instances.read_binding(self.directory, 27878)
        self.assertNotIn('a' * 64, repr(selected))
        self.assertIn('27878', repr(selected))

    def test_pid_metadata_is_bounded_and_optional(self):
        self.token(27878, pid='not a PID')
        self.assertIsNone(instances.read_binding(self.directory, 27878).pid)
        self.token(27878, pid='2' * 257)
        self.assertIsNone(instances.read_binding(self.directory, 27878).pid)

    def test_directory_enumeration_has_a_limit_and_missing_directory_is_empty(self):
        self.assertEqual(instances.discover_bindings(self.directory / 'absent'), [])
        for i in range(5):
            (self.directory / f'other-{i}').write_text('', encoding='ascii')
        with patch.object(instances, 'MAX_DIRECTORY_ENTRIES', 4):
            with self.assertRaises(instances.DiscoveryLimitError):
                instances.discover_bindings(self.directory)


if __name__ == '__main__':
    unittest.main()
