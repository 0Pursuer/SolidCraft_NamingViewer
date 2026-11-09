"""Focused regression and resource-bound checks for the geometry adapter."""
import copy
from contextlib import contextmanager
import json
import math
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4
import zipfile

import topology_geometry as geometry


FIXTURES = Path(__file__).resolve().parent.parent / 'topology-visualization'


@contextmanager
def temporary_mesh():
    # Use an existing workspace directory. tempfile's private 0700 directories are
    # inaccessible to this host's restricted Windows process token after creation.
    path = Path(__file__).resolve().parent / f'geometry-test-{uuid4().hex}.3mf'
    try:
        yield path
    finally:
        if path.exists():
            path.unlink()


class GeometryTests(unittest.TestCase):
    def capture(self, stage='before'):
        return json.loads((FIXTURES / f'{stage}-capture.json').read_text(encoding='utf-8-sig'))

    def test_three_real_snapshots_keep_names_and_counts(self):
        for stage_id, expected in [('before', (6, 12, 8)), ('split', (8, 18, 12)), ('chamfer', (9, 21, 14))]:
            with self.subTest(stage=stage_id):
                capture = self.capture(stage_id)
                before = copy.deepcopy(capture)
                stage = geometry.build_body_stage(capture['2']['bodies'][0], capture['3']['faces'], capture['4']['edges'], FIXTURES / f'{stage_id}.3mf', stage_id)
                self.assertEqual((stage['stats']['faces'], stage['stats']['edges'], stage['stats']['displayPoints']), expected)
                self.assertEqual(stage['warnings'], [])
                self.assertTrue(all(not edge['displayApproximate'] for edge in stage['edges']))
                self.assertEqual([edge['name'] for edge in stage['edges']], [edge['name'] for edge in capture['4']['edges']])
                self.assertEqual([face['nativeRecord'] for face in stage['faces']], capture['3']['faces'])
                self.assertEqual([edge['nativeRecord'] for edge in stage['edges']], capture['4']['edges'])
                self.assertTrue(all(face['nameSource'] == 'model.faces' and face['displaySource'] == 'native-face-tag' for face in stage['faces']))
                self.assertTrue(all(edge['nameSource'] == 'model.edges' and edge['displayMapping'] == 'reconstructed-unique' for edge in stage['edges']))
                self.assertEqual(capture, before, 'input query data must stay unchanged')

    def test_unmatched_name_retains_native_record_but_hides_named_geometry(self):
        capture = self.capture()
        capture['4']['edges'][0]['name'] = 'missing|edge'
        stage = geometry.build_body_stage(capture['2']['bodies'][0], capture['3']['faces'], capture['4']['edges'], FIXTURES / 'before.3mf', 'unknown')
        self.assertTrue(stage['edges'][0]['displayApproximate'])
        self.assertEqual(stage['edges'][0]['displaySource'], 'unresolved')
        self.assertEqual(stage['edges'][0]['displayMapping'], 'unresolved')
        self.assertEqual(stage['edges'][0]['name'], 'missing|edge')
        self.assertEqual(stage['edges'][0]['nativeRecord'], capture['4']['edges'][0])
        self.assertTrue(stage['warnings'])
        self.assertEqual(stage['edges'][0]['points'], [])
        self.assertEqual(stage['edges'][0]['faceIndices'], [])

    def test_distinct_native_edges_cannot_claim_the_same_reconstructed_path(self):
        capture = self.capture()
        duplicate = copy.deepcopy(capture['4']['edges'][0])
        duplicate['index'] = 999
        duplicate['name'] += '~2'
        edges = [*capture['4']['edges'], duplicate]
        stage = geometry.build_body_stage(capture['2']['bodies'][0], capture['3']['faces'], edges, FIXTURES / 'before.3mf', 'duplicate-edge')
        conflicting = [e for e in stage['edges'] if e['index'] in (0, 999)]
        self.assertEqual(len(conflicting), 2)
        for edge in conflicting:
            self.assertEqual(edge['points'], [])
            self.assertEqual(edge['faceIndices'], [])
            self.assertEqual(edge['displayMapping'], 'unresolved')
        self.assertEqual(conflicting[0]['nativeRecord'], capture['4']['edges'][0])
        self.assertEqual(conflicting[1]['nativeRecord'], duplicate)
        self.assertTrue(any('0, 999' in warning and 'same reconstructed' in warning for warning in stage['warnings']))

    def test_reversed_duplicate_is_rejected_even_after_display_budget_ends(self):
        capture = self.capture()
        duplicate = copy.deepcopy(capture['4']['edges'][0])
        duplicate['index'] = 999
        duplicate['name'] += '~2'
        duplicate['start'], duplicate['end'] = duplicate['end'], duplicate['start']
        edges = [*capture['4']['edges'], duplicate]
        # Only the first line fits; the later duplicate still has to revoke it.
        budget = len(edges) * geometry.PATH_IDENTITY_BUDGET + 2
        with patch.object(geometry, 'MAX_DISPLAY_EDGE_POINTS', budget):
            stage = geometry.build_body_stage(capture['2']['bodies'][0], capture['3']['faces'], edges, FIXTURES / 'before.3mf', 'duplicate-budget')
        for index in (0, 999):
            edge = next(e for e in stage['edges'] if e['index'] == index)
            self.assertEqual(edge['points'], [])
            self.assertEqual(edge['displayMapping'], 'unresolved')
        self.assertTrue(any('0, 999' in warning and 'same reconstructed' in warning for warning in stage['warnings']))

    def test_closed_loop_duplicates_ignore_rotated_seam_positions(self):
        vertices = [[1, 0, 0], [0, 1, 0], [-1, 0, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]]
        triangles = []
        for index in range(4):
            nxt = (index + 1) % 4
            triangles.extend([[4, index, nxt, 0], [5, nxt, index, 1]])
        faces = [{'index': 0, 'name': 'A', 'exact': False}, {'index': 1, 'name': 'B', 'exact': False}]
        length = 4 * math.sqrt(2)
        edges = [{'index': 0, 'name': 'A|B~1', 'start': vertices[0], 'end': vertices[0], 'mid': vertices[2], 'length': length},
                 {'index': 1, 'name': 'A|B~2', 'start': vertices[1], 'end': vertices[1], 'mid': vertices[3], 'length': length}]
        body = {'name': 'loopBody', 'bbox': {'min': [-1, -1, -1], 'max': [1, 1, 1]}}
        with patch.object(geometry, '_read_mesh', return_value=(vertices, triangles)):
            stage = geometry.build_body_stage(body, faces, edges, Path('not-read.3mf'), 'rotated-loop')
        self.assertTrue(all(edge['points'] == [] and edge['displayMapping'] == 'unresolved' for edge in stage['edges']))
        self.assertTrue(any('0, 1' in warning and 'same reconstructed' in warning for warning in stage['warnings']))

    def test_missing_exact_stays_missing_and_present_values_stay_unchanged(self):
        capture = self.capture()
        capture['3']['faces'][0].pop('exact', None)
        capture['3']['faces'][1]['exact'] = False
        capture['3']['faces'][2]['exact'] = True
        stage = geometry.build_body_stage(capture['2']['bodies'][0], capture['3']['faces'], capture['4']['edges'], FIXTURES / 'before.3mf', 'exact')
        self.assertNotIn('exact', stage['faces'][0])
        self.assertNotIn('exact', stage['faces'][0]['nativeRecord'])
        self.assertIs(stage['faces'][1]['exact'], False)
        self.assertIs(stage['faces'][2]['exact'], True)
        self.assertEqual([face['nativeRecord'] for face in stage['faces']], capture['3']['faces'])

    def test_face_tag_coverage_must_equal_all_queried_faces(self):
        capture = self.capture()
        faces = capture['3']['faces'] + [{**capture['3']['faces'][0], 'index': 999, 'name': 'native-extra-face'}]
        with self.assertRaisesRegex(geometry.GeometryError, 'face tags'):
            geometry.build_body_stage(capture['2']['bodies'][0], faces, capture['4']['edges'], FIXTURES / 'before.3mf', 'missing-face')
        with zipfile.ZipFile(FIXTURES / 'before.3mf') as archive:
            model = geometry.ET.fromstring(archive.read('3D/3dmodel.model'))
        triangle = model.find(f'{geometry.CORE}resources/{geometry.CORE}object/{geometry.CORE}mesh/{geometry.CORE}triangles/{geometry.CORE}triangle')
        del triangle.attrib[geometry.FACE_ATTRIBUTE]
        with temporary_mesh() as path:
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('3D/3dmodel.model', geometry.ET.tostring(model))
            with self.assertRaisesRegex(geometry.GeometryError, 'missing B-rep face tags'):
                geometry.build_body_stage(capture['2']['bodies'][0], capture['3']['faces'], capture['4']['edges'], path, 'missing-tag')

    def test_native_names_cannot_be_synthesized_from_non_string_values(self):
        capture = self.capture()
        for bad in (None, 7, '', ['not-a-name']):
            with self.subTest(value=bad):
                faces = copy.deepcopy(capture['3']['faces'])
                faces[0]['name'] = bad
                with self.assertRaises(geometry.GeometryError):
                    geometry.build_body_stage(capture['2']['bodies'][0], faces, capture['4']['edges'], FIXTURES / 'before.3mf', 'invalid-name')

    def test_sample_plate_hole_arcs_keep_real_tagged_boundaries(self):
        fixture = Path(__file__).resolve().parent / 'debug-curve'
        payload = json.loads((fixture / 'snapshot.json').read_text(encoding='utf-8-sig'))
        original = payload['data']['stages'][0]
        body = {'name': original['bodyName'], 'bbox': original['bounds'],
                'vertices': original['stats']['kernelVertices'], 'volume_mm3': original['stats']['volume']}
        stage = geometry.build_body_stage(body, original['faces'], original['edges'], fixture / 'source.3mf', 'plate')
        expected = [6, 8, 10, 11, 36, 38, 40, 41, 42, 44, 46, 47]
        for index in expected:
            with self.subTest(edge=index):
                edge = next(e for e in stage['edges'] if e['index'] == index)
                self.assertEqual(edge['displaySource'], 'tagged-mesh-boundary')
                self.assertGreater(len(edge['points']), 20)
                self.assertFalse(edge['displayApproximate'])
                # All sampled points remain on the real circular rim, not on the
                # start/mid/end diamond produced by the earlier fallback.
                cylinder = next(f['cylinder'] for f in original['faces']
                                if f['index'] in edge['faceIndices'] and f.get('cylinder'))
                axis = cylinder['axis']
                origin = cylinder['origin']
                for p in edge['points']:
                    v = [p[i] - origin[i] for i in range(3)]
                    height = sum(v[i] * axis[i] for i in range(3))
                    radial = math.sqrt(sum((v[i] - height * axis[i]) ** 2 for i in range(3)))
                    self.assertAlmostEqual(radial, cylinder['radius'], delta=1e-5)

    def test_adjacent_brep_edges_on_one_boundary_are_truncated(self):
        vertices = [[0, 0, 0], [1, 0, 0], [2, 0, 0], [10, 0, 0], [11, 0, 0], [12, 0, 0]]
        boundary = geometry._Boundary((0, 1), [(0, 1), (1, 2), (3, 4), (4, 5)], vertices, 1e-5, geometry._Warnings())
        self.assertEqual(boundary.path(vertices[0], vertices[1], [.5, 0, 0], 1, 100)[0], [0, 1])
        self.assertEqual(boundary.path(vertices[3], vertices[5], vertices[4], 2, 100)[0], [3, 4, 5])

    def test_closed_boundary_and_two_possible_arcs(self):
        vertices = [[1, 0, 0], [0, 1, 0], [-1, 0, 0], [0, -1, 0]]
        boundary = geometry._Boundary((0, 1), [(0, 1), (1, 2), (2, 3), (3, 0)], vertices, 1e-5, geometry._Warnings())
        loop = boundary.path(vertices[0], vertices[0], vertices[2], 4 * math.sqrt(2), 100)[0]
        self.assertEqual(len(loop), 5)
        self.assertEqual(loop[0], loop[-1])
        self.assertEqual(boundary.path(vertices[0], vertices[2], vertices[1], 2 * math.sqrt(2), 100)[0], [0, 1, 2])
        self.assertIsNone(boundary.path(vertices[0], vertices[0], vertices[2], 4 * math.sqrt(2), 3))

    def test_two_equal_arcs_without_discriminating_midpoint_are_unresolved(self):
        vertices = [[1, 0, 0], [0, 1, 0], [-1, 0, 0], [0, -1, 0]]
        boundary = geometry._Boundary((0, 1), [(0, 1), (1, 2), (2, 3), (3, 0)], vertices, 1e-5, geometry._Warnings())
        self.assertIsNone(boundary.path(vertices[0], vertices[2], [0, 0, 0], 2 * math.sqrt(2), 100))
        # Both candidates fit a deliberately coarse native sampling budget. They
        # must still be rejected instead of choosing by traversal/index order.
        coarse = geometry._Boundary((0, 1), [(0, 1), (1, 2), (2, 3), (3, 0)], vertices, 1e-5, geometry._Warnings(), query_tolerance=1.0)
        self.assertIsNone(coarse.path(vertices[0], vertices[2], [0, 0, 0], 2 * math.sqrt(2), 100))

    def test_inconsistent_length_or_midpoint_does_not_select_a_nearest_path(self):
        vertices = [[0, 0, 0], [1, 0, 0], [2, 0, 0]]
        boundary = geometry._Boundary((0, 1), [(0, 1), (1, 2)], vertices, 1e-5, geometry._Warnings())
        self.assertIsNone(boundary.path(vertices[0], vertices[2], [1, 1, 0], 2, 100))
        self.assertIsNone(boundary.path(vertices[0], vertices[2], vertices[1], 100, 100))

    def test_closed_boundary_without_endpoint_correspondence_is_unresolved(self):
        vertices = [[1, 0, 0], [0, 1, 0], [-1, 0, 0], [0, -1, 0]]
        boundary = geometry._Boundary((0, 1), [(0, 1), (1, 2), (2, 3), (3, 0)], vertices, 1e-5, geometry._Warnings())
        seam = [.5, .5, 0]
        self.assertIsNone(boundary.path(seam, seam, [-.5, -.5, 0], 4 * math.sqrt(2), 100))

    def test_oversized_file_and_decompressed_member_are_rejected(self):
        capture = self.capture()
        arguments = (capture['2']['bodies'][0], capture['3']['faces'], capture['4']['edges'], FIXTURES / 'before.3mf', 'limit')
        with patch.object(geometry, 'MAX_FILE_BYTES', 10):
            with self.assertRaises(geometry.GeometryLimitError):
                geometry.build_body_stage(*arguments)
        with patch.object(geometry, 'MAX_MEMBER_BYTES', 10):
            with self.assertRaises(geometry.GeometryLimitError):
                geometry.build_body_stage(*arguments)

    def test_count_limits_reject_before_mesh_access(self):
        with self.assertRaises(geometry.GeometryLimitError):
            geometry.build_body_stage({'name': 'Body1'}, [{}] * 4001, [], Path('missing.3mf'), 'limit')
        with self.assertRaises(geometry.GeometryLimitError):
            geometry.build_body_stage({'name': 'Body1'}, [], [{}] * 8001, Path('missing.3mf'), 'limit')

    def test_dtd_and_wrong_body_are_rejected(self):
        with temporary_mesh() as path:
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('3D/3dmodel.model', '<!DOCTYPE x [<!ENTITY x "test">]><model/>')
            with self.assertRaises(geometry.GeometryError):
                geometry._read_mesh(path, 'Body1')
        with self.assertRaises(geometry.GeometryError):
            geometry._read_mesh(FIXTURES / 'before.3mf', 'SomeOtherBody')

    def test_selects_named_object_from_multi_object_export(self):
        with zipfile.ZipFile(FIXTURES / 'before.3mf') as archive:
            xml = archive.read('3D/3dmodel.model')
        model = geometry.ET.fromstring(xml)
        resources = model.find(f'{geometry.CORE}resources')
        original = resources.find(f'{geometry.CORE}object')
        other = copy.deepcopy(original)
        other.set('name', 'OtherBody')
        other.set('id', '500')
        resources.append(other)
        with temporary_mesh() as path:
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('3D/3dmodel.model', geometry.ET.tostring(model))
            vertices, triangles = geometry._read_mesh(path, 'OtherBody')
            self.assertEqual((len(vertices), len(triangles)), (8, 12))


if __name__ == '__main__':
    unittest.main()
