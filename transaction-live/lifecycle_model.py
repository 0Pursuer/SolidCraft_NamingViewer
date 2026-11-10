"""Source-backed Session/Document/Model/Snapshot walkthrough.

Only completed exported CLI phases are observations. Intermediate call frames
describe fixed v0.30.1 source and never pretend to inspect Rust objects or stacks.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

SOURCE_VERSION = '0.30.1'
SOURCE_COMMIT = '8e2b0cd'
MAX_ASSET_BYTES = 2 * 1024 * 1024
MAX_INPUT_BYTES = 12 * 1024 * 1024
_BASE = 'https://github.com/tangency-labs/solidcraft/blob/v0.30.1/'


def _source(ident, file, line, end, title):
    return {'id': ident, 'title': title, 'file': file, 'line': line,
            'source_url': f'{_BASE}{file}#L{line}-L{end}'}


SOURCES = [
    _source('session', 'crates/engine/src/lib.rs', 434, 445, 'Session owns the document, evaluator and history'),
    _source('snapshot', 'crates/engine/src/lib.rs', 280, 290, 'Snapshot contains a Document and editing context'),
    _source('document', 'crates/doc/src/document.rs', 2886, 2911, 'Document contains parameters, ordered features and marker'),
    _source('doc-mut', 'crates/engine/src/lib.rs', 944, 952, 'Document copy on write'),
    _source('execute-before', 'crates/engine/src/lib.rs', 1460, 1503, 'Find command and capture before Snapshot'),
    _source('execute-commit', 'crates/engine/src/lib.rs', 1547, 1582, 'Commit undoable change and finish refresh'),
    _source('execute-failure', 'crates/engine/src/lib.rs', 1585, 1594, 'Restore before on a failed change'),
    _source('edit', 'crates/engine/src/cmd/edit.rs', 1232, 1239, 'Write the feature, refresh, return immediate counts'),
    _source('refresh', 'crates/engine/src/lib.rs', 1090, 1097, 'Session refresh calls evaluate_shared'),
    _source('outer-refresh', 'crates/engine/src/lib.rs', 1154, 1186, 'Finish an ordinary geometry change with refresh_from'),
    _source('shared', 'crates/doc/src/placement_reuse.rs', 14, 38, 'Shared model proof can avoid another evaluation'),
    _source('model', 'crates/doc/src/eval.rs', 1278, 1337, 'FeatureResult, Model and saved cumulative states'),
    _source('evaluate', 'crates/doc/src/eval.rs', 2195, 2492, 'Sequential feature evaluation and reuse decisions'),
    _source('undo', 'crates/engine/src/cmd/edit.rs', 114, 171, 'Undo pops a Snapshot, go_to restores it, Redo keeps the current definition'),
    _source('undo-registration', 'crates/engine/src/cmd/edit.rs', 21, 22, 'Undo and Redo are noundo commands'),
    _source('compute-all', 'crates/engine/src/cmd/edit.rs', 189, 194, 'Compute All replaces Model and refreshes'),
    _source('inspect', 'crates/engine/src/cmd/inspect.rs', 340, 385, 'Only public phase observations are exported'),
]
_SOURCES = {source['id']: source for source in SOURCES}

OBJECTS = [
    {'id': 'session', 'title': 'Session · 总协调', 'type': 'Session',
     'description': '持有当前 Document、Model、Undo/Redo 栈和编辑上下文；命令规则来自注册表。',
     'fields': [{'name': 'doc', 'type': 'Arc<Document>', 'description': '当前定义'},
                {'name': 'model', 'type': 'Model', 'description': '当前求值器'},
                {'name': 'undo / redo', 'type': 'Vec<Snapshot>', 'description': '编辑历史栈'}],
     'source_url': _SOURCES['session']['source_url']},
    {'id': 'document', 'title': 'Document · 建模定义', 'type': 'Arc<Document>',
     'description': '定义特征、参数、引用与时间线顺序。修改通过 doc_mut 的写时复制保留历史定义。',
     'fields': [{'name': 'features', 'type': 'Vec<Feature>', 'description': '时间线和各特征定义'},
                {'name': 'features[F].extent.distance', 'type': 'expression (JSON)', 'description': '本次拉伸定义中的编辑路径'}],
     'source_url': _SOURCES['document']['source_url']},
    {'id': 'model', 'title': 'Model · 求值与复用', 'type': 'Model',
     'description': '按时间线求值，持有 FeatureResult、累计 ModelState 和缓存；不是 Document 中的定义。',
     'fields': [{'name': 'results', 'type': 'Vec<FeatureResult>', 'description': '各特征结果'},
                {'name': 'results[].state', 'type': 'Arc<ModelState>', 'description': 'FeatureResult 引用的累计状态'},
                {'name': 'cache', 'type': 'Arc<Mutex<EvalCache>>', 'description': '满足输入条件时复用的结果'}],
     'source_url': _SOURCES['model']['source_url']},
    {'id': 'snapshot', 'title': 'Snapshot · 历史定义', 'type': 'Snapshot',
     'description': '保存 label、Arc<Document>、活动草图、待提交移动和可见性。没有完整 ModelState 字段。',
     'fields': [{'name': 'doc', 'type': 'Arc<Document>', 'description': '历史定义'},
                {'name': 'label', 'type': 'String', 'description': '历史步骤名称'},
                {'name': 'editing context', 'type': 'context', 'description': 'active_sketch / pending_moves / visibility'}],
     'source_url': _SOURCES['snapshot']['source_url']},
]
RELATIONS = [
    {'id': 'session_document', 'from': 'session', 'to': 'document', 'label': 'doc · 当前定义', 'source_url': _SOURCES['session']['source_url']},
    {'id': 'session_model', 'from': 'session', 'to': 'model', 'label': 'model · 当前求值器', 'source_url': _SOURCES['session']['source_url']},
    {'id': 'session_undo_snapshot', 'from': 'session', 'to': 'snapshot', 'label': 'undo · Vec<Snapshot>', 'source_url': _SOURCES['session']['source_url']},
    {'id': 'session_redo_snapshot', 'from': 'session', 'to': 'snapshot', 'label': 'redo · Vec<Snapshot>', 'source_url': _SOURCES['session']['source_url']},
    {'id': 'snapshot_document', 'from': 'snapshot', 'to': 'document', 'label': 'doc · 历史定义引用', 'source_url': _SOURCES['snapshot']['source_url']},
    {'id': 'document_model', 'from': 'document', 'to': 'model', 'label': 'refresh 时作为求值输入', 'source_url': _SOURCES['refresh']['source_url']},
]
LIMITATIONS = [
    '调用帧依据固定 v0.30.1 源码展开，不是逐函数运行 trace；分支、时序和内部对象地址没有被现场探针采集。',
    '只有完整导出的 baseline / edited / undone 阶段是独立 CLI 实测；这些阶段属于文档副本，不是原生窗口的 Session。',
    'Snapshot 内容和 Undo/Redo 栈条目未由接口提供。before.doc、Undo.doc、Redo.doc 是角色示意，不是读取到的对象或地址。',
    'Snapshot 保存定义与编辑上下文；恢复定义后刷新 Model，不等于直接从 Snapshot 恢复完整 ModelState。',
    '逐特征缓存命中、内部指纹和 ModelState 对象未提供；只有 timeline.edit 的即时回复提供整体 recomputed/restored。',
    'timeline.edit 内部刷新之后，execute 还会进行收尾刷新；evaluate_shared 可以复用，不能据此断言几何计算了两遍。',
]


def _field(name, label, value, provenance='source_explanation'):
    return {'name': name, 'label': label, 'value': value, 'provenance': provenance}


def _data(height, feature_id, action, role, snapshot_height, location, evaluation):
    return {
        'session': {'fields': [_field('undo_depth', 'Undo 深度', None, 'unavailable'),
                              _field('redo_depth', 'Redo 深度', None, 'unavailable'),
                              _field('history_action', '源码中的动作', action)],
                    'note': '中间调用阶段未采样；不编造该时刻的实际栈深度。'},
        'document': {'fields': [_field('feature_id', '拉伸特征 ID', feature_id, 'saved_definition'),
                               _field('height', 'extent.distance', height, 'source_explanation'),
                               _field('document_identity', '文档引用', 'Arc<Document>；地址未提供', 'unavailable')],
                     'note': '高度按已选观察的定义说明该步骤；不声称读取到中间 Rust 对象。'},
        'model': {'fields': [_field('evaluation', '源码中的动作', evaluation),
                            _field('runtime_cache_branch', '实际复用分支', None, 'unavailable'),
                            _field('model_state_identity', '原始 ModelState 对象', None, 'unavailable'),
                            _field('recomputed', '重算总数', None, 'unavailable'),
                            _field('restored', '恢复总数', None, 'unavailable')],
                  'note': '累计状态、指纹和候选缓存由 Model 管理；本帧只解释机制。'},
        'snapshot': {'fields': [_field('role', '快照角色（示意）', role),
                               _field('doc_height', '示意快照中的定义', snapshot_height),
                               _field('location', '源码中的位置（示意）', location),
                               _field('history_available', '实际栈条目是否取得', False, 'unavailable')],
                     'note': '快照只保存定义与编辑上下文；不包含完整 ModelState。角色文字不是实际对象标识。'},
    }


def _frame(ident, title, call, source, objects, relations, description, data):
    return {'id': ident, 'title': title, 'call': call,
            'source_url': _SOURCES[source]['source_url'],
            'active_objects': objects, 'active_relations': relations,
            'description': description, 'provenance': 'source_explanation',
            'availability': 'explanation', 'phase_index': None, 'data': data}


def _frames(spec):
    old, new, ident = spec.get('before_distance'), spec.get('after_distance'), spec.get('feature_id')
    def data(height=old, action='未执行历史操作', role='尚未生成本次 before', snap=None, location='仅说明类型', evaluation='当前求值结果'):
        return _data(height, ident, action, role, snap, location, evaluation)
    return [
        _frame('baseline', '修改前：读取当前定义与模型', 'Session.doc / Session.model', 'session',
               ['session', 'document', 'model'], ['session_document', 'session_model'],
               '先区分定义与结果：Document 保存拉伸距离；Model 保存各特征求值后的累计状态。', data()),
        _frame('before_snapshot', '进入命令：保留 before Snapshot', 'Session::execute → before = Snapshot { … }', 'execute-before',
               ['session', 'snapshot', 'document'], ['session_document', 'snapshot_document'],
               '统一入口先查找命令规则，保留当前 Document 的 Arc 引用和编辑上下文。before 是命令内部临时快照，尚未登记到 Undo。',
               data(action='保存修改前定义及上下文', role='before.doc（角色示意）', snap=old, location='execute 的局部 before')),
        _frame('write_document', '修改 Document 中的拉伸定义', 'timeline.edit → Session::doc_mut → feature_mut', 'edit',
               ['document', 'session', 'snapshot'], ['session_document', 'snapshot_document'],
               'doc_mut 使用写时复制；当前特征的 extent.distance 改变，before.doc 仍代表修改前定义。对象是否分配及其地址未导出。',
               data(new, '尚未提交 Undo', 'before.doc（角色示意）', old, '局部 before', '定义已改；随后刷新求值')),
        _frame('internal_refresh', '处理函数内部刷新 Model', 'Session::refresh → Model::evaluate_shared → evaluate（需要时）', 'refresh',
               ['session', 'document', 'model'], ['session_document', 'session_model', 'document_model'],
               'Model 按 doc.features 顺序检查旧前缀、跳过和缓存条件；无法复用时执行特征。不能把定义未变直接标成实际缓存命中。',
               data(new, '尚未提交 Undo', 'before.doc（角色示意）', old, '局部 before', '检查复用条件；必要时执行特征')),
        _frame('edit_reply', '保留 timeline.edit 的即时回复', 'timeline.edit → { feature, recomputed, restored }', 'edit',
               ['model', 'session'], ['session_model'],
               '内部刷新后检查该特征错误；成功才构造整体计数回复。它记录此处理函数的计数，不是逐节点执行 trace。',
               data(new, '处理函数返回后由 execute 收尾', 'before.doc（角色示意）', old, '局部 before', '即时计数在下方真实阶段回复中保留')),
        _frame('commit_undo', '成功提交：登记 Undo，清空 Redo', 'Session::execute → undo.push(before); redo.clear()', 'execute-commit',
               ['session', 'snapshot'], ['session_undo_snapshot', 'session_redo_snapshot', 'snapshot_document'],
               '普通命令成功、定义或可见性改变且 undoable 时，execute 把修改前快照压入 Undo 并清空 Redo。该分支每次保存的是快照，不是每次字段赋值日志。',
               data(new, 'undo.push(before)，redo.clear()', 'Undo 中的 before.doc（角色示意）', old, 'Session.undo', '等待统一收尾刷新')),
        _frame('outer_refresh', '统一入口完成收尾刷新', 'Session::execute → refresh_from → refresh → evaluate_shared', 'outer-refresh',
               ['session', 'document', 'model'], ['session_model', 'document_model'],
               '普通几何修改在 execute 外层还会刷新。evaluate_shared 的共享模型证明可能直接复用；两次刷新调用不代表几何必然计算两遍。',
               data(new, '本次普通命令已登记历史', 'Undo 中的 before.doc（角色示意）', old, 'Session.undo', '收尾刷新；实际短路分支未采集')),
        _frame('edited', '修改后：检查导出的阶段数据', 'document.inspect / document export', 'inspect',
               ['session', 'document', 'model'], ['session_document', 'session_model'],
               '若选择完整的隔离实验，本帧展示实际导出的定义、模型摘要、Undo/Redo 深度和先前保存的命令回复。',
               data(new, '修改后的历史状态', 'Undo 中的 before.doc（角色示意）', old, 'Session.undo', '修改后模型；等待真实阶段证据')),
        _frame('undo_pop', 'Undo 从历史栈取出目标快照', 'Session::execute("edit.undo") → edit.undo → prev = undo.pop()', 'undo',
               ['session', 'snapshot'], ['session_undo_snapshot'],
               'Undo 同样经过 Session::execute，但注册为 noundo 命令；它自己移动两侧历史栈，不再作为一次普通编辑压入新的 Undo。',
               data(new, 'undo.pop()', 'prev.doc（角色示意）', old, 'Undo 处理函数的局部 prev', '尚未恢复目标定义')),
        _frame('restore_document', 'go_to：恢复旧 Document 与上下文', 'go_to → was = Snapshot { current … }; s.doc = snap.doc', 'undo',
               ['session', 'snapshot', 'document'], ['snapshot_document', 'session_document'],
               'go_to 先把当前定义及上下文保存为 was，再替换为目标快照中的定义并清理选择。普通几何变化随后交由 execute 刷新 Model。',
               data(old, '恢复目标定义与编辑上下文', 'was.doc（当前定义的角色示意）', new, 'go_to 返回的局部 was', '定义已恢复；完整 ModelState 未由 Snapshot 直接恢复')),
        _frame('redo_push', '把刚离开的定义保存到 Redo', 'edit.undo → redo.push(now)', 'undo',
               ['session', 'snapshot'], ['session_redo_snapshot', 'snapshot_document'],
               'Undo 把 go_to 返回的当前定义快照压入 Redo，使之后的 Redo 能重新恢复修改后的定义。接口只公开栈深度，没有公开该条目的实际内容。',
               data(old, 'redo.push(now)', 'Redo 中的 was.doc（角色示意）', new, 'Session.redo', '等待恢复定义后的刷新')),
        _frame('undo_refresh', '恢复定义后更新 Model', 'Session::execute → refresh_from → refresh → evaluate_shared', 'outer-refresh',
               ['session', 'document', 'model'], ['document_model', 'session_model'],
               'Model 根据恢复后的定义再次检查复用条件。旧结果仍保留且输入匹配时可恢复，否则重新计算；Undo 不保证命中缓存。',
               data(old, 'Undo 完成收尾刷新', 'Redo 中的 was.doc（角色示意）', new, 'Session.redo', '恢复或重算；实际分支未采集')),
        _frame('undone', 'Undo 后：检查恢复后的真实阶段', 'edit.undo reply / document.inspect / document export', 'inspect',
               ['session', 'document', 'model', 'snapshot'], ['session_document', 'session_model', 'session_redo_snapshot'],
               '真实 Undo 回复只有操作标签，没有 recomputed/restored。本帧只显示取得的阶段数据；没有取得的内部计数保留不可获取。',
               data(old, 'Undo 后的历史状态', 'Redo 中的 was.doc（角色示意）', new, 'Session.redo', '恢复后模型；等待真实阶段证据')),
    ]


def _pairs(items):
    output = {}
    for key, value in items:
        if key in output:
            raise ValueError('JSON 存在重复字段')
        output[key] = value
    return output


class _Reader:
    def __init__(self, read_asset):
        self.read_asset, self.used = read_asset, 0

    def read(self, phase, name):
        ref = phase.get('assets', {}).get(name)
        if not isinstance(ref, dict) or type(ref.get('size')) is not int or not 0 <= ref['size'] <= MAX_ASSET_BYTES:
            raise ValueError(name + ' 未提供或超过 2 MiB')
        if self.used + ref['size'] > MAX_INPUT_BYTES:
            raise ValueError('阶段读取超过 12 MiB 总预算')
        raw = self.read_asset(phase['index'], name)
        if not isinstance(raw, bytes) or len(raw) > MAX_ASSET_BYTES or self.used + len(raw) > MAX_INPUT_BYTES:
            raise ValueError('阶段资源格式或容量无效')
        self.used += len(raw)
        if len(raw) != ref['size'] or hashlib.sha256(raw).hexdigest() != ref.get('sha256'):
            raise ValueError('阶段资源哈希或长度不符')
        def constant(_):
            raise ValueError('JSON 包含非有限数值')
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_pairs, parse_constant=constant)
        if not isinstance(value, dict):
            raise ValueError('阶段资源不是 JSON 对象')
        return value, ref['sha256']


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except (ValueError, OverflowError):
        return False


def _edit_result(phase, spec):
    """Bind command counters to this exact edit, independently of its exports."""
    params, result = phase.get('command_params'), phase.get('command_result')
    if (phase.get('command') != 'timeline.edit'
            or not isinstance(params, dict) or params.get('feature') != spec['feature_id']
            or params.get('set') != {'extent.distance': spec['after_distance']}
            or not isinstance(result, dict) or result.get('feature') != spec['feature_id']
            or any(type(result.get(k)) is not int or result[k] < 0 for k in ('recomputed', 'restored'))):
        return None
    return result


def _saved_edit_reply(frames, job, phases, spec):
    """Keep a native reply even if its later structural capture did not finish."""
    candidates = [p for p in phases if isinstance(p, dict) and p.get('key') == 'edited'
                  and p.get('command') == 'timeline.edit']
    if len(candidates) != 1:
        return
    phase = candidates[0]
    result = _edit_result(phase, spec)
    if result is None:
        return
    frame = next(f for f in frames if f['id'] == 'edit_reply')
    for field in frame['data']['model']['fields']:
        if field['name'] in ('recomputed', 'restored'):
            field.update(value=result[field['name']], provenance='native_command_result')
    frame.update(command='timeline.edit', command_result=copy.deepcopy(result),
                 command_evidence={'job_id': job.get('id'), 'phase_index': phase.get('index'),
                                   'provenance': 'native_command_result'})
    frame['description'] += ' 计数已由原生命令回复保存；此帧的调用位置和其余对象仍为源码说明。'
    frame['data']['model']['note'] = '计数来自已保存的即时回复，即使后续采集失败也保留；逐特征分支和 ModelState 仍未提供。'


def _observation(frame, phase, job, reader, spec, expected_height):
    if phase.get('status') != 'completed':
        raise ValueError('阶段尚未完成一致采集')
    document, doc_hash = reader.read(phase, 'document.json')
    inspected, inspect_hash = reader.read(phase, 'inspect.json')
    features = document.get('features')
    if not isinstance(features, list) or len(features) > 500:
        raise ValueError('阶段缺少有效特征定义')
    feature = next((f for f in features if isinstance(f, dict) and type(f.get('id')) is int and f['id'] == spec.get('feature_id')), None)
    extent = feature.get('extent') if feature else None
    height = extent.get('distance') if isinstance(extent, dict) else None
    if height != expected_height:
        raise ValueError('阶段拉伸距离与选定修改不一致')
    if any(type(inspected.get(key)) is not int or inspected[key] < 0 for key in ('undo', 'redo')):
        raise ValueError('阶段未提供有效 Undo/Redo 深度')
    bodies = inspected.get('bodies')
    if not isinstance(bodies, list) or len(bodies) > 128 or any(not isinstance(body, dict) for body in bodies):
        raise ValueError('阶段模型摘要无效')
    summary = []
    volume, volume_available = 0, True
    for body in bodies:
        bbox = body.get('bbox')
        if not isinstance(bbox, dict) or any(not isinstance(bbox.get(k), list) or len(bbox[k]) != 3 or not all(_number(n) for n in bbox[k]) for k in ('min', 'max')):
            bbox = None
        v = body.get('volume_mm3', body.get('volume'))
        if _number(v):
            volume += v
        else:
            volume_available = False
        summary.append({'name': str(body.get('name', ''))[:200], 'bbox': bbox,
                        'volume_mm3': v if _number(v) else None,
                        'faces': body.get('faces') if type(body.get('faces')) is int else None,
                        'edges': body.get('edges') if type(body.get('edges')) is int else None})
    bbox = summary[0]['bbox'] if len(summary) == 1 else None
    z_height = bbox['max'][2] - bbox['min'][2] if bbox else None
    if not _number(z_height):
        z_height = None
    if not _number(volume):
        volume_available = False
    timeline = inspected.get('timeline')
    timeline = [{k: row.get(k) for k in ('id', 'name', 'error', 'warning')} for row in timeline[:64] if isinstance(row, dict)] if isinstance(timeline, list) else None
    provenance = 'native_phase_observation'
    frame['data']['session'] = {'fields': [_field('undo_depth', '此副本 Undo 深度', inspected['undo'], provenance),
                                            _field('redo_depth', '此副本 Redo 深度', inspected['redo'], provenance),
                                            _field('phase', '实测阶段', phase.get('label'), provenance)],
                                    'note': '深度来自该独立 CLI 的 document.inspect，不是原窗口的 Session。'}
    frame['data']['document'] = {'fields': [_field('feature_id', '拉伸特征 ID', spec['feature_id'], provenance),
                                             _field('height', 'extent.distance', height, provenance),
                                             _field('feature_count', '文档特征数', len(features), provenance),
                                             _field('document_hash', '导出文档 SHA256', doc_hash, provenance)],
                                     'note': 'SHA256 标识导出文件，不代表 Arc 地址或原生缓存指纹。'}
    counts = {'recomputed': None, 'restored': None}
    result = phase.get('command_result')
    edit_result = _edit_result(phase, spec)
    if edit_result is not None:
        counts = {name: edit_result[name] for name in counts}
    frame['data']['model'] = {'fields': [_field('bodies', '实体摘要', summary, provenance),
                                          _field('height', '单实体模型 Z 跨度（mm）', z_height, provenance),
                                          _field('volume_mm3', '总体积（mm³）', volume if volume_available else None, provenance),
                                          _field('timeline', '公开时间线结果', timeline, provenance),
                                          _field('recomputed', '命令即时重算总数', counts['recomputed'], 'native_command_result' if counts['recomputed'] is not None else 'unavailable'),
                                          _field('restored', '命令即时恢复总数', counts['restored'], 'native_command_result' if counts['restored'] is not None else 'unavailable'),
                                          _field('runtime_cache_branch', '逐特征实际缓存分支', None, 'unavailable'),
                                          _field('model_state_identity', '原始 ModelState 对象', None, 'unavailable')],
                                  'note': '模型摘要来自实际导出；内部 Arc<ModelState> 未提供。Undo 不返回重算/恢复计数。'}
    frame.update(provenance=provenance, availability='observed', phase_index=phase['index'],
                 command=phase.get('command'), command_result=copy.deepcopy(result),
                 evidence={'job_id': job['id'], 'phase_index': phase['index'],
                           'document_sha256': doc_hash, 'inspect_sha256': inspect_hash,
                           'cli_version': job.get('cli_version')})
    return frame


def build_lifecycle(plan: dict, job: dict | None = None, read_asset=None) -> dict:
    """Build a bounded explanation and, when available, real CLI phase facts."""
    if not isinstance(plan, dict):
        raise ValueError('需要有效的重算观察计划')
    before, after = plan.get('before') or {}, plan.get('after') or {}
    pair = {'before_id': before.get('id'), 'after_id': after.get('id')}
    if job is not None:
        if not isinstance(job, dict) or any(job.get(k) != v for k, v in pair.items()):
            raise ValueError('实验与当前观察对不一致')
        if job.get('mode') not in ('edit_undo', 'full'):
            raise ValueError('实验类型不支持生命周期展示')
    output = {'schema_version': 1, 'available': bool(plan.get('available')),
              'source_version': SOURCE_VERSION, 'source_commit': SOURCE_COMMIT,
              'mode': 'source_explanation', 'pair': pair,
              'objects': copy.deepcopy(OBJECTS), 'relations': copy.deepcopy(RELATIONS),
              'sources': copy.deepcopy(SOURCES), 'frames': [],
              'limitations': LIMITATIONS[:], 'warnings': [], 'input_bytes': 0,
              'runtime_trace_available': False}
    spec = plan.get('lab_edit')
    valid = (plan.get('lab_compatible') is True and isinstance(spec, dict)
             and type(spec.get('feature_id')) is int and spec['feature_id'] > 0
             and all(isinstance(spec.get(k), str) and len(spec[k]) <= 512 for k in ('before_distance', 'after_distance')))
    if not valid:
        output['available'] = False
        output['warnings'].append('对象关系可阅读；调用场景要求同一既有拉伸仅 extent.distance 改变，当前观察不满足。')
        return output
    if job is not None and job['mode'] == 'edit_undo' and job.get('spec') != spec:
        # Names can differ by harmless display metadata; the replay inputs must not.
        candidate = job.get('spec') or {}
        if any(candidate.get(k) != spec[k] for k in ('feature_id', 'before_distance', 'after_distance')):
            raise ValueError('实验的拉伸输入与当前观察不一致')
    output['frames'] = _frames(spec)
    if job is None:
        return output
    output.update(mode='isolated_' + job['mode'], job_id=job.get('id'), job_status=job.get('status'))
    reader = _Reader(read_asset) if callable(read_asset) else None
    phases = job.get('phases')
    if not isinstance(phases, list) or len(phases) > 4:
        raise ValueError('实验阶段列表无效')
    if job['mode'] == 'full':
        output['frames'] = [_frame('full_model_reset', '完整重算：替换 Model 后刷新',
            'timeline.compute_all → s.model = Model::new(); s.refresh()', 'compute-all',
            ['session', 'model', 'document'], ['session_model', 'document_model'],
            '完整重算命令新建 Model 再刷新，保留 Document 定义。execute 仍创建局部 before Snapshot；该命令注册为 noundo，不把它压入 Undo。本实验没有执行修改或 Undo。',
            _data(spec['after_distance'], spec['feature_id'], 'noundo：不登记普通编辑历史',
                  'execute 局部 before（不入 Undo；未导出）', None, 'execute 局部变量；noundo 不压入 Undo', 'Model::new() 后完整求值')),
            _frame('full', '完整重算后的导出阶段', 'timeline.compute_all reply / document.inspect', 'inspect',
                   ['session', 'model', 'document'], ['session_model', 'session_document'],
                   '此帧仅显示完整重算任务取得的阶段数据，不串联并未运行的修改和 Undo。',
                   _data(spec['after_distance'], spec['feature_id'], '完整重算后状态', '实际栈条目未提供', None, '未提供', '完整重算结果'))]
        wanted = [('full', 'full', spec['after_distance'])]
    else:
        _saved_edit_reply(output['frames'], job, phases, spec)
        wanted = [('baseline', 'baseline', spec['before_distance']),
                  ('edited', 'edited', spec['after_distance']),
                  ('undone', 'undone', spec['before_distance'])]
    for frame_id, phase_key, expected_height in wanted:
        frame = next(item for item in output['frames'] if item['id'] == frame_id)
        matching = [p for p in phases if isinstance(p, dict) and p.get('key') == phase_key]
        try:
            if len(matching) != 1 or reader is None:
                raise ValueError('该阶段没有唯一、可读取的完整导出')
            phase = matching[0]
            if type(phase.get('index')) is not int or not 0 <= phase['index'] < 4:
                raise ValueError('阶段编号无效')
            _observation(frame, phase, job, reader, spec, expected_height)
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, UnicodeError, RecursionError) as exc:
            frame.update(availability='unavailable', phase_index=None)
            frame['description'] += ' 实际证据不可获取：' + str(exc)[:240]
            output['warnings'].append(frame['title'] + '：' + str(exc)[:240])
    output['input_bytes'] = reader.used if reader else 0
    return output
