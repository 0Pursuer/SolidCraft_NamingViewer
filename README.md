# SolidCraft 拓扑命名查看器

在浏览器中实时查看模型的面、边名称，也可以记录建模过程、回看历史和重算演示。

## 首次准备

1. 在 Windows 上安装 **SolidCraft**（已验证版本：0.30.1）和 **Python 3.10 或更新版本**。
2. 下载本项目并解压到本地文件夹。
3. 启动 SolidCraft，打开模型，并开启 **AI 控制通道**。

无需编译，也无需额外安装 Python 包。

## 一键启动

在项目文件夹中，按需要双击下面的文件：

| 想做什么 | 双击哪个文件 |
| --- | --- |
| **实时查看拓扑名称** | [topology-live/start-live-viewer.cmd](topology-live/start-live-viewer.cmd) |
| **记录建模过程** | [transaction-live/start-transaction-viewer.cmd](transaction-live/start-transaction-viewer.cmd) |
| **查看已有记录、流程和重算演示** | [transaction-live/start-flow-viewer.cmd](transaction-live/start-flow-viewer.cmd) |

实时查看或记录时，按窗口提示选择 SolidCraft 实例；只有一个实例时，直接按 **回车**。
随后会自动打开浏览器，并自动选择空闲端口，无需手动配置地址。

**第一次查看建模流程，请先启动“记录建模过程”，在 SolidCraft 中完成一些建模操作。**
之后启动“查看已有记录”，会自动打开最近更新的档案；点击页面中的“重算演示”可进入对象关系与修改/Undo 演示。

## 运行与退出

- 使用期间保持启动窗口运行；关闭网页后，仍可用窗口中显示的地址重新打开。
- 停止服务：在启动窗口按 **Ctrl+C**。
- SolidCraft 退出并重新启动后，请重新启动实时查看器或记录器，选择实例。
- 多个用户使用时，各自在自己的 Windows 账号下启动并选择自己的实例即可。

## 常见问题

| 遇到的情况 | 处理方法 |
| --- | --- |
| 提示需要 Python | 安装 Python 3.10 或更新版本后，重新双击启动脚本。 |
| 找不到 SolidCraft 实例 | 确认 SolidCraft 已启动、AI 控制通道已开启，并与查看器使用同一 Windows 账号。 |
| 浏览器没有自动打开 | 复制启动窗口显示的 `http://127.0.0.1:端口/` 地址到浏览器。 |
| 提示没有观察档案 | 先运行“记录建模过程”，完成建模操作后再查看记录。 |

只想先看效果？直接用浏览器打开 [topology-visualization/solidcraft-topology.html](topology-visualization/solidcraft-topology.html)，即可查看固定样例，无需启动 SolidCraft 或安装 Python。
