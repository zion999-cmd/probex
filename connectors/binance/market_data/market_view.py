"""Market View：任意受支持交易对的**公开行情研究视图**（只读，无凭据，无写能力）。

设计边界（P0001.12.1 Market 工作区 / 人类指令“Market View Symbol ≠ Runtime Trading Symbol”）：

- **Market View Symbol**：用户想查看/研究的交易对。本服务按 symbol 参数取公开行情。
- **Runtime Trading Symbol**：运行实例实际交易的标的，由启动参数决定，**本服务不触碰**。

数据来源：交易所公开 REST（K线 / 盘口 / 逐笔成交 / 标记价 / 24h 概览）。不使用 mock、不编造数字：

- **按组件可用性**：每项能力独立尝试；失败的那项进 `errors`（带 code）并保持缺失，
  不把失败伪装成“无数据”，也不把缺失值写成 0。全部失败才 503。
- 交易对格式非法 ⇒ `invalid_symbol`；交易所不认（HTTP 400 / 目录中非 TRADING）⇒ `unsupported_symbol`；
- 交易所反爬/限流（实测 HTTP 418/429）⇒ `rate_limited`；其它 ⇒ `market_view_unavailable`。
- 每个响应都带 `source` / `fetched_at_ms` / `available` / `errors`，UI 必须显示来源、更新时间与不可用原因。

本模块只读：不 import 任何执行/下单路径，不持有凭据。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from connectors.binance.market_data.endpoints import (
    DEPTH_PATH,
    EXCHANGE_INFO_PATH,
    KLINES_PATH,
    PREMIUM_INDEX_PATH,
    REST_BASE_URL,
    TICKER_24H_PATH,
    TRADES_PATH,
)

#: 交易对形态（币安 USDⓈ-M：字母数字，3–20 位）。仅做**格式**校验；受支持性以交易所为准。
_SYMBOL_MIN_LENGTH = 3
_SYMBOL_MAX_LENGTH = 20
_SYMBOL_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


def _valid_symbol_shape(candidate: str) -> bool:
    if not (_SYMBOL_MIN_LENGTH <= len(candidate) <= _SYMBOL_MAX_LENGTH):
        return False
    return all(char in _SYMBOL_ALPHABET for char in candidate)

#: 允许的 K 线周期（与产品其它时间框架一致；未列出的周期显式拒绝）。
INTERVALS = ("1m", "5m", "15m", "1h", "4h", "1d")

#: 有界上限（防止 UI/外部输入造成无界拉取）。
MAX_CANDLES = 1000
MAX_DEPTH_LEVELS = 500
MAX_TRADES = 500

COMPONENTS = ("candles", "depth", "trades", "mark", "overview")


class JsonHttpClient(Protocol):
    def get_json(self, path: str, params: dict[str, str], *, timeout_s: float) -> object: ...


class MarketViewError(ValueError):
    """Market View 的明确失败（携带机器可读 code，供 API 映射为状态码）。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    symbol: str
    contract_type: str
    quote_asset: str
    status: str

    def view(self) -> dict[str, str]:
        return {"symbol": self.symbol, "contract_type": self.contract_type,
                "quote_asset": self.quote_asset, "status": self.status}


