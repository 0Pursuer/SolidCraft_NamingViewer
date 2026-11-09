"""Read-only, bounded reconstruction of *observed* operation relationships.

The public control protocol exposes revisions and stack depths, not native
transactions or stack entries. Every operation returned here is an inference
from immutable documents. Missing observations break the confirmable chain.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

from archive import document_diff

MAX_EVENTS = 200
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_INSPECT_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024
MAX_INPUT_BYTES = 24 * 1024 * 1024
MAX_TREE_NODES = 2000
MAX_FIELDS = 600
MAX_OPERATION_FIELDS = 32
MAX_VALUE_CHARS = 512
MAX_DOCUMENTS = 32

LIMITATIONS = [
    '操作名称、分组与 Undo/Redo 关联由观察推断，原生事务与栈条目接口未提供。',
    '横向链只包含有前后文档及栈深度证据的操作，未采到的步骤单独标为未知。',
    '模型阶段来自保存的公开结构；此图不读取或复用原进程的 ModelState 对象。',
]


def _integer(value):
    return type(value) is int and value >= 0


def _label(ident):
    return f'O{ident:04d}'


def _same(left, right):
    return json.dumps(left, sort_keys=True, separators=(',', ':'), ensure_ascii=False) == json.dumps(right, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _without_marker(document):
    return {key: value for key, value in document.items() if key != 'marker'}


def _depths(snapshot):
    summary = snapshot['summary']
    return tuple(summary.get(key) if _integer(summary.get(key)) else None for key in ('undo', 'redo'))


class _Assets:
    def __init__(self, archive):
        self.archive = archive
        self.used = 0
        self.cache = {}
        self.snapshots = {}
        self.errors = []

    def snapshot(self, ident):
        if ident in self.snapshots:
            return self.snapshots[ident]
        with self.archive._lock:
            dimensions = self.archive._db.execute('SELECT length(CAST(summary AS BLOB)),length(CAST(assets AS BLOB)) FROM snapshots WHERE id=?', (ident,)).fetchone()
            if dimensions is None:
                return None, None
            size = sum(dimensions)
            if size > MAX_METADATA_BYTES or self.used + size > MAX_INPUT_BYTES:
                return None, 'snapshot_metadata_limit'
            row = self.archive._db.execute('SELECT id,document_id,created_at,summary,assets FROM snapshots WHERE id=?', (ident,)).fetchone()
        self.used += size
        try:
            summary = json.loads(row['summary'])
            references = json.loads(row['assets'])
            if not isinstance(summary, dict) or not isinstance(references, dict):
                raise ValueError('invalid metadata')
            # Only retain scalar source metadata needed by the flow. The full
            # native timeline and parameters remain in the ordinary raw view.
            needed = ('document_id', 'document_hash', 'revision', 'name', 'marker', 'undo', 'redo')
            snapshot = {'id': row['id'], 'label': _label(row['id']), 'document_id': row['document_id'],
                        'created_at': row['created_at'], 'summary': {key: summary.get(key) for key in needed}, 'assets': references}
            result = (snapshot, None)
        except (ValueError, TypeError, RecursionError):
            result = (None, 'invalid_snapshot_metadata')
        self.snapshots[ident] = result
        return result

    def json(self, snapshot, name, limit):
        reference = snapshot.get('assets', {}).get(name)
        if not isinstance(reference, dict):
            return None, 'missing_asset'
        digest, size = reference.get('sha256'), reference.get('size')
        if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest) or not _integer(size):
            return None, 'invalid_asset_reference'
        if size > limit:
            return None, 'input_limit'
        if digest in self.cache:
            return self.cache[digest]
        if self.used + size > MAX_INPUT_BYTES:
            return None, 'input_limit'
        try:
            path = self.archive.asset_path(digest)
            # Do not let a corrupted object grow an unbounded read before a hash
            # check. Recorded objects are immutable; still verify both limits.
            with path.open('rb') as source:
                data = source.read(limit + 1)
            self.used += len(data)
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                return None, 'asset_integrity'
            def constant(_):
                raise ValueError('non-finite number')
            value = json.loads(data.decode('utf-8-sig'), parse_constant=constant)
            result = (value, None)
        except (OSError, ValueError, UnicodeError, RuntimeError, RecursionError):
            result = (None, 'unreadable_asset')
        self.cache[digest] = result
        return result


def _geometry(inspected):
    if not isinstance(inspected, dict) or not isinstance(inspected.get('bodies'), list):
        return {'available': False, 'body_count': None, 'bodies': [], 'total_volume_mm3': None}
    bodies = []
    for body in inspected['bodies'][:32]:
        if not isinstance(body, dict):
            continue
        kernel = body.get('kernel') if isinstance(body.get('kernel'), dict) else {}
        bbox = body.get('bbox')
        def numeric(value):
            try:
                return type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                return False
        if (not isinstance(bbox, dict) or any(not isinstance(bbox.get(key), list) or len(bbox[key]) != 3
                or not all(numeric(value) for value in bbox[key]) for key in ('min', 'max'))):
            bbox = None
        else:
            bbox = {key: bbox[key] for key in ('min', 'max')}
        volume = body.get('volume_mm3')
        faces = kernel.get('faces', body.get('faces'))
        edges = kernel.get('edges', body.get('edges'))
        bodies.append({'name': str(body.get('name', '未命名'))[:200],
                       'bbox': bbox, 'volume_mm3': volume if numeric(volume) else None,
                       'faces': faces if _integer(faces) else None,
                       'edges': edges if _integer(edges) else None})
    volumes = [body['volume_mm3'] for body in bodies]
    valid = all(type(value) in (int, float) and math.isfinite(value) for value in volumes)
    total = sum(volumes) if valid else None
    return {'available': True, 'body_count': len(inspected['bodies']), 'bodies': bodies,
            'total_volume_mm3': total if total is not None and math.isfinite(total) else None,
            'truncated': len(inspected['bodies']) > 32}


def _features(document):
    if not isinstance(document, dict) or not isinstance(document.get('features'), list):
        return None
    marker = document.get('marker')
    if marker is not None and (not _integer(marker) or marker > len(document['features'])):
        return None
    output = {}
    for feature in document['features']:
        if not isinstance(feature, dict) or not _integer(feature.get('id')) or feature['id'] in output:
            return None
        output[feature['id']] = feature
    return output


def _rectangular(sketch):
    """Use geometric evidence, never a guessed native command name."""
    points, curves = sketch.get('points'), sketch.get('curves')
    if not isinstance(points, list) or not isinstance(curves, list) or len(curves) != 4:
        return False
    segments = []
    try:
        for curve in curves:
            if curve.get('type') != 'line' or curve.get('construction', False):
                return False
            if not _integer(curve.get('a')) or not _integer(curve.get('b')):
                return False
            a, b = points[curve['a']]['pos'], points[curve['b']]['pos']
            if (not isinstance(a, list) or not isinstance(b, list) or len(a) != 2 or len(b) != 2
                    or not all(type(x) in (int, float) and math.isfinite(x) for x in a + b)):
                return False
            if (a[0] == b[0]) == (a[1] == b[1]):
                return False
            segments.append((tuple(a), tuple(b)))
    except (IndexError, KeyError, TypeError, OverflowError):
        return False
    corners = {point for segment in segments for point in segment}
    return (len(corners) == 4 and len({p[0] for p in corners}) == 2 and len({p[1] for p in corners}) == 2
            and all(sum(p in pair for pair in segments) == 2 for p in corners)
            and len({frozenset(pair) for pair in segments}) == 4)


def _classify(before, after, changes):
    old, new = _features(before), _features(after)
    added = [new[key] for key in new.keys() - old.keys()]
    removed = [old[key] for key in old.keys() - new.keys()]
    if len(added) == 1 and not removed:
        feature = added[0]
        if feature.get('type') == 'sketch':
            return 'create_sketch', '创建草图'
        if feature.get('type') == 'extrude':
            distance = feature.get('extent', {}).get('distance')
            return 'create_extrude', '创建拉伸' + (f' · {distance}' if distance is not None else '')
        return 'create_feature', '创建特征'
    if removed and not added:
        return 'delete_feature', '删除特征定义'
    for ident in old.keys() & new.keys():
        left, right = old[ident], new[ident]
        if right.get('type') == 'extrude' and left.get('extent', {}).get('distance') != right.get('extent', {}).get('distance'):
            return 'edit_extrude_height', '修改拉伸高度'
        if right.get('type') == 'sketch' and not _same(left.get('sketch'), right.get('sketch')):
            previous_curves = left.get('sketch', {}).get('curves', [])
            if not previous_curves and _rectangular(right.get('sketch', {})):
                return 'draw_rectangle', '绘制矩形'
            return 'edit_sketch', '修改草图几何'
    if changes and all(change.get('path', '').startswith('params') for change in changes):
        return 'edit_parameters', '修改参数'
    return 'edit_document', '修改文档定义'


def _preview(value):
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return ({'preview': text[:MAX_VALUE_CHARS], 'truncated': True}, True) if len(text) > MAX_VALUE_CHARS else (value, False)


class _Tree:
    def __init__(self):
        self.nodes = 0
        self.fields = 0

    def node(self, ident, kind, title, **fields):
        if self.nodes >= MAX_TREE_NODES:
            return None
        self.nodes += 1
        return {'id': ident, 'kind': kind, 'title': title, **fields}

    def build(self, operation_id, before, after, changes):
        old, new = _features(before), _features(after)
        groups, root = {}, []
        local_fields, truncated = 0, False

        def field(parent, path, old_value, new_value, change='changed', title=None):
            nonlocal local_fields, truncated
            if self.fields >= MAX_FIELDS or local_fields >= MAX_OPERATION_FIELDS:
                truncated = True
                return
            left, a = _preview(old_value)
            right, b = _preview(new_value)
            node = self.node(f'{operation_id}-field-{local_fields}', 'field', title or path,
                             name=title or path, path=path, old=left, new=right, change=change, truncated=a or b)
            if node is not None:
                parent.append(node)
                local_fields += 1
                self.fields += 1
            else:
                truncated = True

        def feature_group(ident):
            if ident not in groups:
                feature = new.get(ident, old.get(ident, {}))
                node = self.node(f'{operation_id}-feature-{ident}', 'feature',
                                 f'{str(feature.get("name", "未命名"))[:200]} · F{ident}', feature_id=ident,
                                 feature_type=str(feature.get('type', ''))[:80], children=[])
                if node is None:
                    return None
                groups[ident] = node
                root.append(node)
            return groups[ident]

        for change in changes:
            path = str(change.get('path', '$'))
            match = re.match(r'^features\[id=(\d+)\](?:\.(.*))?$', path)
            if match:
                ident = int(match[1])
                parent = feature_group(ident)
                if parent is None:
                    truncated = True
                    break
                target = parent['children']
                suffix = match[2] or ''
                if not suffix and ident not in old and ident in new:
                    feature = new[ident]
                    for key, title in [('type', '新增特征类型'), ('plane', '草图平面'), ('sketch', '草图引用'),
                                       ('profiles', '选中轮廓'), ('extent', '拉伸定义'), ('operation', '实体操作')]:
                        if key not in feature:
                            continue
                        value = feature[key]
                        if key == 'sketch' and isinstance(value, dict):
                            node = self.node(f'{operation_id}-sketch-definition-{ident}', 'group', '草图定义', children=[])
                            if node:
                                target.append(node)
                                for collection, label in [('points', '几何点'), ('curves', '曲线'), ('constraints', '约束')]:
                                    field(node['children'], f'{path}.sketch.{collection}', 0, len(value.get(collection, [])), 'added', f'{label}数量')
                        elif key == 'extent' and isinstance(value, dict):
                            for extent_key, extent_value in value.items():
                                field(target, f'{path}.extent.{extent_key}', None, extent_value, 'added', '拉伸高度' if extent_key == 'distance' else extent_key)
                        else:
                            field(target, f'{path}.{key}', None, value, 'added', title)
                    continue
                group_name = next((title for prefix, title in [('sketch.points', '几何点变化'), ('sketch.curves', '曲线变化'),
                    ('sketch.constraints', '约束变化'), ('extent.', '拉伸参数变化'), ('profiles', '轮廓引用变化')]
                    if suffix.startswith(prefix)), None)
                if group_name:
                    subgroup = next((node for node in target if node['kind'] == 'group' and node['title'] == group_name), None)
                    if subgroup is None:
                        subgroup = self.node(f'{operation_id}-group-{ident}-{len(target)}', 'group', group_name, children=[])
                        if subgroup:
                            target.append(subgroup)
                    if subgroup:
                        target = subgroup['children']
                title = '拉伸高度' if suffix == 'extent.distance' else suffix or '特征定义'
                field(target, path, change.get('old'), change.get('new'), change.get('kind', 'changed'), title)
            else:
                field(root, path, change.get('old'), change.get('new'), change.get('kind', 'changed'))
        return root, truncated


def _metadata(archive, document_id, limit, assets):
    """Narrow bounded reads under the archive lock; never recover or mutate it."""
    with archive._lock:
        rows = archive._db.execute('SELECT document_id,max(id) AS latest,count(*) AS observations FROM snapshots WHERE length(CAST(document_id AS BLOB))<=256 GROUP BY document_id ORDER BY latest DESC LIMIT ?', (MAX_DOCUMENTS,)).fetchall()
        documents = []
        for row in rows:
            snapshot, error = assets.snapshot(row['latest'])
            if isinstance(row['document_id'], str) and 1 <= len(row['document_id']) <= 256:
                documents.append({'id': row['document_id'],
                    'name': str(snapshot['summary'].get('name', 'Untitled'))[:200] if snapshot else '文档（摘要不可获取）',
                    'latest_snapshot_id': row['latest'], 'latest_label': _label(row['latest']),
                    'latest_revision': snapshot['summary'].get('revision') if snapshot else None,
                    'observations': row['observations'], 'metadata_unavailable': bool(error)})
        selected = document_id if document_id is not None else (documents[0]['id'] if documents else None)
        if selected is None:
            return documents, None, [], False
        first = archive._db.execute('SELECT min(id) FROM events WHERE document_id=?', (selected,)).fetchone()[0]
        query = '''SELECT e.id,substr(e.session_id,1,100) AS session_id,e.document_id,e.revision,
                   substr(e.kind,1,80) AS kind,substr(e.status,1,40) AS status,
                   substr(e.message,1,500) AS message,e.observed_at,
                   s.id AS snapshot_id FROM events e LEFT JOIN snapshots s ON s.id=e.id
                   WHERE e.id>=? AND (e.document_id=? OR (e.document_id IS NULL AND e.kind='gap'))
                   ORDER BY e.id DESC LIMIT ?'''
        result = [dict(row) for row in archive._db.execute(query, (first or 0, selected, limit + 1))]
        truncated = len(result) > limit
        return documents, selected, list(reversed(result[:limit])), truncated


def build_flow(archive, document_id=None, limit=MAX_EVENTS):
    """Build a bounded view; it sends no CAD commands and writes no archive rows."""
    if type(limit) is not int or not 1 <= limit <= MAX_EVENTS:
        raise ValueError('flow limit must be an integer from 1 to 200')
    if document_id is not None and (not isinstance(document_id, str) or not 1 <= len(document_id) <= 256):
        raise ValueError('invalid document identity')
    assets = _Assets(archive)
    documents, selected, events, truncated = _metadata(archive, document_id, limit, assets)
    output = {'schema_version': 1, 'provenance': 'observed_inference', 'document_id': selected,
              'documents': documents, 'current': None, 'baseline': None, 'operations': [],
              'undo_observed': [], 'redo_observed': [], 'transitions': [], 'boundaries': [],
              'events': [], 'truncated': truncated, 'limitations': LIMITATIONS[:],
              'input_bytes': 0, 'tree_nodes': 0}
    tree = _Tree()
    operations, applied, undone = {}, [], []
    previous, pending_editor = None, None
    interrupted = False
    latest_complete_event = None
    previous_session = None

    def boundary(kind, message, ids, clear=True):
        nonlocal interrupted, pending_editor
        output['boundaries'].append({'kind': kind, 'label': _label(ids[-1]) if ids else '观察窗口',
                                     'message': message, 'observation_ids': ids, 'unknown': True})
        if clear:
            for ident in applied + undone:
                operations[ident]['status'] = 'unknown'
            applied.clear()
            undone.clear()
            pending_editor = None
            interrupted = True

    def describe(state):
        snapshot, document, geometry = state
        summary = snapshot['summary']
        undo, redo = _depths(snapshot)
        return {'snapshot_id': snapshot['id'], 'label': snapshot['label'], 'revision': summary.get('revision'),
                'document_id': summary.get('document_id'), 'name': str(summary.get('name', 'Untitled'))[:200],
                'document_hash': summary.get('document_hash'), 'marker': document.get('marker'),
                'undo_depth': undo, 'redo_depth': redo, 'geometry': geometry, 'temporary': False,
                'editor_base_snapshot_id': None}

    if truncated:
        boundary('window_truncated', f'仅展示最近 {limit} 条相关通知，更早操作未展开。', [], False)

    for event in events:
        event = {**event, 'label': _label(event['id'])}
        output['events'].append(event)
        snapshot, snapshot_error = assets.snapshot(event['id'])
        if snapshot_error:
            boundary(snapshot_error, '该观察的元数据超过读取预算或不完整，无法确认操作链。', [event['id']])
            continue
        if snapshot is None:
            if event['status'] in {'pending', 'capturing', 'collecting'}:
                boundary('snapshot_pending', '此通知尚在采集，尚不能确定操作或模型结果。', [event['id']], False)
            else:
                boundary('observation_gap', '此处没有一致快照，后续操作链从下一份可用观察重新确认。', [event['id']])
            continue
        if snapshot['summary'].get('document_id') != selected:
            continue
        document, error = assets.json(snapshot, 'document.json', MAX_DOCUMENT_BYTES)
        if error or _features(document) is None:
            boundary(error or 'invalid_document', '该观察的完整设计定义不可读取或超过预算，无法确认事务关系。', [event['id']])
            continue
        inspected, inspect_error = assets.json(snapshot, 'inspect.json', MAX_INSPECT_BYTES)
        geometry = _geometry(inspected)
        if inspect_error:
            boundary('geometry_unavailable', '该观察的模型测量摘要不可获取，定义变化仍可查看。', [event['id']], False)
        state = (snapshot, document, geometry)
        latest_complete_event = event['id']
        output['current'] = describe(state)
        if previous_session is not None and event['session_id'] != previous_session:
            boundary('session_boundary', '记录会话发生变化，间隔内的操作与原生栈条目没有完整采集。', [previous[0]['id'], event['id']])
        previous_session = event['session_id']
        if previous is None or interrupted:
            previous = state
            interrupted = False
            if output['baseline'] is None:
                output['baseline'] = describe(state)
            undo, redo = _depths(snapshot)
            if undo or redo:
                boundary('unobserved_history', f'此处原生深度为 Undo {undo} / Redo {redo}，更早栈条目没有采集。', [snapshot['id']], False)
            continue
        old_snapshot, old_document, old_geometry = previous
        old_hash, new_hash = old_snapshot['summary'].get('document_hash'), snapshot['summary'].get('document_hash')
        old_depth, new_depth = _depths(old_snapshot), _depths(snapshot)
        ids = [snapshot['id']]
        transition = {'from_snapshot_id': old_snapshot['id'], 'to_snapshot_id': snapshot['id'],
                      'observation_ids': ids, 'operation_ids': [], 'inferred': True}

        # Only a marker-only move with identical native depths is an editor
        # candidate. Never fold arbitrary state changes into the next commit.
        marker_only = (not _same(old_document.get('marker'), document.get('marker'))
                       and _same(_without_marker(old_document), _without_marker(document)))
        if marker_only and old_depth == new_depth:
            if pending_editor is None and document.get('marker') is not None:
                pending_editor = {'base': previous, 'ids': [snapshot['id']]}
                transition.update({'kind': 'editor_rollback', 'evidence': ['只有时间线标记变化；原生 Undo/Redo 深度未变。']})
            elif pending_editor is not None:
                base = pending_editor['base']
                if _same(base[1], document):
                    transition.update({'kind': 'editor_restore', 'evidence': ['编辑器恢复原定义，没有新增 Undo 步骤。']})
                    pending_editor = None
                else:
                    pending_editor['ids'].append(snapshot['id'])
                    transition.update({'kind': 'editor_rollback', 'evidence': ['编辑器中继续移动时间线标记。']})
            else:
                transition.update({'kind': 'timeline_position', 'evidence': ['时间线位置变化，未识别为新增 Undo 操作。']})
            output['transitions'].append(transition)
            previous = state
            if pending_editor:
                output['current'].update({'temporary': True, 'editor_base_snapshot_id': pending_editor['base'][0]['id']})
            continue

        base_state = pending_editor['base'] if pending_editor else previous
        base_snapshot, base_document, base_geometry = base_state
        base_depth = _depths(base_snapshot)
        editor_ids = pending_editor['ids'][:] if pending_editor else []
        can_fold = (pending_editor is not None and base_depth[0] is not None and new_depth[0] == base_depth[0] + 1
                    and new_depth[1] == 0 and _same(base_document.get('marker'), document.get('marker')))
        if pending_editor and not can_fold:
            boundary('editor_unresolved', '临时编辑后的变化缺少单步提交证据，无法合并为一个事务。', editor_ids + ids)
            previous = state
            continue
        if can_fold:
            ids = editor_ids + ids
            transition['observation_ids'] = ids
            transition['from_snapshot_id'] = base_snapshot['id']
            old_snapshot, old_document, old_geometry = base_state
            old_depth = base_depth
            old_hash = old_snapshot['summary'].get('document_hash')
        pending_editor = None

        known_depths = all(value is not None for value in old_depth + new_depth)
        undo_steps = old_depth[0] - new_depth[0] if known_depths else 0
        redo_steps = new_depth[0] - old_depth[0] if known_depths else 0
        if (known_depths and undo_steps > 0 and new_depth[1] - old_depth[1] == undo_steps
                and undo_steps <= len(applied)):
            popped = list(reversed(applied[-undo_steps:]))
            target = operations[popped[-1]]['before_document_hash']
            if target == new_hash:
                for ident in popped:
                    applied.pop()
                    undone.append(ident)
                    operations[ident]['status'] = 'undone'
                transition.update({'kind': 'undo', 'operation_ids': popped,
                    'evidence': [f'原生 Undo 深度减少 {undo_steps}、Redo 深度增加 {undo_steps}，目标文档哈希匹配已观察状态。']})
                output['transitions'].append(transition)
                previous = state
                continue
        if (known_depths and redo_steps > 0 and old_depth[1] - new_depth[1] == redo_steps
                and redo_steps <= len(undone)):
            restored = list(reversed(undone[-redo_steps:]))
            if operations[restored[-1]]['after_document_hash'] == new_hash:
                for ident in restored:
                    undone.pop()
                    applied.append(ident)
                    operations[ident]['status'] = 'applied'
                transition.update({'kind': 'redo', 'operation_ids': restored,
                    'evidence': [f'原生 Undo 深度增加 {redo_steps}、Redo 深度减少 {redo_steps}，目标文档哈希匹配。']})
                output['transitions'].append(transition)
                previous = state
                continue
        if old_hash == new_hash and old_depth == new_depth:
            transition.update({'kind': 'observation', 'evidence': ['文档字节和栈深度相同，仅新增一次观察。']})
            output['transitions'].append(transition)
            previous = state
            continue

        changes = document_diff(old_document, document)
        if not changes:
            boundary('unmatched_history', '栈深度发生变化，但没有可对应的完整已观察操作。', ids)
            transition.update({'kind': 'boundary', 'evidence': ['不根据深度差伪造原生栈条目。']})
            output['transitions'].append(transition)
            previous = state
            continue
        kind, title = _classify(old_document, document, changes)
        ident = f'op-{snapshot["id"]}'
        nodes, tree_truncated = tree.build(ident, old_document, document, changes)
        single_step = (known_depths and new_depth[0] == old_depth[0] + 1 and new_depth[1] == 0)
        operation = {'id': ident, 'title': title, 'kind': kind, 'inferred': True,
                     'status': 'applied' if single_step else 'unknown',
                     'before_snapshot_id': old_snapshot['id'], 'after_snapshot_id': snapshot['id'],
                     'before_document_hash': old_hash, 'after_document_hash': new_hash,
                     'observation_ids': ids, 'labels': [_label(item) for item in ids],
                     'before_label': old_snapshot['label'], 'after_label': snapshot['label'],
                     'undo_before': old_depth[0], 'undo_after': new_depth[0],
                     'redo_before': old_depth[1], 'redo_after': new_depth[1],
                     'tree': nodes, 'tree_truncated': tree_truncated,
                     'geometry': {'before': old_geometry, 'after': geometry},
                     'evidence': ['由前后不可变 Document 的字段差异推断操作名称。'],
                     'editor_observation_ids': editor_ids,
                     'native_transaction_available': False}
        if can_fold:
            operation['evidence'].append('临时 marker 回退与后续提交合并；最终 Undo 深度仅增加 1。')
        if single_step:
            operation['evidence'].append('原生 Undo 深度增加 1，Redo 深度为 0。')
            if undone:
                for prior in undone:
                    operations[prior]['status'] = 'discarded'
                operation['discarded_redo_operation_ids'] = undone[:]
                undone.clear()
            applied.append(ident)
        else:
            boundary('unconfirmed_step_count', '定义发生变化，但无法确认它恰好对应一个原生 Undo 步骤。', ids)
        operations[ident] = operation
        output['operations'].append(operation)
        transition.update({'kind': 'edit', 'operation_ids': [ident], 'evidence': operation['evidence'][:]})
        output['transitions'].append(transition)
        previous = state

    output['undo_observed'] = applied
    output['redo_observed'] = undone
    output['input_bytes'], output['tree_nodes'] = assets.used, tree.nodes
    if output['current']:
        current = output['current']
        current['unobserved_undo_depth'] = max(0, current['undo_depth'] - len(applied)) if current['undo_depth'] is not None else None
        current['unobserved_redo_depth'] = max(0, current['redo_depth'] - len(undone)) if current['redo_depth'] is not None else None
        current['is_latest_observation'] = bool(events and latest_complete_event == events[-1]['id'])
        current['latest_event_id'] = events[-1]['id'] if events else None
        current['history_chain_known'] = not interrupted
    if pending_editor:
        output['pending_edit'] = {'base_snapshot_id': pending_editor['base'][0]['id'],
                                  'observation_ids': pending_editor['ids'][:], 'status': 'editing'}
    return output
