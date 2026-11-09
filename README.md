# SolidCraft 本机观察工具

面向 Windows 和 SolidCraft 0.30.1 的独立 Python / HTML 工具，不需要编译或替换 SolidCraft。
需要 Python 3.10+ 和现代浏览器；正常运行只使用 Python 标准库。

## 工具

| 目录 | 用途 | 启动方式 |
| --- | --- | --- |
| [topology-live](topology-live/README.md) | 实时查看原生面、边名称、历史来源状态和显示映射 | 双击 `topology-live/start-live-viewer.cmd` |
| [transaction-live](transaction-live/README.md) | 记录建模观察、查看历史几何、按需离线重建阶段 | 双击 `transaction-live/start-transaction-viewer.cmd` |
| [topology-visualization](topology-visualization/README.md) | 固定样例的分裂前后及倒角离线演示 | 打开 `topology-visualization/solidcraft-topology.html` |

每个 Windows 用户在自己的账号下启动程序并选择 SolidCraft 实例；网页端口默认由系统分配。
实时查看器固定连接所选的启动实例，断线不会跟随另一实例。名称来源、原生 `exact` 历史标志、
显示重建是不同信息，不能把它们当作任意拓扑修改后永久稳定的承诺。

HTTP 只监听本机，但尚无 Windows 用户身份鉴权；不同网页端口不提供本机用户之间的模型保密隔离。
建模档案可能包含完整设计，需按模型本身的保密要求管理。

## 源码与本机数据

- 源码、文档、自包含 HTML 和测试必需的固定样例纳入 Git。提交 HTML 后，干净检出即可直接运行。
- `topology-visualization/` 的三个 `*-capture.json` / `.3mf` 样例，以及
  `topology-live/debug-curve/` 的 Sample Plate 数据，是有意保留的离线回归测试输入。
- 现场模型捕获、`transaction-live/records/`、临时导出、凭据、日志、截图、安装包和下载的
  `investigation/` 研究材料由 `.gitignore` 排除；这些本机文件不会被删除。
- `VERIFICATION.md` 保存历史验证记录，其中提到的截图和运行证据属于本机产物，不随仓库提供。
- `transaction-live/start-demo-viewer.cmd` 需要本机已有 `records/verified-demo` 档案；仓库不附带现场档案。

## 开发验证

在 `topology-live` 目录执行：

```powershell
python -X utf8 -m unittest -v test_startup test_instances test_live test_topology_geometry test_name_integrity
node test_name_ui.js
node test_selection_integrity.js
node test_rotation.js
```

在 `transaction-live` 目录执行：

```powershell
python -X utf8 -m unittest -v test_archive test_capture_server test_native test_rebuild
node --test test_ui.js
```

Node.js 仅用于 JavaScript 开发检查。档案模块的部分测试需要已安装的 SolidCraft CLI，
只使用独立的 headless 临时模型；缺少 CLI 时跳过。可选浏览器测试需要额外安装 Playwright。
`integration_live.py` 会创建独立桌面测试实例，不属于普通离线测试命令。

修改实时界面后，用 `python topology-live/build-live-ui.py` 重新生成 HTML；
离线样例界面由 `topology-visualization/build-page.py` 生成，档案几何页由
`transaction-live/build_geometry.py` 生成。生成后检查相应 HTML 变更再提交。
