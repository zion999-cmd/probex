"""凭据、签名与时间偏移（P0001.9.2 §0.1 / §0.2）。

纪律：

- 凭据**只**来自环境变量（`BINANCE_API_KEY` / `BINANCE_API_SECRET`），缺失即拒绝；
- secret 绝不进入 `repr` / `str` / 异常 / 日志 / telemetry；**签名**同样按敏感处理
  （只报 path 与状态码，绝不回显 query string）；
- 签名 = `HMAC-SHA256(secret, canonical_query)`，其中 canonical query 已按 key 排序并追加
  `timestamp` 与 `recvWindow`；发出的 query 与签名的 query 逐字节一致。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import urllib.parse
from dataclasses import dataclass
from typing import Mapping

from connectors.binance.market_data.errors import TransportError
from connectors.binance.private.errors import CredentialsError, PrivateAuthError

#: 凭据环境变量名。
API_KEY_ENV = "BINANCE_API_KEY"
API_SECRET_ENV = "BINANCE_API_SECRET"

#: 默认 `recvWindow`（毫秒）：由调用方显式传入，这里只提供常量供测试/CLI 引用。
DEFAULT_RECV_WINDOW_MS = 5_000

#: 敏感字段名（出现在任何对外字符串/telemetry 里都必须被遮蔽）。
SENSITIVE_KEYS = ("signature", "sign", "apiKey", "api_key", "secret", "listenKey", "listen_key")

_REDACTED = "***"


def redact(value: str) -> str:
    """遮蔽敏感文本（用于必须打印的诊断路径；正常路径根本不应打印）。"""
    if not isinstance(value, str):
        raise TypeError("redact expects a string")
    return _REDACTED if value else ""


def sanitize_mapping(payload: Mapping[str, object]) -> dict[str, object]:
    """返回遮蔽敏感键之后的浅拷贝（供 telemetry / 诊断使用）。"""
    sanitized: dict[str, object] = {}
    for key, value in payload.items():
        sanitized[key] = _REDACTED if str(key) in SENSITIVE_KEYS else value
    return sanitized


def sanitize_query(query: str) -> str:
    """遮蔽 query string 中的签名与 listenKey（只保留参数名）。"""
    if not query:
        return ""
    parts: list[str] = []
    for chunk in query.split("&"):
        name, separator, _value = chunk.partition("=")
        parts.append(f"{name}={_REDACTED}" if separator and name in SENSITIVE_KEYS else chunk)
    return "&".join(parts)


def wall_clock_ms() -> int:
    """本地 wall-clock（毫秒）。只在 live 边界使用。"""
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class ApiCredentials:
    """API 凭据。`repr` / `str` 均不包含 secret。"""

    api_key: str
    _api_secret: str

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key:
            raise CredentialsError("API key must be a non-empty string")
        if not isinstance(self._api_secret, str) or not self._api_secret:
            raise CredentialsError("API secret must be a non-empty string")

    @property
    def has_secret(self) -> bool:
        """是否存在 secret（不返回其内容）。"""
        return bool(self._api_secret)

    def __repr__(self) -> str:
        return "ApiCredentials(api_key='***', api_secret='***')"

    __str__ = __repr__

    def sign(self, query: str) -> str:
        """对 canonical query 计算 HMAC-SHA256 十六进制签名。"""
        if not isinstance(query, str) or not query:
            raise PrivateAuthError("cannot sign an empty query")
        return hmac.new(self._api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ApiCredentials:
        """从环境变量读取凭据；缺失即 `CredentialsError`（不降级、不猜）。"""
        source = os.environ if env is None else env
        api_key = source.get(API_KEY_ENV)
        api_secret = source.get(API_SECRET_ENV)
        missing = [name for name, value in ((API_KEY_ENV, api_key), (API_SECRET_ENV, api_secret)) if not value]
        if missing:
            raise CredentialsError(
                f"missing credentials in environment: {', '.join(missing)} "
                "(private runtime refuses to start without them)"
            )
        return cls(api_key=str(api_key), _api_secret=str(api_secret))


def canonical_query(params: Mapping[str, object]) -> str:
    """canonical query：按参数名排序、紧凑分隔符。空参数集返回空串（签名端点可以没有业务参数）。"""
    for key, value in params.items():
        if not isinstance(key, str) or not key:
            raise PrivateAuthError("parameter names must be non-empty strings")
        if value is None or isinstance(value, bool):
            raise PrivateAuthError(f"parameter {key!r} must be a concrete non-boolean value")
    return urllib.parse.urlencode(sorted((key, str(value)) for key, value in params.items()), quote_via=urllib.parse.quote)


@dataclass(frozen=True, slots=True)
class SignedRequest:
    """一次签名请求的产物：完整 query（含签名）与用于审计的遮蔽形式。"""

    path: str
    query: str
    sanitized_query: str

    def __repr__(self) -> str:
        return f"SignedRequest(path={self.path!r}, query={self.sanitized_query!r})"

    __str__ = __repr__


def build_signed_request(
    *,
    path: str,
    params: Mapping[str, object],
    credentials: ApiCredentials,
    recv_window_ms: int,
    timestamp_ms: int,
) -> SignedRequest:
    """构造签名请求（timestamp 由调用方提供，通常是 `local_now + server_time_offset`）。"""
    if not isinstance(path, str) or not path.startswith("/"):
        raise PrivateAuthError(f"path must start with '/', got {path!r}")
    if isinstance(recv_window_ms, bool) or not isinstance(recv_window_ms, int) or recv_window_ms <= 0:
        raise PrivateAuthError(f"recv_window_ms must be a positive int, got {recv_window_ms!r}")
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int) or timestamp_ms <= 0:
        raise PrivateAuthError(f"timestamp_ms must be a positive int, got {timestamp_ms!r}")

    base = canonical_query(params)
    with_time = "&".join(part for part in (base, f"timestamp={timestamp_ms}", f"recvWindow={recv_window_ms}") if part)
    signature = credentials.sign(with_time)
    return SignedRequest(
        path=path,
        query=f"{with_time}&signature={signature}",
        sanitized_query=sanitize_query(f"{with_time}&signature={signature}"),
    )


@dataclass
class ServerTimeOffset:
    """`server_time - local_time`（毫秒）。仅在 live 边界测量/使用。"""

    offset_ms: int | None = None

    @property
    def measured(self) -> bool:
        return self.offset_ms is not None

    def timestamp_ms(self, *, local_now_ms: int) -> int:
        """以本地时间 + offset 生成签名时间戳（未测量时退化为本地时间）。"""
        if isinstance(local_now_ms, bool) or not isinstance(local_now_ms, int) or local_now_ms <= 0:
            raise PrivateAuthError(f"local_now_ms must be a positive int, got {local_now_ms!r}")
        return local_now_ms + (self.offset_ms or 0)

    def measure(self, *, server_time_ms: int, local_receive_ms: int, round_trip_ms: int = 0) -> int:
        """记录一次测量：估算 `server_time - local_time`（用往返时间的一半校正）。"""
        for name, value in (("server_time_ms", server_time_ms), ("local_receive_ms", local_receive_ms)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise PrivateAuthError(f"{name} must be a positive int, got {value!r}")
        if isinstance(round_trip_ms, bool) or not isinstance(round_trip_ms, int) or round_trip_ms < 0:
            raise PrivateAuthError(f"round_trip_ms must be an int >= 0, got {round_trip_ms!r}")
        self.offset_ms = server_time_ms - local_receive_ms + round_trip_ms // 2
        return self.offset_ms


def parse_server_time(raw: object) -> int:
    """解析 `GET /fapi/v1/time` 响应（严格）。"""
    if not isinstance(raw, dict) or "serverTime" not in raw:
        raise PrivateAuthError("server time response must be an object with 'serverTime'")
    value = raw["serverTime"]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PrivateAuthError(f"serverTime must be a positive int, got {value!r}")
    return value


def fetch_server_time(*, path: str, base_url: str, fetcher, timeout_s: float) -> tuple[int, int, int]:
    """测量服务器时间：返回 `(server_time_ms, local_receive_ms, round_trip_ms)`。"""
    started = wall_clock_ms()
    raw = fetcher(f"{base_url.rstrip('/')}{path}", {}, timeout_s)
    local_receive = wall_clock_ms()
    try:
        server_time = parse_server_time(raw)
    except PrivateAuthError as exc:
        raise PrivateAuthError(f"could not read server time from {path}: {exc}") from None
    return server_time, local_receive, max(0, local_receive - started)


def is_transport_error(exc: BaseException) -> bool:
    return isinstance(exc, TransportError)


__all__ = [
    "API_KEY_ENV",
    "API_SECRET_ENV",
    "DEFAULT_RECV_WINDOW_MS",
    "SENSITIVE_KEYS",
    "ApiCredentials",
    "ServerTimeOffset",
    "SignedRequest",
    "build_signed_request",
    "canonical_query",
    "fetch_server_time",
    "is_transport_error",
    "parse_server_time",
    "redact",
    "sanitize_mapping",
    "sanitize_query",
    "wall_clock_ms",
]
