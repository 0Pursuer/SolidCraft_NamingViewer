"""Reject altered native naming records independently of geometry adaptation."""
import copy
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import server


class NameIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.faces = [
            {'index': 4, 'name': 'F2:side:c1#1', 'exact': False, 'area': 1.25},
            {'index': 9, 'name': '面名@F8.2#[边界]', 'exact': True, 'point': [1.0, 2.0, 3.0]},
        ]
        self.edges = [{'index': 8, 'name': 'F2:side:c1#1|面名@F8.2#[边界]~2',
                       'mid': [1.0, 0.0, 0.0], 'length': 2.5}]
        self.stage = {'bodyName': 'Body1',
            'faces': [{**copy.deepcopy(x), 'nativeRecord': copy.deepcopy(x), 'nameSource': 'model.faces'} for x in self.faces],
            'edges': [{**copy.deepcopy(x), 'nativeRecord': copy.deepcopy(x), 'nameSource': 'model.edges'} for x in self.edges]}

    def verify(self):
        server.verify_stage_names(self.stage, self.faces, self.edges)

    def test_original_names_and_raw_records_are_accepted_without_promoting_exact(self):
        self.verify()
        self.assertIs(self.stage['faces'][0]['exact'], False)
        self.assertIs(self.stage['faces'][1]['exact'], True)
        self.assertNotIn('exact', self.stage['edges'][0])
        for entity in [*self.stage['faces'], *self.stage['edges']]:
            self.assertEqual(entity['nameAudit'], 'native-records-match')

    def test_name_edits_on_faces_or_edges_are_rejected_even_after_prior_audit(self):
        self.verify()
        for kind in ('faces', 'edges'):
            with self.subTest(kind=kind):
                original = self.stage[kind][0]['name']
                self.stage[kind][0]['name'] = original + ' '
                with self.assertRaises(server.NameIntegrityError):
                    self.verify()
                self.stage[kind][0]['name'] = original

    def test_missing_native_exact_must_stay_missing(self):
        del self.faces[0]['exact']
        del self.stage['faces'][0]['exact']
        del self.stage['faces'][0]['nativeRecord']['exact']
        self.verify()
        self.stage['faces'][0]['exact'] = True
        with self.assertRaises(server.NameIntegrityError):
            self.verify()

    def test_exact_presence_type_and_value_are_preserved(self):
        for replacement in (True, 0, None, 'false'):
            with self.subTest(replacement=replacement):
                self.stage['faces'][0]['exact'] = replacement
                with self.assertRaises(server.NameIntegrityError):
                    self.verify()
        del self.stage['faces'][0]['exact']
        with self.assertRaises(server.NameIntegrityError):
            self.verify()

    def test_native_raw_record_cannot_be_modified_or_omitted(self):
        for kind in ('faces', 'edges'):
            with self.subTest(kind=kind):
                record = self.stage[kind][0].pop('nativeRecord')
                with self.assertRaises(server.NameIntegrityError):
                    self.verify()
                self.stage[kind][0]['nativeRecord'] = copy.deepcopy(record)
                self.stage[kind][0]['nativeRecord']['name'] = 'forged'
                with self.assertRaises(server.NameIntegrityError):
                    self.verify()
                self.stage[kind][0]['nativeRecord'] = record
        self.stage['faces'][0]['nativeRecord']['exact'] = 0
        with self.assertRaises(server.NameIntegrityError):
            self.verify()

    def test_raw_record_nested_properties_cannot_be_changed(self):
        self.stage['faces'][1]['nativeRecord']['point'][0] = 99.0
        with self.assertRaises(server.NameIntegrityError):
            self.verify()

    def test_name_source_must_be_the_matching_native_command(self):
        for kind in ('faces', 'edges'):
            source = self.stage[kind][0].pop('nameSource')
            with self.assertRaises(server.NameIntegrityError):
                self.verify()
            self.stage[kind][0]['nameSource'] = 'viewer-generated'
            with self.assertRaises(server.NameIntegrityError):
                self.verify()
            self.stage[kind][0]['nameSource'] = source

    def test_deleted_added_duplicate_or_changed_native_indices_are_rejected(self):
        for kind in ('faces', 'edges'):
            original = copy.deepcopy(self.stage[kind])
            for mutation in ('delete', 'add', 'duplicate', 'change', 'boolean'):
                with self.subTest(kind=kind, mutation=mutation):
                    rows = copy.deepcopy(original)
                    if mutation == 'delete':
                        rows.pop()
                    elif mutation in ('add', 'duplicate'):
                        rows.append(copy.deepcopy(rows[0]))
                        if mutation == 'add':
                            rows[-1]['index'] = 100
                    else:
                        rows[0]['index'] = 100 if mutation == 'change' else True
                    self.stage[kind] = rows
                    with self.assertRaises(server.NameIntegrityError):
                        self.verify()
            self.stage[kind] = original

    def test_native_record_order_does_not_change_index_identity(self):
        self.stage['faces'].reverse()
        self.verify()

    def test_capture_defends_original_query_records_from_adapter_input_mutation(self):
        from test_live import CaptureTests
        fixture = CaptureTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.collector.capture(('fixture-document', 1))
        original_builder = server.build_body_stage
        def bad_adapter(body, faces, edges, path, stage_id):
            faces[0]['name'] = 'changed-along-with-the-input'
            return original_builder(body, faces, edges, path, stage_id)
        with patch('server.build_body_stage', side_effect=bad_adapter):
            with self.assertRaises(server.NameIntegrityError):
                fixture.collector.capture(('fixture-document', 1))
        self.assertEqual(fixture.state.generation, 1)


class InvalidGeometryWorkerTests(unittest.TestCase):
    def test_bad_geometry_version_is_not_exported_repeatedly(self):
        class Control:
            revision = 1
            exports = 0
            def request(self, method, params=None, **kwargs):
                if method == 'document.revision':
                    return {'document_id': 'test', 'revision': self.revision}
                if method == 'document.inspect':
                    return {'name': 'test', 'bodies': [{'name': 'Body1'}]}
                if method == 'control.running':
                    return {'running': []}
                raise AssertionError(method)
            def query(self, command, params):
                return {'faces' if command == 'model.faces' else 'edges': []}
            def export_mesh(self, destination):
                self.exports += 1
            def close(self):
                pass
        control = Control()
        state = server.ViewerState()
        state.publish({'meta': {}, 'stages': []}, {'document_id': 'previous', 'revision': 7})
        collector = server.Collector(state, Path(__file__).parent, client=control, interval=.4)
        self.addCleanup(collector.stop)
        with patch('server.build_body_stage', side_effect=server.GeometryError('invalid face tag')):
            collector.thread.start()
            deadline = time.monotonic() + 3
            while control.exports == 0 and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(control.exports, 1)
            time.sleep(1.8)
            self.assertEqual(control.exports, 1, 'the same invalid snapshot must not be recaptured automatically')
            self.assertEqual(state.generation, 1)
            self.assertEqual(state.read()['source']['document_id'], 'previous')
            control.revision = 2
            deadline = time.monotonic() + 3
            while control.exports < 2 and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(control.exports, 2, 'a new native revision may be tried')
            collector.stop()


if __name__ == '__main__':
    unittest.main(verbosity=2)