def _number(value: object, *, default: float | None = None) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class MarketViewService:
    """按 symbol 取公开行情的只读服务（exchangeInfo 有 TTL 缓存；行情有极短 TTL 缓存）。"""

    def __init__(self, *, client: JsonHttpClient, clock: Callable[[], int],
                  sleeper: Callable[[float], None], base_url: str = REST_BASE_URL,
                  timeout_s: float = 10.0, exchange_info_ttl_s: float = 300.0,
                  venue: str = "binance", cache_ttl_ms: int = 5000,
                  min_request_interval_ms: int = 650, rate_limit_retry_ms: int = 2500) -> None:
        if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
            raise ValueError(f"base_url must be http(s), got {base_url!r}")
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout_s = float(timeout_s)
        self._ttl_ms = int(float(exchange_info_ttl_s) * 1000)
        # 环境访问（时钟/睡眠）由 composition root 注入：本层不 import time（分层守卫要求）。
        self._clock = clock
        self._venue = venue
        self._symbols: tuple[SymbolInfo, ...] = ()
        self._symbols_at: int = -(10 ** 15)
        #: 极短 TTL 缓存：避免 UI 轮询/双渲染连拍触发交易所 418（不改变数据语义）。
        self._cache_ttl_ms = max(0, int(cache_ttl_ms))
        self._cache: dict[tuple, tuple[int, dict[str, object]]] = {}
        self._catalog_error: str | None = None
        #: 请求节流：实测交易所对连拍返回 HTTP 418（I'm a teapot）⇒ 最小间隔 + 限流单次重试。
        self._min_interval_ms = max(0, int(min_request_interval_ms))
        self._retry_ms = max(0, int(rate_limit_retry_ms))
        self._sleep = sleeper
        self._last_request_at: int = -(10 ** 15)

    # ---------------------------------------------------------------- 标的目录
    @property
    def venue(self) -> str:
        return self._venue

    @property
    def base_url(self) -> str:
        return self._base_url

    def _load_symbols(self, *, force: bool = False) -> tuple[SymbolInfo, ...]:
        now = int(self._clock())
        if not force and self._symbols and now - self._symbols_at < self._ttl_ms:
            return self._symbols
        try:
            raw = self._client.get_json(EXCHANGE_INFO_PATH, {}, timeout_s=self._timeout_s)
        except Exception as exc:  # noqa: BLE001 - 网络失败如实暴露
            raise MarketViewError("market_view_unavailable",
                                   f"exchangeInfo fetch failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("symbols"), list):
            raise MarketViewError("market_view_unavailable",
                                   "exchangeInfo payload has no symbols list")
        discovered: list[SymbolInfo] = []
        for item in raw["symbols"]:  # type: ignore[union-attr]
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "")
            if not symbol:
                continue
            discovered.append(SymbolInfo(
                symbol=symbol,
                contract_type=str(item.get("contractType") or ""),
                quote_asset=str(item.get("quoteAsset") or ""),
                status=str(item.get("status") or "")))
        self._symbols = tuple(discovered)
        self._symbols_at = now
        return self._symbols

    def _tradable(self, symbol: str) -> SymbolInfo | None:
        for info in self._load_symbols():
            if info.symbol == symbol and info.status == "TRADING":
                return info
        return None

    def symbols_view(self, *, query: str | None = None, limit: int = 200,
                      quote_asset: str | None = None) -> dict[str, object]:
        """可查看的交易对目录（用于选择器搜索）。`query` 子串匹配（大小写不敏感）。"""
        bound = max(1, min(int(limit), 2000))
        needle = (query or "").strip().upper()
        rows = [info for info in self._load_symbols()
                if info.status == "TRADING"
                and (quote_asset is None or info.quote_asset == quote_asset)
                and (not needle or needle in info.symbol)]
        rows.sort(key=lambda item: item.symbol)
        return {
            "venue": self._venue,
            "source": "exchangeInfo",
            "fetched_at_ms": int(self._clock()),
            "query": needle,
            "quote_asset": quote_asset,
            "total": len(rows),
            "truncated": len(rows) > bound,
            "symbols": [item.view() for item in rows[:bound]],
        }

    # ---------------------------------------------------------------- 内部取数
    def _throttle(self) -> None:
        """按最小间隔节流（防交易所反爬限流）。仅影响请求节奏，不改变数据语义。"""
        if self._min_interval_ms <= 0:
            return
        wait_ms = self._min_interval_ms - (int(self._clock()) - self._last_request_at)
        if wait_ms > 0:
            self._sleep(wait_ms / 1000.0)

    def _fetch_once(self, path: str, params: dict[str, str]) -> object:
        self._throttle()
        try:
            return self._client.get_json(path, params, timeout_s=self._timeout_s)
        finally:
            self._last_request_at = int(self._clock())

    def _get(self, path: str, params: dict[str, str]) -> object:
        attempts = 2 if self._retry_ms > 0 else 1
        last_error: MarketViewError | None = None
        for index in range(attempts):
            try:
                return self._fetch_once(path, params)
            except MarketViewError:
                raise
            except Exception as exc:  # noqa: BLE001 - 失败如实分类，不返回假数据
                message = str(exc)
                if "HTTP 400" in message:
                    # 上游把“非法/不存在交易对”归为 400（如 Binance -1121 Invalid symbol）
                    raise MarketViewError("unsupported_symbol",
                                           f"{path} rejected by venue: {message}") from exc
                if "HTTP 418" in message or "HTTP 429" in message:
                    # 实测：交易所对默认 UA 指纹/连拍请求返回 418（I'm a teapot）⇒ 限流单次重试
                    last_error = MarketViewError(
                        "rate_limited", f"{path}: venue rate/anti-bot response: {message}")
                    if index + 1 < attempts:
                        self._sleep(self._retry_ms / 1000.0)
                        continue
                    raise last_error from exc
                raise MarketViewError("market_view_unavailable",
                                       f"{path} failed: {type(exc).__name__}: {message}") from exc
        raise last_error or MarketViewError("market_view_unavailable", f"{path} failed")

    def _require_symbol(self, symbol: str) -> str:
        """返回规范化 symbol。

        权威是**交易所响应**：目录（exchangeInfo）不可用时仍允许尝试格式合法的交易对，
        并把目录不可用记录为事实（`catalog_unavailable`），而不是因此拒绝用户查看行情；
        未知交易对最终会由交易所的 400 拒绝 ⇒ `unsupported_symbol`。
        """
        candidate = str(symbol or "").strip().upper()
        if not _valid_symbol_shape(candidate):
            raise MarketViewError(
                "invalid_symbol",
                f"symbol must be {_SYMBOL_MIN_LENGTH}-{_SYMBOL_MAX_LENGTH} chars of A-Z0-9, "
                f"got {symbol!r}")
        self._catalog_error = None
        try:
            info = self._tradable(candidate)
        except MarketViewError:
            self._catalog_error = "catalog_unavailable"
            return candidate
        if info is None:
            raise MarketViewError(
                "unsupported_symbol",
                f"{candidate} is not a TRADING symbol on {self._venue} (see symbols catalog)")
        return info.symbol

    # ---------------------------------------------------------------- 行情视图
    def view(self, symbol: str, *, interval: str = "1m", candles: int = 200,
              depth_levels: int = 100, trades: int = 50,
              components: tuple[str, ...] | None = None) -> dict[str, object]:
        """取一个交易对的公开行情视图。

        **按组件可用性**：K线 / 盘口 / 逐笔 / 标记价 / 24h 概览各自尝试、各自报告原因；
        只要有一项拿到真实数据就返回（其余标 UNAVAILABLE + 原因），全部失败才 503。
        """
        if interval not in INTERVALS:
            raise MarketViewError("invalid_interval",
                                   f"interval must be one of {list(INTERVALS)}, got {interval!r}")
        sym = self._require_symbol(symbol)
        candle_limit = max(1, min(int(candles), MAX_CANDLES))
        depth_limit = max(5, min(int(depth_levels), MAX_DEPTH_LEVELS))
        trade_limit = max(1, min(int(trades), MAX_TRADES))

        wanted_key = tuple(components) if components else COMPONENTS
        cache_key = (sym, interval, candle_limit, depth_limit, trade_limit, wanted_key)
        now = int(self._clock())
        cached = self._cache.get(cache_key)
        if cached is not None and now - cached[0] < self._cache_ttl_ms:
            return cached[1]

        # 要取的组件（入参 `components` 是名字元组）；与下面存数据的 `values` 区分开。
        wanted = tuple(components) if components else COMPONENTS
        unknown_components = [name for name in wanted if name not in COMPONENTS]
        if unknown_components:
            raise MarketViewError("invalid_components",
                                   f"unknown components {unknown_components}; "
                                   f"allowed={list(COMPONENTS)}")
        values: dict[str, object] = {}
        errors: dict[str, dict[str, str]] = {}
        notes: list[str] = []
        if self._catalog_error:
            notes.append(f"symbol catalog UNKNOWN ({self._catalog_error})")

        def attempt(name: str, fetch: Callable[[], object]) -> None:
            try:
                values[name] = fetch()
            except MarketViewError as exc:
                errors[name] = {"code": exc.code, "detail": exc.detail}

        if "candles" in wanted:
            attempt("candles", lambda: self._map_candles(
                self._get(KLINES_PATH, {"symbol": sym, "interval": interval,
                                         "limit": str(candle_limit)}), interval=interval))
        if "depth" in wanted:
            attempt("depth", lambda: self._map_depth(
                self._get(DEPTH_PATH, {"symbol": sym, "limit": str(depth_limit)})))
        if "trades" in wanted:
            attempt("trades", lambda: self._map_trades(
                self._get(TRADES_PATH, {"symbol": sym, "limit": str(trade_limit)})))
        if "mark" in wanted:
            attempt("mark", lambda: self._map_mark(
                self._get(PREMIUM_INDEX_PATH, {"symbol": sym})))
        if "overview" in wanted:
            attempt("overview", lambda: self._map_overview(
                self._get(TICKER_24H_PATH, {"symbol": sym})))

        # 交易所明确拒绝该交易对（400 / 非 TRADING）⇒ 整个视图不成立（不是“部分缺数据”）
        for name, info in sorted(errors.items()):
            if info["code"] == "unsupported_symbol":
                raise MarketViewError("unsupported_symbol",
                                       f"{sym} rejected by venue on {name}: {info['detail']}")
        if not values:
            detail = "; ".join(f"{name}: {info['code']}" for name, info in sorted(errors.items()))
            raise MarketViewError("market_view_unavailable",
                                   f"no market data available for {sym} ({detail})")

        view: dict[str, object] = {
            "venue": self._venue,
            "symbol": sym,
            "interval": interval,
            "source": "binance_public_rest",
            "fetched_at_ms": int(self._clock()),
            "candles": values.get("candles", []),
            "depth": values.get("depth"),
            "trades": values.get("trades", []),
            "mark": values.get("mark"),
            "overview": values.get("overview"),
            # 每项能力的真实可用性与失败原因（UI 必须显示原因，不得静默当作“无数据”）
            "available": {name: (name in values) for name in wanted},
            "errors": errors,
            "notes": notes,
            "limits": {"candles": candle_limit, "depth_levels": depth_limit,
                       "trades": trade_limit},
            # 明确语义：这是**研究视图**，不是运行实例的交易标的。
            "view_kind": "market_view",
        }
        self._cache[cache_key] = (now, view)
        return view

    # ---------------------------------------------------------------- 映射
    @staticmethod
    def _map_candles(raw: object, *, interval: str) -> list[dict[str, object]]:
        if not isinstance(raw, list):
            raise MarketViewError("market_view_unavailable", "klines payload is not a list")
        out: list[dict[str, object]] = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) < 6:
                continue
            ts = _number(item[0])
            out.append({
                "ts": int(ts or 0),
                "open": _number(item[1]),
                "high": _number(item[2]),
                "low": _number(item[3]),
                "close": _number(item[4]),
                "volume": _number(item[5]),
                "source": "trades",
                "interval": interval,
            })
        return out

    @staticmethod
    def _map_depth(raw: object) -> dict[str, object]:
        if not isinstance(raw, dict):
            raise MarketViewError("market_view_unavailable", "depth payload is not an object")

        def levels(key: str) -> list[list[float]]:
            rows = raw.get(key) or []
            out: list[list[float]] = []
            if isinstance(rows, list):
                for pair in rows:
                    if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                        price, qty = _number(pair[0]), _number(pair[1])
                        if price is not None and qty is not None:
                            out.append([price, qty])
            return out

        bids, asks = levels("bids"), levels("asks")
        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
        mid = ((best_bid + best_ask) / 2) if spread is not None else None
        return {"bids": bids, "asks": asks, "best_bid": best_bid, "best_ask": best_ask,
                "spread": spread, "mid": mid,
                "last_update_id": int(_number(raw.get("lastUpdateId"), default=0.0) or 0),
                "ts": int(_number(raw.get("E"), default=0.0) or 0) or None}

    @staticmethod
    def _map_trades(raw: object) -> list[dict[str, object]]:
        if not isinstance(raw, list):
            raise MarketViewError("market_view_unavailable", "trades payload is not a list")
        out: list[dict[str, object]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            out.append({
                "ts": int(_number(item.get("time"), default=0.0) or 0),
                "price": _number(item.get("price")),
                "quantity": _number(item.get("qty")),
                "quote_quantity": _number(item.get("quoteQty")),
                # isBuyerMaker=true ⇒ 主动方是卖方（与项目 AggressorSide 语义一致）
                "aggressor": "sell" if item.get("isBuyerMaker") else "buy",
                "trade_id": item.get("id"),
            })
        return out

    @staticmethod
    def _map_mark(raw: object) -> dict[str, object]:
        if not isinstance(raw, dict):
            raise MarketViewError("market_view_unavailable",
                                   "premiumIndex payload is not an object")
        return {
            "price": _number(raw.get("markPrice")),
            "index_price": _number(raw.get("indexPrice")),
            "funding_rate": _number(raw.get("lastFundingRate")),
            "ts": int(_number(raw.get("time"), default=0.0) or 0),
            "source": "premiumIndex",
        }

    @staticmethod
    def _map_overview(raw: object) -> dict[str, object]:
        if not isinstance(raw, dict):
            raise MarketViewError("market_view_unavailable",
                                   "ticker24hr payload is not an object")
        return {
            "last_price": _number(raw.get("lastPrice")),
            "change_pct_24h": _number(raw.get("priceChangePercent")),
            "high_24h": _number(raw.get("highPrice")),
            "low_24h": _number(raw.get("lowPrice")),
            "volume_24h": _number(raw.get("volume")),
            "quote_volume_24h": _number(raw.get("quoteVolume")),
            "source": "ticker24hr",
        }


__all__ = ["COMPONENTS", "INTERVALS", "MAX_CANDLES", "MAX_DEPTH_LEVELS", "MAX_TRADES",
           "MarketViewError", "MarketViewService", "SymbolInfo"]
