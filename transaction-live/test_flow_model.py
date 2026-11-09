"""Evidence tests for the read-only operation flow, not a mirrored UI snapshot."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from archive import Archive
import flow_model
from flow_model import build_flow


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def documents():
    empty = {'format': 'solidcraft/51', 'name': 'Experiment', 'features': [], 'marker': None, 'next_id': 11}
    sketch = copy.deepcopy(empty)
    sketch['features'] = [{'id': 10, 'name': 'Sketch1', 'type': 'sketch', 'suppressed': False,
        'plane': {'type': 'origin', 'name': 'XY'}, 'sketch': {
            'points': [{'id': 'origin', 'pos': [0, 0], 'fixed': True}], 'curves': [], 'constraints': []}}]
    rectangle = copy.deepcopy(sketch)
    sk = rectangle['features'][0]['sketch']
    sk['points'] += [{'id': f'p{i + 1}', 'pos': point, 'fixed': False}
                     for i, point in enumerate([[0, 0], [40, 0], [40, 25], [0, 25]])]
    sk['curves'] = [{'id': f'l{i + 1}', 'type': 'line', 'a': i + 1, 'b': (i + 1) % 4 + 1, 'construction': False} for i in range(4)]
    sk['constraints'] = [{'id': 'k1', 'type': 'horizontal', 'l': 0}]
    twenty = copy.deepcopy(rectangle)
    twenty['features'].append({'id': 20, 'name': 'Extrude1', 'type': 'extrude', 'sketch': 10,
        'profiles': {'type': 'curves', 'loops': [['l1', 'l2', 'l3', 'l4']]},
        'extent': {'distance': '20 mm', 'direction': 'positive', 'convention': 'total_distance'},
        'operation': 'new_body', 'suppressed': False})
    marker = copy.deepcopy(twenty)
    marker['marker'] = 1
    forty = copy.deepcopy(twenty)
    forty['features'][1]['extent']['distance'] = '40 mm'
    return empty, sketch, rectangle, twenty, marker, forty


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = Archive(Path(self.temp.name) / 'archive')
        self.session = self.archive.new_session({'version': '0.30.1'})
        self.document_id = 'doc-A'
        self.revision = 0

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def capture(self, document, undo, redo=0, document_id=None, inspected=None):
        self.revision += 1
        identity = self.document_id if document_id is None else document_id
        event = self.archive.add_event(self.session, identity, self.revision, 'design.changed')
        if inspected is None:
            body = None
            if len(document['features']) > 1 and document.get('marker') != 1:
                height = float(document['features'][1]['extent']['distance'].split()[0])
                body = {'name': 'Body1', 'bbox': {'min': [0, 0, 0], 'max': [40, 25, height]},
                        'volume_mm3': 1000 * height, 'faces': 6, 'edges': 12}
            inspected = {'bodies': [body] if body else []}
        return self.archive.commit_snapshot(event, {'document.json': encoded(document), 'inspect.json': encoded(inspected)},
            {'name': document.get('name', 'Experiment'), 'source': 'observed', 'undo': undo, 'redo': redo,
             'document_id': identity, 'revision': self.revision, 'marker': document.get('marker')})

    def experiment(self, undo=True):
        empty, sketch, rectangle, twenty, marker, forty = documents()
        records = [self.capture(empty, 0), self.capture(sketch, 1), self.capture(rectangle, 2),
                   self.capture(twenty, 3), self.capture(marker, 3), self.capture(forty, 4)]
        if undo:
            records.append(self.capture(twenty, 3, 1))
        return records

    def test_empty_archive_has_no_invented_current_or_stack(self):
        result = build_flow(self.archive)
        self.assertIsNone(result['current'])
        self.assertEqual(result['operations'], [])
        self.assertEqual(result['undo_observed'], [])

    def test_realistic_edit_marker_is_folded_and_undo_restores_a_known_prefix(self):
        records = self.experiment()
        result = build_flow(self.archive)
        self.assertEqual([x['kind'] for x in result['operations']],
            ['create_sketch', 'draw_rectangle', 'create_extrude', 'edit_extrude_height'])
        self.assertEqual(result['undo_observed'], ['op-2', 'op-3', 'op-4'])
        self.assertEqual(result['redo_observed'], ['op-6'])
        changed = result['operations'][-1]
        self.assertEqual(changed['before_snapshot_id'], records[3]['id'])
        self.assertEqual(changed['observation_ids'], [records[4]['id'], records[5]['id']])
        self.assertEqual(changed['status'], 'undone')
        self.assertEqual(changed['tree'][0]['feature_id'], 20)  # Position 2 is not Feature ID 20.
        field = changed['tree'][0]['children'][0]['children'][0]
        self.assertEqual((field['path'], field['old'], field['new']), ('features[id=20].extent.distance', '20 mm', '40 mm'))
        self.assertFalse(changed['native_transaction_available'])
        current = result['current']
        self.assertEqual((current['undo_depth'], current['redo_depth']), (3, 1))
        self.assertEqual(current['geometry']['bodies'][0]['bbox']['max'], [40, 25, 20])
        self.assertEqual(current['geometry']['total_volume_mm3'], 20000)
        self.assertEqual(result['boundaries'], [])

    def test_read_only_builder_does_not_change_archive_bytes_or_rows(self):
        self.experiment()
        before = (self.archive.root / 'archive.sqlite3').read_bytes()
        counts = self.archive.status()['counts'].copy()
        first, second = build_flow(self.archive), build_flow(self.archive)
        self.assertEqual(first, second)
        self.assertEqual((self.archive.root / 'archive.sqlite3').read_bytes(), before)
        self.assertEqual(self.archive.status()['counts'], counts)

    def test_redo_moves_the_same_operation_back_to_observed_undo(self):
        self.experiment()
        self.capture(documents()[5], 4, 0)
        result = build_flow(self.archive)
        self.assertEqual(result['redo_observed'], [])
        self.assertEqual(result['undo_observed'], ['op-2', 'op-3', 'op-4', 'op-6'])
        self.assertEqual(len(result['operations']), 4)
        self.assertEqual(result['transitions'][-1]['kind'], 'redo')

    def test_multiple_undo_and_partial_redo_use_the_known_chain_in_lifo_order(self):
        self.experiment(undo=False)
        sixty = copy.deepcopy(documents()[5])
        sixty['features'][1]['extent']['distance'] = '60 mm'
        sixty_record = self.capture(sixty, 5)
        self.capture(documents()[3], 3, 2)
        result = build_flow(self.archive)
        self.assertEqual(result['redo_observed'], [f'op-{sixty_record["id"]}', 'op-6'])
        self.assertEqual(result['transitions'][-1]['operation_ids'], [f'op-{sixty_record["id"]}', 'op-6'])
        self.capture(documents()[5], 4, 1)
        result = build_flow(self.archive)
        self.assertEqual(result['redo_observed'], [f'op-{sixty_record["id"]}'])
        self.assertEqual(result['operations'][3]['status'], 'applied')

    def test_edit_after_undo_retains_but_marks_the_old_redo_branch_discarded(self):
        self.experiment()
        thirty = copy.deepcopy(documents()[3])
        thirty['features'][1]['extent']['distance'] = '30 mm'
        branch = self.capture(thirty, 4, 0)
        result = build_flow(self.archive)
        self.assertEqual(result['operations'][3]['status'], 'discarded')
        self.assertEqual(result['redo_observed'], [])
        self.assertEqual(result['undo_observed'][-1], f'op-{branch["id"]}')
        self.assertEqual(result['operations'][-1]['discarded_redo_operation_ids'], ['op-6'])

    def test_gap_breaks_the_confirmable_chain_instead_of_fabricating_stack_entries(self):
        self.experiment(undo=False)
        self.archive.add_event(self.session, None, None, 'gap', status='gap')
        self.capture(documents()[3], 3, 1)
        result = build_flow(self.archive)
        self.assertEqual(result['undo_observed'], [])
        self.assertEqual(result['redo_observed'], [])
        self.assertEqual(result['current']['unobserved_undo_depth'], 3)
        self.assertEqual(result['current']['unobserved_redo_depth'], 1)
        self.assertTrue(any(x['kind'] == 'observation_gap' for x in result['boundaries']))
        self.assertTrue(all(x['status'] == 'unknown' for x in result['operations']))

    def test_superseded_notification_remains_a_visible_unknown_boundary(self):
        self.experiment(undo=False)
        self.archive.add_event(self.session, self.document_id, 99, 'design.changed', status='superseded')
        self.capture(documents()[3], 3, 1)
        result = build_flow(self.archive)
        self.assertTrue(any(x['kind'] == 'observation_gap' for x in result['boundaries']))
        self.assertEqual(result['redo_observed'], [])

    def test_pending_latest_event_keeps_last_complete_model_and_marks_it_not_latest(self):
        self.experiment()
        event = self.archive.add_event(self.session, self.document_id, 100, 'design.changed')
        result = build_flow(self.archive)
        self.assertFalse(result['current']['is_latest_observation'])
        self.assertEqual(result['current']['latest_event_id'], event)
        self.assertEqual(result['current']['snapshot_id'], 7)
        self.assertTrue(any(x['kind'] == 'snapshot_pending' for x in result['boundaries']))

    def test_native_depths_before_recording_are_counts_only_not_twenty_fake_nodes(self):
        self.capture(documents()[3], 20, 2)
        result = build_flow(self.archive)
        self.assertEqual(result['operations'], [])
        self.assertEqual(result['current']['unobserved_undo_depth'], 20)
        self.assertEqual(result['current']['unobserved_redo_depth'], 2)
        self.assertEqual(result['boundaries'][0]['kind'], 'unobserved_history')

    def test_editor_cancel_returns_without_adding_an_operation(self):
        empty, sketch, rectangle, twenty, marker, _ = documents()
        for document, undo in [(empty, 0), (sketch, 1), (rectangle, 2), (twenty, 3), (marker, 3), (twenty, 3)]:
            self.capture(document, undo)
        result = build_flow(self.archive)
        self.assertEqual(len(result['operations']), 3)
        self.assertEqual(result['transitions'][-1]['kind'], 'editor_restore')
        self.assertFalse(result['current']['temporary'])
        self.assertNotIn('pending_edit', result)

    def test_open_editor_is_not_published_as_a_completed_edit_transaction(self):
        self.experiment(undo=False)
        # Return to the before-edit state through a known Undo, then enter edit.
        self.capture(documents()[3], 3, 1)
        opening = self.capture(documents()[4], 3, 1)
        result = build_flow(self.archive)
        self.assertTrue(result['current']['temporary'])
        self.assertEqual(result['pending_edit']['observation_ids'], [opening['id']])
        self.assertEqual(result['current']['geometry']['body_count'], 0)
        self.assertEqual(len(result['operations']), 4)

    def test_unresolved_editor_position_is_an_unknown_boundary(self):
        empty, sketch, rectangle, twenty, marker, _ = documents()
        for document, undo in [(empty, 0), (sketch, 1), (rectangle, 2), (twenty, 3), (marker, 3)]:
            self.capture(document, undo)
        # A commit whose marker is not restored cannot be folded as the
        # normal editor transaction, despite the native depth increasing.
        current = copy.deepcopy(documents()[5])
        current['marker'] = 1
        self.capture(current, 4, 0)
        result = build_flow(self.archive)
        self.assertTrue(any(x['kind'] == 'editor_unresolved' for x in result['boundaries']))
        self.assertEqual(result['undo_observed'], [])

    def test_depth_boolean_and_no_depth_increment_do_not_create_confirmed_undo_steps(self):
        self.capture(documents()[0], 0)
        self.capture(documents()[1], True)
        result = build_flow(self.archive)
        self.assertEqual(result['undo_observed'], [])
        self.assertEqual(result['operations'][0]['status'], 'unknown')
        self.assertIsNone(result['current']['undo_depth'])

    def test_geometry_failure_does_not_destroy_a_definition_relationship(self):
        self.experiment()
        last = self.archive.latest_snapshot()
        self.archive.asset_path(last['assets']['inspect.json']['sha256']).write_bytes(b'corrupt')
        result = build_flow(self.archive)
        self.assertEqual(result['redo_observed'], ['op-6'])
        self.assertFalse(result['current']['geometry']['available'])
        self.assertTrue(any(x['kind'] == 'geometry_unavailable' for x in result['boundaries']))

    def test_documents_are_separate_and_default_is_most_recent_document(self):
        self.experiment()
        self.capture(documents()[0], 0, document_id='doc-B')
        default = build_flow(self.archive)
        selected = build_flow(self.archive, document_id='doc-A')
        self.assertEqual(default['document_id'], 'doc-B')
        self.assertEqual(default['operations'], [])
        self.assertEqual(selected['redo_observed'], ['op-6'])
        self.assertEqual(len(selected['documents']), 2)

    def test_recent_window_is_bounded_and_exposes_unobserved_history(self):
        self.experiment()
        result = build_flow(self.archive, limit=3)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['events']), 3)
        self.assertTrue(any(x['kind'] == 'window_truncated' for x in result['boundaries']))
        self.assertEqual(result['undo_observed'], [])

    def test_asset_and_total_input_budgets_fail_explicitly(self):
        self.experiment()
        with patch.object(flow_model, 'MAX_DOCUMENT_BYTES', 30):
            limited = build_flow(self.archive)
        self.assertIsNone(limited['current'])
        self.assertTrue(all(x['kind'] == 'input_limit' for x in limited['boundaries']))
        with patch.object(flow_model, 'MAX_INPUT_BYTES', 700):
            limited = build_flow(self.archive)
        self.assertLessEqual(limited['input_bytes'], 700)
        self.assertTrue(any(x['kind'] == 'input_limit' for x in limited['boundaries']))

    def test_tree_budget_and_large_values_are_marked_not_silently_dropped(self):
        self.experiment()
        with patch.object(flow_model, 'MAX_FIELDS', 2), patch.object(flow_model, 'MAX_TREE_NODES', 10):
            result = build_flow(self.archive)
        self.assertLessEqual(result['tree_nodes'], 10)
        self.assertTrue(any(x['tree_truncated'] for x in result['operations']))
        large = copy.deepcopy(documents()[3])
        large['features'][1]['name'] = '<script>' + 'a' * 5000
        self.capture(large, 4)
        result = build_flow(self.archive)
        self.assertLessEqual(len(result['operations'][-1]['tree'][0]['title']), 220)
        self.assertTrue(result['operations'][-1]['tree'][0]['children'][0]['truncated'])

    def test_invalid_limits_are_rejected_before_queries(self):
        for limit in [0, -1, 201, True, '20']:
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                build_flow(self.archive, limit=limit)
        with self.assertRaises(ValueError):
            build_flow(self.archive, document_id='x' * 257)

    def test_new_recorder_session_exposes_a_history_boundary(self):
        self.experiment()
        self.session = self.archive.new_session({'version': '0.30.1', 'note': 'recorder restarted'})
        self.capture(documents()[3], 3, 1)
        result = build_flow(self.archive)
        self.assertTrue(any(x['kind'] == 'session_boundary' for x in result['boundaries']))
        self.assertEqual(result['undo_observed'], [])
        self.assertEqual(result['redo_observed'], [])
        self.assertEqual(result['current']['unobserved_undo_depth'], 3)

    def test_gap_before_a_new_document_does_not_belong_to_its_history(self):
        self.capture(documents()[0], 0)
        self.archive.add_event(self.session, None, None, 'gap', status='gap')
        self.capture(documents()[0], 0, document_id='doc-B')
        result = build_flow(self.archive)
        self.assertEqual(result['document_id'], 'doc-B')
        self.assertEqual(result['boundaries'], [])
        self.assertEqual(len(result['events']), 1)

    def test_metadata_budget_does_not_silently_select_an_older_document(self):
        self.experiment()
        with patch.object(flow_model, 'MAX_METADATA_BYTES', 30):
            result = build_flow(self.archive)
        self.assertEqual(result['document_id'], 'doc-A')
        self.assertIsNone(result['current'])
        self.assertTrue(result['documents'][0]['metadata_unavailable'])
        self.assertTrue(all(x['kind'] == 'snapshot_metadata_limit' for x in result['boundaries']))

    def test_geometry_keeps_only_bounded_finite_native_measurements(self):
        invalid = {'bodies': [{'name': 'x' * 5000, 'bbox': {'min': [0, 0, 0], 'max': [1, 1, 10 ** 400]},
            'volume_mm3': 10 ** 400, 'faces': {'untrusted': 'x' * 10000}, 'edges': -1}]}
        self.capture(documents()[0], 0, inspected=invalid)
        current = build_flow(self.archive)['current']['geometry']
        body = current['bodies'][0]
        self.assertEqual(len(body['name']), 200)
        self.assertIsNone(body['bbox'])
        self.assertIsNone(body['volume_mm3'])
        self.assertIsNone(body['faces'])
        self.assertIsNone(body['edges'])

    def test_invalid_python_negative_indexes_are_not_rectangular_native_geometry(self):
        empty, sketch, rectangle, *_ = documents()
        for curve in rectangle['features'][0]['sketch']['curves']:
            curve['a'] -= 5
            curve['b'] -= 5
        self.capture(empty, 0)
        self.capture(sketch, 1)
        self.capture(rectangle, 2)
        result = build_flow(self.archive)
        self.assertEqual(result['operations'][-1]['kind'], 'edit_sketch')


if __name__ == '__main__':
    unittest.main()
