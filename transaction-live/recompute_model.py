"""Bounded, read-only evidence and source explanation for SolidCraft recomputation.

This module never executes CAD commands. Definition comparisons are observations;
the evaluator steps describe v0.30.1 source, not a trace of the native process.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import re

from archive import document_diff
import flow_model

MAX_EVENTS = 200
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_INSPECT_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024
MAX_INPUT_BYTES = 12 * 1024 * 1024
MAX_FEATURES = 500
MAX_CHANGES = 400
SOURCE_VERSION = '0.30.1'
SOURCE_COMMIT = '8e2b0cd'
_BASE = 'https://github.com/tangency-labs/solidcraft/blob/v0.30.1/'


def _source(ident, path, line, end, title):
    return {'id': ident, 'file': path, 'line': line, 'end_line': end,
            'source_url': f'{_BASE}{path}#L{line}-L{end}', 'title': title}


SOURCES = [
    _source('shared-entry', 'crates/doc/src/placement_reuse.rs', 14, 38, 'evaluate_shared 入口短路'),
    _source('references', 'crates/doc/src/feature_refs.rs', 1, 91, '类型化引用与统一重映射'),
    _source('extrude-reference', 'crates/doc/src/document.rs', 604, 618, 'Extrude 的草图与轮廓引用'),
    _source('fingerprint', 'crates/doc/src/eval.rs', 1361, 1379, '局部特征指纹'),
    _source('chain-fingerprint', 'crates/doc/src/eval.rs', 1572, 1627, '链指纹'),
    _source('cache-key', 'crates/doc/src/eval.rs', 1652, 1697, '缓存键与输入状态'),
    _source('timeline-prefix', 'crates/doc/src/eval.rs', 2194, 2268, '按时间线遍历与未变前缀'),
    _source('skip', 'crates/doc/src/eval.rs', 2270, 2285, '抑制和历史标记之后的特征'),
    _source('failed-source', 'crates/doc/src/eval.rs', 2289, 2317, '来源草图失败检查'),
    _source('cache-lookup', 'crates/doc/src/eval.rs', 2319, 2359, 'cache、kept、rolled 与 primed'),
    _source('execute-result', 'crates/doc/src/eval.rs', 2361, 2492, '执行特征、保留结果与状态'),
    _source('undo-snapshot', 'crates/engine/src/lib.rs', 280, 290, 'Undo Snapshot 保存定义与编辑上下文'),
    _source('undo-go-to', 'crates/engine/src/cmd/edit.rs', 114, 171, 'Undo/Redo 恢复定义'),
    _source('engine-refresh', 'crates/engine/src/lib.rs', 1090, 1186, 'Session 刷新与求值入口'),
    _source('edit-counters', 'crates/engine/src/cmd/edit.rs', 1224, 1239, 'timeline.edit 的整体计数'),
    _source('compute-all', 'crates/engine/src/cmd/edit.rs', 189, 194, 'Compute All 创建新 Model 再刷新'),
    _source('inspect-limits', 'crates/engine/src/cmd/inspect.rs', 340, 385, '公开观察字段'),
]
_SOURCES = {source['id']: source for source in SOURCES}

LIMITATIONS = [
    '源码说明固定为 SolidCraft v0.30.1（8e2b0cd）；不是现场进程的求值日志。',
    '现场公开观察没有逐特征的缓存分支、Rust 指纹或原始 ModelState；本页不伪造这些值。',
    'ms 是保存的 FeatureResult 耗时；相同或不同的 ms 都不能单独证明本次缓存命中或重新执行。',
    '引用图只解析已核验的 Extrude.sketch 字段，是原生 Walk 引用的子集；原点 XY 不是伪造的 Feature 节点。',
    '时间线按 doc.features 顺序评估；显示的引用连线不代表 Kahn 拓扑排序队列。',
    'S0…Sn 是累计模型阶段的示意名称；未取得的阶段数据不冒充重建结果，也不代表现场原始 ModelState 地址或缓存身份。',
    'timeline.edit 可返回整体 recomputed/restored；未变化前缀复用不计入 restored，本页不从旧观察补造计数。',
]


def _step(ident, source, title, description):
    return {**_SOURCES[source], 'id': ident, 'title': title, 'description': description,
            'provenance': 'source_explanation', 'runtime_observed': False}


def _steps():
    return [
        _step('restore-or-edit', 'undo-go-to', '编辑定义或 Undo 恢复定义',
              '普通编辑改变 Feature 定义。Undo/Redo 在两个栈之间交换 Snapshot，恢复 Document 和编辑上下文；Snapshot 不保存完整 ModelState。'),
        _step('refresh', 'engine-refresh', '进入 Session::refresh',
              '普通几何变化进入 evaluate_shared；专门的名称、显示或装配路径可能有额外短路，此处没有现场调用追踪。'),
        _step('shared', 'shared-entry', '先检查整体模型复用证明',
              'evaluate_shared 对支持的放置变化可能直接复用模型，否则进入普通时间线求值；不能只凭文档相等判断这条分支。'),
        _step('chain', 'chain-fingerprint', '计算链指纹',
              'fingerprint 纳入前缀值、rolled_back、设计长度单位、Feature JSON、直接使用参数的实值和维度；chain_fingerprint 还纳入放置摘要及相关规则和重放源。真实 Rust 值未导出，文档 SHA256 不是该指纹。'),
        _step('prefix', 'timeline-prefix', '按时间线顺序检查未变前缀',
              '循环按 doc.features 的顺序检查 ID、链指纹和 stale 条件；未变连续前缀可直接沿用旧 FeatureResult 的 state。这不是 Kahn 调度，也不增加 restored。'),
        _step('skip', 'skip', '检查抑制与历史标记',
              '特征被抑制或位于 marker 之后时跳过执行，把当前输入状态继续传给下一步。这里的标记来自记录，不代表已观察到此次运行分支。'),
        _step('source-failure', 'failed-source', '检查来源草图是否失败',
              '使用失败草图的特征先生成相应错误，保留输入状态；这类失败不会冒充正常缓存命中。'),
        _step('key', 'cache-key', '组合候选缓存键',
              '键涉及局部特征输入、文档上下文、输入 ModelState.digest、特征 ID；特定重放特征还纳入 chain_fp。本页不在 Python 中仿造这些值。'),
        _step('lookup', 'cache-lookup', '检查 cache、kept、rolled / primed',
              '依次尝试有界缓存、额外保留结果，以及保留的回滚/预计算结果；只有源码要求的输入匹配才可恢复 state。旧观察无法告诉本次走了哪个分支。'),
        _step('execute', 'execute-result', '未复用时执行 eval_feature',
              '克隆输入状态为 next，执行特征。成功时保存新的 Arc<ModelState>；失败时保留输入 state 并记录错误。FeatureResult 保存 state、错误、警告和耗时，并维护相应计数与缓存。'),
    ]


def _integer(value):
    return type(value) is int and value >= 0


def _same(left, right):
    return json.dumps(left, sort_keys=True, separators=(',', ':'), ensure_ascii=False) == json.dumps(right, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _pairs(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ValueError('JSON 包含重复键')
        output[key] = value
    return output


def _parse(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError('文档超过 2 MiB 读取限制')
    def constant(_):
        raise ValueError('JSON 包含非有限数值')
    try:
        document = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_pairs, parse_constant=constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('文档不是有效的有界 JSON') from exc
    if (not isinstance(document, dict) or not re.fullmatch(r'solidcraft/[0-9]+', str(document.get('format', '')))
            or 'commands' in document or not isinstance(document.get('features'), list)):
        raise ValueError('需要完整的原生 SolidCraft Document JSON')
    features = document['features']
    if len(features) > MAX_FEATURES:
        raise ValueError('时间线超过 500 个特征的展示限制')
    ids = []
    for feature in features:
        if not isinstance(feature, dict) or not _integer(feature.get('id')) or feature['id'] == 0:
            raise ValueError('特征 ID 无效')
        ids.append(feature['id'])
    if len(ids) != len(set(ids)):
        raise ValueError('特征 ID 重复')
    marker = document.get('marker')
    if marker is not None and (not _integer(marker) or marker > len(features)):
        raise ValueError('历史标记超出时间线范围')
    return document


def validate_replay_documents(before: bytes, after: bytes) -> dict:
    """The lab's shared allowlist: only one extrude distance may differ.

    Returns the native edit arguments, or raises ValueError. No command, path,
    reference or unknown field is silently rewritten to make a pair acceptable.
    """
    left, right = _parse(before), _parse(after)
    for document in (left, right):
        if document.get('links'):
            raise ValueError('含有未冻结的外部设计链接，拒绝隔离重放')
        if document.get('marker') not in (None, len(document['features'])):
            raise ValueError('隔离重放要求历史标记位于时间线末尾')
    if len(left['features']) != len(right['features']):
        raise ValueError('隔离重放只支持一个既有拉伸的距离变化')
    changed = [(a, b) for a, b in zip(left['features'], right['features']) if not _same(a, b)]
    if len(changed) != 1:
        raise ValueError('需要且只能有一个拉伸距离变化')
    old, new = changed[0]
    if old.get('type') != 'extrude' or new.get('type') != 'extrude' or old['id'] != new['id']:
        raise ValueError('发生变化的对象必须是同一个拉伸特征')
    old_extent, new_extent = old.get('extent'), new.get('extent')
    if not isinstance(old_extent, dict) or not isinstance(new_extent, dict):
        raise ValueError('拉伸缺少 extent 定义')
    a, b = old_extent.get('distance'), new_extent.get('distance')
    if not isinstance(a, str) or not isinstance(b, str) or a == b or len(a) > 512 or len(b) > 512:
        raise ValueError('需要两个不同的原生距离表达式')
    expected = copy.deepcopy(left)
    slot = next(feature for feature in expected['features'] if feature['id'] == old['id'])
    slot['extent']['distance'] = b
    if not _same(expected, right):
        raise ValueError('除拉伸 extent.distance 外还有其他 Document 字段变化')
    return {'feature_id': old['id'], 'feature_name': str(old.get('name', ''))[:200],
            'before_distance': a, 'after_distance': b}


class _Cursor:
    def __init__(self, cursor, budget):
        self.cursor, self.budget = cursor, budget

    def fetchone(self):
        row = self.cursor.fetchone()
        if row is not None:
            self.budget.metadata(row)
        return row


class _Database:
    def __init__(self, archive, budget):
        self.db, self.budget = archive._db, budget

    def execute(self, query, arguments=()):
        if 'SELECT id,document_id,created_at,summary,assets FROM snapshots WHERE id=' in query:
            if arguments[0] not in self.budget.metadata_ids:
                sizes = self.db.execute('SELECT length(CAST(summary AS BLOB)),length(CAST(assets AS BLOB)) FROM snapshots WHERE id=?', arguments).fetchone()
                if sizes and (sum(sizes) > MAX_METADATA_BYTES or self.budget.used + sum(sizes) > MAX_INPUT_BYTES):
                    raise RuntimeError('观察元数据超过读取预算')
            cursor = self.db.execute(query, arguments)
            return _Cursor(cursor, self.budget)
        return self.db.execute(query, arguments)


class _Asset:
    def __init__(self, budget, digest):
        self.budget, self.digest = budget, digest

    def open(self, mode):
        if mode != 'rb':
            raise RuntimeError('read-only archive view')
        return io.BytesIO(self.budget.raw(self.digest))


class _Budget:
    """Shared budget for flow inference, selected documents, and inspect data."""
    def __init__(self, archive):
        self.archive, self.used = archive, 0
        self.contents, self.metadata_ids = {}, set()

    def metadata(self, row):
        if row['id'] in self.metadata_ids:
            return
        size = sum(len(row[key].encode('utf-8')) for key in ('summary', 'assets'))
        if size > MAX_METADATA_BYTES or self.used + size > MAX_INPUT_BYTES:
            raise ValueError('观察元数据超过读取预算')
        self.used += size
        self.metadata_ids.add(row['id'])

    def raw(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('资源哈希无效')
        if digest not in self.contents:
            if self.used >= MAX_INPUT_BYTES:
                raise RuntimeError('读取预算已用尽')
            path = self.archive.asset_path(digest)
            bound = min(MAX_DOCUMENT_BYTES, MAX_INPUT_BYTES - self.used)
            if path.stat().st_size > bound:
                raise RuntimeError('资源超过读取预算')
            with path.open('rb') as source:
                raw = source.read(bound)
            self.used += len(raw)
            if path.stat().st_size != len(raw) or hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError('归档资源完整性检查失败')
            self.contents[digest] = raw
        return self.contents[digest]

    def snapshot(self, ident):
        with self.archive._lock:
            dimensions = self.archive._db.execute('SELECT length(CAST(summary AS BLOB)),length(CAST(assets AS BLOB)) FROM snapshots WHERE id=?', (ident,)).fetchone()
            if dimensions is None:
                raise ValueError('观察记录不存在')
            if sum(dimensions) > MAX_METADATA_BYTES or (ident not in self.metadata_ids and self.used + sum(dimensions) > MAX_INPUT_BYTES):
                raise ValueError('观察元数据超过 256 KiB 限制')
            row = self.archive._db.execute('SELECT id,document_id,created_at,summary,assets FROM snapshots WHERE id=?', (ident,)).fetchone()
            self.metadata(row)
        summary, assets = json.loads(row['summary']), json.loads(row['assets'])
        if not isinstance(summary, dict) or not isinstance(assets, dict):
            raise ValueError('观察元数据不是对象')
        return {**dict(row), 'summary': summary, 'assets': assets}

    def asset(self, snapshot, name, bound):
        ref = snapshot['assets'].get(name)
        if not isinstance(ref, dict) or not _integer(ref.get('size')) or ref['size'] > bound:
            raise ValueError(f'{name} 缺失或超过读取限制')
        raw = self.raw(ref.get('sha256'))
        if len(raw) != ref['size']:
            raise ValueError('资源长度与元数据不符')
        return raw


class _View:
    def __init__(self, archive, budget):
        self._lock, self._db, self.budget = archive._lock, _Database(archive, budget), budget

    def asset_path(self, digest):
        return _Asset(self.budget, digest)


def _preview(value):
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return {'preview': text[:512], 'truncated': True} if len(text) > 512 else value


def _result(inspect, ident):
    if not isinstance(inspect, dict) or not isinstance(inspect.get('timeline'), list):
        return {'available': False}
    for row in inspect['timeline'][:MAX_FEATURES]:
        if isinstance(row, dict) and type(row.get('id')) is int and row['id'] == ident:
            return {'available': True, **{key: _preview(row[key]) for key in (
                'id', 'name', 'type', 'suppressed', 'rolled_back', 'ms', 'error', 'warning', 'boolean_diagnostics') if key in row}}
    return {'available': False}


def _load_state(budget, ident):
    snapshot = budget.snapshot(ident)
    raw = budget.asset(snapshot, 'document.json', MAX_DOCUMENT_BYTES)
    document = _parse(raw)
    error, inspected = None, None
    try:
        inspected = json.loads(budget.asset(snapshot, 'inspect.json', MAX_INSPECT_BYTES))
    except (ValueError, OSError, RuntimeError, RecursionError) as exc:
        error = str(exc)[:300]
    return snapshot, document, raw, inspected, error


def _describe(state):
    snapshot, document, raw, inspected, error = state
    summary = snapshot['summary']
    return {'id': snapshot['id'], 'label': f'O{snapshot["id"]:04d}',
            'document_id': snapshot['document_id'], 'doc_hash': hashlib.sha256(raw).hexdigest(),
            'revision': summary.get('revision'), 'undo': summary.get('undo'), 'redo': summary.get('redo'),
            'marker': document.get('marker'), 'geometry': flow_model._geometry(inspected),
            'geometry_error': error}


def _feature_rows(before, after):
    old = {feature['id']: (i, feature) for i, feature in enumerate(before[1]['features'])}
    new = {feature['id']: (i, feature) for i, feature in enumerate(after[1]['features'])}
    order = list(new) + [ident for ident in old if ident not in new]
    output, remaining = [], MAX_CHANGES
    for ident in order:
        left = old.get(ident)
        right = new.get(ident)
        feature = right[1] if right else left[1]
        changes = document_diff(left[1] if left else None, right[1] if right else None)
        truncated = len(changes) > remaining
        changes = changes[:remaining]
        remaining -= len(changes)
        references = []
        if feature.get('type') == 'extrude' and _integer(feature.get('sketch')) and feature['sketch'] > 0:
            references.append({'kind': 'sketch', 'target_feature_id': feature['sketch'], 'field': 'sketch',
                'source_url': _SOURCES['extrude-reference']['source_url'], 'provenance': 'saved_definition'})
        position = right[0] if right else left[0]
        marker = after[1].get('marker')
        output.append({'id': ident, 'name': str(feature.get('name', ''))[:200], 'type': str(feature.get('type', ''))[:80],
            'position': position, 'position_before': left[0] if left else None, 'position_after': right[0] if right else None,
            'suppressed': bool(feature.get('suppressed', False)), 'rolled_back': bool(right and marker is not None and position >= marker),
            'removed': right is None, 'added': left is None, 'definition_changed': not _same(left[1] if left else None, right[1] if right else None),
            'changes': changes, 'changes_truncated': truncated, 'references': references,
            'before_result': _result(before[3], ident), 'after_result': _result(after[3], ident),
            'runtime_cache_branch': 'unavailable'})
    return output


def build_recompute(archive, before_id=None, after_id=None) -> dict:
    """Read at most 200 recent observations and 12 MiB of immutable input.

    Default selection follows a known Undo/Redo operation back to its original
    edit pair. Explicit IDs compare precisely those observations, without
    silently substituting another version or another document.
    """
    if (before_id is None) != (after_id is None):
        raise ValueError('before 和 after 必须同时指定')
    if before_id is not None and (not _integer(before_id) or not _integer(after_id) or not before_id or not after_id):
        raise ValueError('观察 ID 必须为正整数')
    output = {'schema_version': 1, 'available': False, 'source_version': SOURCE_VERSION, 'source_commit': SOURCE_COMMIT,
        'provenance': 'definition_comparison_and_source_explanation', 'before': None, 'after': None,
        'features': [], 'steps': _steps(), 'sources': copy.deepcopy(SOURCES), 'limitations': LIMITATIONS[:],
        'lab_compatible': False, 'lab_reason': '没有可比较的观察记录', 'observations': [],
        'related_operation': None, 'selection_reason': None, 'input_bytes': 0, 'window_limit': MAX_EVENTS,
        'runtime_cache_branch': 'unavailable', 'references_complete': False}
    budget = _Budget(archive)
    try:
        with archive._lock:
            rows = archive._db.execute('SELECT id,substr(document_id,1,256) AS document_id,created_at FROM snapshots ORDER BY id DESC LIMIT ?', (MAX_EVENTS,)).fetchall()
        options = [dict(row) for row in rows]
        budget.used += len(json.dumps(options, ensure_ascii=False).encode('utf-8'))
        output['observations'] = [{**row, 'label': f'O{row["id"]:04d}'} for row in options]
        if before_id is None:
            if not options:
                return output
            identity = options[0]['document_id']
            candidates = [row['id'] for row in options if row['document_id'] == identity]
            if len(candidates) < 2:
                output['lab_reason'] = '同一文档至少需要两份一致观察'
                return output
            # Reserve the fallback pair before the bounded flow scan reads older assets.
            before_id, after_id = candidates[1], candidates[0]
            _load_state(budget, before_id)
            _load_state(budget, after_id)
            try:
                flow = flow_model.build_flow(_View(archive, budget), document_id=identity, limit=MAX_EVENTS)
                operations = {item['id']: item for item in flow['operations']}
                related = None
                if flow['transitions']:
                    transition = flow['transitions'][-1]
                    for operation_id in reversed(transition.get('operation_ids', [])):
                        if operation_id in operations:
                            related = operations[operation_id]
                            break
                if related is None:
                    related = next((item for item in reversed(flow['operations']) if item.get('kind') == 'edit_extrude_height'), None)
                if related is not None:
                    before_id, after_id = related['before_snapshot_id'], related['after_snapshot_id']
                    output['related_operation'] = {key: related.get(key) for key in ('id', 'kind', 'title', 'status', 'before_snapshot_id', 'after_snapshot_id')}
                    output['selection_reason'] = 'observed_operation_original_edit_pair'
                else:
                    output['selection_reason'] = 'latest_two_snapshots_same_document'
            except (ValueError, OSError, RuntimeError, RecursionError, TypeError):
                output['selection_reason'] = 'latest_two_snapshots_flow_unavailable'
        else:
            output['selection_reason'] = 'explicit_observation_pair'
        before, after = _load_state(budget, before_id), _load_state(budget, after_id)
        if before[0]['document_id'] != after[0]['document_id']:
            raise ValueError('不能把不同文档的观察拼成一次重算')
        output.update({'available': True, 'before': _describe(before), 'after': _describe(after),
                       'features': _feature_rows(before, after)})
        try:
            output['lab_edit'] = validate_replay_documents(before[2], after[2])
            output['lab_compatible'], output['lab_reason'] = True, '已核验：同一既有拉伸仅 extent.distance 变化，标记在末尾且没有外部设计链接'
        except ValueError as exc:
            output['lab_reason'] = str(exc)[:300]
    except (ValueError, OSError, RuntimeError, RecursionError, TypeError) as exc:
        output['error'] = str(exc)[:300]
        output['lab_reason'] = output['error']
    finally:
        output['input_bytes'] = budget.used
    return output
