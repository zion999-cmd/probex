"""私有账户层的错误类型（P0001.9.2）。

纪律：**错误消息绝不携带 secret 或签名**（两者都等价于可重放凭证）。
"""

from __future__ import annotations

import functools
from typing import Callable, TypeVar

from connectors.binance.market_data.errors import MarketDataFormatError

_F = TypeVar("_F", bound=Callable[..., object])


def translate_market_errors(func: _F) -> _F:
    """把 market_data 窄化助手抛出的边界错误统一为 `PrivateFormatError`。

    私有层复用 `market_data.parsing` 的窄化函数（同一套不变量），但对外只暴露自己的错误类型；
    `UnsupportedAccountModeError` 等私有层错误不受影响。
    """

    @functools.wraps(func)
    def wrapper(*args: object, **kwargs: object) -> object:
        try:
            return func(*args, **kwargs)
        except MarketDataFormatError as exc:
            raise PrivateFormatError(str(exc)) from None

    return wrapper  # type: ignore[return-value]


class PrivateApiError(RuntimeError):
    """私有账户层错误基类。"""


class CredentialsError(PrivateApiError):
    """凭据缺失或不可用（绝不回显凭据内容）。"""


class PrivateAuthError(PrivateApiError):
    """签名 / 时间戳 / 认证失败。"""


class PrivateResponseError(PrivateApiError):
    """签名请求的响应不可接受（只报 path 与状态码，不报 query string）。"""


class PrivateStreamError(PrivateApiError):
    """user data stream 传输 / 协议错误。"""


class ReconnectExhaustedError(PrivateStreamError):
    """重连尝试用尽：调用方必须显式处理（不静默放弃）。"""


class ListenKeyError(PrivateStreamError):
    """listenKey 生命周期错误（创建 / 续期 / 关闭 / 非法状态转换）。"""


class UnsupportedAccountModeError(PrivateApiError):
    """非 one-way / 非 USDT-M / 多 symbol —— fail closed，不做自动兼容。"""


class PrivateFormatError(PrivateApiError):
    """私有报文不满足预期结构（边界错误，绝不进入核心）。"""


__all__ = [
    "CredentialsError",
    "translate_market_errors",
    "ListenKeyError",
    "PrivateApiError",
    "PrivateAuthError",
    "PrivateFormatError",
    "PrivateResponseError",
    "PrivateStreamError",
    "ReconnectExhaustedError",
    "UnsupportedAccountModeError",
]
