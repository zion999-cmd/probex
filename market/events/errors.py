"""Market 领域的错误类型。

边界约束：事件与载荷的非法语义在此层被拒绝，不向下游扩散。
"""

from __future__ import annotations


class MarketEventError(Exception):
    """MarketEvent 领域错误基类。"""


class InvalidPayloadError(MarketEventError):
    """载荷语义非法（价格 / 数量 / id 不满足不变量）。"""


class InvalidEventError(MarketEventError):
    """MarketEvent 自身不满足不变量（字段缺失、类型不符、event_type 与 payload 不一致）。"""
