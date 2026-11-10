"""Truthfulness and bounded evidence tests for the lifecycle presentation model."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

import lifecycle_model
from lifecycle_model import build_lifecycle


def fixture():
    spec = {'feature_id': 2, 'feature_name': 'Extrude1', 'before_distance': '20 mm', 'after_distance': '40 mm'}
    plan = {'available': True, 'before': {'id': 9}, 'after': {'id': 11},
            'lab_compatible': True, 'lab_edit': spec}
    job = {'id': 'a' * 32, 'before_id': 9, 'after_id': 11, 'mode': 'edit_undo',
           'status': 'completed', 'cli_version': '0.30.1', 'spec': copy.deepcopy(spec), 'phases': []}
    assets = {}
    for index, (key, distance, undo, redo) in enumerate([
            ('baseline', '20 mm', 0, 0), ('edited', '40 mm', 1, 0), ('undone', '20 mm', 0, 1)]):
        height = 40 if distance == '40 mm' else 20
        document = {'format': 'solidcraft/1', 'name': 'derived', 'features': [
            {'id': 1, 'name': 'Sketch1', 'type': 'sketch'},
            {'id': 2, 'name': 'Extrude1', 'type': 'extrude', 'extent': {'distance': distance}}]}
        inspected = {'undo': undo, 'redo': redo, 'timeline': [
            {'id': 1, 'name': 'Sketch1', 'error': None, 'ms': .2},
            {'id': 2, 'name': 'Extrude1', 'error': None, 'ms': 1.2}],
            'bodies': [{'name': 'Body1', 'bbox': {'min': [0, 0, 0], 'max': [40, 25, height]},
                        'volume_mm3': 1000 * height, 'faces': 6, 'edges': 12}]}
        phase = {'index': index, 'key': key, 'label': key, 'status': 'completed', 'assets': {},
                 'command': None, 'command_params': {}, 'command_result': None}
        if key == 'edited':
            phase.update(command='timeline.edit', command_params={'feature': 2, 'set': {'extent.distance': '40 mm'}},
                         command_result={'feature': 2, 'recomputed': 1, 'restored': 0})
        elif key == 'undone':
            phase.update(command='edit.undo', command_result={'undone': 'Edit Feature'})
        for name, value in [('document.json', document), ('inspect.json', inspected)]:
            raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
            assets[index, name] = raw
            phase['assets'][name] = {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        job['phases'].append(phase)
    return plan, job, assets


def frame(value, ident):
    return next(f for f in value['frames'] if f['id'] == ident)


def field(value, obj, name):
    return next(f for f in value['data'][obj]['fields'] if f['name'] == name)


class LifecycleModelTests(unittest.TestCase):
    def test_source_walkthrough_has_no_fabricated_runtime_stacks_or_objects(self):
        plan, _, _ = fixture()
        value = build_lifecycle(plan)
        self.assertEqual(value['source_version'], '0.30.1')
        self.assertEqual(len(value['frames']), 13)
        self.assertEqual([o['id'] for o in value['objects']], ['session', 'document', 'model', 'snapshot'])
        self.assertFalse(value['runtime_trace_available'])
        for f in value['frames']:
            self.assertEqual(f['provenance'], 'source_explanation')
            self.assertEqual(f['availability'], 'explanation')
            self.assertIsNone(f['phase_index'])
            self.assertIsNone(field(f, 'session', 'undo_depth')['value'])
            self.assertIsNone(field(f, 'session', 'redo_depth')['value'])
            self.assertFalse(field(f, 'snapshot', 'history_available')['value'])
            self.assertIsNone(field(f, 'model', 'model_state_identity')['value'])
            self.assertIsNone(field(f, 'model', 'runtime_cache_branch')['value'])
            self.assertIn('/blob/v0.30.1/', f['source_url'])
        self.assertEqual(field(frame(value, 'write_document'), 'document', 'height')['value'], '40 mm')
        self.assertEqual(field(frame(value, 'write_document'), 'snapshot', 'doc_height')['value'], '20 mm')
        self.assertEqual(field(frame(value, 'redo_push'), 'snapshot', 'doc_height')['value'], '40 mm')
        self.assertEqual(field(frame(value, 'undone'), 'document', 'height')['value'], '20 mm')

    def test_native_facts_only_at_three_complete_phase_boundaries(self):
        plan, job, assets = fixture()
        value = build_lifecycle(plan, job, lambda i, n: assets[i, n])
        self.assertEqual(value['warnings'], [])
        observed = [f for f in value['frames'] if f['provenance'] == 'native_phase_observation']
        self.assertEqual([f['id'] for f in observed], ['baseline', 'edited', 'undone'])
        self.assertEqual([field(f, 'document', 'height')['value'] for f in observed], ['20 mm', '40 mm', '20 mm'])
        self.assertEqual([field(f, 'model', 'height')['value'] for f in observed], [20, 40, 20])
        self.assertEqual([field(f, 'model', 'volume_mm3')['value'] for f in observed], [20000, 40000, 20000])
        self.assertEqual([field(f, 'session', 'undo_depth')['value'] for f in observed], [0, 1, 0])
        self.assertEqual([field(f, 'session', 'redo_depth')['value'] for f in observed], [0, 0, 1])
        self.assertEqual(field(frame(value, 'edited'), 'model', 'recomputed')['value'], 1)
        self.assertEqual(field(frame(value, 'edited'), 'model', 'restored')['value'], 0)
        self.assertIsNone(field(frame(value, 'undone'), 'model', 'recomputed')['value'])
        self.assertIsNone(field(frame(value, 'undone'), 'model', 'restored')['value'])
        self.assertEqual(value['input_bytes'], sum(map(len, assets.values())))
        for f in value['frames']:
            self.assertFalse(field(f, 'snapshot', 'history_available')['value'])

    def test_mismatched_pair_or_replay_spec_rejected_before_assets(self):
        plan, job, _ = fixture()
        called = []
        job['after_id'] = 12
        with self.assertRaisesRegex(ValueError, '观察对'):
            build_lifecycle(plan, job, lambda *args: called.append(args))
        self.assertEqual(called, [])
        job['after_id'], job['spec']['after_distance'] = 11, '50 mm'
        with self.assertRaisesRegex(ValueError, '拉伸输入'):
            build_lifecycle(plan, job, lambda *args: called.append(args))

    def test_edit_counts_require_matching_command_target_and_patch(self):
        for mismatch in ('feature', 'patch', 'reply', 'missing_params'):
            with self.subTest(mismatch=mismatch):
                plan, job, assets = fixture()
                edited = job['phases'][1]
                if mismatch == 'feature':
                    edited['command_params']['feature'] = 99
                elif mismatch == 'patch':
                    edited['command_params']['set']['extent.distance'] = '50 mm'
                elif mismatch == 'reply':
                    edited['command_result']['feature'] = 99
                else:
                    del edited['command_params']
                value = build_lifecycle(plan, job, lambda i, n: assets[i, n])
                self.assertEqual(frame(value, 'edited')['availability'], 'observed')
                for ident in ('edit_reply', 'edited'):
                    for name in ('recomputed', 'restored'):
                        self.assertIsNone(field(frame(value, ident), 'model', name)['value'])

    def test_missing_or_partial_stage_does_not_turn_into_a_runtime_observation(self):
        plan, job, assets = fixture()
        job['phases'][1]['status'] = 'incomplete'
        job['status'] = 'failed'
        value = build_lifecycle(plan, job, lambda i, n: assets[i, n])
        self.assertEqual(frame(value, 'edited')['availability'], 'unavailable')
        self.assertEqual(frame(value, 'edited')['provenance'], 'source_explanation')
        reply = frame(value, 'edit_reply')
        self.assertEqual(reply['provenance'], 'source_explanation')
        self.assertEqual(field(reply, 'model', 'recomputed')['value'], 1)
        self.assertEqual(field(reply, 'model', 'recomputed')['provenance'], 'native_command_result')
        self.assertEqual(field(reply, 'model', 'restored')['value'], 0)
        self.assertIsNone(field(reply, 'session', 'undo_depth')['value'])

    def test_missing_assets_no_reader_and_duplicate_phase_remain_explicit(self):
        plan, job, assets = fixture()
        value = build_lifecycle(plan, job)
        self.assertTrue(all(frame(value, k)['availability'] == 'unavailable' for k in ('baseline', 'edited', 'undone')))
        del job['phases'][0]['assets']['inspect.json']
        job['phases'].append(copy.deepcopy(job['phases'][2]))
        value = build_lifecycle(plan, job, lambda i, n: assets[i, n])
        self.assertEqual(frame(value, 'baseline')['availability'], 'unavailable')
        self.assertEqual(frame(value, 'undone')['availability'], 'unavailable')
        self.assertEqual(frame(value, 'edited')['availability'], 'observed')

    def test_corruption_wrong_height_invalid_depth_and_json_rejected(self):
        for failure in ('hash', 'height', 'depth', 'json', 'extent'):
            with self.subTest(failure=failure):
                plan, job, assets = fixture()
                name = 'inspect.json' if failure == 'depth' else 'document.json'
                raw = assets[1, name]
                if failure == 'hash':
                    raw = raw.replace(b'40 mm', b'41 mm')
                elif failure == 'height':
                    raw = raw.replace(b'40 mm', b'50 mm')
                elif failure == 'depth':
                    raw = raw.replace(b'"undo": 1', b'"undo": -1')
                elif failure == 'json':
                    raw = b'{"features":[],"features":[]}'
                else:
                    value = json.loads(raw)
                    value['features'][1]['extent'] = []
                    raw = json.dumps(value).encode('utf-8')
                assets[1, name] = raw
                if failure != 'hash':
                    job['phases'][1]['assets'][name] = {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                value = build_lifecycle(plan, job, lambda i, n: assets[i, n])
                self.assertEqual(frame(value, 'edited')['availability'], 'unavailable')
                self.assertEqual(frame(value, 'edited')['provenance'], 'source_explanation')
                self.assertTrue(value['warnings'])

    def test_per_asset_and_total_limits_checked_before_callback(self):
        plan, job, _ = fixture()
        for phase in job['phases']:
            phase['assets']['document.json']['size'] = lifecycle_model.MAX_ASSET_BYTES + 1
        called = []
        value = build_lifecycle(plan, job, lambda *args: called.append(args))
        self.assertEqual(called, [])
        self.assertEqual(value['input_bytes'], 0)
        plan, job, _ = fixture()
        with patch.object(lifecycle_model, 'MAX_INPUT_BYTES', 1):
            value = build_lifecycle(plan, job, lambda *args: called.append(args))
        self.assertEqual(called, [])
        self.assertEqual(value['input_bytes'], 0)

    def test_full_job_never_fabricates_edit_or_undo_execution(self):
        plan, job, assets = fixture()
        phase = copy.deepcopy(job['phases'][1])
        phase.update(index=0, key='full', label='完整重算', command='timeline.compute_all',
                     command_params={}, command_result={'features': 2, 'errors': 0})
        job.update(mode='full', phases=[phase], spec=None)
        value = build_lifecycle(plan, job, lambda i, n: assets[1, n])
        self.assertEqual([f['id'] for f in value['frames']], ['full_model_reset', 'full'])
        self.assertIn('execute 仍创建局部 before Snapshot', frame(value, 'full_model_reset')['description'])
        self.assertIn('不入 Undo', field(frame(value, 'full_model_reset'), 'snapshot', 'role')['value'])
        self.assertEqual(frame(value, 'full')['provenance'], 'native_phase_observation')
        self.assertEqual(frame(value, 'full')['command_result'], {'features': 2, 'errors': 0})
        self.assertIsNone(field(frame(value, 'full'), 'model', 'recomputed')['value'])
        self.assertNotIn('undo_pop', json.dumps(value['frames']))

    def test_unsupported_source_pair_keeps_type_relationships_but_no_fake_demo(self):
        value = build_lifecycle({'available': True, 'lab_compatible': False})
        self.assertFalse(value['available'])
        self.assertEqual(len(value['objects']), 4)
        self.assertEqual(len(value['relations']), 6)
        self.assertEqual(value['frames'], [])
        self.assertTrue(value['warnings'])

    def test_source_field_ownership_and_nonfinite_aggregate_remain_precise(self):
        plan, job, assets = fixture()
        inspected = json.loads(assets[1, 'inspect.json'])
        inspected['bodies'] = [{'name': 'Body1', 'bbox': {'min': [0, 0, -1e308], 'max': [40, 25, 1e308]},
                                'volume_mm3': 1e308}, {'name': 'Body2', 'volume_mm3': 1e308}]
        raw = json.dumps(inspected).encode('utf-8')
        assets[1, 'inspect.json'] = raw
        job['phases'][1]['assets']['inspect.json'] = {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        value = build_lifecycle(plan, job, lambda i, n: assets[i, n])
        model = next(obj for obj in value['objects'] if obj['id'] == 'model')
        document = next(obj for obj in value['objects'] if obj['id'] == 'document')
        self.assertIn('/crates/doc/src/document.rs#L2886-L2911', document['source_url'])
        self.assertIn('results[].state', [f['name'] for f in model['fields']])
        self.assertNotIn('state', [f['name'] for f in model['fields']])
        self.assertIsNone(field(frame(value, 'edited'), 'model', 'volume_mm3')['value'])
        json.dumps(value, allow_nan=False)
        self.assertFalse(lifecycle_model._number(10 ** 500))


if __name__ == '__main__':
    unittest.main()
