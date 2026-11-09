"""Bounded, standard-library-only geometry adapter for the live topology viewer.

Face/edge names and properties come from SolidCraft queries.  A tagged 3MF supplies
display triangles and the sampled boundaries between those named faces.  P IDs
are coordinate-deduplicated display endpoints, never persistent vertex names.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from itertools import product
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile


MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_VERTICES = 120_000
MAX_TRIANGLES = 200_000
MAX_FACES = 4_000
MAX_EDGES = 8_000
MAX_NAME_CHARS = 4 * 1024 * 1024
MAX_KEY_CHARS = 8 * 1024 * 1024
MAX_DISPLAY_EDGE_POINTS = 400_000
# Fixed-size path descriptors and ownership lists reserve point-budget units;
# they never contain path-length-sized strings/tuples or exceed MAX_EDGES entries.
PATH_IDENTITY_BUDGET = 4
MAX_ENDPOINT_CANDIDATES = 64
MAX_WARNINGS = 100
POINT_TOLERANCE = 1e-5
CORE = '{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}'
FACE_ATTRIBUTE = '{https://github.com/bherbruck/solidcraft/formats/3mf-face-tags/2026/10}face'
_EDGE_NUMBER = re.compile(r'~[0-9]+$')
_NEIGHBOR_OFFSETS = tuple(product((-1, 0, 1), repeat=3))


class GeometryError(ValueError):
    """The snapshot is malformed or cannot be associated with this queried body."""


class GeometryLimitError(GeometryError):
    """A snapshot exceeds a deliberate memory/work bound."""


def _point(value, label='point'):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise GeometryError(f'{label} must contain three coordinates')
    try:
        result = [float(coordinate) for coordinate in value]
    except (TypeError, ValueError, OverflowError) as error:
        raise GeometryError(f'{label} contains an invalid coordinate') from error
    if not all(math.isfinite(coordinate) and abs(coordinate) <= 1e12 for coordinate in result):
        raise GeometryError(f'{label} contains a non-finite or out-of-range coordinate')
    return result


def _rounded(point):
    return [round(coordinate, 8) for coordinate in point]


def _distance(a, b):
    return math.dist(a, b)


def _cell(point, width):
    return tuple(math.floor(coordinate / width) for coordinate in point)


def _near_cells(cell):
    x, y, z = cell
    for dx, dy, dz in _NEIGHBOR_OFFSETS:
        yield x + dx, y + dy, z + dz


class _Warnings:
    def __init__(self):
        self.items = []
        self.omitted = 0

    def add(self, message):
        if len(self.items) < MAX_WARNINGS:
            self.items.append(message)
        else:
            self.omitted += 1

    def result(self):
        if self.omitted:
            return self.items + [f'{self.omitted} additional display warnings omitted.']
        return self.items


def _read_mesh(path: Path, body_name: str):
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise GeometryLimitError('3MF file exceeds 32 MiB')
        with zipfile.ZipFile(path) as archive:
            try:
                member = archive.getinfo('3D/3dmodel.model')
            except KeyError as error:
                raise GeometryError('3MF model member is missing') from error
            if member.file_size > MAX_MEMBER_BYTES:
                raise GeometryLimitError('3MF XML member exceeds 32 MiB decompressed')
            with archive.open(member) as stream:
                xml = stream.read(MAX_MEMBER_BYTES + 1)
            if len(xml) > MAX_MEMBER_BYTES:
                raise GeometryLimitError('3MF XML exceeds the decompression limit')
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise GeometryError(f'Cannot read tagged 3MF: {error}') from error
    if b'\x00' in xml:
        raise GeometryError('Only the UTF-8 XML written by the tagged exporter is accepted')
    if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
        raise GeometryError('DTD/entity declarations are not accepted in 3MF XML')
    try:
        model = ET.fromstring(xml)
    except ET.ParseError as error:
        raise GeometryError(f'Invalid 3MF XML: {error}') from error
    mesh_objects = [obj for obj in model.findall(f'{CORE}resources/{CORE}object')
                    if obj.find(f'{CORE}mesh') is not None]
    matches = [obj for obj in mesh_objects if obj.get('name') == body_name]
    if len(matches) != 1:
        raise GeometryError(f'Expected one mesh named {body_name!r}, found {len(matches)}')
    mesh = matches[0].find(f'{CORE}mesh')
    vertex_nodes = mesh.find(f'{CORE}vertices')
    triangle_nodes = mesh.find(f'{CORE}triangles')
    if vertex_nodes is None or triangle_nodes is None:
        raise GeometryError('3MF mesh lacks vertices or triangles')
    if len(vertex_nodes) > MAX_VERTICES:
        raise GeometryLimitError(f'Mesh exceeds {MAX_VERTICES} vertices')
    if len(triangle_nodes) > MAX_TRIANGLES:
        raise GeometryLimitError(f'Mesh exceeds {MAX_TRIANGLES} triangles')
    try:
        vertices = [_point([node.attrib[axis] for axis in 'xyz'], 'mesh vertex')
                    for node in vertex_nodes]
        triangles = []
        for node in triangle_nodes:
            row = [int(node.attrib[key]) for key in ('v1', 'v2', 'v3')]
            row.append(int(node.attrib[FACE_ATTRIBUTE]))
            if any(index < 0 or index >= len(vertices) for index in row[:3]):
                raise GeometryError('Triangle references a missing mesh vertex')
            triangles.append(row)
    except (KeyError, ValueError, OverflowError) as error:
        if isinstance(error, GeometryError):
            raise
        raise GeometryError('3MF contains invalid indices or missing B-rep face tags') from error
    return vertices, triangles


@dataclass
class _Component:
    nodes: list
    prefix: list
    closed: bool

    @property
    def length(self):
        return self.prefix[-1]


def _components(segments, vertices):
    """Build each sampled face-pair boundary once; never rescan it per queried edge."""
    graph = defaultdict(set)
    for a, b in segments:
        graph[a].add(b)
        graph[b].add(a)
    pending = set(graph)
    result = []
    invalid = 0
    while pending:
        seed = pending.pop()
        members, queue = {seed}, [seed]
        while queue:
            for neighbor in graph[queue.pop()]:
                if neighbor not in members:
                    members.add(neighbor)
                    pending.discard(neighbor)
                    queue.append(neighbor)
        if any(len(graph[node]) > 2 for node in members):
            invalid += 1
            continue
        ends = [node for node in members if len(graph[node]) == 1]
        closed = not ends
        if len(ends) not in (0, 2):
            invalid += 1
            continue
        first = min(ends or members)
        order, previous, current = [first], None, first
        while True:
            options = graph[current] - ({previous} if previous is not None else set())
            if not options:
                break
            nxt = min(options)
            order.append(nxt)
            if nxt == first:
                break
            if len(order) > len(members):
                invalid += 1
                order = []
                break
            previous, current = current, nxt
        if not order:
            continue
        prefix = [0.0]
        for a, b in zip(order, order[1:]):
            prefix.append(prefix[-1] + _distance(vertices[a], vertices[b]))
        result.append(_Component(order, prefix, closed))
    return result, invalid


class _Boundary:
    def __init__(self, pair, segments, vertices, tolerance, warnings, query_tolerance=None):
        self.pair = pair
        self.components, invalid = _components(segments, vertices)
        self.invalid_components = bool(invalid)
        self.last_ambiguous = False
        self.last_identity = None
        self.vertices = vertices
        self.tolerance = tolerance
        self.query_tolerance = tolerance if query_tolerance is None else query_tolerance
        self.by_cell = defaultdict(list)
        if invalid:
            warnings.add(f'{invalid} branched/non-simple display boundaries for faces {pair}; unresolved named edge geometry is hidden.')
        for component_index, component in enumerate(self.components):
            positions = range(len(component.nodes) - (1 if component.closed else 0))
            for position in positions:
                node = component.nodes[position]
                self.by_cell[_cell(vertices[node], tolerance)].append((component_index, position))

    def _matches(self, point):
        candidates = []
        examined = 0
        for bucket in _near_cells(_cell(point, self.tolerance)):
            for component_index, position in self.by_cell.get(bucket, ()):
                examined += 1
                if len(candidates) >= MAX_ENDPOINT_CANDIDATES or examined > MAX_ENDPOINT_CANDIDATES * 4:
                    return None
                node = self.components[component_index].nodes[position]
                error = _distance(self.vertices[node], point)
                if error <= self.tolerance:
                    candidates.append((component_index, position, error))
        return candidates

    def _at_distance(self, component, distance):
        if component.closed and component.length:
            distance %= component.length
        distance = min(component.length, max(0.0, distance))
        index = min(len(component.nodes) - 2, max(0, bisect_right(component.prefix, distance) - 1))
        a = self.vertices[component.nodes[index]]
        b = self.vertices[component.nodes[index + 1]]
        span = component.prefix[index + 1] - component.prefix[index]
        t = (distance - component.prefix[index]) / span if span else 0.0
        return [a[axis] + (b[axis] - a[axis]) * t for axis in range(3)]

    def path(self, start, end, mid, expected_length, point_budget):
        # The caller combines possible face pairs. Keep ambiguity distinct from
        # "no matching path" so another pair cannot hide an unresolved contender.
        self.last_ambiguous = self.invalid_components
        self.last_identity = None
        if self.invalid_components:
            return None
        starts, ends = self._matches(start), self._matches(end)
        if starts is None or ends is None:
            self.last_ambiguous = True
            return None
        ends_by_component = defaultdict(list)
        for component_index, position, error in ends:
            ends_by_component[component_index].append((position, error))
        candidates = []
        for component_index, first, first_error in starts:
            component = self.components[component_index]
            for last, last_error in ends_by_component.get(component_index, ()):
                if len(candidates) >= MAX_ENDPOINT_CANDIDATES:
                    self.last_ambiguous = True
                    return None
                if not component.closed:
                    directions = [1 if last >= first else -1]
                elif first == last:
                    directions = [1]
                else:
                    directions = [1, -1]
                for direction in directions:
                    delta = direction * (component.prefix[last] - component.prefix[first])
                    if component.closed and delta <= 0:
                        delta += component.length
                    midpoint = self._at_distance(component, component.prefix[first] + direction * delta / 2)
                    # The native query uses a coarser tessellation than the tagged
                    # exporter. Require agreement inside that spatial sampling
                    # budget, then require a unique path rather than ranking
                    # competing paths. Four sampling tolerances is a conservative
                    # display acceptance policy, not a topology/history proof;
                    # legitimate complicated curves may deliberately be omitted.
                    midpoint_error = _distance(midpoint, mid)
                    if (midpoint_error <= self.query_tolerance + self.tolerance
                            and abs(delta - expected_length) <= 4 * self.query_tolerance + 2 * self.tolerance):
                        candidates.append((component_index, first, last, direction, delta))
        if not candidates:
            # No endpoint correspondence means no named display path, even for a
            # similar-length loop. Length alone cannot identify its seam or phase.
            return None
        if len(candidates) != 1:
            self.last_ambiguous = True
            return None
        component_index, first, last, direction, length = candidates[0]
        component = self.components[component_index]
        # In a simple component, endpoint positions and direction identify the
        # complete node path. Normalize reversal; a full closed loop has the same
        # identity at every seam/phase. This is equivalent to canonicalizing all
        # nodes but uses constant-size metadata, even after display budget ends.
        if component.closed and first == last:
            self.last_identity = (component_index, 'loop')
        elif first <= last:
            self.last_identity = (component_index, first, last, direction if component.closed else 0)
        else:
            self.last_identity = (component_index, last, first, -direction if component.closed else 0)
        count = len(component.nodes) - (1 if component.closed else 0)
        path_count = (abs(last - first) + 1 if not component.closed else
                      (count + 1 if first == last else ((last - first) * direction) % count + 1))
        if path_count > point_budget:
            return None
        if not component.closed:
            nodes = component.nodes[first:last + 1] if direction > 0 else component.nodes[last:first + 1][::-1]
        else:
            nodes, position = [component.nodes[first]], first
            for _ in range(count):
                position = (position + direction) % count
                nodes.append(component.nodes[position])
                if position == last:
                    break
        return nodes, length


def _display_points(edges):
    """Endpoint deduplication and incidence in expected linear work via a spatial hash."""
    buckets = defaultdict(list)
    records = []
    for edge in edges:
        for position in (edge['start'], edge['end']):
            key = _cell(position, POINT_TOLERANCE)
            found = None
            for neighbor in _near_cells(key):
                for index in buckets.get(neighbor, ()):
                    if _distance(position, records[index]['position']) < POINT_TOLERANCE:
                        found = index
                        break
                if found is not None:
                    break
            if found is None:
                found = len(records)
                records.append({'position': position, 'edgeIndices': set(), 'faceIndices': set()})
                buckets[key].append(found)
            records[found]['edgeIndices'].add(edge['index'])
            records[found]['faceIndices'].update(edge['faceIndices'])
    records.sort(key=lambda record: tuple(round(value, 5) for value in record['position']))
    return [{'index': index, 'id': f'P{index}', 'position': _rounded(record['position']),
             'nameSource': 'viewer-display-id', 'displaySource': 'native-edge-endpoints-derived',
             'edgeIndices': sorted(record['edgeIndices']), 'faceIndices': sorted(record['faceIndices'])}
            for index, record in enumerate(records)]


def _hide_edge_geometry(edge):
    edge['points'] = []
    edge['faceIndices'] = []
    edge['displaySource'] = 'unresolved'
    edge['displayMapping'] = 'unresolved'
    edge['displayApproximate'] = True


def build_body_stage(body: dict, faces: list, edges: list, mesh_path: Path, stage_id: str) -> dict:
    """Join a single body's live queries and tagged display mesh into a viewer stage.

    The caller must bracket all queries/export with matching document.revision values.
    This function does not contact SolidCraft, mutate its arguments, or write files.
    """
    if len(faces) > MAX_FACES or len(edges) > MAX_EDGES:
        raise GeometryLimitError(f'Body exceeds {MAX_FACES} faces or {MAX_EDGES} edges')
    if any(not isinstance(item.get('name'), str) or not item['name'] for item in [*faces, *edges]):
        raise GeometryError('Native face/edge names must be nonempty strings')
    if sum(len(item['name']) for item in [*faces, *edges]) > MAX_NAME_CHARS:
        raise GeometryLimitError('Face/edge names exceed the aggregate name limit')
    body_name = str(body.get('name', ''))
    if not body_name:
        raise GeometryError('Body name is missing')
    face_by_index = {}
    for face in faces:
        index = face.get('index')
        if type(index) is not int or index < 0 or index in face_by_index:
            raise GeometryError('Face indices must be unique nonnegative integers')
        if 'exact' in face and type(face['exact']) is not bool:
            raise GeometryError('Native face exact must be a boolean when present')
        face_by_index[index] = face
    vertices, triangles = _read_mesh(Path(mesh_path), body_name)
    if not vertices or not triangles:
        raise GeometryError('The selected 3MF mesh is empty')
    if {triangle[3] for triangle in triangles} != set(face_by_index):
        raise GeometryError('Mesh face tags do not match the queried face indices')
    bounds = body.get('bbox') or {
        'min': [min(point[axis] for point in vertices) for axis in range(3)],
        'max': [max(point[axis] for point in vertices) for axis in range(3)]}
    bounds = {'min': _point(bounds['min'], 'bbox min'), 'max': _point(bounds['max'], 'bbox max')}
    diagonal = _distance(bounds['min'], bounds['max'])
    tolerance = max(POINT_TOLERANCE, diagonal * 2e-6)
    # SolidCraft v0.30.1 model.edges uses max(Body::size()*2e-3, 1e-3),
    # and Body::size is the native bounding-box diagonal.
    query_tolerance = max(diagonal * 2e-3, 1e-3)
    warnings = _Warnings()

    # Only actual mesh adjacency generates name keys.  There is no F x F scan.
    segment_faces = {}
    for a, b, c, face in triangles:
        for u, v in ((a, b), (b, c), (c, a)):
            segment = (u, v) if u < v else (v, u)
            if segment not in segment_faces:
                segment_faces[segment] = face
            else:
                previous = segment_faces[segment]
                if previous != face:
                    if isinstance(previous, tuple):
                        if face not in previous:
                            segment_faces[segment] = (*previous, face)
                    else:
                        segment_faces[segment] = (previous, face)
    groups = defaultdict(list)
    for segment, incident in segment_faces.items():
        if isinstance(incident, tuple) and len(incident) == 2:
            groups[tuple(sorted(incident))].append(segment)
        elif isinstance(incident, tuple):
            warnings.add('A non-manifold display segment was omitted from edge reconstruction.')
    del segment_faces
    by_name = defaultdict(list)
    boundary_by_pair = {}
    key_characters = 0
    for pair, segments in groups.items():
        parts = [face_by_index[index]['name'] for index in pair]
        if not all(parts):
            continue
        key_characters += sum(map(len, parts)) + 1
        if key_characters > MAX_KEY_CHARS:
            raise GeometryLimitError('Assembled adjacency names exceed the name-key limit')
        name = '|'.join(sorted(parts))
        by_name[name].append(pair)
        boundary_by_pair[pair] = _Boundary(pair, segments, vertices, tolerance, warnings, query_tolerance)
    del groups

    output_edges = []
    used_display_points = len(edges) * PATH_IDENTITY_BUDGET
    path_owners = {}
    seen_edge_indices = set()
    for ordinal, original in enumerate(edges):
        edge = dict(original)
        edge['nativeRecord'] = dict(original)
        edge['nameSource'] = 'model.edges'
        index = edge.get('index')
        if type(index) is not int or index < 0 or index in seen_edge_indices:
            raise GeometryError('Edge indices must be unique nonnegative integers')
        seen_edge_indices.add(index)
        start = _point(edge.get('start'), 'edge start')
        end = _point(edge.get('end'), 'edge end')
        mid = _point(edge.get('mid'), 'edge midpoint')
        try:
            length = float(edge.get('length', 0))
        except (TypeError, ValueError, OverflowError) as error:
            raise GeometryError('Invalid edge length') from error
        if not math.isfinite(length) or length < 0:
            raise GeometryError('Invalid edge length')
        name = edge['name']
        pairs = by_name.get(name)
        if pairs is None:
            pairs = by_name.get(_EDGE_NUMBER.sub('', name), ())
        matches = []
        ambiguous = False
        point_budget = MAX_DISPLAY_EDGE_POINTS - used_display_points
        if len(pairs) <= MAX_ENDPOINT_CANDIDATES:
            for pair in pairs:
                boundary = boundary_by_pair[pair]
                path = boundary.path(start, end, mid, length, point_budget)
                if boundary.last_ambiguous:
                    ambiguous = True
                    break
                if boundary.last_identity is not None:
                    matches.append((pair, boundary.last_identity, path[0] if path else None))
        unique = not ambiguous and len(matches) == 1
        owners = None
        nodes = None
        if unique:
            pair, identity, nodes = matches[0]
            owners = path_owners.setdefault((pair, identity), [])
            if owners:
                # Previously published candidates are still local to this stage.
                # Revoke the first as well as every subsequent conflicting edge.
                _hide_edge_geometry(owners[0])
            owners.append(edge)
        if unique and len(owners) == 1 and nodes is not None and used_display_points + len(nodes) <= MAX_DISPLAY_EDGE_POINTS:
            points = [_rounded(vertices[node]) for node in nodes]
            edge['faceIndices'] = list(pair)
            edge['displaySource'] = 'tagged-mesh-boundary'
            edge['displayMapping'] = 'reconstructed-unique'
            edge['displayApproximate'] = False
        else:
            points = []
            _hide_edge_geometry(edge)
            warnings.add(f'Edge {index}: display boundary unavailable, ambiguous, inconsistent with native samples, or over budget; named edge geometry is hidden.')
        used_display_points += len(points)
        edge['start'], edge['end'], edge['mid'] = _rounded(start), _rounded(end), _rounded(mid)
        edge['points'] = points
        output_edges.append(edge)
    for owners in path_owners.values():
        if len(owners) > 1:
            indices = ', '.join(str(edge['index']) for edge in owners[:8])
            remaining = f' and {len(owners) - 8} more' if len(owners) > 8 else ''
            warnings.add(f'Native edges {indices}{remaining}: same reconstructed display boundary claimed by distinct native edges; all conflicting named edge geometries are hidden.')
    points = _display_points(output_edges)
    return {'id': str(stage_id), 'title': body_name, 'bodyName': body_name,
            'subtitle': '实时模型快照 · 面边名称来自 SolidCraft',
            'features': [], 'warning': None, 'warnings': warnings.result(),
            'mesh': {'vertices': [_rounded(point) for point in vertices], 'triangles': triangles},
            'faces': [{**face, 'nativeRecord': dict(face), 'nameSource': 'model.faces',
                       'displaySource': 'native-face-tag', 'displayMapping': 'native-face-tag'} for face in faces],
            'edges': output_edges, 'points': points,
            'bounds': {'min': _rounded(bounds['min']), 'max': _rounded(bounds['max'])},
            'stats': {'faces': len(faces), 'edges': len(edges), 'displayPoints': len(points),
                      'kernelVertices': body.get('vertices'), 'volume': body.get('volume_mm3')}}
