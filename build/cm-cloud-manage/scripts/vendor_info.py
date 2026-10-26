"""随包分发的第三方纯 Python 依赖（vendor）。

本包只放"必须随技能包一起分发"的第三方源码，不做 pip 安装 —— 技能目标机不允许新增
第三方安装依赖，且这些能力不能因为目标机缺少 pip 包而整体不可用。

当前内容：
- `segno` 1.6.6（BSD-3-Clause，Copyright (c) 2016 - 2025 Lars Heuer，许可证见
  `vendor_segno_LICENSE.md`）：二维码编码器，把 CLI 已校验的授权入口短链编码成本地二维码。

上游只有两处平台适配，且都在**终端彩色输出**路径（`vendor_segno_writers.py` 的 Windows 控制台
ctypes、`vendor_segno.py` 的 `sys.platform` 判断）；本技能只用 `kind="png"` 输出，不触碰
该路径。包内无 C 扩展、无二进制、无第三方导入，Windows / macOS / Linux 行为一致。

升级方式：用上游发布包中同名文件更新 `vendor_segno*.py`，并同步许可证；
升级后必须重跑 `cmcloud auth login` 的真机扫码验证。
"""
