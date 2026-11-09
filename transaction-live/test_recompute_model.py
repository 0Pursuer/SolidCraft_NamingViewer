"""Check evidence boundaries, real operation pairing, and bounded archive reads."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from archive import Archive
import recompute_model as model


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def document(height='20 mm'):
    return {'format': 'solidcraft/48', 'name': 'Height experiment', 'params': [], 'next_id': 3,
        'features': [
            {'id': 1, 'name': 'Sketch1', 'type': 'sketch', 'suppressed': False,
             'plane': {'type': 'origin', 'name': 'XY'},
             'sketch': {'points': [{'id': 'origin', 'pos': [0, 0], 'fixed': True}],
                        'curves': [], 'constraints': []}},
            {'id': 2, 'name': 'Extrude1', 'type': 'extrude', 'suppressed': False,
             'sketch': 1, 'profiles': {'type': 'curves', 'loops': [['l1', 'l2', 'l3', 'l4']]},
             'extent': {'distance': height, 'direction': 'positive', 'convention': 'total_distance'},
             'operation': 'new_body', 'body_names': ['Body1'], 'param_names': ['d1']}],
        'reserved_body_names': ['Body1']}


class RecomputeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = Archive(Path(self.temp.name) / 'archive')
        self.session = self.archive.new_session({'version': '0.30.1'})
        self.revision = 0

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def capture(self, doc, undo, redo=0, identity='doc-A', inspect=True):
        self.revision += 1
        event = self.archive.add_event(self.session, identity, self.revision, 'design.changed')
        assets = {'document.json': encoded(doc)}
        if inspect:
            height = float(doc['features'][-1]['extent']['distance'].split()[0])
            assets['inspect.json'] = encoded({'timeline': [
                {'id': 1, 'name': 'Sketch1', 'type': 'sketch', 'ms': .2286, 'error': None, 'warning': None},
                {'id': 2, 'name': 'Extrude1', 'type': 'extrude', 'ms': 1.2, 'error': None, 'warning': None}],
                'bodies': [{'name': 'Body1', 'volume_mm3': 1000 * height,
                            'bbox': {'min': [0, 0, 0], 'max': [40, 25, height]}, 'faces': 6, 'edges': 12}]})
        return self.archive.commit_snapshot(event, assets, {'document_id': identity, 'revision': self.revision,
            'undo': undo, 'redo': redo, 'name': doc['name'], 'marker': doc.get('marker')})

    def experiment(self):
        twenty, forty = document(), document('40 mm')
        before = self.capture(twenty, 3)
        temporary = copy.deepcopy(twenty)
        temporary['marker'] = 1
        self.capture(temporary, 3)
        after = self.capture(forty, 4)
        undone = self.capture(twenty, 3, 1)
        return before, after, undone

    def test_undo_selects_original_edit_pair_and_preserves_definition_facts(self):
        before, after, undone = self.experiment()
        result = model.build_recompute(self.archive)
        self.assertTrue(result['available'])
        self.assertEqual((result['before']['id'], result['after']['id']), (before['id'], after['id']))
        self.assertEqual(result['related_operation']['status'], 'undone')
        self.assertEqual(result['observations'][0]['id'], undone['id'])
        sketch, extrude = result['features']
        self.assertFalse(sketch['definition_changed'])
        self.assertTrue(extrude['definition_changed'])
        self.assertEqual(extrude['changes'], [{'path': 'extent.distance', 'kind': 'changed', 'old': '20 mm', 'new': '40 mm'}])
        self.assertEqual(extrude['references'][0]['target_feature_id'], 1)
        self.assertEqual(sketch['references'], [])  # XY is not invented as a Feature.
        self.assertEqual(result['before']['geometry']['total_volume_mm3'], 20000)
        self.assertEqual(result['after']['geometry']['total_volume_mm3'], 40000)
        self.assertTrue(result['lab_compatible'])

    def test_identical_ms_does_not_become_a_cache_hit_claim(self):
        a, b, _ = self.experiment()
        result = model.build_recompute(self.archive, a['id'], b['id'])
        self.assertTrue(all(feature['runtime_cache_branch'] == 'unavailable' for feature in result['features']))
        self.assertEqual(result['features'][1]['before_result']['ms'], result['features'][1]['after_result']['ms'])
        self.assertNotIn('recomputed', result['features'][1])
        self.assertTrue(all(not step['runtime_observed'] for step in result['steps']))
        self.assertTrue(all('/blob/v0.30.1/' in step['source_url'] for step in result['steps']))
        self.assertEqual(result['source_commit'], '8e2b0cd')

    def test_explicit_pair_is_not_replaced_with_the_related_operation(self):
        a, b, undone = self.experiment()
        result = model.build_recompute(self.archive, b['id'], undone['id'])
        self.assertEqual((result['before']['id'], result['after']['id']), (b['id'], undone['id']))
        self.assertEqual(result['features'][1]['changes'][0]['new'], '20 mm')
        self.assertEqual(result['selection_reason'], 'explicit_observation_pair')

    def test_read_only_output_and_database_are_stable(self):
        self.experiment()
        counts = self.archive.status()['counts']
        db = self.archive.root / 'archive.sqlite3'
        digest = hashlib.sha256(db.read_bytes()).hexdigest()
        first = model.build_recompute(self.archive)
        self.assertEqual(first, model.build_recompute(self.archive))
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), digest)
        self.assertEqual(counts, self.archive.status()['counts'])
        self.assertLessEqual(first['input_bytes'], model.MAX_INPUT_BYTES)

    def test_missing_geometry_remains_missing_without_losing_definition_diff(self):
        a = self.capture(document(), 3, inspect=False)
        b = self.capture(document('40 mm'), 4, inspect=False)
        result = model.build_recompute(self.archive, a['id'], b['id'])
        self.assertTrue(result['available'])
        self.assertFalse(result['before']['geometry']['available'])
        self.assertFalse(result['features'][1]['before_result']['available'])
        self.assertTrue(result['features'][1]['definition_changed'])

    def test_cross_document_and_invalid_ids_are_rejected(self):
        a = self.capture(document(), 3)
        b = self.capture(document('40 mm'), 4, identity='doc-B')
        result = model.build_recompute(self.archive, a['id'], b['id'])
        self.assertFalse(result['available'])
        self.assertIsNone(result['before'])
        for pair in ((None, 2), (True, 2), (0, 2), ('1', 2)):
            with self.assertRaises(ValueError):
                model.build_recompute(self.archive, *pair)

    def test_snapshot_asset_integrity_is_checked_before_using_definition(self):
        a = self.capture(document(), 3)
        b = self.capture(document('40 mm'), 4)
        path = self.archive.asset_path(a['assets']['document.json']['sha256'])
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b'20 mm', b'21 mm'))
        result = model.build_recompute(self.archive, a['id'], b['id'])
        self.assertFalse(result['available'])
        self.assertFalse(result['lab_compatible'])

    def test_whole_request_input_budget_covers_the_default_flow_scan(self):
        for i in range(12):
            doc = document(f'{20 + i} mm')
            doc['padding'] = 'x' * 9000
            self.capture(doc, 3 + i)
        with patch.object(model, 'MAX_INPUT_BYTES', 32 * 1024):
            result = model.build_recompute(self.archive)
        self.assertLessEqual(result['input_bytes'], 32 * 1024)
        self.assertTrue(result['available'])
        self.assertEqual(result['after']['id'], 12)

    def test_oversize_metadata_is_not_deserialized(self):
        a = self.capture(document(), 3)
        b = self.capture(document('40 mm'), 4)
        with self.archive._lock:
            self.archive._db.execute('UPDATE snapshots SET summary=? WHERE id=?', (json.dumps({'payload': 'x' * (model.MAX_METADATA_BYTES + 1)}), a['id']))
        result = model.build_recompute(self.archive, a['id'], b['id'])
        self.assertFalse(result['available'])
        self.assertLessEqual(result['input_bytes'], model.MAX_INPUT_BYTES)

    def test_empty_archive_has_source_steps_but_no_invented_model(self):
        result = model.build_recompute(self.archive)
        self.assertFalse(result['available'])
        self.assertEqual(result['features'], [])
        self.assertEqual(result['observations'], [])
        self.assertGreater(len(result['steps']), 5)


class ReplayValidationTests(unittest.TestCase):
    def test_only_one_distance_change_is_allowed(self):
        result = model.validate_replay_documents(encoded(document()), encoded(document('40 mm')))
        self.assertEqual(result['feature_id'], 2)
        self.assertEqual((result['before_distance'], result['after_distance']), ('20 mm', '40 mm'))
        for modify in (
            lambda doc: doc.update(name='Renamed too'),
            lambda doc: doc.update(marker=1),
            lambda doc: doc.update(links=[{'path': 'other.sc'}]),
            lambda doc: doc['features'][1].update(sketch=7),
            lambda doc: doc['features'][0].update(id=2),
            lambda doc: doc['features'][1].update(suppressed=True),
        ):
            changed = document('40 mm')
            modify(changed)
            with self.subTest(modify=modify):
                with self.assertRaises(ValueError):
                    model.validate_replay_documents(encoded(document()), encoded(changed))

    def test_duplicate_keys_nonfinite_json_and_oversize_are_refused(self):
        bad = [b'{"format":"solidcraft/48","format":"solidcraft/48","features":[]}',
               b'{"format":"solidcraft/48","features":[],"value":NaN}',
               b'x' * (model.MAX_DOCUMENT_BYTES + 1)]
        for raw in bad:
            with self.subTest(length=len(raw)):
                with self.assertRaises(ValueError):
                    model.validate_replay_documents(raw, encoded(document('40 mm')))


if __name__ == '__main__':
    unittest.main()
