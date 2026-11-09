# 重算机制查看器

启动 `E:\solidcraft\transaction-live\start-flow-viewer.cmd`，在事务流点「重算演示」。它读取原有记录档案，在独立 CLI 进程中运行实验，不连接正在打开的 SolidCraft 控制端口。

## 阅读与操作

1. 比较观察 O0009 → O0011：这是此前矩形拉伸从 `20 mm` 改为 `40 mm` 的已采集记录。当前 O0012 是 Undo 后的现场观察，不能把它误当作新提交的编辑。
2. 点「演示源码流程」或「下一步」：查看定义恢复、Session 刷新、链指纹、旧前缀、跳过、来源失败、缓存键、缓存候选及执行特征的代码路径。蓝色只表示源码讲解进度。
3. 中间按修改后的 `doc.features` 顺序展示特征。点击特征查看具体字段和已核验的 `Extrude.sketch` 引用。删除特征只在变化对照区显示，不混入求值链。
4. 点「实测修改 → Undo」：独立 CLI 加载 O0009 文档副本，执行 `timeline.edit`，然后 `edit.undo`。点击阶段卡片，检查原始回复、此副本的 Undo/Redo 深度、JSON 和三维网格。
5. 点「完整重算副本」：独立 CLI 加载 O0011 文档副本，执行 `timeline.compute_all`。该原生命令创建新 `Model` 后刷新，回复 `features/errors`，没有 `recomputed/restored` 字段。

## 真实源码依据

固定版本为 SolidCraft v0.30.1（安装 CLI 提交 8e2b0cd）。本页不是 FreeCAD DAG/Kahn 调度演示。

- [时间线主循环与前缀复用](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/doc/src/eval.rs#L2194-L2285)：按 `doc.features.iter().enumerate()` 遍历。
- [缓存候选](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/doc/src/eval.rs#L2319-L2359)：cache、kept、rolled/primed 满足条件时恢复结果。
- [执行和保存结果](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/doc/src/eval.rs#L2361-L2492)：不能复用才调用 `eval_feature`；成功形成新的 `Arc<ModelState>`，失败保持输入状态。
- [Undo Snapshot](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/lib.rs#L280-L290)：保存定义和编辑上下文，并不直接保存完整 ModelState。
- [修改后的原生计数](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/cmd/edit.rs#L1224-L1239)：`timeline.edit` 返回整体 `recomputed/restored`。
- [完整重算](https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/engine/src/cmd/edit.rs#L189-L194)：`Model::new()` 然后 `refresh()`。

`restored` 不包括未变连续前缀的直接复用。`document.inspect` 没有逐特征缓存命中分支；缓存也可沿用旧 `ms`。因此查看器没有用文档 SHA256、耗时、几何相同来伪造内部哈希、命中或地址。

S0、S1… 是图中的阶段示意名称。实验的三份阶段快照是独立 CLI 重新求值得到的导出数据，不是从原进程读取的 Rust ModelState 对象；不能补回过去未记录的内部事务。

## 资源与持久化

- 原档案以 SQLite 只读连接访问；原记录器与原生窗口不停止、不修改。实验文件另存 `recompute-runs/<档案路径哈希>/jobs/job_<编号>/`。
- 实验有独占目录锁、单工作线程、最多两项等待任务；支持取消、120 秒默认总时限和重启后恢复已完成证据。
- 独立实验目录默认预算 256 MiB，单资源 32 MiB、单阶段 48 MiB、输入文档 2 MiB、最多 64 个特征、512 个实验。此预算单独于原记录档案的 2 GiB 预算，到限拒绝新实验，不自动删除历史。
- 文件先原子发布，清单随后提交；即时命令回复先保存，采集失败仍保留回复和诊断。重启将未完成实验标为中断；资源读取验证哈希。
- 原生 CLI 导出在返回前可能增长，应用预算不等于操作系统磁盘或进程内存硬配额。
- HTTP 仅监听 127.0.0.1，使用每端口 cookie、Host/Origin 校验，只有固定实验命令与白名单资源接口。没有任意路径或任意 CAD 命令接口。
- `--recompute-lab` 只允许和 `--read-only` 搭配，避免同一服务同时运行旧阶段重建和新实验。

## 验证记录

真实隔离 CLI 首轮测得：`timeline.edit {feature:2,set:{"extent.distance":"40 mm"}}` 返回 `{"feature":2,"recomputed":1,"restored":0}`；`edit.undo {}` 返回 `{"undone":"Edit Feature"}`；`timeline.compute_all {}` 返回 `{"errors":0,"features":2}`。这些计数属于新实验副本，不回填现场 O0009/O0011/O0012 的运行分支。

2026-11-09 验证结果：全量发现运行 131 项 Python 测试全部通过（117.75 秒）；随后新增的实验选择乱序和删除节点显示回归包含在 5 项浏览器测试中再次全部通过。10 项 Node UI 测试全部通过；隔离 CLI 测试共 16 项，覆盖取消、超时、队列、恢复、篡改、容量与发布失败。审计修复记录见 RECOMPUTE-AUDIT.md。

已部署接口实测于 `http://127.0.0.1:51277/`：

- 修改/Undo 实验 `6e13991c5d8a421aa20a6df3a7cfa765`，阶段高度为 `20 mm → 40 mm → 20 mm`；副本 Undo/Redo 深度为 `0/0 → 1/0 → 0/1`。
- 完整重算实验 `d4a93f4a295644628275e3ada2fa1742`，高度 `40 mm`，原生命令返回 2 个特征、0 个错误。
- 现场原始档案计数前后一致：12 个事件、12 份观察、55 个资源；没有往档案中插入虚构的现场事务。实验各自保存在独立目录。
- 原始实验接口输出和摘要另存工作区 `recompute-live-demo.json`，每份实际阶段包含 Document、inspect、草图、拓扑、显示网格。端口可能随重新启动变化，以启动窗口打印的地址为准。
