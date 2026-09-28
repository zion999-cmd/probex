"""Prediction provider 契约与实现。

热路径：`SystemOneProvider`（原生 typed Jev，`POST /api/v1/systemone`）。

注意：Chat Completions 热路径（`prediction.providers.openrouter`）已于 P0001.4.2 正式**废弃**
（结论见 `context/decisions.md` D-017 / D-018），因此**不再从包命名空间导出**，避免误用；
模块本身保留，作为 P0001.4.1 的实验记录。
"""

from __future__ import annotations

from prediction.providers.base import PredictionProvider, ProviderResponse
from prediction.providers.jev import JevProvider, JevTransport
from prediction.providers.systemone import SystemOneCall, SystemOneProvider, SystemOneTransport

__all__ = [
    "JevProvider",
    "JevTransport",
    "PredictionProvider",
    "ProviderResponse",
    "SystemOneCall",
    "SystemOneProvider",
    "SystemOneTransport",
]
