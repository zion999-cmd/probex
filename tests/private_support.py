"""P0001.9.2 测试脚手架：私有层的假 REST / 假 WS 与报文构造器。

所有值都是**测试值**；凭据只存在于内存（不写文件、不进 fixture 输出）。
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field

from connectors.binance.market_data.errors import TransportError, WebSocketClosed, WebSocketTimeout
from connectors.binance.market_data.transport import ReconnectPolicy
from connectors.binance.private.auth import ApiCredentials
from connectors.binance.private.rest import PrivateRestClient
from connectors.binance.private.runtime import PrivateAccountConfig
from connectors.binance.private.user_stream import UserStreamClient
from tests.support import BASE_TS

SYMBOL = "BTCUSDT"
FAKE_API_KEY = "test-api-key-not-a-credential"
FAKE_API_SECRET = "test-api-secret-not-a-credential"
FAKE_LISTEN_KEY = "test-listen-key-not-a-credential"


def private_config(**overrides: object) -> PrivateAccountConfig:
    """测试用私有运行时配置。"""
    values: dict[str, object] = {
        "symbol": SYMBOL,
        "recv_window_ms": 5_000,
        "listen_key_ttl_ms": 60 * 60 * 1000,
        "keepalive_interval_ms": 30 * 60 * 1000,
        "connect_timeout_s": 1.0,
        "read_timeout_s": 0.5,
        "request_timeout_s": 1.0,
        "max_median_private_lag_ms": 2_000,
        "latency_sample_limit": 256,
        "reconnect": ReconnectPolicy(max_attempts=3, base_backoff_ms=1, max_backoff_ms=2),
    }
    values.update(overrides)
    return PrivateAccountConfig(**values)  # type: ignore[arg-type]


def credentials() -> ApiCredentials:
    return ApiCredentials(FAKE_API_KEY, _api_secret=FAKE_API_SECRET)


#: 真实形态的账户快照（数值为测试值）。
def account_payload(
    *,
    can_trade: bool = True,
    dual_side_position: bool | None = None,
    position_side: str = "BOTH",
    wallet_balance: str = "1000.50",
    available_balance: str = "900.25",
) -> dict:
    payload: dict[str, object] = {
        "canTrade": can_trade,
        "totalWalletBalance": wallet_balance,
        "totalMarginBalance": "1001.50",
        "totalUnrealizedProfit": "1.00",
        "availableBalance": available_balance,
        "maxWithdrawAmount": "800.00",
        "updateTime": BASE_TS,
        "assets": [
            {
                "asset": "USDT",
                "walletBalance": wallet_balance,
                "availableBalance": available_balance,
                "marginBalance": "1001.50",
                "unrealizedProfit": "1.00",
                "maxWithdrawAmount": "800.00",
                "updateTime": BASE_TS,
            }
        ],
        "positions": [{"symbol": SYMBOL, "positionSide": position_side}],
    }
    if dual_side_position is not None:
        payload["dualSidePosition"] = dual_side_position
    return payload


def position_risk_payload(
    *,
    symbol: str = SYMBOL,
    position_amt: str = "0.5",
    entry_price: str = "60000.10",
    mark_price: str = "60100.00",
    unrealized_profit: str = "49.95",
    liquidation_price: str = "55000.00",
    leverage: int | str = 10,
    margin_type: str = "cross",
    position_side: str = "BOTH",
    margin_asset: str | None = "USDT",
) -> list[dict]:
    """positionRisk 条目。

    `margin_asset=None` 与 `leverage="20"`（字符串）以及空仓 `mark_price="0"` 都是**真实测试网**形态
    （2026-09-28 实测：payload 没有 marginAsset/asset，整数以字符串传输，空仓 markPrice 为 "0"）。
    """
    entry: dict[str, object] = {
        "symbol": symbol,
        "positionAmt": position_amt,
        "entryPrice": entry_price,
        "markPrice": mark_price,
        "unRealizedProfit": unrealized_profit,
        "liquidationPrice": liquidation_price,
        "leverage": leverage,
        "marginType": margin_type,
        "positionSide": position_side,
        "updateTime": BASE_TS,
    }
    if margin_asset is not None:
        entry["marginAsset"] = margin_asset
    return [entry]


def testnet_position_risk_payload(
    *, symbol: str = SYMBOL, position_amt: str = "0", mark_price: str = "0", leverage: str = "20"
) -> list[dict]:
    """真实测试网形态的 positionRisk（字符串整数 + 无 marginAsset）。"""
    return position_risk_payload(
        symbol=symbol, position_amt=position_amt, mark_price=mark_price, leverage=leverage, margin_asset=None
    )


def account_update_message(
    *, event_ts: int = BASE_TS, transaction_ts: int | None = None, reason: str = "ORDER",
    wallet_balance: str = "1000.50", position_amt: str = "0.5",
) -> str:
    payload = {
        "e": "ACCOUNT_UPDATE",
        "E": event_ts,
        "T": event_ts if transaction_ts is None else transaction_ts,
        "a": {
            "m": reason,
            "B": [{"a": "USDT", "wb": wallet_balance, "cw": wallet_balance, "bc": "0"}],
            "P": [
                {
                    "s": SYMBOL,
                    "pa": position_amt,
                    "ep": "60000.10",
                    "up": "1.50",
                    "mt": "cross",
                    "iw": "0",
                    "ps": "BOTH",
                }
            ],
        },
    }
    return json.dumps(payload)


def order_update_message(
    *,
    event_ts: int = BASE_TS,
    transaction_ts: int | None = None,
    client_order_id: str = "probex-s1-000001",
    order_id: int = 101,
    execution_type: str = "TRADE",
    order_status: str = "FILLED",
    last_fill_quantity: str = "0.1",
    cumulative_fill_quantity: str = "0.2",
    trade_id: int = 77,
    is_maker: bool = True,
) -> str:
    # 真实 Binance USDⓈ-M `ORDER_TRADE_UPDATE`：symbol 在 `o.s`，**没有** top-level `s`
    payload = {
        "e": "ORDER_TRADE_UPDATE",
        "E": event_ts,
        "T": event_ts if transaction_ts is None else transaction_ts,
        "o": {
            "s": SYMBOL,
            "c": client_order_id,
            "i": order_id,
            "S": "BUY",
            "o": "LIMIT",
            "x": execution_type,
            "X": order_status,
            "l": last_fill_quantity,
            "z": cumulative_fill_quantity,
            "L": "60000.00",
            "ap": "60000.05",
            "n": "0.12",
            "N": "USDT",
            "t": trade_id,
            "m": is_maker,
            "q": "0.2",
            "p": "60000.00",
            "R": False,
        },
    }
    return json.dumps(payload)


def listen_key_expired_message(*, event_ts: int = BASE_TS) -> str:
    return json.dumps({"e": "listenKeyExpired", "E": event_ts})


def binance_order_payload(
    *,
    order_id: int = 101,
    client_order_id: str = "probex-s1-000001",
    symbol: str = SYMBOL,
    side: str = "BUY",
    status: str = "NEW",
    price: str = "60000.0",
    orig_qty: str = "0.002",
    executed_qty: str = "0.000",
    avg_price: str = "0.0",
    update_time: int = BASE_TS,
) -> dict:
    """真实形态的 `/fapi/v1/openOrders` / `/fapi/v1/allOrders` 条目。"""
    return {
        "orderId": order_id,
        "clientOrderId": client_order_id,
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "status": status,
        "price": price,
        "origQty": orig_qty,
        "executedQty": executed_qty,
        "avgPrice": avg_price,
        "time": update_time,
        "updateTime": update_time,
    }


def binance_trade_payload(
    *,
    trade_id: int = 555,
    order_id: int = 101,
    symbol: str = SYMBOL,
    price: str = "60000.0",
    qty: str = "0.001",
    commission: str = "0.012",
    commission_asset: str = "USDT",
    timestamp: int = BASE_TS + 5,
) -> dict:
    """真实形态的 `/fapi/v1/userTrades` 条目（**没有 clientOrderId**，只有 orderId）。"""
    return {
        "id": trade_id,
        "orderId": order_id,
        "symbol": symbol,
        "side": "BUY",
        "price": price,
        "qty": qty,
        "realizedPnl": "0.0",
        "commission": commission,
        "commissionAsset": commission_asset,
        "time": timestamp,
        "maker": True,
        "buyer": True,
    }


@dataclass
class FakeRestFetcher:
    """脚本化 REST：按 path 返回预置响应，并记录调用。"""

    responses: dict[str, object] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    #: 注入传输层失败（模拟网络/超时）。
    failures: dict[str, int] = field(default_factory=dict)
    #: 注入 **HTTP 状态** 失败（模拟 4xx/5xx；业务层必须把它当作失败而不是继续 RENEWING）。
    http_failures: dict[str, int] = field(default_factory=dict)
    #: 依次由 `POST /fapi/v1/listenKey` 返回的 key 序列（耗尽后重复最后一个）。
    listen_keys: deque[str] = field(default_factory=lambda: deque([FAKE_LISTEN_KEY]))
    key_index: int = 0
    #: 同一 path 的**连续不同响应**（用于「再次读取拿到不同事实」的竞态测试）；耗尽后回退到 `responses`。
    sequences: dict[str, list[object]] = field(default_factory=dict)

    def send(self, *, method: str, url: str, headers: dict[str, str], timeout_s: float):
        path = url.split("?")[0].split("fapi.binance.com")[-1]
        self.calls.append((method, path))
        remaining = self.failures.get(path, 0)
        if remaining > 0:
            self.failures[path] = remaining - 1
            raise TransportError(f"injected failure for {path}")
        http_remaining = self.http_failures.get(path, 0)
        if http_remaining > 0:
            self.http_failures[path] = http_remaining - 1
            from connectors.binance.private.errors import PrivateResponseError

            raise PrivateResponseError(f"{method} {path} -> HTTP 500")
        if path == "/fapi/v1/listenKey":
            if method == "POST":
                keys = list(self.listen_keys)
                key = keys[min(self.key_index, len(keys) - 1)]
                self.key_index += 1
                return {"listenKey": key}
            return {}
        queued = self.sequences.get(path)
        if queued:
            return queued.pop(0)
        if path not in self.responses:
            raise TransportError(f"no stub response for {path}")
        return self.responses[path]


@dataclass
class FakeConnection:
    """脚本化 WS 连接。"""

    messages: deque[str] = field(default_factory=deque)
    sent: list[str] = field(default_factory=list)
    closed: bool = False
    dropped: bool = False
    #: 模拟传输层观测到的心跳（SC-6 证据用）。
    ping_count: int = 0

    def send_text(self, text: str) -> None:
        if self.closed or self.dropped:
            raise WebSocketClosed("connection is not usable")
        self.sent.append(text)

    def recv_text(self, *, timeout_s: float) -> str | None:
        if self.dropped:
            raise WebSocketClosed("peer closed the connection")
        if self.closed:
            return None
        if not self.messages:
            raise WebSocketTimeout("no message within the timeout")
        return self.messages.popleft()

    def close(self) -> None:
        self.closed = True

    def push(self, text: str) -> None:
        self.messages.append(text)

    def drop(self) -> None:
        self.dropped = True


@dataclass
class ScriptedStreamFactory:
    """注入式 WS 工厂：记录 URL，并复用同一个 FakeConnection。"""

    connection: FakeConnection = field(default_factory=FakeConnection)
    urls: list[str] = field(default_factory=list)
    failures: deque[str] = field(default_factory=deque)

    def __call__(self, url: str, *, timeout_s: float) -> FakeConnection:
        self.urls.append(url)
        if self.failures:
            raise TransportError(self.failures.popleft())
        connection = self.connection
        connection.closed = False
        connection.dropped = False
        return connection


#: 恢复测试用的只读端点 → 响应映射构造器。
def recovery_responses(
    *,
    account: dict | None = None,
    position: list[dict] | None = None,
    open_orders: list[dict] | None = None,
    history: list[dict] | None = None,
    trades: list[dict] | None = None,
) -> dict[str, object]:
    return {
        "/fapi/v1/time": {"serverTime": BASE_TS + 500},
        "/fapi/v2/account": account if account is not None else account_payload(),
        "/fapi/v2/positionRisk": position if position is not None else position_risk_payload(),
        "/fapi/v1/openOrders": open_orders if open_orders is not None else [],
        "/fapi/v1/allOrders": history if history is not None else [],
        "/fapi/v1/userTrades": trades if trades is not None else [],
    }


def build_recovery(**overrides: object):
    """组装 StartupRecovery（假 REST + 空 tracker/accounting）。"""
    from connectors.binance.private.recovery import StartupRecovery
    from execution.tracker import OrderTracker
    from portfolio.accounting import AccountingCore

    responses = overrides.pop("responses", None)
    fetcher = FakeRestFetcher(responses=dict(responses) if responses else recovery_responses())
    clock = overrides.pop("clock", lambda: BASE_TS)
    rest = PrivateRestClient(credentials=credentials(), fetcher=fetcher, clock=clock)
    tracker = overrides.pop("tracker", None) or OrderTracker(session_id="s1")
    accounting = overrides.pop("accounting", None) or AccountingCore(initial_balance=0.0)
    recovery = StartupRecovery(
        rest=rest, tracker=tracker, accounting=accounting, symbol=SYMBOL, clock=clock,
        **overrides,  # type: ignore[arg-type]
    )
    return recovery, fetcher, tracker, accounting


def stream_state(*, active: bool = True, continuity: bool = True, boundary: bool = True):
    from connectors.binance.private.recovery import StreamState

    return StreamState(
        listen_key_state="ACTIVE" if active else "EXPIRED",
        continuity_assumed=continuity,
        boundary_present=boundary,
    )


def build_runtime(**overrides: object):
    """组装一个私有运行时（假 REST + 假 WS）。"""
    from connectors.binance.private.runtime import PrivateAccountRuntime

    fetcher = FakeRestFetcher(
        responses={"/fapi/v1/time": {"serverTime": BASE_TS + 500}, "/fapi/v2/account": account_payload(),
                   "/fapi/v2/positionRisk": position_risk_payload()}
    )
    clock = overrides.pop("clock", lambda: BASE_TS)
    rest = PrivateRestClient(credentials=credentials(), fetcher=fetcher, clock=clock)
    factory = ScriptedStreamFactory()
    runtime = PrivateAccountRuntime(
        config=private_config(**overrides),  # type: ignore[arg-type]
        rest=rest,
        stream=UserStreamClient(transport_factory=factory),
        credentials=credentials(),
        clock=clock,
        sleeper=lambda _seconds: None,
    )
    return runtime, fetcher, factory


__all__ = [
    "FAKE_API_KEY",
    "FAKE_API_SECRET",
    "FAKE_LISTEN_KEY",
    "SYMBOL",
    "FakeConnection",
    "FakeRestFetcher",
    "ScriptedStreamFactory",
    "account_payload",
    "account_update_message",
    "build_runtime",
    "credentials",
    "listen_key_expired_message",
    "order_update_message",
    "position_risk_payload",
    "testnet_position_risk_payload",
    "private_config",
    "binance_order_payload",
    "binance_trade_payload",
    "build_recovery",
    "recovery_responses",
    "stream_state",
]
