"""Binance 市场数据归一化的错误类型。"""

from __future__ import annotations


class MarketDataFormatError(ValueError):
    """外部报文不满足 Binance 市场数据结构。属于边界错误，绝不进入核心。"""


class TransportError(RuntimeError):
    """传输层错误基类（连接、握手、帧、超时）。"""


class WebSocketHandshakeError(TransportError):
    """WS 握手失败（状态码非 101 或 `Sec-WebSocket-Accept` 不匹配）。"""


class WebSocketProtocolError(TransportError):
    """对端违反 WebSocket 协议（例如服务端帧被 mask、意外的二进制帧）。"""


class WebSocketTimeout(TransportError):
    """在超时内没有收到完整消息（调用方决定重连或继续等待）。"""


class WebSocketClosed(TransportError):
    """连接已被对端或本地关闭。"""


class ReconnectExhaustedError(TransportError):
    """重连尝试用尽：调用方必须显式处理（不静默放弃）。"""
