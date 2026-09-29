"""local replay session control（P0001.12 §3/§4）：**simulation control，不是交易 write path**。

- 只允许作用于 REPLAY runtime；`ReplayControl` 在构造时即拒绝非 REPLAY 模式；
- 命令由 REPLAY owner（ReplaySource / runtime session）执行，API 只转交意图；
- 与交易写路径完全隔离（本模块不 import 任何执行/连接器代码）。
"""

from __future__ import annotations

PATH = "/api/v1/replay"
VERBS = ("play", "pause", "step", "speed", "seek")
SECTIONS = ()


def payload(snapshot: dict) -> dict:  # pragma: no cover - 该端点由 server 直接生成
    raise KeyError("replay control is handled by the server and targets the replay runtime only")
