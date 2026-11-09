"""Build an offline viewer dataset from real SolidCraft replay and face-tagged meshes."""
from __future__ import annotations

import collections
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parent
CORE = '{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}'
FACE = '{https://github.com/bherbruck/solidcraft/formats/3mf-face-tags/2026/10}face'


def distance(a, b):
    return math.dist(a, b)


def ordered_polyline(segments, vertices, start, end, near):
    graph = collections.defaultdict(set)
    for a, b in segments:
        graph[a].add(b)
        graph[b].add(a)
    if not graph:
        return None
    pending = set(graph)
    components = []
    while pending:
        seed = pending.pop()
        component, queue = {seed}, [seed]
        while queue:
            for neighbor in graph[queue.pop()]:
                if neighbor not in component:
                    component.add(neighbor)
                    pending.discard(neighbor)
                    queue.append(neighbor)
        components.append(component)
    component = min(components, key=lambda c: min(distance(vertices[i], near) for i in c))
    endpoints = [i for i in component if len(graph[i]) == 1]
    first = min(endpoints or component, key=lambda i: distance(vertices[i], start))
    ordered, previous, current = [first], None, first
    for _ in range(len(component) + 1):
        options = [i for i in graph[current] if i != previous and (i not in ordered or i == first)]
        if not options:
            break
        nxt = min(options, key=lambda i: distance(vertices[i], end))
        ordered.append(nxt)
        if nxt == first:
            break
        previous, current = current, nxt
    return [vertices[i] for i in ordered]


def build_stage(stage_id, title, subtitle):
    captured = json.loads((ROOT / f'{stage_id}-capture.json').read_text(encoding='utf-8-sig'))
    faces = captured['3']['faces']
    edges = captured['4']['edges']
    design = captured['2']
    with zipfile.ZipFile(ROOT / f'{stage_id}.3mf') as archive:
        model = ET.fromstring(archive.read('3D/3dmodel.model'))
    mesh_node = model.find(f'{CORE}resources/{CORE}object/{CORE}mesh')
    vertices = [[float(v.attrib[c]) for c in 'xyz'] for v in mesh_node.find(f'{CORE}vertices')]
    triangles = [[int(t.attrib[c]) for c in ('v1', 'v2', 'v3')] + [int(t.attrib[FACE])] for t in mesh_node.find(f'{CORE}triangles')]
    segment_faces = collections.defaultdict(set)
    for a, b, c, face in triangles:
        for u, v in ((a, b), (b, c), (c, a)):
            segment_faces[tuple(sorted((u, v)))].add(face)
    boundary_groups = collections.defaultdict(list)
    for segment, adjoining in segment_faces.items():
        if len(adjoining) == 2:
            boundary_groups[tuple(sorted(adjoining))].append(segment)
    warnings = []
    for edge in edges:
        pairs = []
        for a in range(len(faces)):
            for b in range(a + 1, len(faces)):
                name = '|'.join(sorted((faces[a]['name'], faces[b]['name'])))
                if edge['name'] == name or edge['name'].startswith(name + '~'):
                    pairs.append((a, b))
        if len(pairs) != 1:
            raise ValueError(f"Ambiguous adjacent faces for {stage_id}: {edge['name']}: {pairs}")
        edge['faceIndices'] = list(pairs[0])
        points = ordered_polyline(boundary_groups[pairs[0]], vertices, edge['start'], edge['end'], edge['mid'])
        if not points:
            raise ValueError(f"Missing tagged mesh boundary for {stage_id}: {edge['name']}")
        edge['points'] = points
        polyline_length = sum(distance(a, b) for a, b in zip(points, points[1:]))
        if abs(polyline_length - edge['length']) > max(0.08, edge['length'] * 0.006):
            warnings.append({'edge': edge['index'], 'kernelLength': edge['length'], 'meshLength': polyline_length})
    # These are coordinate-deduplicated display endpoints, never kernel vertex identities.
    positions = []
    for edge in edges:
        for point in (edge['start'], edge['end']):
            if not any(distance(point, old) < 1e-5 for old in positions):
                positions.append(point)
    positions.sort(key=lambda point: tuple(round(c, 5) for c in point))
    points = []
    for index, point in enumerate(positions):
        touching_edges = [edge for edge in edges if min(distance(point, edge['start']), distance(point, edge['end'])) < 1e-5]
        points.append({'index': index, 'id': f'P{index}', 'position': point, 'edgeIndices': [edge['index'] for edge in touching_edges], 'faceIndices': sorted({i for edge in touching_edges for i in edge['faceIndices']})})
    body = design['bodies'][0]
    stage = {'id': stage_id, 'title': title, 'subtitle': subtitle,
             'features': design['timeline'], 'warning': next((f['warning'] for f in design['timeline'] if f['warning']), None),
             'mesh': {'vertices': vertices, 'triangles': triangles}, 'faces': faces, 'edges': edges, 'points': points,
             'bounds': body['bbox'],
             'stats': {'faces': len(faces), 'edges': len(edges), 'displayPoints': len(points), 'kernelVertices': body['vertices'], 'volume': body['volume_mm3']}}
    return stage, warnings


stages = []
validation = []
for spec in [('before', '① 分裂前 · Extrude1', '原始拉伸：顶面和警告目标边仍完整'),
             ('split', '② 分裂后 · Extrude2', '圆形切割将顶面分为左右两片，原目标边变为两条分片边'),
             ('chamfer', '③ 倒角后 · Chamfer1', '恢复选择右侧分片，生成倒角面及新的边界边')]:
    stage, warnings = build_stage(*spec)
    stages.append(stage)
    validation.append({'stage': stage['id'], **stage['stats'], 'triangles': len(stage['mesh']['triangles']), 'meshBoundaryWarnings': warnings})

live = json.loads((ROOT / 'live-state-before.json').read_text(encoding='utf-8'))
data = {'meta': {'version': '0.30.1', 'design': live['name'], 'liveRevision': live['revision'],
    'vertexNote': 'P 编号为各阶段按边端点坐标去重生成的展示编号；不是 SolidCraft 的持久拓扑名或内核顶点序号。重合但拓扑不同的顶点可能合并；不用于跨阶段身份追踪。',
    'geometryNote': '来自当前设计的 timeline.export_script；在独立 headless 会话重放三个阶段。面、边名称来自 SolidCraft 查询，显示网格来自带 B-rep 面标签的 3MF。曲线显示为导出网格的折线近似。',
    'storedEdgeName': 'F2:side:l3|F2:top',
    'resolvedEdgeName': 'F2:side:l3|F2:top#[F2:side:l2]',
    'chamferFaceName': 'F6:blend:F2:side:l3|F2:top#[F2:side:l2]',
    'sourceLinks': [{'title': 'SolidCraft 持久命名说明', 'url': 'https://github.com/tangency-labs/solidcraft/blob/v0.30.1/docs/book/src/dev/naming.md'},
      {'title': '边名生成', 'url': 'https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/doc/src/naming.rs#L911-L929'},
      {'title': '警告恢复逻辑', 'url': 'https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/doc/src/eval.rs#L4330-L4396'},
      {'title': '仅面和边的持久命名类型', 'url': 'https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/doc/src/persistent_name.rs#L1-L22'}]},
    'stages': stages}
(ROOT / 'topology-data.json').write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
(ROOT / 'data-validation.json').write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(validation, ensure_ascii=False, indent=2))
