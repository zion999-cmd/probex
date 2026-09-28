"""Binance USDⓈ-M 外部端点常量（P0001.9.1 §0.2）。

**单一 Owner**：本模块是仓库内唯一硬编码 Binance URL 的地方，便于在端点迁移时一处修正。

外部事实与不确定性（如实记录，不得在实现中静默假设）：

- Binance 于 2026-03-06 公告 USDⓈ-M Futures WebSocket 路由升级，旧入口
  `wss://fstream.binance.com/ws` 与 `/stream` 计划于 2026-04-23 退役；
  新入口按 tier 拆分：`/public`（`@depth`、`@trade`、`@bookTicker`）、
  `/market`（`@aggTrade`、`@markPrice`、`@kline`、`@ticker`、`@forceOrder`）、`/private`（user data）。
- 来源为 Binance 支持公告的镜像与多个第三方库的迁移 PR/issue（ccxt #28091、ccxt/go-binance #809、
  tiagosiebler/binance v3.5.0、unicorn-binance-websocket-api #437）。
- **未取得官方开发者文档页面直接确认**；`/public` 与 `/public/ws`、combined 形式在各来源间描述略有出入。
  已知陷阱：订阅到错误 tier 可能返回 ACK 但**不推送数据**。
- 因此：所有 URL 都可被调用方覆盖（见 `StreamTier` / 各 client 构造参数），本模块**不做**任何
  静默 fallback 或自动换 tier —— 端点错误必须以可观察的失败暴露（连接失败 / 无数据 + telemetry）。

REST 端点未受本次 WS 迁移影响。
"""

from __future__ import annotations

from enum import Enum

#: USDⓈ-M Futures REST 基础 URL。
REST_BASE_URL = "https://fapi.binance.com"

#: REST 路径。
DEPTH_PATH = "/fapi/v1/depth"
EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"
SERVER_TIME_PATH = "/fapi/v1/time"

#: 私有账户端点（P0001.9.2）。**只读**：本仓库不包含任何下单/撤单端点常量。
ACCOUNT_PATH = "/fapi/v2/account"
POSITION_RISK_PATH = "/fapi/v2/positionRisk"
LISTEN_KEY_PATH = "/fapi/v1/listenKey"

#: 启动恢复用的**只读**查询（P0001.9.3；本仓库依然没有任何下单/撤单端点）。
OPEN_ORDERS_PATH = "/fapi/v1/openOrders"
ALL_ORDERS_PATH = "/fapi/v1/allOrders"
USER_TRADES_PATH = "/fapi/v1/userTrades"

#: WS 主机（tier 只是路径前缀）。
WS_HOST = "wss://fstream.binance.com"


class StreamTier(Enum):
    """WS 路由 tier（2026 迁移后的分层入口）。"""

    #: 高频行情：`@depth`、`@trade`、`@bookTicker`。
    PUBLIC = "public"
    #: 常规行情：`@aggTrade`、`@markPrice`、`@kline`、`@forceOrder`。
    MARKET = "market"
    #: 用户数据（本阶段不使用）。
    PRIVATE = "private"

    @property
    def base_url(self) -> str:
        """该 tier 的 WS 基础 URL（可整体覆盖：见 `with_host`）。"""
        return f"{WS_HOST}/{self.value}"

    def single_stream_url(self, stream: str) -> str:
        """单流入口：`<base>/ws/<stream>`。"""
        return f"{self.base_url}/ws/{stream}"

    def combined_stream_url(self, streams: tuple[str, ...]) -> str:
        """组合流入口：`<base>/stream?streams=a/b`。"""
        if not streams:
            raise ValueError("streams must not be empty")
        return f"{self.base_url}/stream?streams={'/'.join(streams)}"

    def user_data_url(self, listen_key: str, *, ws_host: str | None = None) -> str:
        """user data stream URL（private tier，listenKey 作为查询参数）。

        不确定性：2026 tier 迁移后的 private 入口形态来自第三方迁移 PR 与支持公告镜像，
        未取得官方开发者文档直证 ⇒ 允许整体覆盖（`ws_host`），错误以可观察失败暴露。
        """
        if not isinstance(listen_key, str) or not listen_key:
            raise ValueError("listen_key must be a non-empty string")
        base = self.base_url if ws_host is None else self.with_host(ws_host)
        return f"{base}/ws?listenKey={listen_key}"

    def sanitized_user_data_url(self, listen_key: str, *, ws_host: str | None = None) -> str:
        """遮蔽 listenKey 的 URL（用于日志 / telemetry / 异常文本）。"""
        return self.user_data_url(listen_key, ws_host=ws_host).replace(listen_key, "***")

    def with_host(self, ws_host: str) -> str:
        """覆盖 WS 主机（用于测试 / 端点迁移），返回该 tier 的基础 URL。"""
        if not isinstance(ws_host, str) or not ws_host.startswith(("ws://", "wss://")):
            raise ValueError(f"ws_host must start with ws:// or wss://, got {ws_host!r}")
        return f"{ws_host.rstrip('/')}/{self.value}"


__all__ = [
    "ACCOUNT_PATH",
    "DEPTH_PATH",
    "EXCHANGE_INFO_PATH",
    "ALL_ORDERS_PATH",
    "LISTEN_KEY_PATH",
    "OPEN_ORDERS_PATH",
    "USER_TRADES_PATH",
    "POSITION_RISK_PATH",
    "REST_BASE_URL",
    "SERVER_TIME_PATH",
    "WS_HOST",
    "StreamTier",
]
