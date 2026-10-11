"""P0017.1：多标的采集状态与共享数据集查询（路径由 server 按前缀匹配）。"""

from __future__ import annotations

SUBSCRIPTIONS_PATH = "/api/v1/market/subscriptions"
SYMBOL_STATUS_PREFIX = "/api/v1/market/symbols/"
DATASET_PREFIX = "/api/v1/market/dataset/"
