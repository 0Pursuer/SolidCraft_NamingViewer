# Session / Document / Model / Snapshot：源码核验与演示帧

核验版本：`tangency-labs/solidcraft` 的 `v0.30.1`。本文件仅解释源码和可观察证据，没有修改现场模型、控制连接、记录器或实现文件。

## 对象关系

| 对象 | 实际字段或关系 | 来源 |
| --- | --- | --- |
| Session | `doc: Arc<Document>`、`model: Model`、`undo: Vec<Snapshot>`、`redo: Vec<Snapshot>`；还有活动草图、选择等编辑上下文。命令注册表不在 Session 字段中。 | [lib.rs 434–445](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/lib.rs#L434-L445) |
| Document | `params`、有序 `features`、`marker` 等定义。`marker` 位置及之后的特征处于回退状态。 | [document.rs 2886–2911](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/doc/src/document.rs#L2886-L2911) |
| Model | `results: Vec<FeatureResult>`、`cache`、`last_recomputed`、`last_restored`，以及 kept、rolled_results、primed 等求值复用数据。 | [eval.rs 1302–1337](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/doc/src/eval.rs#L1302-L1337) |
| FeatureResult | 每个结果引用 `Arc<ModelState>`；还保存特征 ID、错误、警告、耗时等。`fingerprint` 与 `state` 均跳过序列化。 | [eval.rs 1278–1300](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/doc/src/eval.rs#L1278-L1300) |
| ModelState | 累计实体 `bodies`、求解草图 `sketches`、构造对象等。它是求值结果，不是特征定义清单。 | [eval.rs 854–885](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/doc/src/eval.rs#L854-L885) |
| Snapshot | `label`、`doc: Arc<Document>`、`active_sketch`、`pending_moves`、`visibility`。没有 Model 或 ModelState 字段。 | [lib.rs 280–290](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/lib.rs#L280-L290) |
| CommandSpec | 全局注册表条目定义 `run/enabled/undoable/reads_model` 等规则，`find_command` 按 ID 查找。Undo/Redo 注册为 `.noundo()`，由处理器自己移动两栈。 | [cmd/mod.rs 142–186](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/cmd/mod.rs#L142-L186)、[查找 374–377](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/cmd/mod.rs#L374-L377)、[Undo 注册](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/cmd/edit.rs#L21-L22) |

Session 拥有 Model，Session 与各 Snapshot 通过 Arc 引用 Document。写定义时，`doc_mut` 调用 `Arc::make_mut`，保留历史快照引用的旧定义；不能把图中的 D20/D40 解释为真实指针、内存地址或 JSON 中的原生对象 ID。[写时复制](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/lib.rs#L944-L952)

## 建议的 12 帧播放顺序

以下 D20、D40 仅表示“拉伸距离为 20/40 的文档定义内容”。M20、M40 仅表示“对应导出几何的模型结果投影”。这些是查看器说明用名称，不能冒充内部对象身份。

| 帧 | 高亮调用与对象 | 数据变化说明 | 证据性质与源码 |
| --- | --- | --- | --- |
| 0 修改前 | Session.doc → Document；Session.model → Model | 文档距离 20；导出模型高 20、体积 20000。独立 CLI 副本的 Undo/Redo 深度从实际 baseline inspect 读取，当前示例为 0/0。 | 原生导出阶段可观察；对象所有权是源码说明。[Session](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/lib.rs#L434-L445) |
| 1 命令入口与 before | `Session::execute("timeline.edit")` → 临时 `before Snapshot` | before.doc 引用修改前定义 D20；这时 before 还没有被压入 Undo。 | 此临时对象未被 API 采集，必须标“源码推演”。[lib.rs 1460–1503](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/lib.rs#L1460-L1503) |
| 2 写入定义 | `edit_feature` → `Session::doc_mut` → Document | 将 F2 的 `extent.distance` 写成 40；Snapshot 仍保留旧定义。Model 尚待本次刷新。此瞬间的“新定义／旧结果”组合仅来自代码顺序。 | 源码推演，不能冒充中间实测快照。[写入 1232–1235](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/cmd/edit.rs#L1232-L1235)、[COW](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/lib.rs#L944-L952) |
| 3 请求求值 | `Session::refresh` → `Model::evaluate_shared` | 当前 Document 交给 Model；共享模型复用证明不成立时才进入普通 evaluate。高度变化属于几何输入变化。 | 源码调用说明。[refresh](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/lib.rs#L1090-L1097)、[入口](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/doc/src/placement_reuse.rs#L14-L38) |
| 4 时间线求值 | Model → FeatureResult → ModelState | 按 features 顺序检查前缀、跳过和缓存；不能复用时执行特征。F1 定义未改、F2 距离改变来自文档比较；逐特征实际复用分支未知。 | 流程来自源码，分支命中不可观测。[主循环](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/doc/src/eval.rs#L2195-L2285)、[缓存与执行](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/doc/src/eval.rs#L2319-L2492) |
| 5 处理器验证与回复 | `edit_feature` 检查结果 → 构造 command result | 处理器内部刷新后检查目标特征 error；成功时构造 `{feature,recomputed,restored}`。已保存命令回复证明本例总计 1/0，不能据此给每个节点填缓存命中。40 高度的几何来自稍后导出的 edited 阶段。 | 计数锚定真实 command_result；这一内部时刻本身未采样。[edit.rs 1235–1239](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/cmd/edit.rs#L1235-L1239) |
| 6 提交历史 | execute → Undo.push(before)；Redo.clear | 成功、确实改变文档或可见性、命令 undoable 时，才压入 before。解释性“Undo 顶部对应 D20”来自源码，原生栈条目对象没有导出；本例最终深度 1/0 由 inspect 验证。 | 栈规则为源码推演，深度为命令完成后的实测。[lib.rs 1547–1567](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/lib.rs#L1547-L1567) |
| 7 完成外层刷新 | execute → refresh_from → refresh → evaluate_shared | 正常几何变更再次进入刷新。可整体复用时返回，不必再次计算几何；该短路会把 Model 的 last 计数置零。因此页面使用第 5 帧保存的回复，不能用之后读到的字段覆盖它。完成后的实际 Document 为 40、模型高 40、体积 40000，副本深度 1/0。 | 完成阶段可实测；外层具体复用分支仍未 trace。[外层调用](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/lib.rs#L1568-L1578)、[refresh_from](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/lib.rs#L1151-L1186)、[复用证明](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/doc/src/placement_reuse.rs#L14-L38) |
| 8 Undo 入口 | `Session::execute("edit.undo")` → undo handler | execute 也捕获本次 before D40，但 Undo 命令为 noundo，不走普通命令的额外 Undo.push。 | 源码说明。[入口](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/lib.rs#L1460-L1479)、[noundo 注册](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/cmd/edit.rs#L21-L22) |
| 9 恢复定义 | Undo.pop → go_to → Session.doc / 上下文 | 从 Undo 取旧 Snapshot；go_to 先把当前 D40 和上下文保存在 was，再把 Session.doc 换成 D20，并恢复相应上下文、清选择。Model 没有从 Snapshot 中取出；等待后续求值。 | 这是源码顺序推演，中间 doc20/model40 不可当成实测阶段。[edit.rs 114–119](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/cmd/edit.rs#L114-L119)、[go_to 147–171](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/cmd/edit.rs#L147-L171) |
| 10 保存 Redo 并刷新 | Redo.push(was)；execute → refresh_from | 当前 D40 进入反方向历史。不同 doc 的 go_to 把普通求值留给 execute 外层；Model 根据恢复后的 D20 重新检查复用或计算，缓存命中不保证。 | 栈内容对应关系是源推演；具体 Model 分支未知。[Undo](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/cmd/edit.rs#L114-L119)、[外层](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/lib.rs#L1568-L1578) |
| 11 Undo 完成 | Session.doc=D20；Model 导出结果=M20 | 文档距离 20、模型高 20、体积 20000；副本 Undo/Redo 深度 0/1。Undo 回复只有 undone 标签，没有重算/恢复计数，因此两项显示“未提供”。 | 原生 undone 阶段可观察。恢复后的导出数据与旧结果内容相同，不证明相同 Arc、原始对象身份或缓存命中。[Undo 回复](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/cmd/edit.rs#L114-L119) |

## 必须保留的边界

- 源码帧不等于执行 trace。公共 API 只提供命令完成后的导出数据和部分即时回复；没有临时 before/was 对象、原生 Undo 栈条目或每步 Arc 指针。
- 值为 20 或 40 的“定义”来自 Document；包围盒/体积是阶段导出模型的投影，不能显示成完整 Rust ModelState JSON。
- 现场观察 O0009/O0011/O0012 的深度为 3/0、4/0、3/1。独立 CLI 副本 baseline/edited/undone 的深度为 0/0、1/0、0/1。两条历史必须分开标注，不能拼成一条原生 Session 历史。
- 命令处理器内部 refresh 与 execute 外层 refresh 都存在。不能把两次进入 refresh 解说成必定执行几何两遍，也不能在外层刷新后抹掉已保存的原生命令回复计数。
- Undo 复用同一 Session 的 Model 求值器与其可用缓存，恢复的是定义快照；没有从 Snapshot 直接取 ModelState 的代码。
- 修改失败的支线：execute 在改变过文档/可见性/挂起移动时恢复 before 的定义和相应上下文，并 refresh；不应将失败支线放进本例成功实测历史。[失败恢复](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/lib.rs#L1585-L1594)
- 浏览器可以用播放位置高亮调用对象，但必须将其标成源码演示进度。动作、对象字段和调用边都应提供对应源码 URL。
