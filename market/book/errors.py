"""L2 盘口领域的错误类型。"""

from __future__ import annotations


class BookError(Exception):
    """L2 盘口领域错误基类。"""


class UnexpectedMarketEventError(BookError):
    """事件不是本盘口负责的范围（venue / symbol 不匹配，或事件类型不受支持）。"""


class MarketBookInvariantError(BookError):
    """内部不变量被破坏。出现即代表实现缺陷，不允许静默降级。"""
