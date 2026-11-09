"""Bounded, read-only collection shared by the live channel and isolated CLI."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from topology_geometry import build_body_stage, GeometryError

MAX_DATA = 16 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
MAX_BODIES = 32
MAX_SKETCHES = 256
MAX_DISPLAY_WORK = 8_000_000


class SourceChanged(RuntimeError):
    pass


class CaptureLimit(RuntimeError):
    pass


def json_bytes(value, limit=MAX_DATA):
    data = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(data) > limit:
        raise CaptureLimit('数据超过单份资源容量上限')
    return data


def revision_key(reply):
    if not isinstance(reply, dict) or not isinstance(reply.get('document_id'), str) or type(reply.get('revision')) is not int:
        raise ValueError('SolidCraft 没有返回有效 document_id/revision')
    return reply['document_id'], reply['revision']


def bounded_file(path):
    if path.stat().st_size > MAX_FILE:
        raise CaptureLimit('导出文件超过 32 MiB 上限')
    with path.open('rb') as stream:
        data = stream.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise CaptureLimit('导出文件超过 32 MiB 上限')
    return data


def verify_stage_names(stage, faces, edges, body_name):
    """The adapter must retain exact native records, including their JSON types."""
    if stage.get('bodyName') != body_name:
        raise CaptureLimit('几何适配器实体名称校验失败')
    for kind, original in [('faces', faces), ('edges', edges)]:
        adapted = stage.get(kind, [])
        source = {x['index']: x for x in original}
        if len(source) != len(original) or {x['index'] for x in adapted} != source.keys():
            raise CaptureLimit('几何适配器原生索引校验失败')
        for item in adapted:
            native = source[item['index']]
            if (item.get('name') != native.get('name') or
                    json_bytes(item.get('nativeRecord')) != json_bytes(native) or
                    ('exact' in item) != ('exact' in native) or
                    ('exact' in native and json_bytes(item['exact']) != json_bytes(native['exact']))):
                raise CaptureLimit('几何适配器原生名称/记录校验失败')
            item['nameAudit'] = 'native-records-match'


def collect_bundle(client, workdir: Path, version: str, expected_key=None, source='observed'):
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    key = revision_key(client.request('document.revision'))
    if expected_key is not None and key != tuple(expected_key):
        raise SourceChanged('该通知已被后续版本越过')

    def check():
        if revision_key(client.request('document.revision')) != key:
            raise SourceChanged('采集期间文档发生变化；未提交混合版本快照')

    inspected = client.request('document.inspect', {'measure': True, 'sketch_counts': True}, timeout=15)
    bodies = inspected.get('bodies', [])
    sketches_meta = inspected.get('sketches', [])
    if len(bodies) > MAX_BODIES or len(sketches_meta) > MAX_SKETCHES:
        raise CaptureLimit('实体或草图数量超过工具上限 (32 / 256)')
    document_path = workdir / 'document.json'
    client.export(document_path, 'json', timeout=30)
    document_bytes = bounded_file(document_path)
    document = json.loads(document_bytes)
    if not isinstance(document, dict) or not isinstance(document.get('features'), list):
        raise ValueError('JSON 导出不是完整设计文档')
    warnings, sketches, topology, stages = [], [], [], []
    for item in sketches_meta:
        sketches.append(client.query('sketch.inspect', {'sketch': item['id']}, timeout=12))
    check()
    for body in bodies:
        name = body.get('name')
        if not isinstance(name, str) or len(name) > 4096:
            raise CaptureLimit('无效实体名称')
        faces = client.query('model.faces', {'body': name}, timeout=12)
        edges = client.query('model.edges', {'body': name}, timeout=12)
        topology.append({'body': copy.deepcopy(body), 'faces': faces, 'edges': edges})
    mesh_bytes = None
    mesh_path = workdir / 'model.3mf'
    if bodies:
        try:
            client.export(mesh_path, '3mf-tagged', timeout=30)
            mesh_bytes = bounded_file(mesh_path)
        except (RuntimeError, GeometryError) as exc:
            warnings.append('显示网格导出失败：' + str(exc)[:400])
    check()
    if mesh_bytes is not None:
        for i, records in enumerate(topology):
            body = records['body']
            faces, edges = records['faces']['faces'], records['edges']['edges']
            try:
                stage = build_body_stage(body, copy.deepcopy(faces), copy.deepcopy(edges), mesh_path, 'body' + str(i))
                verify_stage_names(stage, faces, edges, body['name'])
                segments = sum(max(0, len(x.get('points', [])) - 1) for x in stage['edges'])
                work = len(stage['mesh']['triangles']) * (segments + len(stage['faces']) + len(stage['edges']) + len(stage['points']))
                if work > MAX_DISPLAY_WORK:
                    raise CaptureLimit('实体超过三维显示预算；原始结构已保留')
                stage['stats']['displayWork'] = work
                stage['visible'] = body.get('visible', True)
                stage['features'] = inspected.get('timeline', [])
                stages.append(stage)
            except (GeometryError, CaptureLimit) as exc:
                warnings.append(body['name'] + ': ' + str(exc)[:400])
    all_work = sum(len(s['mesh']['triangles']) for s in stages) * sum(
        sum(max(0, len(e.get('points', [])) - 1) for e in s['edges']) + len(s['faces']) + len(s['edges']) + len(s['points']) for s in stages)
    geometry = {'meta': {'version': version, 'design': inspected.get('name', 'Untitled'),
        'allViewWork': all_work, 'allViewAllowed': all_work <= MAX_DISPLAY_WORK,
        'geometryNote': '带面标签的显示网格；不等同于完整 B-rep。边的显示来自明确标记的网格适配。',
        'vertexNote': 'P 为查看器按边端点坐标生成的显示编号，并非原生拓扑顶点名。',
        'sourceLinks': [], 'sketches': sketches_meta}, 'stages': stages}
    assets = {'document.json': document_bytes, 'inspect.json': json_bytes(inspected),
        'sketches.json': json_bytes({'sketches': sketches, 'warnings': []}),
        'topology.json': json_bytes(topology), 'geometry.json': json_bytes(geometry)}
    if mesh_bytes is not None:
        assets['model.3mf'] = mesh_bytes
    check()
    summary = {'document_hash': hashlib.sha256(document_bytes).hexdigest(), 'document_id': key[0],
        'revision': key[1], 'name': inspected.get('name', document.get('name', 'Untitled')),
        'undo': inspected.get('undo') if source == 'observed' else None,
        'redo': inspected.get('redo') if source == 'observed' else None,
        'timeline': inspected.get('timeline', []), 'marker': document.get('marker'), 'params': inspected.get('params', []),
        'version': version, 'warnings': warnings, 'source': source,
        'completeness': 'partial_geometry' if warnings else 'complete_public_api',
        'unavailable': ['原进程 Undo/Redo 栈条目', '原始 ModelState 对象', '未采到的中间事务', 'Profile 内部解析几何']}
    return assets, summary
