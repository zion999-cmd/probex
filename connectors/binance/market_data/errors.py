"""Binance 市场数据归一化的错误类型。"""

from __future__ import annotations


class MarketDataFormatError(ValueError):
    """外部报文不满足 Binance 市场数据结构。属于边界错误，绝不进入核心。"""
