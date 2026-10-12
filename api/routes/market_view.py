"""Market View 路由（只读）：任意受支持交易对的公开行情研究视图。

- `PATH`：`?symbol=<SYMBOL>&interval=&candles=&depth=&trades=`
- `SYMBOLS_PATH`：可查看交易对目录（选择器搜索用），`?q=&limit=&quote_asset=`

与 **Runtime Trading Symbol** 无关：本路由只做公开行情研究，不修改运行实例、不触发任何交易动作。
"""

from __future__ import annotations

PATH = "/api/v1/market/view"
SYMBOLS_PATH = "/api/v1/market/view/symbols"
SECTIONS = ()


def payload(snapshot: dict) -> dict:  # pragma: no cover - server 直接处理
    raise KeyError("market view is served from the MarketView service")
